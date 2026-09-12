from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "launcher"))

import importer  # noqa: E402
from library import LibraryError, validate_song_id  # noqa: E402


class SongIdTests(unittest.TestCase):
    def test_english_filename_keeps_words_in_a_safe_id(self) -> None:
        self.assertEqual(
            "take_my_breath_away",
            importer.suggested_id_from_filename("Take My Breath Away.mid"),
        )
        self.assertEqual(
            "summer_song",
            importer.suggested_id_from_filename(r"C:\Music Files\Summer Song.MIDI"),
        )
        self.assertEqual("mariage_d_amour", importer.suggested_id("Mariage d'Amour"))
        self.assertEqual("beyonce", importer.suggested_id("Beyoncé"))

    def test_chinese_and_mixed_filename_use_pinyin(self) -> None:
        table = {
            "当": "dang",
            "年": "nian",
            "情": "qing",
            "月": "yue",
            "半": "ban",
            "小": "xiao",
            "夜": "ye",
            "曲": "qu",
        }

        def fake_lazy_pinyin(text: str) -> list[str]:
            return [table[character] for character in text]

        with mock.patch.object(importer, "_lazy_pinyin", side_effect=fake_lazy_pinyin):
            self.assertEqual("dang_nian_qing", importer.suggested_id_from_filename("当年情.MID"))
            self.assertEqual(
                "yue_ban_xiao_ye_qu_take_my_breath_away",
                importer.suggested_id("月半小夜曲 Take My Breath Away"),
            )

    def test_chinese_needs_the_declared_transliteration_component(self) -> None:
        with mock.patch.object(importer, "_lazy_pinyin", None):
            with self.assertRaisesRegex(LibraryError, "pypinyin"):
                importer.suggested_id("当年情")

    def test_punctuation_empty_values_and_length_remain_safe(self) -> None:
        self.assertEqual("a_b", importer.suggested_id("A&B"))
        self.assertEqual("my_song", importer.suggested_id("---"))
        identifier = importer.suggested_id("A" * 100)
        self.assertEqual(64, len(identifier))
        self.assertEqual(identifier, validate_song_id(identifier))

    def test_next_available_id_adds_sequential_suffixes_case_insensitively(self) -> None:
        occupied = {"song", "song1", "SONG2", "other"}
        self.assertEqual("song3", importer.next_available_song_id("song", occupied))
        self.assertEqual("fresh", importer.next_available_song_id("fresh", occupied))

    def test_next_available_id_trims_a_64_character_base_before_suffix(self) -> None:
        base = "a" * 64
        result = importer.next_available_song_id(base, {base, "a" * 63 + "1"})
        self.assertEqual("a" * 63 + "2", result)
        self.assertEqual(result, validate_song_id(result))

    def test_next_available_id_rejects_unsafe_preferred_value(self) -> None:
        with self.assertRaises(LibraryError):
            importer.next_available_song_id("../escape", set())

    def test_current_song_ids_covers_builtins_both_libraries_and_orphaned_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = root / "songs"
            legacy = root / "legacy"
            safe.mkdir()
            legacy.mkdir()
            (safe / "partial_import").mkdir()
            (legacy / "legacy_folder").mkdir()
            builtin = mock.Mock(identifier="builtin_song")
            custom = mock.Mock(identifier="custom_song")
            with (
                mock.patch.object(importer, "SAFE_SONGS_DIR", safe),
                mock.patch.object(importer, "LEGACY_SONGS_DIR", legacy),
                mock.patch.object(importer, "ensure_user_directories"),
                mock.patch.object(importer, "load_builtin_songs", return_value=[builtin]),
                mock.patch.object(importer, "load_user_songs", return_value=[custom]),
            ):
                self.assertEqual(
                    {"builtin_song", "custom_song", "partial_import", "legacy_folder"},
                    importer.current_song_ids(),
                )


if __name__ == "__main__":
    unittest.main()
