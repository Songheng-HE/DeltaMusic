from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "launcher"))

import library  # noqa: E402


class LibraryTests(unittest.TestCase):
    def _personal_library(self, root: Path):
        safe = root / "songs"
        legacy = root / "legacy"
        return mock.patch.multiple(
            library,
            USER_LIBRARY=root,
            SAFE_SONGS_DIR=safe,
            LEGACY_SONGS_DIR=legacy,
        )

    @staticmethod
    def _write_safe_song(root: Path, identifier: str = "my_song") -> Path:
        song_root = root / "songs" / identifier
        song_root.mkdir(parents=True)
        (song_root / "melody.mid").write_bytes(b"MThd")
        manifest = song_root / "song.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": identifier,
                    "title": "Original title",
                    "type": "generic_midi",
                    "midi": "melody.mid",
                    "future_extension": {"keep": True},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return manifest

    def test_existing_builtin_catalog_is_complete(self) -> None:
        songs = library.load_builtin_songs()
        self.assertEqual(4, len(songs))
        self.assertTrue(all(song.available for song in songs), [(song.title, song.issue) for song in songs])

    def test_song_id_rejects_unsafe_values(self) -> None:
        for invalid in ("", "Capital", "has space", "../escape", "a" * 65):
            with self.assertRaises(library.LibraryError):
                library.validate_song_id(invalid)

    def test_manifest_cannot_escape_its_song_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "melody.mid").write_bytes(b"MThd")
            manifest = root / "song.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "safe_song",
                        "title": "Safe song",
                        "type": "generic_midi",
                        "midi": "../melody.mid",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(library.LibraryError):
                library.validate_custom_manifest(manifest)

    def test_valid_generic_manifest_normalizes_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "melody.mid").write_bytes(b"MThd")
            manifest = root / "song.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "my_song",
                        "title": "My song",
                        "type": "generic_midi",
                        "midi": "melody.mid",
                    }
                ),
                encoding="utf-8",
            )
            parsed = library.validate_custom_manifest(manifest)
            self.assertEqual("generic_midi", parsed["type"])
            self.assertEqual(10.0, parsed["playback"]["start_delay_seconds"])
            self.assertEqual(0, parsed["playback"]["octave_shift"])

    def test_rename_personal_song_changes_only_display_title(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "user_library"
            manifest = self._write_safe_song(root)
            with self._personal_library(root):
                returned = library.rename_custom_song(manifest, "  新曲名  ")

            self.assertEqual(manifest, returned)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual("新曲名", payload["title"])
            self.assertEqual("my_song", payload["id"])
            self.assertEqual("melody.mid", payload["midi"])
            self.assertEqual({"keep": True}, payload["future_extension"])
            self.assertTrue((manifest.parent / "melody.mid").is_file())

    def test_rename_rejects_non_personal_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "user_library"
            outside = Path(temporary) / "outside" / "song.json"
            outside.parent.mkdir(parents=True)
            outside.write_text("{}", encoding="utf-8")
            with self._personal_library(root):
                with self.assertRaises(library.LibraryError):
                    library.rename_custom_song(outside, "Should not change")
            self.assertEqual("{}", outside.read_text(encoding="utf-8"))

    def test_delete_personal_song_removes_only_its_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "user_library"
            manifest = self._write_safe_song(root)
            sibling = self._write_safe_song(root, "other_song")
            with self._personal_library(root):
                library.delete_custom_song(manifest)

            self.assertFalse(manifest.parent.exists())
            self.assertTrue(sibling.is_file())

    def test_delete_can_remove_invalid_personal_song_but_not_nested_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "user_library"
            broken_root = root / "songs" / "broken"
            broken_root.mkdir(parents=True)
            broken_manifest = broken_root / "song.json"
            broken_manifest.write_text("not json", encoding="utf-8")
            nested = root / "songs" / "nested" / "subdir" / "song.json"
            nested.parent.mkdir(parents=True)
            nested.write_text("{}", encoding="utf-8")
            with self._personal_library(root):
                library.delete_custom_song(broken_manifest)
                with self.assertRaises(library.LibraryError):
                    library.delete_custom_song(nested)

            self.assertFalse(broken_root.exists())
            self.assertTrue(nested.is_file())


if __name__ == "__main__":
    unittest.main()
