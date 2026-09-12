import os
import time
import atexit
import sys
from bisect import bisect_right
from collections import defaultdict, Counter

import keyboard
import mido
import pydirectinput as pdi


def _add_playback_control_path():
    """Find the shared controller when this script is run standalone."""
    search_dir = os.path.dirname(os.path.abspath(__file__))

    for _ in range(6):
        for candidate in (
            os.path.join(search_dir, "launcher"),
            os.path.join(
                search_dir,
                "coding",
                "DeltaMusicLauncher",
                "launcher",
            ),
        ):
            if os.path.isfile(os.path.join(candidate, "playback_control.py")):
                if candidate not in sys.path:
                    sys.path.insert(0, candidate)
                return

        parent = os.path.dirname(search_dir)
        if parent == search_dir:
            break
        search_dir = parent


try:
    from playback_control import PlaybackStopController, PlaybackStopped
except ModuleNotFoundError as error:
    if error.name != "playback_control":
        raise
    _add_playback_control_path()
    from playback_control import PlaybackStopController, PlaybackStopped

# Delta Force may capture/warp the cursor to screen edges.
# We use F10 as our own emergency stop.
pdi.FAILSAFE = False


# ============================================================
# 《稻香》 - 三角洲口琴三八度单旋律播放器
#
# 源 MIDI:
#   稻香—周杰伦（适合鬼畜调教）.mid
#
# 这份 MIDI 已检查：
#   - 593 个音
#   - 593 个独立起音时刻
#   - 同时最多 1 个音
#   - 没有重叠旋律音
#   - 4/4
#   - 82 BPM
#   - MIDI 音域 E4 ~ E6
#
# 为完整适配三角洲三八度，将整条旋律下移 1 个八度：
#   E4~E6 -> E3~E5
#
# 这只是“八度摆位”，不是换调：
#   E 还是 E
#   F# 还是 F#
#   C# 还是 C#
#
# 三角洲：
#   左键按住   = 低一个八度
#   中键按住   = 升半音
#   右键按住   = 高一个八度
#
# 基础自然音：
#   C D E F G A B C
#   Z X C V B N M ,
# ============================================================


START_DELAY = 10.0
SPEED = 1.00
STOP_KEY = "f10"

# 只移动八度，不改变调性。
GLOBAL_OCTAVE_SHIFT = -12

# 在音符真正开始前，提前几毫秒设置鼠标 modifier。
MODIFIER_PREROLL = 0.008

DEBUG_NOTES = False


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MIDI_CANDIDATES = [
    os.path.join(BASE_DIR, "稻香—周杰伦（适合鬼畜调教）.mid"),
    os.path.join(BASE_DIR, "daoxiang.mid"),
]


LEFT = "left"
MIDDLE = "middle"
RIGHT = "right"


NATURAL_KEYS = {
    0: "z",    # C
    2: "x",    # D
    4: "c",    # E
    5: "v",    # F
    7: "b",    # G
    9: "n",    # A
    11: "m",   # B
}

CHROMATIC_KEYS = {
    1: "z",    # C# / Db
    3: "x",    # D# / Eb
    6: "v",    # F# / Gb
    8: "b",    # G# / Ab
    10: "n",   # A# / Bb
}

ALL_KEYS = ["z", "x", "c", "v", "b", "n", "m", ","]

stop_controller = PlaybackStopController()

mouse_state = {
    LEFT: False,
    MIDDLE: False,
    RIGHT: False,
}


# ============================================================
# 文件
# ============================================================

def find_midi():
    for path in MIDI_CANDIDATES:
        if os.path.exists(path):
            return path

    raise FileNotFoundError(
        "找不到 MIDI。请把以下任一文件放到脚本同目录：\n"
        "  稻香—周杰伦（适合鬼畜调教）.mid\n"
        "  daoxiang.mid"
    )


# ============================================================
# 安全释放
# ============================================================

def release_everything():
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


def emergency_stop():
    """Request a stop without injecting input from keyboard's hook thread."""
    stop_controller.request_stop()


def set_button(button, wanted):
    current = mouse_state[button]

    if wanted and not current:
        stop_controller.raise_if_requested()
        pdi.mouseDown(button=button)
        mouse_state[button] = True

    elif not wanted and current:
        stop_controller.raise_if_requested()
        pdi.mouseUp(button=button)
        mouse_state[button] = False


def set_modifiers(range_button, chromatic):
    # 永远不会同时按住左右键。
    set_button(LEFT, range_button == LEFT)
    set_button(RIGHT, range_button == RIGHT)
    set_button(MIDDLE, chromatic)


# ============================================================
# MIDI tempo map
# ============================================================

def build_tempo_map(mid):
    tempo_events = []

    for track in mid.tracks:
        tick = 0

        for msg in track:
            tick += msg.time

            if msg.type == "set_tempo":
                tempo_events.append((tick, msg.tempo))

    tempo_events.sort(key=lambda x: x[0])

    current_tempo = 500000  # MIDI 默认 120 BPM
    last_tick = 0
    seconds = 0.0

    result = [(0, 0.0, current_tempo)]

    for tick, new_tempo in tempo_events:
        if tick > last_tick:
            seconds += mido.tick2second(
                tick - last_tick,
                mid.ticks_per_beat,
                current_tempo,
            )

        last_tick = tick
        current_tempo = new_tempo

        if result and result[-1][0] == tick:
            result[-1] = (tick, seconds, current_tempo)
        else:
            result.append((tick, seconds, current_tempo))

    return result


def tick_to_seconds(tick, mid, tempo_map):
    tempo_ticks = [x[0] for x in tempo_map]

    i = bisect_right(tempo_ticks, tick) - 1

    if i < 0:
        i = 0

    base_tick, base_seconds, tempo = tempo_map[i]

    return base_seconds + mido.tick2second(
        tick - base_tick,
        mid.ticks_per_beat,
        tempo,
    )


# ============================================================
# 从 MIDI 读取真正的单旋律 note_on / note_off
# ============================================================

def collect_notes(mid):
    notes = []

    for track_index, track in enumerate(mid.tracks):
        tick = 0
        active = defaultdict(list)

        for msg in track:
            tick += msg.time

            channel = getattr(msg, "channel", None)

            # 跳过 MIDI 鼓通道。
            if channel == 9:
                continue

            if msg.type == "note_on" and msg.velocity > 0:
                active[(channel, msg.note)].append(
                    (tick, msg.velocity)
                )

            elif (
                msg.type == "note_off"
                or (
                    msg.type == "note_on"
                    and msg.velocity == 0
                )
            ):
                key = (channel, msg.note)

                if active[key]:
                    start_tick, velocity = active[key].pop(0)

                    if tick > start_tick:
                        notes.append({
                            "start_tick": start_tick,
                            "end_tick": tick,
                            "pitch": msg.note,
                            "velocity": velocity,
                            "track": track_index,
                            "channel": channel,
                        })

    notes.sort(
        key=lambda n: (
            n["start_tick"],
            n["pitch"]
        )
    )

    return notes


def validate_monophonic(notes):
    if not notes:
        raise RuntimeError("MIDI 里没有可演奏音符。")

    onsets = Counter(
        n["start_tick"]
        for n in notes
    )

    max_same_onset = max(onsets.values())

    overlaps = 0

    for i in range(len(notes) - 1):
        if notes[i]["end_tick"] > notes[i + 1]["start_tick"]:
            overlaps += 1

    if max_same_onset > 1 or overlaps > 0:
        raise RuntimeError(
            "这个文件不是严格单声部："
            f"同时最大音数={max_same_onset}, "
            f"重叠音={overlaps}"
        )

    return max_same_onset, overlaps


# ============================================================
# MIDI tick -> 秒
# ============================================================

def notes_to_seconds(mid, notes):
    tempo_map = build_tempo_map(mid)

    events = []

    for note in notes:
        events.append({
            **note,
            "start_seconds": tick_to_seconds(
                note["start_tick"],
                mid,
                tempo_map,
            ),
            "end_seconds": tick_to_seconds(
                note["end_tick"],
                mid,
                tempo_map,
            ),
        })

    return events


# ============================================================
# 音名
# ============================================================

NOTE_NAMES = [
    "C", "C#", "D", "D#", "E", "F",
    "F#", "G", "G#", "A", "A#", "B"
]


def midi_name(note):
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 1}"


# ============================================================
# 三角洲三八度映射
#
# GLOBAL_OCTAVE_SHIFT = -12 后，本文件范围 E3~E5，
# 全部都可以精确表示，无需夹到最高音。
#
# E3-B3:
#   左键 + 基础键
#
# C4-B4:
#   基础键
#
# C5:
#   ,
#
# C#5-B5:
#   右键 + 基础键（半音再叠加中键）
# ============================================================

def base_key_for_pc(pc):
    if pc in NATURAL_KEYS:
        return NATURAL_KEYS[pc], False

    if pc in CHROMATIC_KEYS:
        return CHROMATIC_KEYS[pc], True

    raise RuntimeError(f"未知 pitch class: {pc}")


def command_for_pitch(source_pitch):
    pitch = source_pitch + GLOBAL_OCTAVE_SHIFT

    # 低八度 C3-B3
    if 48 <= pitch <= 59:
        key, chromatic = base_key_for_pc(pitch % 12)
        return key, LEFT, chromatic, pitch

    # 中八度 C4-B4
    if 60 <= pitch <= 71:
        key, chromatic = base_key_for_pc(pitch % 12)
        return key, None, chromatic, pitch

    # C5 有独立逗号键
    if pitch == 72:
        return ",", None, False, pitch

    # 高八度 C#5-B5
    if 73 <= pitch <= 83:
        key, chromatic = base_key_for_pc(pitch % 12)
        return key, RIGHT, chromatic, pitch

    # C6
    if pitch == 84:
        return ",", RIGHT, False, pitch

    # C#6
    if pitch == 85:
        return ",", RIGHT, True, pitch

    raise RuntimeError(
        f"音域超出三角洲三八度范围："
        f"{midi_name(source_pitch)} -> {midi_name(pitch)}"
    )


# ============================================================
# 高精度等待
# ============================================================

def wait_for(seconds):
    stop_controller.wait_for(seconds)


def wait_until(target):
    stop_controller.wait_until(target)


# ============================================================
# MIDI 信息
# ============================================================

def print_info(mid, notes):
    tempo_events = []
    time_signatures = []

    for track in mid.tracks:
        tick = 0

        for msg in track:
            tick += msg.time

            if msg.type == "set_tempo":
                tempo_events.append(
                    (tick, mido.tempo2bpm(msg.tempo))
                )

            elif msg.type == "time_signature":
                time_signatures.append(
                    (
                        tick,
                        msg.numerator,
                        msg.denominator
                    )
                )

    shifted = [
        n["pitch"] + GLOBAL_OCTAVE_SHIFT
        for n in notes
    ]

    print()
    print("============================================")
    print(" 《稻香》三角洲口琴 - 单旋律 MIDI")
    print("============================================")
    print(f"MIDI ticks/beat: {mid.ticks_per_beat}")
    print(f"旋律音符数: {len(notes)}")
    print(
        f"原 MIDI 音域: "
        f"{midi_name(min(n['pitch'] for n in notes))}"
        f" ~ "
        f"{midi_name(max(n['pitch'] for n in notes))}"
    )
    print(
        f"游戏实际音域: "
        f"{midi_name(min(shifted))}"
        f" ~ "
        f"{midi_name(max(shifted))}"
    )

    if time_signatures:
        print(
            "拍号:",
            ", ".join(
                f"{a}/{b}"
                for _, a, b in time_signatures
            )
        )

    if tempo_events:
        print(
            "速度:",
            ", ".join(
                f"{bpm:.2f} BPM"
                for _, bpm in tempo_events
            )
        )

    print()
    print("音高来源：MIDI 原始音高")
    print("节奏来源：MIDI note_on / note_off")
    print("休止符：MIDI 音符之间的真实空白")
    print("换调：无")
    print("八度摆位：整体 -1 octave，仅为适配三八度")
    print()
    print("左键   = 低一个八度")
    print("中键   = 升半音")
    print("右键   = 高一个八度")
    print("F10    = 紧急停止")
    print("============================================")
    print()


# ============================================================
# 演奏
# ============================================================

def play(events):
    print(
        f"{START_DELAY:.0f} 秒后开始演奏，"
        "请切回三角洲并拿出口琴。"
    )

    stop_controller.reset()
    stop_hook = stop_controller.install_single_key_hook(keyboard, STOP_KEY)
    pdi.PAUSE = 0
    try:
        wait_for(START_DELAY)
        stop_controller.raise_if_requested()
        clock = time.perf_counter()

        for i, event in enumerate(events):
            start = (
                clock
                +
                event["start_seconds"] / SPEED
            )

            end = (
                clock
                +
                event["end_seconds"] / SPEED
            )

            (
                key,
                range_button,
                chromatic,
                played_pitch,
            ) = command_for_pitch(
                event["pitch"]
            )

            # 提前准备“左/中/右”modifier，
            # 但琴键仍严格在 MIDI note_on 时刻按下。
            prep = max(
                clock,
                start - MODIFIER_PREROLL
            )

            wait_until(prep)

            set_modifiers(
                range_button,
                chromatic
            )

            wait_until(start)

            stop_controller.raise_if_requested()
            pdi.keyDown(key)

            if DEBUG_NOTES:
                print(
                    f"{i:4d} "
                    f"{midi_name(event['pitch']):4s}"
                    f" -> "
                    f"{midi_name(played_pitch):4s} "
                    f"key={key:2s} "
                    f"range={str(range_button):5s} "
                    f"half={chromatic}"
                )

            # 直接按照 MIDI note_off 松键。
            wait_until(end)

            stop_controller.raise_if_requested()
            pdi.keyUp(key)

            # 如果后面是真正的休止符，
            # 马上松掉鼠标 modifier。
            if i + 1 < len(events):
                next_start = (
                    clock
                    +
                    events[i + 1]["start_seconds"] / SPEED
                )

                if next_start - end > 0.020:
                    set_modifiers(
                        None,
                        False
                    )

        set_modifiers(
            None,
            False
        )

    except (PlaybackStopped, KeyboardInterrupt):
        print()
        print("已停止演奏。")

    finally:
        release_everything()
        print("所有键盘与鼠标按键已释放。")


# ============================================================
# MAIN
# ============================================================

def main():
    midi_path = find_midi()

    try:
        mid = mido.MidiFile(
            midi_path
        )

        notes = collect_notes(
            mid
        )

        max_same, overlaps = validate_monophonic(
            notes
        )

        print(
            f"单声部检查通过："
            f"同时最大音数={max_same}, "
            f"重叠音={overlaps}"
        )

        print_info(
            mid,
            notes
        )

        events = notes_to_seconds(
            mid,
            notes
        )

        play(
            events
        )

    finally:
        release_everything()

        stop_controller.uninstall_hook(keyboard)


if __name__ == "__main__":
    main()
