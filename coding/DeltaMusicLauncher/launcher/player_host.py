"""Console-only, trusted playback dispatcher.

The GUI starts this module with Windows UAC only when input injection is
needed.  Keeping the GUI/importer outside this process prevents a newly
imported song from receiving administrator privileges by default.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys

from generic_midi_player import play_manifest
from library import (
    APP_ROOT,
    CONTROL_DIR,
    IS_FROZEN,
    USER_LIBRARY,
    LibraryError,
    get_builtin_song,
    is_within,
    load_builtin_songs,
    safe_child_path,
    sha256_file,
    validate_custom_manifest,
)
from playback_control import STOP_FILE_ENV, STOP_TOKEN_ENV


CONTROL_PROTOCOL_VERSION = 1
CONTROL_FILE_RE = re.compile(r"^playback-[0-9a-f]{32}\.json$")
CONTROL_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


@dataclass(frozen=True)
class StopControl:
    """A GUI-created, token-protected cancellation request file."""

    path: Path
    token: str


def _read_control_payload(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _validate_stop_control(path: Path, token: str) -> StopControl:
    """Accept only a fresh launcher control file under DeltaMusic's data root."""
    if not CONTROL_TOKEN_RE.fullmatch(token):
        raise LibraryError("播放器停止令牌格式无效。")
    if path.is_symlink():
        raise LibraryError("播放器停止控制文件不能是链接。")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise LibraryError("找不到播放器停止控制文件。") from exc

    control_root = CONTROL_DIR.resolve()
    if resolved.parent != control_root or not CONTROL_FILE_RE.fullmatch(resolved.name):
        raise LibraryError("播放器停止控制文件路径无效。")
    if not resolved.is_file():
        raise LibraryError("播放器停止控制文件不是普通文件。")

    payload = _read_control_payload(resolved)
    if (
        payload is None
        or payload.get("version") != CONTROL_PROTOCOL_VERSION
        or payload.get("token") != token
        or payload.get("command") not in {"ready", "stop"}
    ):
        raise LibraryError("播放器停止控制文件未通过验证。")
    return StopControl(path=resolved, token=token)


def _remove_stop_control_if_owned(control: StopControl) -> None:
    """Do not delete a file if it was replaced with a different run's token."""
    try:
        if control.path.is_symlink():
            return
        payload = _read_control_payload(control.path)
        if payload is None or payload.get("token") != control.token:
            return
        control.path.unlink()
    except OSError:
        pass


@contextmanager
def _stop_control_environment(control: StopControl | None):
    """Expose the validated channel to the player without persisting it globally."""
    if control is None:
        yield
        return

    previous = {
        STOP_FILE_ENV: os.environ.get(STOP_FILE_ENV),
        STOP_TOKEN_ENV: os.environ.get(STOP_TOKEN_ENV),
    }
    os.environ[STOP_FILE_ENV] = str(control.path)
    os.environ[STOP_TOKEN_ENV] = control.token
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _remove_stop_control_if_owned(control)


def _run_legacy(script: Path, cwd: Path) -> int:
    print()
    print("播放器会在当前窗口显示倒计时和运行信息。")
    print("请在倒计时内切回游戏；F10 或启动器的“紧急停止”可停止演奏。")
    print()
    if not IS_FROZEN:
        result = subprocess.run([sys.executable, str(script)], cwd=str(cwd), check=False)
        return result.returncode

    # A frozen EXE does not contain a general-purpose python.exe that can be
    # pointed at an external script.  Run the trusted/explicitly-confirmed
    # legacy script in this dedicated console host instead, while preserving
    # the usual __file__, argv, working-directory and local-import behavior.
    original_argv = sys.argv[:]
    original_cwd = Path.cwd()
    original_path = sys.path[:]
    try:
        sys.argv = [str(script)]
        sys.path.insert(0, str(cwd))
        os.chdir(cwd)
        runpy.run_path(str(script), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = original_argv
        sys.path[:] = original_path
        os.chdir(original_cwd)


def run_builtin(identifier: str) -> int:
    song = get_builtin_song(identifier)
    if not song.available or song.script_path is None:
        raise LibraryError(f"内置曲目文件不完整：{song.issue or song.title}")
    if not is_within(song.script_path, APP_ROOT):
        raise LibraryError("内置播放器路径异常，已拒绝启动。")
    print(f"启动内置曲目：{song.title}")
    return _run_legacy(song.script_path, song.root)


def run_custom(manifest_path: Path, allow_custom_code: bool) -> int:
    resolved = manifest_path.resolve()
    if not is_within(resolved, USER_LIBRARY):
        raise LibraryError("只能运行个人曲库中的 song.json。")
    if resolved.name != "song.json" or not resolved.is_file():
        raise LibraryError("个人曲目配置必须是 song.json。")

    manifest = validate_custom_manifest(resolved)
    if manifest["type"] == "generic_midi":
        print(f"启动安全 MIDI 曲目：{manifest['title']}")
        play_manifest(resolved)
        return 0

    if not allow_custom_code:
        raise LibraryError("高级 Python 曲目需要在启动器中明确确认后才能运行。")

    script = safe_child_path(resolved.parent, manifest["script"])
    expected_hash = manifest.get("script_sha256")
    if expected_hash and sha256_file(script) != expected_hash:
        raise LibraryError("该 Python 文件在导入后已被修改。请重新导入并重新确认后再运行。")
    print("警告：你已确认以当前权限运行个人导入的 Python 代码。")
    print("只应运行自己写的或完全信任来源的曲目。")
    return _run_legacy(script, resolved.parent)


def self_check() -> int:
    """Verify the frozen host can find its catalog and external song assets."""
    songs = load_builtin_songs()
    unavailable = [song.title for song in songs if not song.available]
    if unavailable:
        raise LibraryError("内置曲目文件不完整：" + "、".join(unavailable))
    print(f"自检通过：发现 {len(songs)} 首内置曲目。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="DeltaMusic elevated playback host")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--builtin", help="catalog.json 中的内置曲目 ID")
    choice.add_argument("--custom-manifest", type=Path, help="个人曲库中的 song.json")
    parser.add_argument("--self-check", action="store_true", help="验证发布包中的曲库文件，然后退出")
    parser.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--stop-token", help=argparse.SUPPRESS)
    parser.add_argument(
        "--run-custom-code",
        action="store_true",
        help="表示用户已确认允许高级 Python 曲目以此进程权限运行",
    )
    args = parser.parse_args()
    if (args.stop_file is None) != (args.stop_token is None):
        parser.error("--stop-file 和 --stop-token 必须同时提供。")

    control: StopControl | None = None
    try:
        if args.stop_file is not None and args.stop_token is not None:
            control = _validate_stop_control(args.stop_file, args.stop_token)
        with _stop_control_environment(control):
            if args.self_check:
                return self_check()
            if args.builtin:
                return run_builtin(args.builtin)
            if args.custom_manifest:
                return run_custom(args.custom_manifest, args.run_custom_code)
            parser.error("请提供 --builtin、--custom-manifest 或 --self-check。")
    except (LibraryError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"无法启动播放器：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
