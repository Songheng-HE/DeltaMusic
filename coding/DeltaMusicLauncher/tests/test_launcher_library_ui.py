from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "launcher"))

import DeltaMusicLauncher as gui  # noqa: E402
from library import Song  # noqa: E402


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class _Tree:
    def __init__(self, row: str) -> None:
        self.row = row
        self.selected: list[str] = []
        self.focused = ""

    def identify_row(self, _y: int) -> str:
        return self.row

    def selection_set(self, item: str) -> None:
        self.selected.append(item)

    def focus(self, item: str) -> None:
        self.focused = item

    def see(self, _item: str) -> None:
        pass


class _Menu:
    def __init__(self) -> None:
        self.states: list[tuple[int, str]] = []
        self.popup: tuple[int, int] | None = None
        self.released = False

    def entryconfigure(self, index: int, *, state: str) -> None:
        self.states.append((index, state))

    def tk_popup(self, x: int, y: int) -> None:
        self.popup = (x, y)

    def grab_release(self) -> None:
        self.released = True


class _Event:
    y = 4
    x_root = 100
    y_root = 200


class LauncherLibraryUiTests(unittest.TestCase):
    def _bare_app(self) -> gui.LauncherApp:
        app = gui.LauncherApp.__new__(gui.LauncherApp)
        app.root = object()
        app.status_var = _Value()
        app.refresh_library = mock.Mock()
        app._write_log = mock.Mock()
        return app

    def test_automatic_safe_import_uses_filename_ids_and_sequential_suffixes(self) -> None:
        app = self._bare_app()
        calls: list[tuple[Path, str, str, int]] = []

        def fake_import(source: Path, title: str, identifier: str, octave_shift: int = 0) -> Path:
            calls.append((source, title, identifier, octave_shift))
            return Path("song.json")

        with (
            mock.patch.object(gui, "current_song_ids", return_value={"song"}),
            mock.patch.object(gui, "import_midi", side_effect=fake_import),
            mock.patch.object(gui.messagebox, "showinfo") as showinfo,
        ):
            app.import_dropped_midi(
                (Path(r"C:\music\Song.mid"), Path(r"C:\music\Song.midi"), Path(r"C:\music\Song.mid"))
            )

        self.assertEqual(
            [
                (Path(r"C:\music\Song.mid"), "Song", "song1", 0),
                (Path(r"C:\music\Song.midi"), "Song", "song2", 0),
            ],
            calls,
        )
        app.refresh_library.assert_called_once_with()
        showinfo.assert_called_once()

    def test_right_click_disables_mutation_for_builtins_and_enables_it_for_personal_song(self) -> None:
        app = self._bare_app()
        builtin = Song(
            identifier="built_in",
            title="内置",
            source="内置",
            kind="legacy_python",
            root=Path("built"),
            description="",
            available=True,
        )
        custom = Song(
            identifier="personal",
            title="个人",
            source="个人 MIDI",
            kind="generic_midi",
            root=Path("personal"),
            description="",
            available=True,
            manifest_path=Path("personal/song.json"),
        )
        app.tree = _Tree("built")
        app.songs = {"built": builtin, "personal": custom}
        app._song_menu = _Menu()
        app._show_selected_detail = mock.Mock()

        self.assertEqual("break", app._show_song_context_menu(_Event()))
        self.assertEqual([(0, "disabled"), (1, "disabled")], app._song_menu.states)

        app.tree.row = "personal"
        app._song_menu.states.clear()
        self.assertEqual("break", app._show_song_context_menu(_Event()))
        self.assertEqual([(0, "normal"), (1, "normal")], app._song_menu.states)
        self.assertEqual((100, 200), app._song_menu.popup)
        self.assertTrue(app._song_menu.released)


if __name__ == "__main__":
    unittest.main()
