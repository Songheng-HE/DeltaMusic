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

pdi.FAILSAFE = False

START_DELAY = 10.0
SPEED = 1.00
STOP_KEY = "f10"
MODIFIER_PREROLL = 0.008

SCHEME = "B"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

FILES = {
    "1": "autumn_I_B_complete_melody.mid",
    "2": "autumn_II_B_complete_melody.mid",
    "3": "autumn_III_B_complete_melody.mid",
}

LEFT = "left"
MIDDLE = "middle"
RIGHT = "right"

NATURAL_KEYS = {
    0: "z", 2: "x", 4: "c", 5: "v",
    7: "b", 9: "n", 11: "m",
}

CHROMATIC_KEYS = {
    1: "z", 3: "x", 6: "v", 8: "b", 10: "n",
}

ALL_KEYS = ["z", "x", "c", "v", "b", "n", "m", ","]

stop_controller = PlaybackStopController()
mouse_state = {LEFT: False, MIDDLE: False, RIGHT: False}


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
    set_button(LEFT, range_button == LEFT)
    set_button(RIGHT, range_button == RIGHT)
    set_button(MIDDLE, chromatic)


def build_tempo_map(mid):
    tempo_events = []

    for track in mid.tracks:
        tick = 0
        for msg in track:
            tick += msg.time
            if msg.type == "set_tempo":
                tempo_events.append((tick, msg.tempo))

    tempo_events.sort(key=lambda x: x[0])

    current_tempo = 500000
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
    ticks = [x[0] for x in tempo_map]
    i = bisect_right(ticks, tick) - 1
    i = max(i, 0)

    base_tick, base_seconds, tempo = tempo_map[i]

    return base_seconds + mido.tick2second(
        tick - base_tick,
        mid.ticks_per_beat,
        tempo,
    )


def collect_notes(mid):
    notes = []

    for track in mid.tracks:
        tick = 0
        active = defaultdict(list)

        for msg in track:
            tick += msg.time
            channel = getattr(msg, "channel", 0)

            if msg.type == "note_on" and msg.velocity > 0:
                active[(channel, msg.note)].append((tick, msg.velocity))

            elif (
                msg.type == "note_off"
                or (msg.type == "note_on" and msg.velocity == 0)
            ):
                key = (channel, msg.note)

                if active[key]:
                    st, vel = active[key].pop(0)

                    if tick > st:
                        notes.append({
                            "start_tick": st,
                            "end_tick": tick,
                            "pitch": msg.note,
                        })

    notes.sort(key=lambda n: n["start_tick"])
    return notes


def validate_monophonic(notes):
    onsets = Counter(n["start_tick"] for n in notes)

    max_sim = max(onsets.values()) if onsets else 0
    overlaps = 0

    for a, b in zip(notes, notes[1:]):
        if a["end_tick"] > b["start_tick"]:
            overlaps += 1

    if max_sim > 1 or overlaps > 0:
        raise RuntimeError(
            f"Not monophonic: max_sim={max_sim}, overlaps={overlaps}"
        )


NOTE_NAMES = [
    "C", "C#", "D", "D#", "E", "F",
    "F#", "G", "G#", "A", "A#", "B",
]


def midi_name(note):
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 1}"


def base_key_for_pc(pc):
    if pc in NATURAL_KEYS:
        return NATURAL_KEYS[pc], False
    return CHROMATIC_KEYS[pc], True


def command_for_pitch(source_pitch, movement):
    """
    Three-octave placement.

    Movements I & III span G3..F6. To avoid the important high passages
    collapsing downward, we place the whole movement one octave lower.
    Only rare original notes G3..B3 then fall below the practical window;
    those are folded UP one octave. This preserves the high climaxes.

    Movement II already fits comfortably and is left at its MIDI octave.
    """

    if movement in ("1", "3"):
        p = source_pitch - 12

        # Rare bottom-edge notes after the global octave shift.
        # Fold upward by one octave rather than destroying the high register.
        while p < 48:
            p += 12
    else:
        p = source_pitch

    # C3-B3 = left octave
    if 48 <= p <= 59:
        key, chromatic = base_key_for_pc(p % 12)
        return key, LEFT, chromatic, p

    # C4-B4 = default octave
    if 60 <= p <= 71:
        key, chromatic = base_key_for_pc(p % 12)
        return key, None, chromatic, p

    # Dedicated high C
    if p == 72:
        return ",", None, False, p

    # C#5-B5 = right octave
    if 73 <= p <= 83:
        key, chromatic = base_key_for_pc(p % 12)
        return key, RIGHT, chromatic, p

    # C6
    if p == 84:
        return ",", RIGHT, False, p

    # C#6
    if p == 85:
        return ",", RIGHT, True, p

    # Defensive upper fallback. In the supplied Autumn files,
    # the I/III octave placement should keep important highs below here.
    if p > 85:
        pc = p % 12
        key, chromatic = base_key_for_pc(pc)
        return key, RIGHT, chromatic, p - 12

    raise RuntimeError(
        f"Pitch outside practical range: {midi_name(source_pitch)}"
    )


def wait_for(seconds):
    stop_controller.wait_for(seconds)


def wait_until(target):
    stop_controller.wait_until(target)


def play(movement):
    midi_name_file = FILES[movement]
    midi_path = os.path.join(BASE_DIR, midi_name_file)

    if not os.path.exists(midi_path):
        raise FileNotFoundError(
            f"Missing {midi_name_file} in {BASE_DIR}"
        )

    mid = mido.MidiFile(midi_path)
    notes = collect_notes(mid)
    validate_monophonic(notes)

    tempo_map = build_tempo_map(mid)

    events = []
    for n in notes:
        events.append({
            **n,
            "start_seconds": tick_to_seconds(
                n["start_tick"], mid, tempo_map
            ),
            "end_seconds": tick_to_seconds(
                n["end_tick"], mid, tempo_map
            ),
        })

    print()
    print("==============================================")
    print(f"Vivaldi Autumn - Scheme {SCHEME} - Movement {movement}")
    print("==============================================")
    print(f"Notes: {len(notes)}")
    print(
        "MIDI range:",
        midi_name(min(n["pitch"] for n in notes)),
        "~",
        midi_name(max(n["pitch"] for n in notes)),
    )
    print("LEFT = -1 octave")
    print("MIDDLE = +1 semitone")
    print("RIGHT = +1 octave")
    print("F10 = emergency stop")
    print()
    print(f"{START_DELAY:.0f} seconds until playback.")
    print("Switch to Delta Force and open the harmonica.")
    print("==============================================")
    print()

    stop_controller.reset()
    stop_hook = stop_controller.install_single_key_hook(keyboard, STOP_KEY)
    pdi.PAUSE = 0
    try:
        wait_for(START_DELAY)
        stop_controller.raise_if_requested()
        clock = time.perf_counter()

        for i, event in enumerate(events):
            start = clock + event["start_seconds"] / SPEED
            end = clock + event["end_seconds"] / SPEED

            key, range_button, chromatic, played_pitch = command_for_pitch(
                event["pitch"], movement
            )

            wait_until(max(clock, start - MODIFIER_PREROLL))
            set_modifiers(range_button, chromatic)

            wait_until(start)
            stop_controller.raise_if_requested()
            pdi.keyDown(key)

            wait_until(end)
            stop_controller.raise_if_requested()
            pdi.keyUp(key)

            # Release mouse modifiers during genuine rests.
            if i + 1 < len(events):
                next_start = (
                    clock + events[i + 1]["start_seconds"] / SPEED
                )

                if next_start - end > 0.020:
                    set_modifiers(None, False)

        set_modifiers(None, False)

    except (PlaybackStopped, KeyboardInterrupt):
        print("Playback stopped.")

    finally:
        release_everything()
        print("All keyboard and mouse inputs released.")


def main():
    print(f"Vivaldi Autumn - Scheme {SCHEME}")
    print("1 = Movement I  Allegro")
    print("2 = Movement II Adagio molto")
    print("3 = Movement III Allegro")
    movement = input("Choose movement [1/2/3]: ").strip()

    if movement not in FILES:
        print("Invalid choice.")
        return

    try:
        play(movement)

    finally:
        release_everything()

        stop_controller.uninstall_hook(keyboard)


if __name__ == "__main__":
    main()
