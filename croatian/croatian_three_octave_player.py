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

# Delta Force captures/warps the mouse during gameplay.
# Disable PyDirectInput's corner fail-safe; F10 is our own emergency stop.
pdi.FAILSAFE = False


# ============================================================
# Croatian Rhapsody - 3 octave exact-register player
#
# MIDI decides:
#   - exact melody note
#   - exact note length
#   - exact rests
#   - tempo
#   - chromatic pitches
#
# Delta Force controls confirmed/assumed here:
#   default: C4 D4 E4 F4 G4 A4 B4 C5
#            Z  X  C  V  B  N  M  ,
#
#   hold LEFT mouse   = one octave DOWN
#   hold MIDDLE mouse = +1 semitone
#   hold RIGHT mouse  = one octave UP
#
# NO whole-piece transposition.
# ============================================================


# -------------------- user settings --------------------------

START_DELAY = 10.0
SPEED = 1.00
STOP_KEY = "f10"
DEBUG_NOTES = False

# Small lead time used to put mouse modifiers into the correct state
# BEFORE the exact MIDI note-on time.
MODIFIER_PREROLL = 0.008


# -------------------- file -----------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MIDI_CANDIDATES = [
    os.path.join(BASE_DIR, "croatian-rhapsody(1).mid"),
    os.path.join(BASE_DIR, "croatian-rhapsody.mid"),
]


def find_midi():
    for path in MIDI_CANDIDATES:
        if os.path.exists(path):
            return path

    raise FileNotFoundError(
        "Cannot find croatian-rhapsody(1).mid or croatian-rhapsody.mid "
        "in the same folder as this script."
    )


# -------------------- game mapping ---------------------------

# Pitch-class -> base key in the default octave.
# Middle mouse raises the base key by one semitone for chromatic notes.
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

LEFT = "left"
MIDDLE = "middle"
RIGHT = "right"


# -------------------- global state ---------------------------

stop_controller = PlaybackStopController()
mouse_state = {
    LEFT: False,
    MIDDLE: False,
    RIGHT: False,
}


# ============================================================
# Safety / cleanup
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


def set_mouse_button(button, wanted):
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
    # Never hold left and right at the same time.
    set_mouse_button(LEFT, range_button == LEFT)
    set_mouse_button(RIGHT, range_button == RIGHT)
    set_mouse_button(MIDDLE, chromatic)


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

    current_tempo = 500000  # default 120 BPM
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
    tempo_ticks = [item[0] for item in tempo_map]

    index = bisect_right(tempo_ticks, tick) - 1

    if index < 0:
        index = 0

    base_tick, base_seconds, tempo = tempo_map[index]

    return base_seconds + mido.tick2second(
        tick - base_tick,
        mid.ticks_per_beat,
        tempo,
    )


# ============================================================
# Read exact monophonic MIDI notes
# ============================================================

def collect_notes(mid):
    notes = []

    for track_index, track in enumerate(mid.tracks):
        tick = 0
        active = defaultdict(list)

        for msg in track:
            tick += msg.time

            channel = getattr(msg, "channel", None)

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
                            "channel": channel,
                            "track": track_index,
                        })

    notes.sort(key=lambda n: (n["start_tick"], n["pitch"]))

    return notes


def validate_monophonic(notes):
    if not notes:
        raise RuntimeError("MIDI contains no playable notes.")

    simultaneous = Counter(n["start_tick"] for n in notes)
    max_same_onset = max(simultaneous.values())

    overlaps = 0

    for i in range(len(notes) - 1):
        if notes[i]["end_tick"] > notes[i + 1]["start_tick"]:
            overlaps += 1

    if max_same_onset > 1 or overlaps > 0:
        raise RuntimeError(
            "MIDI is not strictly monophonic: "
            f"max simultaneous onset={max_same_onset}, overlaps={overlaps}."
        )


def notes_to_seconds(mid, notes):
    tempo_map = build_tempo_map(mid)

    result = []

    for note in notes:
        start = tick_to_seconds(
            note["start_tick"],
            mid,
            tempo_map,
        )

        end = tick_to_seconds(
            note["end_tick"],
            mid,
            tempo_map,
        )

        result.append({
            **note,
            "start_seconds": start,
            "end_seconds": end,
        })

    return result


# ============================================================
# Musical note names
# ============================================================

NOTE_NAMES = [
    "C", "C#", "D", "D#", "E", "F",
    "F#", "G", "G#", "A", "A#", "B",
]


def midi_name(note):
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 1}"


# ============================================================
# Exact THREE-OCTAVE Delta Force register mapping
#
# We align the game's default octave to C4..C5:
#
#   LEFT held:
#       C3..B3 using Z..M
#       C4 available as LEFT + ',' if needed
#
#   default:
#       C4..B4 using Z..M
#       C5 = ','
#
#   RIGHT held:
#       C5..B5 using Z..M
#       C6 = RIGHT + ','
#
# With MIDDLE:
#       RIGHT + MIDDLE + ',' = C#6
#
# Source MIDI range is G3..E6.
# Therefore:
#   G3..C#6 can be represented at the correct register.
#   D#6 and E6 are the only two source notes above that limit.
#
# For those two rare peaks we CLAMP to C#6 instead of dropping an octave,
# because staying high sounds much less wrong than an octave collapse.
# ============================================================

def base_key_for_pc(pc):
    if pc in NATURAL_KEYS:
        return NATURAL_KEYS[pc], False

    if pc in CHROMATIC_KEYS:
        return CHROMATIC_KEYS[pc], True

    raise RuntimeError(f"Unknown pitch class {pc}")


def command_for_pitch(midi_pitch):
    """
    Returns:
        key
        range_button: None / 'left' / 'right'
        chromatic: bool
        played_virtual_pitch: MIDI number represented by the command
        exact_register: bool
    """

    # -------- low octave: C3..B3 --------
    if 48 <= midi_pitch <= 59:
        pc = midi_pitch % 12
        key, chromatic = base_key_for_pc(pc)

        return key, LEFT, chromatic, midi_pitch, True

    # -------- default octave: C4..B4 --------
    if 60 <= midi_pitch <= 71:
        pc = midi_pitch % 12
        key, chromatic = base_key_for_pc(pc)

        return key, None, chromatic, midi_pitch, True

    # -------- exact C5 --------
    # Use the dedicated high-C key without a modifier.
    if midi_pitch == 72:
        return ",", None, False, 72, True

    # -------- high octave: C5..B5 --------
    if 73 <= midi_pitch <= 83:
        pc = midi_pitch % 12
        key, chromatic = base_key_for_pc(pc)

        return key, RIGHT, chromatic, midi_pitch, True

    # -------- C6 --------
    if midi_pitch == 84:
        return ",", RIGHT, False, 84, True

    # -------- C#6 --------
    if midi_pitch == 85:
        return ",", RIGHT, True, 85, True

    # -------- source exceeds physical top --------
    # This MIDI contains only two such events: D#6 and E6.
    # Preserve the PEAK register by clamping to C#6 instead of
    # folding them down by a full octave.
    if midi_pitch > 85:
        return ",", RIGHT, True, 85, False

    # The supplied MIDI never goes below G3, but keep a safe fallback.
    if midi_pitch < 48:
        pc = midi_pitch % 12
        key, chromatic = base_key_for_pc(pc)
        return key, LEFT, chromatic, 48 + pc, False

    raise RuntimeError(f"Cannot map MIDI pitch {midi_pitch}")


# ============================================================
# Source/register analysis
# ============================================================

def print_register_analysis(notes):
    counts = Counter()

    for note in notes:
        p = note["pitch"]

        if p < 60:
            counts["low (below C4)"] += 1
        elif p < 72:
            counts["default (C4-B4)"] += 1
        elif p < 84:
            counts["high (C5-B5)"] += 1
        else:
            counts["C6 and above"] += 1

    exact = 0
    approximated = []

    for note in notes:
        _, _, _, played_pitch, is_exact = command_for_pitch(
            note["pitch"]
        )

        if is_exact:
            exact += 1
        else:
            approximated.append(
                (note["pitch"], played_pitch, note["start_tick"])
            )

    print()
    print("========== REGISTER ANALYSIS ==========")
    print(
        f"Source range: {midi_name(min(n['pitch'] for n in notes))} "
        f"to {midi_name(max(n['pitch'] for n in notes))}"
    )

    print(
        f"Semitone span: "
        f"{max(n['pitch'] for n in notes) - min(n['pitch'] for n in notes)}"
    )

    print()
    print("Source-note distribution:")

    for name in (
        "low (below C4)",
        "default (C4-B4)",
        "high (C5-B5)",
        "C6 and above",
    ):
        print(f"  {name:18s}: {counts[name]}")

    print()
    print(
        f"Exact register mapping: {exact}/{len(notes)} "
        f"({exact / len(notes) * 100:.2f}%)"
    )

    if approximated:
        print("Approximate peak notes:")

        for source, played, tick in approximated:
            print(
                f"  {midi_name(source)} -> {midi_name(played)} "
                f"at tick {tick}"
            )

    print("=======================================")
    print()


# ============================================================
# Exact absolute-time wait
# ============================================================

def wait_for(seconds):
    stop_controller.wait_for(seconds)


def wait_until(target):
    stop_controller.wait_until(target)


# ============================================================
# Playback
# ============================================================

def play(events):
    total_time = events[-1]["end_seconds"] / SPEED

    print()
    print("==============================================")
    print(" Croatian Rhapsody - THREE OCTAVE PLAYER")
    print("==============================================")
    print("NO whole-piece transposition")
    print("MIDI pitch + MIDI rhythm are used directly")
    print()
    print("LEFT mouse   = one octave DOWN")
    print("MIDDLE mouse = +1 semitone")
    print("RIGHT mouse  = one octave UP")
    print()
    print(f"Notes: {len(events)}")
    print(f"Duration: {total_time:.2f}s ({total_time / 60:.2f} min)")
    print()
    print(f"{START_DELAY:.0f} seconds until playback.")
    print("Switch to Delta Force and open the harmonica.")
    print("F10 = emergency stop + release all mouse buttons.")
    print("==============================================")
    print()

    stop_controller.reset()
    stop_hook = stop_controller.install_single_key_hook(keyboard, STOP_KEY)
    pdi.PAUSE = 0
    try:
        wait_for(START_DELAY)
        stop_controller.raise_if_requested()
        clock = time.perf_counter()

        for index, event in enumerate(events):
            start = clock + event["start_seconds"] / SPEED
            end = clock + event["end_seconds"] / SPEED

            key, range_button, chromatic, played_pitch, exact = (
                command_for_pitch(event["pitch"])
            )

            # Put modifiers into the desired state just BEFORE note-on.
            prep_time = max(
                clock,
                start - MODIFIER_PREROLL,
            )

            wait_until(prep_time)

            set_modifiers(
                range_button=range_button,
                chromatic=chromatic,
            )

            # Exact MIDI attack time.
            wait_until(start)

            stop_controller.raise_if_requested()
            pdi.keyDown(key)

            if DEBUG_NOTES:
                approx_text = "" if exact else "  [PEAK APPROX]"
                print(
                    f"{index:4d} "
                    f"{midi_name(event['pitch']):4s} -> "
                    f"{midi_name(played_pitch):4s}  "
                    f"key={key:2s} "
                    f"range={str(range_button):5s} "
                    f"half={chromatic}"
                    f"{approx_text}"
                )

            # Exact MIDI note-off time.
            wait_until(end)
            stop_controller.raise_if_requested()
            pdi.keyUp(key)

            # During a real rest, release mouse modifiers immediately
            # so the game does not keep left/right/middle held.
            if index + 1 < len(events):
                next_start = (
                    clock
                    + events[index + 1]["start_seconds"] / SPEED
                )

                if next_start - end > 0.020:
                    set_modifiers(None, False)

        # End of piece.
        set_modifiers(None, False)

    except (PlaybackStopped, KeyboardInterrupt):
        print()
        print("Playback stopped.")

    finally:
        release_everything()
        print("All keyboard and mouse inputs released.")


# ============================================================
# Main
# ============================================================

def main():
    midi_path = find_midi()

    try:
        print("Using MIDI:")
        print(midi_path)

        mid = mido.MidiFile(midi_path)
        notes = collect_notes(mid)

        validate_monophonic(notes)

        print(f"Monophonic source: PASS ({len(notes)} notes)")
        print_register_analysis(notes)

        events = notes_to_seconds(mid, notes)

        play(events)

    finally:
        release_everything()

        stop_controller.uninstall_hook(keyboard)


if __name__ == "__main__":
    main()
