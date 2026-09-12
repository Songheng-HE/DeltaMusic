"""Windows-friendly graphical launcher for the DeltaMusic song library."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import os
import secrets
import subprocess
import sys
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from file_drop import create_application_root, enable_midi_file_drop
from importer import (
    export_package,
    import_legacy_python,
    import_midi,
    import_package,
    current_song_ids,
    next_available_song_id,
    suggested_id,
    suggested_id_from_filename,
)
from library import (
    APP_ROOT,
    CODE_ROOT,
    CONTROL_DIR,
    IS_FROZEN,
    LAUNCHER_DIR,
    LOG_DIR,
    USER_LIBRARY,
    LibraryError,
    Song,
    delete_custom_song,
    ensure_user_directories,
    load_builtin_songs,
    load_all_songs,
    rename_custom_song,
)


HOST_PATH = CODE_ROOT / "DeltaMusicPlayerHost.exe" if IS_FROZEN else LAUNCHER_DIR / "player_host.py"
CONTROL_PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class PlaybackControl:
    """One token-protected GUI-to-player cancellation channel."""

    path: Path
    token: str


def _control_payload(token: str, command: str) -> dict[str, object]:
    return {
        "version": CONTROL_PROTOCOL_VERSION,
        "token": token,
        "command": command,
    }


def _read_owned_control(control: PlaybackControl) -> dict[str, object] | None:
    """Return a control payload only when it still belongs to this GUI run."""
    try:
        if control.path.is_symlink():
            return None
        payload = json.loads(control.path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != CONTROL_PROTOCOL_VERSION or payload.get("token") != control.token:
        return None
    if payload.get("command") not in {"ready", "stop"}:
        return None
    return payload


def _create_playback_control() -> PlaybackControl:
    """Create a fresh control file before the player process is launched."""
    ensure_user_directories()
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    for _attempt in range(8):
        control = PlaybackControl(
            path=CONTROL_DIR / f"playback-{secrets.token_hex(16)}.json",
            token=secrets.token_urlsafe(32),
        )
        try:
            with control.path.open("x", encoding="utf-8") as stream:
                json.dump(_control_payload(control.token, "ready"), stream, ensure_ascii=False)
                stream.write("\n")
            return control
        except FileExistsError:
            # A cryptographic filename collision is exceptionally unlikely, but
            # retry rather than ever reusing an existing control channel.
            continue
        except OSError as exc:
            raise LibraryError("无法创建演奏控制文件。") from exc
    raise LibraryError("无法创建唯一的演奏控制文件，请重试。")


def _request_stop(control: PlaybackControl) -> bool:
    """Atomically change an owned ready file into a stop request."""
    payload = _read_owned_control(control)
    if payload is None:
        return False
    if payload.get("command") == "stop":
        return True

    temporary = control.path.with_name(f".{control.path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(_control_payload(control.token, "stop"), stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, control.path)
        return True
    except OSError:
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _discard_control_if_owned(control: PlaybackControl) -> None:
    """Delete only the exact control file created for this playback run."""
    if _read_owned_control(control) is None:
        return
    try:
        control.path.unlink()
    except OSError:
        pass


def is_administrator() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def console_python() -> Path:
    """Use python.exe, not pythonw.exe, because players need a visible console."""
    current = Path(sys.executable).resolve()
    candidate = current.with_name("python.exe")
    return candidate if candidate.is_file() else current


class LauncherApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("DeltaMusic 启动器")
        self.root.minsize(860, 560)
        self.root.geometry("960x650")
        self.songs: dict[str, Song] = {}
        self._active_controls: dict[Path, PlaybackControl] = {}
        self._stop_button: ttk.Button | None = None
        self.status_var = tk.StringVar(value="正在读取曲库……")
        self.detail_var = tk.StringVar(value="选择一首曲目后可查看说明。")
        ensure_user_directories()
        self._configure_style()
        self._build_ui()
        self.refresh_library()
        self.root.after(100, self._update_environment_status)
        self.root.after(750, self._reconcile_playback_controls)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Muted.TLabel", foreground="#555555")
        style.configure("Drop.TLabel", relief="ridge", padding=8, anchor="center")
        style.configure("Danger.Treeview", foreground="#9b1c1c")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="DeltaMusic 曲库", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="刷新曲库", command=self.refresh_library).pack(side="right")

        status = ttk.Label(outer, textvariable=self.status_var, style="Muted.TLabel", wraplength=900)
        status.pack(fill="x", pady=(0, 10))

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)
        columns = ("title", "source", "state")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("title", text="曲目")
        self.tree.heading("source", text="来源")
        self.tree.heading("state", text="状态")
        self.tree.column("title", width=455, anchor="w")
        self.tree.column("source", width=140, anchor="center")
        self.tree.column("state", width=245, anchor="w")
        self.tree.tag_configure("unavailable", foreground="#a61b1b")
        self.tree.tag_configure("advanced", foreground="#865600")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_detail)
        self.tree.bind("<Double-1>", lambda _event: self.play_selected())
        self.tree.bind("<Button-3>", self._show_song_context_menu)
        self._song_menu = tk.Menu(self.root, tearoff=False)
        self._song_menu.add_command(label="重命名个人曲目…", command=self.rename_selected_song)
        self._song_menu.add_command(label="删除个人曲目…", command=self.delete_selected_song)

        detail = ttk.Label(
            outer,
            textvariable=self.detail_var,
            wraplength=900,
            justify="left",
            relief="groove",
            padding=10,
        )
        detail.pack(fill="x", pady=(10, 10))

        play_row = ttk.Frame(outer)
        play_row.pack(fill="x", pady=(0, 8))
        ttk.Button(play_row, text="开始演奏", command=self.play_selected).pack(side="left")
        self._stop_button = ttk.Button(
            play_row,
            text="紧急停止",
            command=self.stop_all_players,
            state="disabled",
        )
        self._stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(play_row, text="导出所选个人曲目", command=self.export_selected).pack(side="left", padx=(8, 0))
        ttk.Button(play_row, text="打开个人曲库", command=self.open_user_library).pack(side="left", padx=(8, 0))
        ttk.Button(play_row, text="查看说明", command=self.open_help).pack(side="right")

        import_row = ttk.LabelFrame(outer, text="维护个人曲库", padding=8)
        import_row.pack(fill="x")
        import_buttons = ttk.Frame(import_row)
        import_buttons.pack(fill="x")
        ttk.Button(import_buttons, text="导入 MIDI（手动设置）", command=self.import_safe_midi).pack(side="left")
        ttk.Button(import_buttons, text="选择 MIDI（可多选）", command=self.select_safe_midi_files).pack(side="left", padx=(8, 0))
        ttk.Button(import_buttons, text="导入 Python + MIDI（高级）", command=self.import_legacy).pack(side="left", padx=(8, 0))
        ttk.Button(import_buttons, text="导入曲目包", command=self.import_song_package).pack(side="left", padx=(8, 0))
        repair_label = "检查应用文件" if IS_FROZEN else "修复 Python / 依赖"
        ttk.Button(import_buttons, text=repair_label, command=self.repair_environment).pack(side="right")
        self._drop_target = ttk.Label(
            import_row,
            text="把一个或多个 .mid / .midi 文件拖到这里：自动安全导入",
            style="Drop.TLabel",
        )
        self._drop_target.pack(fill="x", pady=(8, 0))
        self._native_file_drop_ready = enable_midi_file_drop(
            self._drop_target, self.root, self.import_dropped_midi
        )
        if not self._native_file_drop_ready:
            self._drop_target.configure(text="当前环境未启用拖放；可用“选择 MIDI（可多选）”安全导入")

        footer = ttk.Label(
            outer,
            text="提示：开始演奏时 Windows 会请求管理员权限，以便向同等权限的游戏窗口发送输入。F10 或“紧急停止”可立即停止内置和安全 MIDI；高级 Python 曲目取决于其自身实现。",
            style="Muted.TLabel",
            wraplength=900,
        )
        footer.pack(fill="x", pady=(10, 0))

    def _write_log(self, message: str, exception: BaseException | None = None) -> None:
        try:
            ensure_user_directories()
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            text = f"[{stamp}] {message}\n"
            if exception is not None:
                text += "".join(traceback.format_exception(exception)) + "\n"
            with (LOG_DIR / "launcher.log").open("a", encoding="utf-8") as stream:
                stream.write(text)
        except OSError:
            pass

    def _update_environment_status(self) -> None:
        dependencies: list[str] = []
        for module in ("mido", "keyboard", "pydirectinput"):
            try:
                imported = __import__(module)
                dependencies.append(f"{module} {getattr(imported, '__version__', '已就绪')}")
            except Exception:
                dependencies.append(f"{module} 缺失")
        privilege = "当前启动器为管理员" if is_administrator() else "导入界面为普通权限；播放时会请求管理员权限"
        runtime = "内置 EXE 运行环境（无需安装 Python）" if IS_FROZEN else f"Python {sys.version.split()[0]}"
        self.status_var.set(
            f"{runtime} ｜ {'，'.join(dependencies)} ｜ {privilege} ｜ 个人曲库：{USER_LIBRARY}"
        )

    def refresh_library(self) -> None:
        previous = self.tree.focus() if hasattr(self, "tree") else ""
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.songs.clear()
        try:
            all_songs = load_all_songs()
        except LibraryError as exc:
            self._write_log("读取曲库失败", exc)
            messagebox.showerror("无法读取曲库", str(exc), parent=self.root)
            return
        except Exception as exc:
            self._write_log("读取曲库时发生未知错误", exc)
            messagebox.showerror("无法读取曲库", "发生未知错误，请查看 launcher.log。", parent=self.root)
            return

        first = ""
        for index, song in enumerate(all_songs):
            key = f"{index}:{song.source}:{song.identifier}"
            self.songs[key] = song
            state = "可用" if song.available else song.issue
            tags: tuple[str, ...] = ()
            if not song.available:
                tags = ("unavailable",)
            elif song.kind == "legacy_python" and song.is_custom:
                tags = ("advanced",)
                state = "高级代码（仅可信来源）"
            self.tree.insert("", "end", iid=key, values=(song.title, song.source, state), tags=tags)
            if not first:
                first = key

        target = previous if previous in self.songs else first
        if target:
            self.tree.selection_set(target)
            self.tree.focus(target)
            self.tree.see(target)
        self._show_selected_detail()
        self._write_log(f"曲库已刷新：{len(all_songs)} 首曲目")

    def _selected_song(self, quiet: bool = False) -> Song | None:
        key = self.tree.focus()
        song = self.songs.get(key)
        if song is None and not quiet:
            messagebox.showinfo("请选择曲目", "请先在曲目表中选择一首曲目。", parent=self.root)
        return song

    def _show_selected_detail(self, _event: object | None = None) -> None:
        song = self._selected_song(quiet=True)
        if song is None:
            self.detail_var.set("选择一首曲目后可查看说明。")
            return
        detail = song.description or "没有附加说明。"
        if song.kind == "legacy_python" and song.is_custom:
            detail = "⚠ 高级兼容曲目：该 Python 文件没有被启动器审核。" + detail
        if not song.available:
            detail = f"不可用：{song.issue}"
        self.detail_var.set(f"{song.title}\n{detail}")

    def _show_song_context_menu(self, event: tk.Event[tk.Misc]) -> str | None:
        """Select the right-clicked row, then expose safe personal-song actions."""
        item = self.tree.identify_row(event.y)
        if not item:
            return None
        self.tree.selection_set(item)
        self.tree.focus(item)
        self.tree.see(item)
        self._show_selected_detail()

        song = self.songs.get(item)
        can_delete = bool(song and song.is_custom and song.manifest_path is not None)
        can_rename = bool(can_delete and song and song.available)
        self._song_menu.entryconfigure(0, state="normal" if can_rename else "disabled")
        self._song_menu.entryconfigure(1, state="normal" if can_delete else "disabled")
        try:
            self._song_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._song_menu.grab_release()
        return "break"

    def rename_selected_song(self) -> None:
        """Rename only a valid personal song's visible title."""
        song = self._selected_song()
        if song is None:
            return
        if not song.is_custom or song.manifest_path is None:
            messagebox.showinfo("内置曲目", "内置曲目由发布包维护，不能在这里重命名。", parent=self.root)
            return
        if not song.available:
            messagebox.showinfo(
                "曲目不可重命名",
                "这个个人曲目的 song.json 已损坏；可先删除后重新导入。",
                parent=self.root,
            )
            return
        title = simpledialog.askstring(
            "重命名个人曲目",
            "输入新的显示名称：\n（不会修改曲目 ID、文件夹或 MIDI 文件。）",
            initialvalue=song.title,
            parent=self.root,
        )
        if title is None:
            return
        try:
            rename_custom_song(song.manifest_path, title)
        except LibraryError as exc:
            self._write_log("重命名个人曲目失败", exc)
            messagebox.showerror("重命名失败", str(exc), parent=self.root)
            return
        except Exception as exc:
            self._write_log("重命名个人曲目时发生未知错误", exc)
            messagebox.showerror("重命名失败", "发生未知错误，请查看 launcher.log。", parent=self.root)
            return
        self.refresh_library()
        self.status_var.set(f"已重命名个人曲目：{title.strip()}")

    def delete_selected_song(self) -> None:
        """Permanently delete the selected personal-song folder after confirmation."""
        song = self._selected_song()
        if song is None:
            return
        if not song.is_custom or song.manifest_path is None:
            messagebox.showinfo("内置曲目", "内置曲目由发布包维护，不能在这里删除。", parent=self.root)
            return
        warning = (
            f"确定永久删除个人曲目“{song.title}”吗？\n\n"
            "将删除该曲目的 song.json、MIDI 文件以及可能的 Python 文件，无法恢复。\n"
            "如果它正在演奏，请先停止演奏。"
        )
        if not messagebox.askyesno(
            "永久删除个人曲目",
            warning,
            icon="warning",
            default=messagebox.NO,
            parent=self.root,
        ):
            return
        try:
            delete_custom_song(song.manifest_path)
        except LibraryError as exc:
            self._write_log("删除个人曲目失败", exc)
            messagebox.showerror("删除失败", str(exc), parent=self.root)
            return
        except Exception as exc:
            self._write_log("删除个人曲目时发生未知错误", exc)
            messagebox.showerror("删除失败", "发生未知错误，请查看 launcher.log。", parent=self.root)
            return
        self.refresh_library()
        self.status_var.set(f"已删除个人曲目：{song.title}")

    def _metadata_dialog(self, default_title: str, include_shift: bool) -> tuple[str, str, int] | None:
        title = simpledialog.askstring("曲目名称", "给曲目起一个显示名称：", initialvalue=default_title, parent=self.root)
        if title is None:
            return None
        title = title.strip()
        if not title:
            messagebox.showerror("名称无效", "曲目名称不能为空。", parent=self.root)
            return None
        identifier = simpledialog.askstring(
            "曲目 ID",
            "输入唯一 ID（小写英文、数字、_ 或 -）：",
            initialvalue=suggested_id(title),
            parent=self.root,
        )
        if identifier is None:
            return None
        identifier = identifier.strip()
        shift = 0
        if include_shift:
            raw_shift = simpledialog.askstring(
                "八度偏移",
                "安全 MIDI 的八度偏移：-24、-12、0、12 或 24：",
                initialvalue="0",
                parent=self.root,
            )
            if raw_shift is None:
                return None
            try:
                shift = int(raw_shift.strip())
            except ValueError:
                messagebox.showerror("偏移无效", "八度偏移必须是整数。", parent=self.root)
                return None
        return title, identifier, shift

    def import_safe_midi(self) -> None:
        source = filedialog.askopenfilename(
            parent=self.root,
            title="选择严格单旋律 MIDI",
            filetypes=[("MIDI 文件", "*.mid *.midi"), ("所有文件", "*.*")],
        )
        if not source:
            return
        metadata = self._metadata_dialog(Path(source).stem, include_shift=True)
        if metadata is None:
            return
        title, identifier, shift = metadata
        try:
            manifest = import_midi(Path(source), title, identifier, shift)
        except LibraryError as exc:
            self._write_log("导入安全 MIDI 失败", exc)
            messagebox.showerror("导入失败", str(exc), parent=self.root)
            return
        except Exception as exc:
            self._write_log("导入安全 MIDI 时发生未知错误", exc)
            messagebox.showerror("导入失败", "发生未知错误，请查看 launcher.log。", parent=self.root)
            return
        messagebox.showinfo("导入完成", f"已导入安全 MIDI 曲目：\n{manifest.parent}", parent=self.root)
        self.refresh_library()

    def select_safe_midi_files(self) -> None:
        """Let a user choose one or more MIDI files for automatic safe import."""
        sources = filedialog.askopenfilenames(
            parent=self.root,
            title="选择一个或多个 MIDI（自动安全导入）",
            filetypes=[("MIDI 文件", "*.mid *.midi"), ("所有文件", "*.*")],
        )
        if sources:
            self.import_dropped_midi(tuple(Path(source) for source in sources))

    def import_dropped_midi(self, sources: tuple[Path, ...]) -> None:
        """Safely import dropped/selected MIDI files without any code execution.

        The visible title stays as the source filename.  IDs are automatically
        derived from English names or Chinese pinyin, then receive ``1``, ``2``
        and so on when an occupied ID already exists.
        """
        unique_sources: list[Path] = []
        seen_sources: set[str] = set()
        for raw_source in sources:
            source = Path(raw_source)
            key = str(source).casefold()
            if key not in seen_sources:
                seen_sources.add(key)
                unique_sources.append(source)
        if not unique_sources:
            return

        try:
            occupied_ids = current_song_ids()
        except LibraryError as exc:
            self._write_log("读取曲目 ID 失败", exc)
            messagebox.showerror("无法导入 MIDI", str(exc), parent=self.root)
            return

        imported: list[tuple[str, str]] = []
        failures: list[tuple[str, str]] = []
        for source in unique_sources:
            title = source.stem.strip() or source.name
            try:
                identifier = next_available_song_id(
                    suggested_id_from_filename(source), occupied_ids
                )
                import_midi(source, title, identifier, octave_shift=0)
            except LibraryError as exc:
                failures.append((source.name, str(exc)))
                self._write_log(f"自动安全导入失败：{source.name}", exc)
                continue
            except Exception as exc:
                failures.append((source.name, "发生未知错误，请查看 launcher.log。"))
                self._write_log(f"自动安全导入时发生未知错误：{source.name}", exc)
                continue
            occupied_ids.add(identifier.casefold())
            imported.append((title, identifier))

        if imported:
            self.refresh_library()
        imported_text = "\n".join(f"• {title}（ID：{identifier}）" for title, identifier in imported)
        failed_text = "\n".join(f"• {name}：{reason}" for name, reason in failures)
        if imported and failures:
            messagebox.showwarning(
                "部分 MIDI 已导入",
                f"已安全导入 {len(imported)} 首：\n{imported_text}\n\n"
                f"以下 {len(failures)} 首未导入：\n{failed_text}",
                parent=self.root,
            )
        elif imported:
            messagebox.showinfo(
                "安全 MIDI 导入完成",
                f"已安全导入 {len(imported)} 首：\n{imported_text}",
                parent=self.root,
            )
        else:
            messagebox.showerror(
                "没有可导入的 MIDI",
                f"未能导入 {len(failures)} 个文件：\n{failed_text}",
                parent=self.root,
            )

    def import_legacy(self) -> None:
        warning = (
            "高级导入会复制 Python 代码，但不会执行它。\n\n"
            "Python 代码不能被可靠地自动判定为安全。只导入你自己写的或完全信任来源的文件；"
            "之后默认仍以普通权限运行，只有你明确确认时才会以管理员权限运行。\n\n继续吗？"
        )
        if not messagebox.askyesno("高级导入风险提示", warning, icon="warning", parent=self.root):
            return
        source_py = filedialog.askopenfilename(
            parent=self.root,
            title="选择播放器 Python 文件",
            filetypes=[("Python 文件", "*.py"), ("所有文件", "*.*")],
        )
        if not source_py:
            return
        midi_sources = filedialog.askopenfilenames(
            parent=self.root,
            title="选择该脚本需要的一个或多个 MIDI 文件",
            filetypes=[("MIDI 文件", "*.mid *.midi"), ("所有文件", "*.*")],
        )
        if not midi_sources:
            return
        metadata = self._metadata_dialog(Path(source_py).stem, include_shift=False)
        if metadata is None:
            return
        title, identifier, _unused = metadata
        try:
            manifest = import_legacy_python(Path(source_py), [Path(item) for item in midi_sources], title, identifier)
        except LibraryError as exc:
            self._write_log("导入高级 Python 曲目失败", exc)
            messagebox.showerror("导入失败", str(exc), parent=self.root)
            return
        except Exception as exc:
            self._write_log("导入高级 Python 曲目时发生未知错误", exc)
            messagebox.showerror("导入失败", "发生未知错误，请查看 launcher.log。", parent=self.root)
            return
        messagebox.showinfo("导入完成", f"已导入高级曲目：\n{manifest.parent}\n\n运行前请确认来源可信。", parent=self.root)
        self.refresh_library()

    def import_song_package(self) -> None:
        source = filedialog.askopenfilename(
            parent=self.root,
            title="选择 DeltaMusic 曲目包",
            filetypes=[("DeltaMusic 曲目包", "*.dmsong *.zip"), ("所有文件", "*.*")],
        )
        if not source:
            return
        try:
            manifest = import_package(Path(source))
        except LibraryError as exc:
            self._write_log("导入曲目包失败", exc)
            messagebox.showerror("导入失败", str(exc), parent=self.root)
            return
        except Exception as exc:
            self._write_log("导入曲目包时发生未知错误", exc)
            messagebox.showerror("导入失败", "发生未知错误，请查看 launcher.log。", parent=self.root)
            return
        messagebox.showinfo("导入完成", f"已导入曲目包：\n{manifest.parent}", parent=self.root)
        self.refresh_library()

    def export_selected(self) -> None:
        song = self._selected_song()
        if song is None:
            return
        if not song.is_custom or song.manifest_path is None or not song.available:
            messagebox.showinfo("无法导出", "只能导出可用的个人曲目。", parent=self.root)
            return
        destination = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出 DeltaMusic 曲目包",
            defaultextension=".dmsong",
            initialfile=f"{song.identifier}.dmsong",
            filetypes=[("DeltaMusic 曲目包", "*.dmsong")],
        )
        if not destination:
            return
        try:
            output = export_package(song.manifest_path, Path(destination))
        except LibraryError as exc:
            self._write_log("导出曲目包失败", exc)
            messagebox.showerror("导出失败", str(exc), parent=self.root)
            return
        messagebox.showinfo("导出完成", f"可分享的曲目包已保存到：\n{output}", parent=self.root)

    def _ask_legacy_privilege(self, song: Song) -> bool | None:
        dialog = tk.Toplevel(self.root)
        dialog.title("高级 Python 曲目")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        choice: dict[str, bool | None] = {"value": None}
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        message = (
            f"“{song.title}”包含由用户导入的 Python 代码。\n\n"
            "启动器没有执行或审核它，普通权限也无法让未知代码变得安全。\n"
            "如游戏以管理员权限运行，普通权限脚本可能无法发送按键。\n\n"
            "只应在你完全信任代码来源时选择管理员运行。"
        )
        ttk.Label(frame, text=message, justify="left", wraplength=510).pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(18, 0))

        def finish(value: bool | None) -> None:
            choice["value"] = value
            dialog.destroy()

        ttk.Button(buttons, text="普通权限运行", command=lambda: finish(False)).pack(side="left")
        ttk.Button(buttons, text="取消", command=lambda: finish(None)).pack(side="right")
        ttk.Button(buttons, text="管理员运行（我信任代码）", command=lambda: finish(True)).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(None))
        self.root.wait_window(dialog)
        return choice["value"]

    def _update_stop_button(self) -> None:
        if self._stop_button is None:
            return
        self._stop_button.configure(state="normal" if self._active_controls else "disabled")

    def _reconcile_playback_controls(self) -> None:
        """Forget sessions after their host deletes its control file on exit."""
        finished = [path for path in self._active_controls if not path.exists()]
        for path in finished:
            self._active_controls.pop(path, None)
        if finished:
            self._update_stop_button()
        try:
            self.root.after(750, self._reconcile_playback_controls)
        except tk.TclError:
            # The window has already been destroyed during application exit.
            pass

    def stop_all_players(self) -> None:
        """Request graceful cancellation from every compatible GUI-started player."""
        sent = 0
        retryable = 0
        stale: list[Path] = []
        for path, control in tuple(self._active_controls.items()):
            if _request_stop(control):
                sent += 1
            elif not path.exists() or _read_owned_control(control) is None:
                stale.append(path)
            else:
                # A short-lived file lock (for example, antivirus scanning the
                # atomic replacement) must not disable the emergency button.
                retryable += 1
        for path in stale:
            self._active_controls.pop(path, None)
        self._update_stop_button()

        if sent:
            self.status_var.set(
                f"已向 {sent} 个受支持播放器发送紧急停止请求，正在释放已按下的键。"
            )
            self._write_log(f"已发送紧急停止请求：{sent} 个播放器")
        elif retryable:
            self.status_var.set("紧急停止请求暂时未写入，请立即再点击一次。")
            self._write_log("紧急停止请求暂时未写入，保留控制通道以便重试。")
        else:
            self.status_var.set("没有正在运行、可响应紧急停止的播放器。")

    def _launch_player(self, song: Song, elevated: bool, allow_custom_code: bool = False) -> None:
        if not HOST_PATH.is_file():
            expected = "DeltaMusicPlayerHost.exe" if IS_FROZEN else "player_host.py"
            raise LibraryError(f"启动器文件不完整：找不到 {expected}。")
        if song.is_custom:
            if song.manifest_path is None:
                raise LibraryError("个人曲目缺少 song.json。")
            args = ["--custom-manifest", str(song.manifest_path)]
            if allow_custom_code:
                args.append("--run-custom-code")
        else:
            args = ["--builtin", song.identifier]

        control = _create_playback_control()
        args.extend(["--stop-file", str(control.path), "--stop-token", control.token])

        if IS_FROZEN:
            player_program = HOST_PATH
            player_args = args
        else:
            player_program = console_python()
            player_args = [str(HOST_PATH), *args]
        try:
            if elevated:
                parameters = subprocess.list2cmdline(player_args)
                result = ctypes.windll.shell32.ShellExecuteW(
                    None,
                    "runas",
                    str(player_program),
                    parameters,
                    str(CODE_ROOT),
                    1,
                )
                if int(result) <= 32:
                    if int(result) == 5:
                        raise LibraryError("你取消了 Windows 的管理员权限请求。")
                    raise LibraryError(f"Windows 无法提升播放器权限（ShellExecute 错误 {result}）。")
            else:
                flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                subprocess.Popen([str(player_program), *player_args], cwd=str(CODE_ROOT), creationflags=flags)
        except Exception:
            _discard_control_if_owned(control)
            raise

        self._active_controls[control.path] = control
        self._update_stop_button()

    def play_selected(self) -> None:
        song = self._selected_song()
        if song is None:
            return
        if not song.available:
            messagebox.showerror("曲目不可用", song.issue or "曲目文件不完整。", parent=self.root)
            return
        elevated = True
        allow_custom_code = False
        if song.kind == "legacy_python" and song.is_custom:
            choice = self._ask_legacy_privilege(song)
            if choice is None:
                return
            elevated = choice
            allow_custom_code = True
        elif song.kind not in {"legacy_python", "generic_midi"}:
            messagebox.showerror("曲目不可用", "该曲目格式无法运行。", parent=self.root)
            return

        try:
            self._launch_player(song, elevated=elevated, allow_custom_code=allow_custom_code)
        except LibraryError as exc:
            self._write_log("启动播放器失败", exc)
            messagebox.showerror("无法开始演奏", str(exc), parent=self.root)
            return
        except Exception as exc:
            self._write_log("启动播放器时发生未知错误", exc)
            messagebox.showerror("无法开始演奏", "发生未知错误，请查看 launcher.log。", parent=self.root)
            return

        privilege = "管理员权限" if elevated else "普通权限"
        self._write_log(f"已请求启动：{song.title}（{privilege}）")
        messagebox.showinfo(
            "播放器已启动",
            "播放器窗口已打开。请看其中的倒计时，切回游戏、拿出口琴后等待开始。\n\n"
            "F10 或启动器中的“紧急停止”可停止内置和安全 MIDI。\n"
            "高级 Python 曲目是否支持停止，取决于其作者。",
            parent=self.root,
        )

    def open_user_library(self) -> None:
        ensure_user_directories()
        try:
            os.startfile(str(USER_LIBRARY))
        except OSError as exc:
            self._write_log("打开个人曲库失败", exc)
            messagebox.showerror("无法打开个人曲库", str(exc), parent=self.root)

    def open_help(self) -> None:
        choices = (
            CODE_ROOT / "README_Launcher.md",
            CODE_ROOT / "00_START_HERE.md",
            CODE_ROOT / "启动器说明.md",
            CODE_ROOT / "README_启动器.md",
            APP_ROOT / "启动器说明.md",
        )
        document = next((item for item in choices if item.is_file()), None)
        if document is None:
            messagebox.showinfo("说明文件", "发布包中没有找到启动器说明文件。", parent=self.root)
            return
        try:
            os.startfile(str(document))
        except OSError as exc:
            self._write_log("打开说明失败", exc)
            messagebox.showerror("无法打开说明", str(exc), parent=self.root)

    def repair_environment(self) -> None:
        if IS_FROZEN:
            messagebox.showinfo(
                "应用运行环境",
                "EXE 版本已经内置 Python 和依赖，无需安装或修复 Python。\n\n"
                "如果应用文件损坏，请重新下载并完整解压发布包。",
                parent=self.root,
            )
            return
        script = CODE_ROOT / "bootstrap.ps1"
        if not script.is_file():
            messagebox.showerror("无法修复", "找不到 bootstrap.ps1。", parent=self.root)
            return
        if not messagebox.askyesno(
            "修复运行环境",
            "将打开一个 PowerShell 窗口检查 Python 和依赖。修复完成后会打开一个新的启动器窗口。\n\n继续吗？",
            parent=self.root,
        ):
            return
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Repair"],
                cwd=str(CODE_ROOT),
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        except OSError as exc:
            self._write_log("启动修复失败", exc)
            messagebox.showerror("无法修复", str(exc), parent=self.root)


def self_check() -> int:
    """Validate frozen GUI resource paths without opening a Tk window."""
    try:
        songs = load_builtin_songs()
        if not songs or any(not song.available for song in songs):
            return 1
    except (LibraryError, OSError, ValueError):
        return 1
    return 0


def main() -> int:
    if "--self-check" in sys.argv:
        return self_check()
    root, _native_file_drop_ready = create_application_root(tk)
    try:
        LauncherApp(root)
        root.mainloop()
    except Exception as exc:
        try:
            ensure_user_directories()
            with (LOG_DIR / "launcher-fatal.log").open("a", encoding="utf-8") as stream:
                stream.write("".join(traceback.format_exception(exc)) + "\n")
        except OSError:
            pass
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
