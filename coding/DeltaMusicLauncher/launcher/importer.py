"""Import and export helpers for user-maintained DeltaMusic libraries.

Imports never execute a supplied Python file.  Advanced Python songs remain
untrusted compatibility content until the user explicitly chooses to run one.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import tokenize
import unicodedata
import zipfile
from typing import Iterable

import mido

try:
    # Kept optional at import time so a partially repaired source checkout can
    # still open its existing library.  The supported EXE build declares this
    # dependency and bundles it for automatic Chinese filename transliteration.
    from pypinyin import lazy_pinyin as _lazy_pinyin
except ImportError:  # pragma: no cover - covered by a patched unit-test path.
    _lazy_pinyin = None

from library import (
    LEGACY_SONGS_DIR,
    MAX_MIDI_BYTES,
    MAX_PYTHON_BYTES,
    SAFE_SONGS_DIR,
    USER_LIBRARY,
    LibraryError,
    ensure_user_directories,
    is_within,
    iter_song_files,
    load_builtin_songs,
    load_user_songs,
    sha256_file,
    validate_custom_manifest,
    validate_song_id,
)


MAX_PACKAGE_FILES = 16
MAX_PACKAGE_TOTAL_BYTES = 60 * 1024 * 1024
ALLOWED_PACKAGE_SUFFIXES = {".json", ".mid", ".midi", ".py"}
_HAN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ebef]+"
)


def _romanize_han(match: re.Match[str]) -> str:
    """Turn one contiguous Han run into space-separated, tone-free pinyin."""
    if _lazy_pinyin is None:
        raise LibraryError(
            "无法把中文文件名转换为拼音：程序缺少 pypinyin 组件。"
            "请使用完整发布版，或先修复应用文件。"
        )
    parts = _lazy_pinyin(match.group(0))
    # pypinyin returns strings for normal input.  Filtering defensively keeps a
    # malformed third-party result from ever leaking an unsafe identifier.
    return " ".join(part for part in parts if isinstance(part, str))


def suggested_id(title: str) -> str:
    """Make a valid ID from English/Chinese display text.

    Chinese Han characters are converted to tone-free pinyin.  Other Unicode
    letters are normalized to their ASCII base when possible.  The result is
    only a suggestion; ``next_available_song_id`` makes it unique.
    """
    if not isinstance(title, str):
        raise LibraryError("无法从非文本名称生成曲目 ID。")

    # Convert Chinese before ASCII normalization.  ``NFKD`` then makes names
    # such as "Beyoncé" produce the useful ID ``beyonce``.
    romanized = _HAN_RE.sub(_romanize_han, title.strip())
    lowered = unicodedata.normalize("NFKD", romanized).lower()
    output: list[str] = []
    last_separator = False
    for char in lowered:
        if char.isascii() and char.isalnum():
            output.append(char)
            last_separator = False
        elif unicodedata.combining(char):
            # NFKD represents accents as a combining mark after the base
            # character; keeping the base gives ``beyonce``, not ``beyonce_``.
            continue
        elif not last_separator:
            # Spaces, punctuation, Unicode separators and discarded characters
            # all form one separator.  This prevents accidental word merging
            # (for example, "A&B" becoming ``a_b``, not ``ab``).
            output.append("_")
            last_separator = True
    result = "".join(output).strip("_-")
    return result[:64] or "my_song"


def suggested_id_from_filename(filename: str | Path) -> str:
    """Return the automatic safe-ID proposal for a MIDI filename's stem."""
    if not isinstance(filename, (str, Path)):
        raise LibraryError("无法从该文件名生成曲目 ID。")
    # A drop callback can supply a Windows path even while a test or tooling
    # process is running on another platform.  Normalize both separators before
    # taking the stem; this function never uses the path for file access.
    stem = PurePosixPath(str(filename).replace("\\", "/")).stem
    return suggested_id(stem)


def current_song_ids() -> set[str]:
    """Collect occupied IDs across built-ins and both personal-library roots.

    Folder names are included as well as valid manifests, so an interrupted or
    malformed earlier import cannot be overwritten on a case-insensitive
    Windows filesystem.
    """
    ensure_user_directories()
    occupied = {song.identifier.casefold() for song in load_builtin_songs()}
    occupied.update(song.identifier.casefold() for song in load_user_songs())
    for folder in (SAFE_SONGS_DIR, LEGACY_SONGS_DIR):
        try:
            occupied.update(child.name.casefold() for child in folder.iterdir())
        except OSError as exc:
            raise LibraryError("无法读取个人曲库中的现有曲目 ID。") from exc
    return occupied


def next_available_song_id(preferred: str, existing_ids: Iterable[str] | None = None) -> str:
    """Return ``preferred``, or ``preferred1``, ``preferred2`` … if occupied.

    Passing ``existing_ids`` keeps the collision rule pure and easy to test;
    omitting it checks the current built-in and personal libraries.  Comparisons
    use ``casefold`` because Windows directory names are case-insensitive.
    """
    base = validate_song_id(preferred)
    if existing_ids is None:
        occupied = current_song_ids()
    else:
        occupied = {value.casefold() for value in existing_ids if isinstance(value, str)}

    if base not in occupied:
        return base

    # A 64-character base still has room for a suffix after trimming.  Looping
    # here is deterministic: no random IDs and no filesystem mutation occur.
    suffix = 1
    while suffix <= 1_000_000:
        text = str(suffix)
        candidate = f"{base[: 64 - len(text)]}{text}"
        if candidate not in occupied:
            return candidate
        suffix += 1
    raise LibraryError("无法为曲目分配唯一 ID；请手动重命名文件后重试。")


def _check_source_file(path: Path, extensions: set[str], maximum: int, label: str) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not candidate.exists() or not candidate.is_file():
        raise LibraryError(f"找不到{label}文件。")
    if candidate.is_symlink():
        raise LibraryError(f"不允许从符号链接导入{label}文件。")
    if candidate.suffix.lower() not in extensions:
        suffixes = "、".join(sorted(extensions))
        raise LibraryError(f"{label}文件必须是：{suffixes}")
    if candidate.stat().st_size > maximum:
        raise LibraryError(f"{label}文件过大。")
    return candidate


def validate_midi_file(path: Path) -> dict[str, int]:
    candidate = _check_source_file(path, {".mid", ".midi"}, MAX_MIDI_BYTES, "MIDI")
    try:
        midi = mido.MidiFile(candidate)
    except (OSError, EOFError, ValueError) as exc:
        raise LibraryError(f"无法读取 MIDI：{exc}") from exc

    note_count = sum(
        1
        for track in midi.tracks
        for message in track
        if message.type == "note_on" and getattr(message, "velocity", 0) > 0
    )
    if note_count == 0:
        raise LibraryError("MIDI 中没有可演奏的 note_on 音符。")
    return {"tracks": len(midi.tracks), "note_on": note_count}


def validate_python_file(path: Path) -> None:
    candidate = _check_source_file(path, {".py"}, MAX_PYTHON_BYTES, "Python")
    try:
        with tokenize.open(candidate) as stream:
            source = stream.read()
        ast.parse(source, filename=str(candidate))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise LibraryError(f"Python 文件无法解析：{exc}") from exc


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _staging_folder() -> Path:
    ensure_user_directories()
    return Path(tempfile.mkdtemp(prefix=".import-", dir=str(USER_LIBRARY)))


def _target_for(song_type: str, identifier: str) -> Path:
    base = SAFE_SONGS_DIR if song_type == "generic_midi" else LEGACY_SONGS_DIR
    target = base / identifier
    if target.exists():
        raise LibraryError(f"个人曲库中已经有 ID 为“{identifier}”的曲目；请换一个 ID。")
    return target


def _commit(staging: Path, target: Path) -> Path:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise LibraryError(f"目标曲目目录已存在：{target.name}")
        staging.replace(target)
        return target / "song.json"
    except OSError as exc:
        raise LibraryError(f"保存个人曲库失败：{exc}") from exc


def import_midi(source: Path, title: str, identifier: str, octave_shift: int = 0) -> Path:
    identifier = validate_song_id(identifier)
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 160:
        raise LibraryError("曲目名称不能为空，且不能超过 160 个字符。")
    if octave_shift not in {-24, -12, 0, 12, 24}:
        raise LibraryError("八度偏移必须是 -24、-12、0、12 或 24。")

    midi_source = _check_source_file(source, {".mid", ".midi"}, MAX_MIDI_BYTES, "MIDI")
    summary = validate_midi_file(midi_source)
    target = _target_for("generic_midi", identifier)
    staging = _staging_folder()
    try:
        midi_name = "melody" + midi_source.suffix.lower()
        shutil.copy2(midi_source, staging / midi_name)
        _write_manifest(
            staging / "song.json",
            {
                "schema_version": 1,
                "id": identifier,
                "title": title.strip(),
                "type": "generic_midi",
                "midi": midi_name,
                "description": f"本地导入：{summary['tracks']} 个轨道，{summary['note_on']} 个音符起点。",
                "playback": {
                    "start_delay_seconds": 10,
                    "speed": 1.0,
                    "octave_shift": octave_shift,
                },
            },
        )
        validate_custom_manifest(staging / "song.json")
        return _commit(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def import_legacy_python(source_py: Path, midi_sources: Iterable[Path], title: str, identifier: str) -> Path:
    identifier = validate_song_id(identifier)
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 160:
        raise LibraryError("曲目名称不能为空，且不能超过 160 个字符。")

    python_source = _check_source_file(source_py, {".py"}, MAX_PYTHON_BYTES, "Python")
    validate_python_file(python_source)
    midi_candidates = [
        _check_source_file(Path(item), {".mid", ".midi"}, MAX_MIDI_BYTES, "MIDI")
        for item in midi_sources
    ]
    if not midi_candidates:
        raise LibraryError("高级导入至少需要选择一个 MIDI 文件。")
    if len(midi_candidates) > 8:
        raise LibraryError("高级导入最多支持 8 个 MIDI 文件。")
    names = [item.name.lower() for item in midi_candidates]
    if len(names) != len(set(names)):
        raise LibraryError("选择的 MIDI 文件有同名文件，请先重命名后再导入。")
    for candidate in midi_candidates:
        validate_midi_file(candidate)

    target = _target_for("legacy_python", identifier)
    staging = _staging_folder()
    try:
        script_name = python_source.name
        shutil.copy2(python_source, staging / script_name)
        copied_midi: list[str] = []
        for candidate in midi_candidates:
            shutil.copy2(candidate, staging / candidate.name)
            copied_midi.append(candidate.name)
        _write_manifest(
            staging / "song.json",
            {
                "schema_version": 1,
                "id": identifier,
                "title": title.strip(),
                "type": "legacy_python",
                "script": script_name,
                "midi_files": copied_midi,
                "script_sha256": sha256_file(staging / script_name),
                "description": "高级兼容模式：导入的 Python 代码未经审核，只运行可信来源。",
                "playback": {
                    "start_delay_seconds": 10,
                    "speed": 1.0,
                    "octave_shift": 0,
                },
            },
        )
        validate_custom_manifest(staging / "song.json")
        return _commit(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _zip_member_is_safe(info: zipfile.ZipInfo) -> bool:
    name = info.filename.replace("\\", "/")
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        return False
    mode = (info.external_attr >> 16) & 0o170000
    # Unix symlink file type.  Archive tools that do not set a Unix mode are OK.
    if mode == 0o120000:
        return False
    return True


def import_package(source: Path) -> Path:
    package = _check_source_file(Path(source), {".zip", ".dmsong"}, MAX_PACKAGE_TOTAL_BYTES, "曲目包")
    staging = _staging_folder()
    try:
        with zipfile.ZipFile(package) as archive:
            entries = [info for info in archive.infolist() if not info.is_dir()]
            if not entries or len(entries) > MAX_PACKAGE_FILES:
                raise LibraryError(f"曲目包必须包含 1–{MAX_PACKAGE_FILES} 个文件。")
            if sum(item.file_size for item in entries) > MAX_PACKAGE_TOTAL_BYTES:
                raise LibraryError("曲目包解压后的内容超过 60 MB 限制。")
            names = set()
            for info in entries:
                if not _zip_member_is_safe(info):
                    raise LibraryError("曲目包包含不安全的路径或符号链接。")
                path = PurePosixPath(info.filename.replace("\\", "/"))
                if path.suffix.lower() not in ALLOWED_PACKAGE_SUFFIXES:
                    raise LibraryError("曲目包只能包含 JSON、MIDI 和 Python 文件。")
                if path.name != "song.json" and path.suffix.lower() == ".json":
                    raise LibraryError("曲目包只允许一个名为 song.json 的 JSON 文件。")
                if path.as_posix() in names:
                    raise LibraryError("曲目包存在重复文件名。")
                names.add(path.as_posix())
                destination = staging / Path(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source_stream, destination.open("wb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)

        manifest_path = staging / "song.json"
        if not manifest_path.is_file():
            raise LibraryError("曲目包根目录必须包含 song.json。")
        manifest = validate_custom_manifest(manifest_path)
        if manifest["type"] == "generic_midi":
            validate_midi_file(staging / manifest["midi"])
        else:
            validate_python_file(staging / manifest["script"])
            for midi_name in manifest["midi_files"]:
                validate_midi_file(staging / midi_name)

        target = _target_for(manifest["type"], manifest["id"])
        return _commit(staging, target)
    except (OSError, zipfile.BadZipFile) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise LibraryError(f"无法读取曲目包：{exc}") from exc
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def export_package(manifest_path: Path, destination: Path) -> Path:
    manifest_path = Path(manifest_path).resolve()
    if not is_within(manifest_path, USER_LIBRARY):
        raise LibraryError("只能导出个人曲库中的曲目。")
    manifest = validate_custom_manifest(manifest_path)
    root = manifest_path.parent
    output = Path(destination).expanduser().resolve()
    if output.suffix.lower() != ".dmsong":
        output = output.with_suffix(".dmsong")
    if output.exists():
        raise LibraryError("目标文件已存在；请换一个文件名。")
    output.parent.mkdir(parents=True, exist_ok=True)

    allowed = {".json", ".mid", ".midi", ".py"}
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in iter_song_files(root):
                relative = path.relative_to(root)
                if path.suffix.lower() not in allowed:
                    continue
                archive.write(path, relative.as_posix())
    except OSError as exc:
        raise LibraryError(f"导出曲目包失败：{exc}") from exc
    return output
