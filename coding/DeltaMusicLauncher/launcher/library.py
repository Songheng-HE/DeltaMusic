"""Shared paths, catalog parsing, and validation for DeltaMusic Launcher.

This module deliberately treats paths from manifests as untrusted data.  The
launcher never discovers arbitrary Python files in the project tree; the only
built-in scripts it can run are enumerated in ``catalog.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile


IS_FROZEN = bool(getattr(sys, "frozen", False))

# A PyInstaller executable unpacks its bundled Python modules and catalog into
# _MEIPASS, while the song folders remain beside the .exe.  Source and normal
# ZIP releases continue to use the existing relative layout below.
if IS_FROZEN:
    CODE_ROOT = Path(sys.executable).resolve().parent
    # Keep the editable release manifest outside the executable.  A bundled
    # copy is retained as a recovery fallback if someone accidentally removes
    # launcher\catalog.json from an extracted release.
    LAUNCHER_DIR = CODE_ROOT / "launcher"
    _BUNDLED_LAUNCHER_DIR = Path(getattr(sys, "_MEIPASS", CODE_ROOT)) / "launcher"
    APP_ROOT = CODE_ROOT
else:
    CODE_ROOT = Path(__file__).resolve().parent.parent
    LAUNCHER_DIR = Path(__file__).resolve().parent
    # In a normal release, code and song assets share one root.  In the
    # checked-in source tree, code lives in DeltaMusic\coding\DeltaMusicLauncher
    # while assets remain in DeltaMusic.  Supporting both keeps local testing
    # honest.
    _development_asset_root = CODE_ROOT.parent.parent
    if (CODE_ROOT / "croatian").is_dir():
        APP_ROOT = CODE_ROOT
    elif (_development_asset_root / "croatian").is_dir():
        APP_ROOT = _development_asset_root
    else:
        APP_ROOT = CODE_ROOT
CATALOG_PATH = LAUNCHER_DIR / "catalog.json"
if IS_FROZEN and not CATALOG_PATH.is_file():
    CATALOG_PATH = _BUNDLED_LAUNCHER_DIR / "catalog.json"
DATA_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "DeltaMusic"
USER_LIBRARY = DATA_ROOT / "user_library"
SAFE_SONGS_DIR = USER_LIBRARY / "songs"
LEGACY_SONGS_DIR = USER_LIBRARY / "legacy"
LOG_DIR = DATA_ROOT / "logs"
CONTROL_DIR = DATA_ROOT / "controls"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ALLOWED_CUSTOM_TYPES = {"generic_midi", "legacy_python"}
MAX_MIDI_BYTES = 20 * 1024 * 1024
MAX_PYTHON_BYTES = 2 * 1024 * 1024


class LibraryError(ValueError):
    """An error that can be shown to a launcher user."""


@dataclass(frozen=True)
class Song:
    identifier: str
    title: str
    source: str
    kind: str
    root: Path
    description: str
    available: bool
    issue: str = ""
    script_path: Path | None = None
    midi_path: Path | None = None
    manifest_path: Path | None = None

    @property
    def is_custom(self) -> bool:
        return self.source != "内置"


def ensure_user_directories() -> None:
    """Create only per-user writable directories, never inside the release."""
    for folder in (SAFE_SONGS_DIR, LEGACY_SONGS_DIR, LOG_DIR, CONTROL_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_song_id(value: Any) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise LibraryError("曲目 ID 只能是 1–64 位小写英文、数字、_ 或 -，且不能以符号开头。")
    return value


def safe_child_path(parent: Path, relative: Any) -> Path:
    """Resolve a manifest relative path without allowing traversal or UNC paths."""
    if not isinstance(relative, str) or not relative.strip():
        raise LibraryError("曲目文件路径不能为空。")

    raw = relative.strip()
    if (
        raw.startswith(("/", "\\"))
        or re.match(r"^[a-zA-Z]:", raw)
        or "\x00" in raw
    ):
        raise LibraryError("曲目文件必须使用曲目目录内的相对路径。")

    parts = Path(raw).parts
    if any(part in ("", ".", "..") for part in parts):
        raise LibraryError("曲目文件路径不能包含 . 或 ..。")

    candidate = (parent / Path(raw)).resolve()
    if not is_within(candidate, parent):
        raise LibraryError("曲目文件路径越出了曲目目录。")
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LibraryError(f"找不到配置文件：{path.name}") from exc
    except json.JSONDecodeError as exc:
        raise LibraryError(f"{path.name} 不是有效 JSON：第 {exc.lineno} 行。") from exc

    if not isinstance(data, dict):
        raise LibraryError(f"{path.name} 的最外层必须是 JSON 对象。")
    return data


def _require_text(data: dict[str, Any], key: str, label: str, limit: int = 160) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise LibraryError(f"{label} 必须是 1–{limit} 个字符的文本。")
    return value.strip()


def _playback_settings(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("playback", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise LibraryError("playback 必须是对象。")

    delay = raw.get("start_delay_seconds", 10)
    speed = raw.get("speed", 1.0)
    shift = raw.get("octave_shift", 0)
    if not isinstance(delay, (int, float)) or not 0 <= delay <= 30:
        raise LibraryError("start_delay_seconds 必须在 0 到 30 之间。")
    if not isinstance(speed, (int, float)) or not 0 < speed <= 3:
        raise LibraryError("speed 必须大于 0 且不超过 3。")
    if not isinstance(shift, int) or shift not in {-24, -12, 0, 12, 24}:
        raise LibraryError("octave_shift 只能是 -24、-12、0、12 或 24。")
    return {
        "start_delay_seconds": float(delay),
        "speed": float(speed),
        "octave_shift": shift,
    }


def validate_custom_manifest(path: Path) -> dict[str, Any]:
    """Read and validate a custom manifest without executing any Python."""
    data = _read_json(path)
    if data.get("schema_version") != 1:
        raise LibraryError("只支持 schema_version 为 1 的曲目包。")

    identifier = validate_song_id(data.get("id"))
    title = _require_text(data, "title", "title")
    song_type = data.get("type")
    if song_type not in ALLOWED_CUSTOM_TYPES:
        raise LibraryError("type 只能是 generic_midi 或 legacy_python。")

    normalized: dict[str, Any] = {
        "schema_version": 1,
        "id": identifier,
        "title": title,
        "type": song_type,
        "playback": _playback_settings(data),
    }
    description = data.get("description", "")
    if description:
        if not isinstance(description, str) or len(description) > 500:
            raise LibraryError("description 必须是不超过 500 个字符的文本。")
        normalized["description"] = description.strip()
    else:
        normalized["description"] = ""

    root = path.parent.resolve()
    if song_type == "generic_midi":
        midi_relative = data.get("midi")
        midi_path = safe_child_path(root, midi_relative)
        if midi_path.suffix.lower() not in {".mid", ".midi"}:
            raise LibraryError("安全 MIDI 曲目的 midi 必须指向 .mid 或 .midi 文件。")
        if not midi_path.is_file():
            raise LibraryError(f"找不到 MIDI：{midi_relative}")
        if midi_path.stat().st_size > MAX_MIDI_BYTES:
            raise LibraryError("MIDI 文件超过 20 MB 限制。")
        normalized["midi"] = str(Path(midi_relative))
    else:
        script_relative = data.get("script")
        script_path = safe_child_path(root, script_relative)
        if script_path.suffix.lower() != ".py" or not script_path.is_file():
            raise LibraryError("高级曲目必须包含存在的 .py script。")
        if script_path.stat().st_size > MAX_PYTHON_BYTES:
            raise LibraryError("Python 文件超过 2 MB 限制。")
        normalized["script"] = str(Path(script_relative))

        midi_files = data.get("midi_files", [])
        if not isinstance(midi_files, list) or not midi_files:
            raise LibraryError("高级曲目至少需要一个 midi_files 文件。")
        if len(midi_files) > 8:
            raise LibraryError("高级曲目最多包含 8 个 MIDI 文件。")
        normalized_midi: list[str] = []
        for midi_relative in midi_files:
            midi_path = safe_child_path(root, midi_relative)
            if midi_path.suffix.lower() not in {".mid", ".midi"} or not midi_path.is_file():
                raise LibraryError("midi_files 中存在无效或缺失的 MIDI 文件。")
            if midi_path.stat().st_size > MAX_MIDI_BYTES:
                raise LibraryError("MIDI 文件超过 20 MB 限制。")
            normalized_midi.append(str(Path(midi_relative)))
        normalized["midi_files"] = normalized_midi

        declared_hash = data.get("script_sha256", "")
        if declared_hash:
            if not isinstance(declared_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", declared_hash):
                raise LibraryError("script_sha256 必须是 SHA-256 小写十六进制值。")
            normalized["script_sha256"] = declared_hash

    return normalized


def _builtin_song(raw: dict[str, Any]) -> Song:
    identifier = validate_song_id(raw.get("id"))
    title = _require_text(raw, "title", "内置曲目 title")
    description = str(raw.get("description", "")).strip()
    folder = safe_child_path(APP_ROOT, raw.get("folder"))
    script = safe_child_path(folder, raw.get("script"))

    required = raw.get("required_files", [])
    if not isinstance(required, list):
        raise LibraryError(f"内置曲目 {identifier} 的 required_files 必须是列表。")
    missing: list[str] = []
    for item in required:
        candidate = safe_child_path(folder, item)
        if not candidate.is_file():
            missing.append(str(item))
    if not script.is_file():
        missing.insert(0, str(raw.get("script", "播放器脚本")))

    issue = "" if not missing else "缺少：" + "、".join(missing)
    return Song(
        identifier=identifier,
        title=title,
        source="内置",
        kind="legacy_python",
        root=folder,
        description=description,
        available=not missing,
        issue=issue,
        script_path=script,
    )


def load_builtin_songs() -> list[Song]:
    data = _read_json(CATALOG_PATH)
    if data.get("schema_version") != 1 or not isinstance(data.get("songs"), list):
        raise LibraryError("catalog.json 格式不正确。")
    songs: list[Song] = []
    ids: set[str] = set()
    for raw in data["songs"]:
        if not isinstance(raw, dict):
            raise LibraryError("catalog.json 的 songs 中存在非对象项目。")
        song = _builtin_song(raw)
        if song.identifier in ids:
            raise LibraryError(f"catalog.json 有重复曲目 ID：{song.identifier}")
        ids.add(song.identifier)
        songs.append(song)
    return songs


def _custom_song_from_manifest(manifest_path: Path, source: str) -> Song:
    try:
        data = validate_custom_manifest(manifest_path)
        root = manifest_path.parent.resolve()
        kind = data["type"]
        script_path = safe_child_path(root, data["script"]) if kind == "legacy_python" else None
        midi_path = safe_child_path(root, data["midi"]) if kind == "generic_midi" else None
        return Song(
            identifier=data["id"],
            title=data["title"],
            source=source,
            kind=kind,
            root=root,
            description=data.get("description", ""),
            available=True,
            script_path=script_path,
            midi_path=midi_path,
            manifest_path=manifest_path.resolve(),
        )
    except LibraryError as exc:
        return Song(
            identifier=manifest_path.parent.name,
            title=f"无效曲目：{manifest_path.parent.name}",
            source=source,
            kind="invalid",
            root=manifest_path.parent.resolve(),
            description="",
            available=False,
            issue=str(exc),
            manifest_path=manifest_path.resolve(),
        )


def load_user_songs() -> list[Song]:
    ensure_user_directories()
    songs: list[Song] = []
    for base, source in ((SAFE_SONGS_DIR, "个人 MIDI"), (LEGACY_SONGS_DIR, "个人 Python")):
        for manifest_path in sorted(base.glob("*/song.json"), key=lambda item: item.parent.name.lower()):
            songs.append(_custom_song_from_manifest(manifest_path, source))
    return songs


def load_all_songs() -> list[Song]:
    return [*load_builtin_songs(), *load_user_songs()]


def _custom_song_root_for_manifest(manifest_path: Path) -> Path:
    """Return a directly-owned personal-song directory, or reject the path.

    This is intentionally stricter than :func:`is_within`: maintenance
    operations must never be able to target an arbitrary directory merely
    because it happens to be somewhere below ``user_library``.  A song is
    owned only when its manifest is exactly ``songs/<one folder>/song.json``
    or ``legacy/<one folder>/song.json``.
    """
    try:
        supplied = Path(manifest_path).expanduser()
    except (TypeError, ValueError) as exc:
        raise LibraryError("个人曲目配置文件路径无效。") from exc

    # First check the path lexically, before resolving symlinks.  Otherwise a
    # directory link such as ``songs/alias -> songs/real_song`` could make an
    # operation requested through ``alias/song.json`` affect ``real_song``.
    # Normalising with abspath handles relative paths and ``..`` without
    # following links.
    supplied_absolute = Path(os.path.abspath(str(supplied)))
    if supplied_absolute.name != "song.json":
        raise LibraryError("只能维护个人曲目的 song.json。")

    declared_base: Path | None = None
    declared_root: Path | None = None
    for base in (SAFE_SONGS_DIR, LEGACY_SONGS_DIR):
        lexical_base = Path(os.path.abspath(str(base.expanduser())))
        try:
            relative = supplied_absolute.relative_to(lexical_base)
        except ValueError:
            continue
        if len(relative.parts) == 2 and relative.name == "song.json":
            declared_base = base
            declared_root = supplied_absolute.parent
            break
    if declared_base is None or declared_root is None:
        raise LibraryError("只能维护个人曲库中直接存放的曲目，内置曲目为只读。")

    try:
        # Do not accept a symlink as the manifest itself.  It could otherwise
        # point a rename or deletion operation outside the personal library.
        if declared_base.is_symlink() or declared_root.is_symlink() or supplied_absolute.is_symlink():
            raise LibraryError("个人曲目目录或配置文件不能是符号链接。")
        manifest = supplied_absolute.resolve(strict=True)
    except FileNotFoundError as exc:
        raise LibraryError("找不到个人曲目的 song.json。") from exc
    except OSError as exc:
        raise LibraryError("无法读取个人曲目的 song.json。") from exc

    if not manifest.is_file():
        raise LibraryError("个人曲目的 song.json 不是普通文件。")

    root = manifest.parent
    try:
        if root.is_symlink():
            raise LibraryError("个人曲目目录不能是符号链接。")
        resolved_root = root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise LibraryError("找不到个人曲目目录。") from exc
    except OSError as exc:
        raise LibraryError("无法读取个人曲目目录。") from exc

    try:
        resolved_base = declared_base.resolve(strict=False)
        relative = resolved_root.relative_to(resolved_base)
    except ValueError:
        relative = Path("..")
    if len(relative.parts) == 1 and relative.parts[0] not in {"", ".", ".."}:
        return resolved_root

    raise LibraryError("只能维护个人曲库中直接存放的曲目，内置曲目为只读。")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace a JSON file atomically without leaving a partial manifest."""
    descriptor: int | None = None
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".song-",
            suffix=".tmp",
            dir=str(path.parent),
            text=True,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
        temporary = ""
    except OSError as exc:
        raise LibraryError(f"保存个人曲目名称失败：{exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def rename_custom_song(manifest_path: Path, title: str) -> Path:
    """Change only the display title of one valid personal song.

    The identifier and directory are deliberately kept intact.  They are the
    stable references used by imports, exports, and the player host; changing
    them as part of a visible-name edit could break a running or shared song.
    """
    root = _custom_song_root_for_manifest(manifest_path)
    manifest = root / "song.json"

    # Validate first so an invalid song is never silently rewritten into a
    # different shape.  A broken song can still be safely deleted instead.
    validate_custom_manifest(manifest)
    normalized_title = _require_text({"title": title}, "title", "曲目名称")
    payload = _read_json(manifest)
    payload["title"] = normalized_title
    _atomic_write_json(manifest, payload)
    return manifest


def delete_custom_song(manifest_path: Path) -> None:
    """Permanently remove one personal song directory, never a built-in song.

    Deletion is permitted even when its manifest has become invalid, so a user
    can recover from a damaged or incomplete import.  The ownership check is
    still performed before recursively removing anything.
    """
    root = _custom_song_root_for_manifest(manifest_path)
    try:
        shutil.rmtree(root)
    except OSError as exc:
        raise LibraryError(f"删除个人曲目失败：{exc}") from exc


def get_builtin_song(identifier: str) -> Song:
    for song in load_builtin_songs():
        if song.identifier == identifier:
            return song
    raise LibraryError(f"未知内置曲目：{identifier}")


def iter_song_files(song_root: Path) -> Iterable[Path]:
    """Yield regular files in a custom song directory, excluding symlinks."""
    for path in song_root.rglob("*"):
        if path.is_file() and not path.is_symlink() and is_within(path, song_root):
            yield path
