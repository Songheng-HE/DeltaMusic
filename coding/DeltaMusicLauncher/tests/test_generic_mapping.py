from __future__ import annotations

from pathlib import Path
import sys
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "launcher"))

from generic_midi_player import command_for_pitch  # noqa: E402


class GenericMappingTests(unittest.TestCase):
    def test_three_octave_landmarks(self) -> None:
        self.assertEqual(("z", "left", False), command_for_pitch(48, 0)[:3])
        self.assertEqual(("z", None, False), command_for_pitch(60, 0)[:3])
        self.assertEqual((",", None, False), command_for_pitch(72, 0)[:3])
        self.assertEqual((",", "right", False), command_for_pitch(84, 0)[:3])
        self.assertEqual((",", "right", True), command_for_pitch(85, 0)[:3])

    def test_octave_shift_and_out_of_range_fold_preserve_pitch_class(self) -> None:
        _key, _range_button, _chromatic, played_pitch, exact = command_for_pitch(36, 0)
        self.assertEqual(36 % 12, played_pitch % 12)
        self.assertFalse(exact)
        _key, _range_button, _chromatic, played_pitch, _exact = command_for_pitch(60, 12)
        self.assertEqual(72, played_pitch)


if __name__ == "__main__":
    unittest.main()

