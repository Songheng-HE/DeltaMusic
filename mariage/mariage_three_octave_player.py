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

# MusicBoxManiacs source is written very high.
# -12 means "one octave down" only; this does NOT change the key.
GLOBAL_OCTAVE_SHIFT = -12

# Delta Force:
# LEFT   = one octave down
# MIDDLE = +1 semitone
# RIGHT  = one octave up
LEFT = "left"
MIDDLE = "middle"
RIGHT = "right"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MIDI_PATH = os.path.join(BASE_DIR, "mariage_damour_melody_only.mid")

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

    tempo_events.sort()
    tempo = 500000
    last_tick = 0
    seconds = 0.0
    result = [(0, 0.0, tempo)]

    for tick, new_tempo in tempo_events:
        if tick > last_tick:
            seconds += mido.tick2second(
                tick - last_tick, mid.ticks_per_beat, tempo
            )
        last_tick = tick
        tempo = new_tempo
        if result and result[-1][0] == tick:
            result[-1] = (tick, seconds, tempo)
        else:
            result.append((tick, seconds, tempo))

    return result


def tick_to_seconds(tick, mid, tempo_map):
    ticks = [x[0] for x in tempo_map]
    i = bisect_right(ticks, tick) - 1
    i = max(i, 0)
    base_tick, base_sec, tempo = tempo_map[i]
    return base_sec + mido.tick2second(
        tick - base_tick, mid.ticks_per_beat, tempo
    )


def collect_notes(mid):
    result = []

    for track in mid.tracks:
        tick = 0
        active = defaultdict(list)

        for msg in track:
            tick += msg.time
            ch = getattr(msg, "channel", 0)

            if msg.type == "note_on" and msg.velocity > 0:
                active[(ch, msg.note)].append((tick, msg.velocity))

            elif msg.type == "note_off" or (
                msg.type == "note_on" and msg.velocity == 0
            ):
                key = (ch, msg.note)
                if active[key]:
                    st, vel = active[key].pop(0)
                    if tick > st:
                        result.append({
                            "start_tick": st,
                            "end_tick": tick,
                            "pitch": msg.note,
                        })

    result.sort(key=lambda n: n["start_tick"])
    return result


def base_key_for_pc(pc):
    if pc in NATURAL_KEYS:
        return NATURAL_KEYS[pc], False
    return CHROMATIC_KEYS[pc], True


def command_for_pitch(source_pitch):
    # Global OCTAVE placement only. Pitch class is unchanged.
    p = source_pitch + GLOBAL_OCTAVE_SHIFT

    # Delta three-octave practical layout:
    # C3-B3 -> LEFT + Z..M
    # C4-B4 -> Z..M
    # C5-B5 -> RIGHT + Z..M
    # C5 also has dedicated ","; C6 = RIGHT + ","
    if 48 <= p <= 59:
        key, half = base_key_for_pc(p % 12)
        return key, LEFT, half, p, True

    if 60 <= p <= 71:
        key, half = base_key_for_pc(p % 12)
        return key, None, half, p, True

    if p == 72:
        return ",", None, False, p, True

    if 73 <= p <= 83:
        key, half = base_key_for_pc(p % 12)
        return key, RIGHT, half, p, True

    if p == 84:
        return ",", RIGHT, False, p, True

    if p == 85:
        return ",", RIGHT, True, p, True

    # Only the very highest extracted notes can exceed C#6.
    # Keep them in the high register instead of dropping one octave.
    if p > 85:
        return ",", RIGHT, True, 85, False

    # Safe low fallback; should not occur with this file.
    if p < 48:
        key, half = base_key_for_pc(p % 12)
        return key, LEFT, half, 48 + (p % 12), False

    raise RuntimeError(f"Cannot map MIDI pitch {source_pitch}")


def wait_for(seconds):
    stop_controller.wait_for(seconds)


def wait_until(target):
    stop_controller.wait_until(target)


def main():
    if not os.path.exists(MIDI_PATH):
        print("Missing:", MIDI_PATH)
        return

    try:
        mid = mido.MidiFile(MIDI_PATH)
        notes = collect_notes(mid)

        onset_counts = Counter(n["start_tick"] for n in notes)
        max_simultaneous = max(onset_counts.values()) if onset_counts else 0

        print("Mariage d'Amour - melody-only player")
        print("-----------------------------------")
        print("Notes:", len(notes))
        print("Max notes at same onset:", max_simultaneous)
        print("Tempo/rhythm: from MIDI")
        print("Key transposition: NONE")
        print("Octave placement:", GLOBAL_OCTAVE_SHIFT, "semitones")
        print("LEFT = -1 octave, MIDDLE = +1 semitone, RIGHT = +1 octave")
        print()
        print(f"{START_DELAY:.0f}s until playback. F10 = emergency stop.")

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

        stop_controller.reset()
        stop_hook = stop_controller.install_single_key_hook(keyboard, STOP_KEY)
        pdi.PAUSE = 0
        wait_for(START_DELAY)
        stop_controller.raise_if_requested()
        clock = time.perf_counter()

        for i, ev in enumerate(events):
            start = clock + ev["start_seconds"] / SPEED
            end = clock + ev["end_seconds"] / SPEED

            key, range_button, half, played_pitch, exact = command_for_pitch(
                ev["pitch"]
            )

            # Set octave/semitone modifiers slightly before the musical attack.
            wait_until(max(clock, start - 0.008))
            set_modifiers(range_button, half)

            wait_until(start)
            stop_controller.raise_if_requested()
            pdi.keyDown(key)

            wait_until(end)
            stop_controller.raise_if_requested()
            pdi.keyUp(key)

            # During genuine rests, release mouse modifiers.
            if i + 1 < len(events):
                next_start = clock + events[i + 1]["start_seconds"] / SPEED
                if next_start - end > 0.020:
                    set_modifiers(None, False)

        set_modifiers(None, False)

    except (PlaybackStopped, KeyboardInterrupt):
        print("Stopped.")

    finally:
        release_everything()
        stop_controller.uninstall_hook(keyboard)
        print("All keyboard and mouse inputs released.")


if __name__ == "__main__":
    main()
