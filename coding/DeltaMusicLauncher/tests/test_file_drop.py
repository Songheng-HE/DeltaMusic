from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "launcher"))

import file_drop  # noqa: E402


class _FakeTcl:
    def __init__(self, values: tuple[str, ...]) -> None:
        self._values = values
        self.seen: str | None = None

    def splitlist(self, value: str) -> tuple[str, ...]:
        self.seen = value
        return self._values


class _FakeRoot:
    def __init__(self, values: tuple[str, ...]) -> None:
        self.tk = _FakeTcl(values)


class _FakeWidget:
    def __init__(self) -> None:
        self.registered: object | None = None
        self.bindings: dict[str, object] = {}

    def drop_target_register(self, value: object) -> None:
        self.registered = value

    def dnd_bind(self, event: str, callback: object) -> None:
        self.bindings[event] = callback


class _RegistrationFailWidget(_FakeWidget):
    def drop_target_register(self, value: object) -> None:
        raise file_drop.TclError("tkdnd registration failed")


class _Event:
    def __init__(self, data: str, action: str = "copy") -> None:
        self.data = data
        self.action = action


class _FallbackTkModule:
    class TclError(Exception):
        pass

    def Tk(self) -> str:
        return "plain-tk-root"


class _BrokenTkinterDnd:
    @staticmethod
    def Tk() -> object:
        raise RuntimeError("tkdnd DLL is unavailable")


class FileDropTests(unittest.TestCase):
    def test_split_drop_paths_uses_tcl_list_parser_for_multiple_paths(self) -> None:
        root = _FakeRoot((r"C:\Music Files\中文 曲目.mid", r"D:\two.midi"))
        result = file_drop.split_drop_paths(root, "{C:/Music Files/中文 曲目.mid} D:/two.midi")
        self.assertEqual((Path(r"C:\Music Files\中文 曲目.mid"), Path(r"D:\two.midi")), result)
        self.assertEqual("{C:/Music Files/中文 曲目.mid} D:/two.midi", root.tk.seen)

    def test_midi_paths_keeps_only_existing_midi_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            good_mid = folder / "song.mid"
            good_midi = folder / "song.midi"
            bad = folder / "not-midi.txt"
            for path in (good_mid, good_midi, bad):
                path.write_bytes(b"x")
            self.assertEqual((good_mid, good_midi), file_drop.midi_paths((good_mid, bad, good_midi)))

    def test_enable_registers_native_dnd_and_calls_callback_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            song = Path(temporary) / "拖入.mid"
            song.write_bytes(b"x")
            root = _FakeRoot((str(song),))
            widget = _FakeWidget()
            received: list[tuple[Path, ...]] = []
            old_type = file_drop.DND_FILES
            file_drop.DND_FILES = "DND_Files"
            try:
                self.assertTrue(file_drop.enable_midi_file_drop(widget, root, received.append))
                self.assertEqual("DND_Files", widget.registered)
                callback = widget.bindings[file_drop.DROP_EVENT]
                self.assertEqual("copy", callback(_Event("{" + str(song) + "}")))
            finally:
                file_drop.DND_FILES = old_type
            self.assertEqual([(song,)], received)

    def test_enable_falls_back_when_widget_registration_fails(self) -> None:
        root = _FakeRoot(())
        old_type = file_drop.DND_FILES
        file_drop.DND_FILES = "DND_Files"
        try:
            self.assertFalse(
                file_drop.enable_midi_file_drop(
                    _RegistrationFailWidget(), root, lambda files: None
                )
            )
        finally:
            file_drop.DND_FILES = old_type

    def test_root_creation_falls_back_when_tkdnd_binary_cannot_load(self) -> None:
        with mock.patch.object(file_drop, "TkinterDnD", _BrokenTkinterDnd):
            root, native_enabled = file_drop.create_application_root(_FallbackTkModule())
        self.assertEqual("plain-tk-root", root)
        self.assertFalse(native_enabled)


if __name__ == "__main__":
    unittest.main()
