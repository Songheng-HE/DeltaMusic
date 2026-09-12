"""Trusted three-octave MIDI player used for safe, MIDI-only personal songs.

It accepts only a validated ``song.json`` manifest.  It intentionally supports
strictly monophonic MIDI: a general MIDI arranger cannot guess which note of a
chord is the melody without changing the music.
"""

from __future__ import annotations

import argparse
import atexit
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path
import time

import keyboard
import mido
import pydirectinput as pdi

from library import LibraryError, safe_child_path, validate_custom_manifest
from playback_control import PlaybackStopController, PlaybackStopped


pdi.FAILSAFE = False
STOP_KEY = "f10"
MODIFIER_PREROLL = 0.008
LEFT = "left"
MIDDLE = "middle"
RIGHT = "right"
NATURAL_KEYS = {0: "z", 2: "x", 4: "c", 5: "v", 7: "b", 9: "n", 11: "m"}
CHROMATIC_KEYS = {1: "z", 3: "x", 6: "v", 8: "b", 10: "n"}
ALL_KEYS = ["z", "x", "c", "v", "b", "n", "m", ","]
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

stop_controller = PlaybackStopController()
mouse_state = {LEFT: False, MIDDLE: False, RIGHT: False}


def release_everything() -> None:
    for key in ALL_KEYS:
        try:
            pdi.keyUp(key)
        except Exception:
            pass
    for button in (LEFT, MIDDLE, RIGHT):
        try:
            pdi.mouseUp(button=button)
        except Exception:
            pass
        mouse_state[button] = False


atexit.register(release_everything)


def set_button(button: str, wanted: bool) -> None:
    current = mouse_state[button]
    if wanted and not current:
        stop_controller.raise_if_requested()
        pdi.mouseDown(button=button)
        mouse_state[button] = True
    elif not wanted and current:
        stop_controller.raise_if_requested()
        pdi.mouseUp(button=button)
        mouse_state[button] = False


def set_modifiers(range_button: str | None, chromatic: bool) -> None:
    set_button(LEFT, range_button == LEFT)
    set_button(RIGHT, range_button == RIGHT)
    set_button(MIDDLE, chromatic)


def midi_name(note: int) -> str:
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 1}"


def build_tempo_map(mid: mido.MidiFile) -> list[tuple[int, float, int]]:
    tempo_events: list[tuple[int, int]] = []
    for track in mid.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "set_tempo":
                tempo_events.append((tick, message.tempo))
    tempo_events.sort(key=lambda item: item[0])

    current_tempo = 500000
    last_tick = 0
    seconds = 0.0
    result = [(0, 0.0, current_tempo)]
    for tick, new_tempo in tempo_events:
        if tick > last_tick:
            seconds += mido.tick2second(tick - last_tick, mid.ticks_per_beat, current_tempo)
        last_tick = tick
        current_tempo = new_tempo
        if result[-1][0] == tick:
            result[-1] = (tick, seconds, current_tempo)
        else:
            result.append((tick, seconds, current_tempo))
    return result


def tick_to_seconds(tick: int, mid: mido.MidiFile, tempo_map: list[tuple[int, float, int]]) -> float:
    ticks = [item[0] for item in tempo_map]
    index = max(bisect_right(ticks, tick) - 1, 0)
    base_tick, base_seconds, tempo = tempo_map[index]
    return base_seconds + mido.tick2second(tick - base_tick, mid.ticks_per_beat, tempo)


def collect_notes(mid: mido.MidiFile) -> list[dict[str, int]]:
    notes: list[dict[str, int]] = []
    for track in mid.tracks:
        tick = 0
        active: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
        for message in track:
            tick += message.time
            channel = getattr(message, "channel", 0)
            if message.type == "note_on" and message.velocity > 0:
                active[(channel, message.note)].append((tick, message.velocity))
            elif message.type == "note_off" or (message.type == "note_on" and message.velocity == 0):
                key = (channel, message.note)
                if active[key]:
                    start_tick, _velocity = active[key].pop(0)
                    if tick > start_tick:
                        notes.append({"start_tick": start_tick, "end_tick": tick, "pitch": message.note})
    notes.sort(key=lambda item: item["start_tick"])
    return notes


def validate_monophonic(notes: list[dict[str, int]]) -> None:
    if not notes:
        raise LibraryError("这个 MIDI 没有完整的可演奏音符。")
    onsets = Counter(note["start_tick"] for note in notes)
    simultaneous = max(onsets.values())
    overlaps = sum(1 for left, right in zip(notes, notes[1:]) if left["end_tick"] > right["start_tick"])
    if simultaneous > 1 or overlaps:
        raise LibraryError(
            "安全 MIDI 播放器只支持严格单旋律 MIDI；"
            f"检测到同时起音 {simultaneous} 个、重叠音 {overlaps} 处。"
        )


def base_key_for_pitch_class(pitch_class: int) -> tuple[str, bool]:
    if pitch_class in NATURAL_KEYS:
        return NATURAL_KEYS[pitch_class], False
    return CHROMATIC_KEYS[pitch_class], True


def command_for_pitch(source_pitch: int, octave_shift: int) -> tuple[str, str | None, bool, int, bool]:
    """Map a MIDI pitch to the documented three-octave DeltaMusic layout."""
    pitch = source_pitch + octave_shift
    folded = False
    while pitch < 48:
        pitch += 12
        folded = True
    while pitch > 85:
        pitch -= 12
        folded = True

    if 48 <= pitch <= 59:
        key, chromatic = base_key_for_pitch_class(pitch % 12)
        return key, LEFT, chromatic, pitch, not folded
    if 60 <= pitch <= 71:
        key, chromatic = base_key_for_pitch_class(pitch % 12)
        return key, None, chromatic, pitch, not folded
    if pitch == 72:
        return ",", None, False, pitch, not folded
    if 73 <= pitch <= 83:
        key, chromatic = base_key_for_pitch_class(pitch % 12)
        return key, RIGHT, chromatic, pitch, not folded
    if pitch == 84:
        return ",", RIGHT, False, pitch, not folded
    if pitch == 85:
        return ",", RIGHT, True, pitch, not folded
    raise LibraryError(f"无法映射音高 {midi_name(source_pitch)}。")


def wait_until(target: float) -> None:
    stop_controller.wait_until(target)


def play_manifest(manifest_path: Path) -> None:
    manifest_path = Path(manifest_path).resolve()
    manifest = validate_custom_manifest(manifest_path)
    if manifest["type"] != "generic_midi":
        raise LibraryError("内置 MIDI 播放器只能运行 generic_midi 曲目。")

    midi_path = safe_child_path(manifest_path.parent, manifest["midi"])
    midi = mido.MidiFile(midi_path)
    notes = collect_notes(midi)
    validate_monophonic(notes)
    playback = manifest["playback"]
    tempo_map = build_tempo_map(midi)
    events = [
        {
            **note,
            "start_seconds": tick_to_seconds(note["start_tick"], midi, tempo_map),
            "end_seconds": tick_to_seconds(note["end_tick"], midi, tempo_map),
        }
        for note in notes
    ]
    source_min = min(note["pitch"] for note in notes)
    source_max = max(note["pitch"] for note in notes)
    approximations = sum(
        not command_for_pitch(note["pitch"], playback["octave_shift"])[4]
        for note in notes
    )

    print()
    print("=" * 52)
    print(f"DeltaMusic 安全 MIDI 播放器：{manifest['title']}")
    print("=" * 52)
    print(f"音符数：{len(notes)}")
    print(f"源 MIDI 音域：{midi_name(source_min)} ~ {midi_name(source_max)}")
    print(f"八度偏移：{playback['octave_shift']:+d} 半音")
    if approximations:
        print(f"提示：有 {approximations} 个音因游戏音域限制进行了八度折叠。")
    print("LEFT = 低八度；MIDDLE = 升半音；RIGHT = 高八度")
    print("F10 = 紧急停止（会释放所有已按下按键和鼠标键）")
    print(f"将在 {playback['start_delay_seconds']:.0f} 秒后开始。")
    print("请切回《三角洲行动》并拿出口琴。")
    print("=" * 52)

    stop_controller.reset()
    pdi.PAUSE = 0
    clock = time.perf_counter() + playback["start_delay_seconds"]
    try:
        stop_controller.install_single_key_hook(keyboard, STOP_KEY)
        wait_until(clock)
        for index, event in enumerate(events):
            start = clock + event["start_seconds"] / playback["speed"]
            end = clock + event["end_seconds"] / playback["speed"]
            key, range_button, chromatic, _played, _exact = command_for_pitch(
                event["pitch"], playback["octave_shift"]
            )
            wait_until(max(clock, start - MODIFIER_PREROLL))
            set_modifiers(range_button, chromatic)
            wait_until(start)
            stop_controller.raise_if_requested()
            pdi.keyDown(key)
            wait_until(end)
            stop_controller.raise_if_requested()
            pdi.keyUp(key)
            if index + 1 < len(events):
                next_start = clock + events[index + 1]["start_seconds"] / playback["speed"]
                if next_start - end > 0.020:
                    set_modifiers(None, False)
        set_modifiers(None, False)
        print("演奏完成。")
    except (PlaybackStopped, KeyboardInterrupt):
        print("已停止演奏。")
    finally:
        release_everything()
        stop_controller.uninstall_hook(keyboard)
        print("已释放所有键盘和鼠标输入。")


def main() -> int:
    parser = argparse.ArgumentParser(description="DeltaMusic trusted MIDI player")
    parser.add_argument("manifest", type=Path, help="个人曲目的 song.json")
    args = parser.parse_args()
    try:
        play_manifest(args.manifest)
    except (LibraryError, OSError, ValueError) as exc:
        print(f"无法开始演奏：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
