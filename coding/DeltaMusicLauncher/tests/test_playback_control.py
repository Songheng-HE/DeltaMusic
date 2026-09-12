from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = SOURCE_ROOT.parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "launcher"))

from playback_control import PlaybackStopController, PlaybackStopped  # noqa: E402


class FakeKeyboard:
    """A recording stand-in: tests must never install a real global keyboard hook."""

    def __init__(self) -> None:
        self.press_hooks: list[tuple[str, object, bool, object]] = []
        self.unhooked: list[object] = []

    def on_press_key(self, key: str, callback: object, suppress: bool = False) -> object:
        hook = object()
        self.press_hooks.append((key, callback, suppress, hook))
        return hook

    def unhook(self, hook: object) -> None:
        self.unhooked.append(hook)


class FakePyDirectInput(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("pydirectinput")
        self.FAILSAFE = True
        self.PAUSE = 0.1
        self.operations: list[tuple[str, object]] = []

    def keyDown(self, key: object) -> None:
        self.operations.append(("keyDown", key))

    def keyUp(self, key: object) -> None:
        self.operations.append(("keyUp", key))

    def mouseDown(self, button: object) -> None:
        self.operations.append(("mouseDown", button))

    def mouseUp(self, button: object) -> None:
        self.operations.append(("mouseUp", button))


class PlaybackControlTests(unittest.TestCase):
    def _stop_waiter(self, controller: PlaybackStopController) -> tuple[threading.Thread, list[object]]:
        result: list[object] = []

        def wait() -> None:
            try:
                controller.wait_for(5.0)
            except PlaybackStopped:
                result.append("stopped")
            else:
                result.append("timed_out")

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        return thread, result

    def test_f10_uses_a_single_suppressed_key_hook_and_only_requests_stop(self) -> None:
        controller = PlaybackStopController()
        keyboard = FakeKeyboard()

        hook = controller.install_single_key_hook(keyboard)

        self.assertEqual(1, len(keyboard.press_hooks))
        key, callback, suppress, returned_hook = keyboard.press_hooks[0]
        self.assertEqual("f10", key)
        self.assertTrue(suppress)
        self.assertIs(hook, returned_hook)
        self.assertFalse(controller.is_stop_requested())

        with patch.object(controller, "request_stop", wraps=controller.request_stop) as request_stop:
            callback(object())

        request_stop.assert_called_once_with()
        self.assertTrue(controller.is_stop_requested())
        controller.uninstall_hook(keyboard)
        self.assertEqual([hook], keyboard.unhooked)

    def test_physical_f10_wakes_a_long_wait_promptly(self) -> None:
        controller = PlaybackStopController()
        keyboard = FakeKeyboard()
        controller.install_single_key_hook(keyboard)
        callback = keyboard.press_hooks[0][1]
        thread, result = self._stop_waiter(controller)

        time.sleep(0.02)
        started = time.monotonic()
        callback(object())
        thread.join(0.35)

        self.assertFalse(thread.is_alive(), "F10 must interrupt a pending long note without polling delay")
        self.assertEqual(["stopped"], result)
        self.assertLess(time.monotonic() - started, 0.35)

    def test_token_protected_external_stop_file_wakes_a_long_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            signal_path = Path(temporary) / "stop.json"
            controller = PlaybackStopController(signal_path=signal_path, token="expected-token", external_poll_seconds=0.01)
            thread, result = self._stop_waiter(controller)

            # A stray or stale file must not stop another player's session.
            signal_path.write_text(
                json.dumps({"version": 1, "token": "wrong-token", "command": "stop"}),
                encoding="utf-8",
            )
            time.sleep(0.03)
            self.assertTrue(thread.is_alive())

            started = time.monotonic()
            signal_path.write_text(
                json.dumps({"version": 1, "token": "expected-token", "command": "stop"}),
                encoding="utf-8",
            )
            thread.join(0.35)

            self.assertFalse(thread.is_alive(), "the elevated player must observe the GUI stop signal")
            self.assertEqual(["stopped"], result)
            self.assertLess(time.monotonic() - started, 0.35)

    def test_wait_until_honors_an_already_requested_stop(self) -> None:
        controller = PlaybackStopController()
        controller.request_stop()

        with self.assertRaises(PlaybackStopped):
            controller.wait_until(time.perf_counter() + 60.0)

    def test_generic_player_uses_the_controller_not_combination_hotkeys_or_sleep(self) -> None:
        source_path = SOURCE_ROOT / "launcher" / "generic_midi_player.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))

        imported_controller = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "playback_control"
            and {alias.name for alias in node.names} >= {"PlaybackStopController", "PlaybackStopped"}
            for node in tree.body
        )
        self.assertTrue(imported_controller)

        calls: list[tuple[str | None, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value.id if isinstance(node.func.value, ast.Name) else None
            calls.append((owner, node.func.attr))

        self.assertIn(("stop_controller", "install_single_key_hook"), calls)
        self.assertIn(("stop_controller", "uninstall_hook"), calls)
        self.assertNotIn(("keyboard", "add_hotkey"), calls)
        self.assertNotIn(("time", "sleep"), calls)

    def test_all_builtin_players_delegate_stopping_to_the_shared_controller(self) -> None:
        players = (
            ASSET_ROOT / "croatian" / "croatian_three_octave_player.py",
            ASSET_ROOT / "mariage" / "mariage_three_octave_player.py",
            ASSET_ROOT / "daoxiang" / "daoxiang_three_octave_player.py",
            ASSET_ROOT / "autumn" / "autumn_scheme_B_player.py",
        )

        for player in players:
            with self.subTest(player=player.name):
                source = player.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(player))
                calls: list[tuple[str | None, str]] = []
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                        continue
                    owner = node.func.value.id if isinstance(node.func.value, ast.Name) else None
                    calls.append((owner, node.func.attr))

                self.assertIn(("stop_controller", "reset"), calls)
                self.assertIn(("stop_controller", "install_single_key_hook"), calls)
                self.assertIn(("stop_controller", "uninstall_hook"), calls)
                self.assertIn(("stop_controller", "wait_for"), calls)
                self.assertIn(("stop_controller", "wait_until"), calls)
                self.assertNotIn(("keyboard", "add_hotkey"), calls)
                self.assertNotIn(("time", "sleep"), calls)

                controller_call_lines = {
                    method: [
                        node.lineno
                        for node in ast.walk(tree)
                        if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "stop_controller"
                        and node.func.attr == method
                    ]
                    for method in ("reset", "install_single_key_hook")
                }
                countdown = next(
                    (
                        node
                        for node in ast.walk(tree)
                        if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "wait_for"
                        and node.args
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == "START_DELAY"
                    ),
                    None,
                )
                self.assertIsNotNone(countdown)
                self.assertTrue(controller_call_lines["reset"])
                self.assertTrue(controller_call_lines["install_single_key_hook"])
                self.assertLess(max(controller_call_lines["reset"]), countdown.lineno)
                self.assertLess(max(controller_call_lines["install_single_key_hook"]), countdown.lineno)

                pdi_pause_lines = [
                    node.lineno
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.Assign, ast.AnnAssign))
                    and any(
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "pdi"
                        and target.attr == "PAUSE"
                        for target in (
                            node.targets if isinstance(node, ast.Assign) else [node.target]
                        )
                    )
                ]
                self.assertTrue(pdi_pause_lines)
                self.assertLess(max(pdi_pause_lines), countdown.lineno)

                emergency_stop = next(
                    (
                        node
                        for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "emergency_stop"
                    ),
                    None,
                )
                self.assertIsNotNone(emergency_stop)
                emergency_calls = [
                    node
                    for node in ast.walk(emergency_stop)
                    if isinstance(node, ast.Call)
                ]
                self.assertEqual(1, len(emergency_calls))
                call = emergency_calls[0]
                self.assertIsInstance(call.func, ast.Attribute)
                self.assertIsInstance(call.func.value, ast.Name)
                self.assertEqual("stop_controller", call.func.value.id)
                self.assertEqual("request_stop", call.func.attr)

    def test_builtin_players_can_find_the_controller_when_run_as_standalone_scripts(self) -> None:
        players = (
            ASSET_ROOT / "croatian" / "croatian_three_octave_player.py",
            ASSET_ROOT / "mariage" / "mariage_three_octave_player.py",
            ASSET_ROOT / "daoxiang" / "daoxiang_three_octave_player.py",
            ASSET_ROOT / "autumn" / "autumn_scheme_B_player.py",
        )
        launcher_path = str((SOURCE_ROOT / "launcher").resolve())

        for index, player in enumerate(players):
            with self.subTest(player=player.name):
                fake_keyboard = types.ModuleType("keyboard")
                fake_pdi = FakePyDirectInput()
                fake_mido = types.ModuleType("mido")
                previous_modules = {
                    name: sys.modules.get(name)
                    for name in ("keyboard", "mido", "pydirectinput", "playback_control")
                }
                original_path = sys.path[:]
                module_name = f"_delta_music_stop_test_{index}"
                try:
                    # Make the player exercise its own source-tree fallback instead
                    # of inheriting the launcher import path from this test suite.
                    sys.path[:] = [entry for entry in sys.path if str(Path(entry).resolve()) != launcher_path]
                    for name, replacement in (
                        ("keyboard", fake_keyboard),
                        ("mido", fake_mido),
                        ("pydirectinput", fake_pdi),
                    ):
                        sys.modules[name] = replacement
                    sys.modules.pop("playback_control", None)

                    spec = importlib.util.spec_from_file_location(module_name, player)
                    self.assertIsNotNone(spec)
                    self.assertIsNotNone(spec.loader)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)

                    self.assertFalse(module.stop_controller.is_stop_requested())
                    module.emergency_stop()
                    self.assertTrue(module.stop_controller.is_stop_requested())
                    self.assertEqual([], fake_pdi.operations, "the hook callback must not inject/release input")
                finally:
                    sys.path[:] = original_path
                    sys.modules.pop(module_name, None)
                    for name, previous in previous_modules.items():
                        if previous is None:
                            sys.modules.pop(name, None)
                        else:
                            sys.modules[name] = previous


if __name__ == "__main__":
    unittest.main()
