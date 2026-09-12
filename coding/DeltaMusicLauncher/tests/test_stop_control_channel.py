from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "launcher"))

import DeltaMusicLauncher as gui  # noqa: E402
import player_host  # noqa: E402
from library import LibraryError  # noqa: E402
from playback_control import STOP_FILE_ENV, STOP_TOKEN_ENV  # noqa: E402


class StopControlChannelTests(unittest.TestCase):
    @staticmethod
    def _payload(token: str, command: str = "ready") -> str:
        return json.dumps(
            {
                "version": player_host.CONTROL_PROTOCOL_VERSION,
                "token": token,
                "command": command,
            },
            ensure_ascii=False,
        )

    def test_host_validates_channel_exposes_it_only_during_playback_and_cleans_owned_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control_dir = Path(temporary) / "controls"
            control_dir.mkdir()
            token = "A" * 32
            path = control_dir / f"playback-{'a' * 32}.json"
            path.write_text(self._payload(token), encoding="utf-8")
            original_file_env = os.environ.get(STOP_FILE_ENV)
            original_token_env = os.environ.get(STOP_TOKEN_ENV)

            with patch.object(player_host, "CONTROL_DIR", control_dir):
                control = player_host._validate_stop_control(path, token)
                with player_host._stop_control_environment(control):
                    self.assertEqual(str(path.resolve()), os.environ[STOP_FILE_ENV])
                    self.assertEqual(token, os.environ[STOP_TOKEN_ENV])
                    self.assertTrue(path.is_file())

            self.assertFalse(path.exists(), "the host may only clean up its own completed control file")
            self.assertEqual(original_file_env, os.environ.get(STOP_FILE_ENV))
            self.assertEqual(original_token_env, os.environ.get(STOP_TOKEN_ENV))

    def test_host_rejects_wrong_token_and_paths_outside_the_control_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control_dir = root / "controls"
            control_dir.mkdir()
            token = "B" * 32
            valid_path = control_dir / f"playback-{'b' * 32}.json"
            valid_path.write_text(self._payload(token), encoding="utf-8")
            outside_path = root / f"playback-{'c' * 32}.json"
            outside_path.write_text(self._payload(token), encoding="utf-8")

            with patch.object(player_host, "CONTROL_DIR", control_dir):
                with self.assertRaises(LibraryError):
                    player_host._validate_stop_control(valid_path, "C" * 32)
                with self.assertRaises(LibraryError):
                    player_host._validate_stop_control(outside_path, token)

    def test_host_never_deletes_a_replaced_control_file_with_another_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control_dir = Path(temporary) / "controls"
            control_dir.mkdir()
            owned_token = "D" * 32
            replacement_token = "E" * 32
            path = control_dir / f"playback-{'d' * 32}.json"
            path.write_text(self._payload(replacement_token, "stop"), encoding="utf-8")
            control = player_host.StopControl(path=path, token=owned_token)

            player_host._remove_stop_control_if_owned(control)

            self.assertTrue(path.exists())
            self.assertEqual(replacement_token, json.loads(path.read_text(encoding="utf-8"))["token"])

    def test_gui_creates_a_fresh_channel_and_atomically_requests_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control_dir = Path(temporary) / "controls"

            def create_only_test_directories() -> None:
                control_dir.mkdir(parents=True, exist_ok=True)

            with (
                patch.object(gui, "CONTROL_DIR", control_dir),
                patch.object(gui, "ensure_user_directories", create_only_test_directories),
            ):
                control = gui._create_playback_control()
                ready = json.loads(control.path.read_text(encoding="utf-8"))
                self.assertEqual(gui.CONTROL_PROTOCOL_VERSION, ready["version"])
                self.assertEqual(control.token, ready["token"])
                self.assertEqual("ready", ready["command"])

                self.assertTrue(gui._request_stop(control))
                stopped = json.loads(control.path.read_text(encoding="utf-8"))
                self.assertEqual(control.token, stopped["token"])
                self.assertEqual("stop", stopped["command"])

                gui._discard_control_if_owned(control)
                self.assertFalse(control.path.exists())


if __name__ == "__main__":
    unittest.main()
