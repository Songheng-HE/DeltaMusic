"""Cancellation primitives shared by DeltaMusic players.

The player uses synthetic keyboard input while listening for a physical F10.
``keyboard.add_hotkey`` is intentionally not used here: it matches the whole
currently-pressed key combination, which can include one of the synthetic
musical keys.  This module uses a single-key hook and a thread-safe Event so
the playback thread remains the only code that injects or releases input.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any


STOP_FILE_ENV = "DELTAMUSIC_STOP_FILE"
STOP_TOKEN_ENV = "DELTAMUSIC_STOP_TOKEN"
CONTROL_PROTOCOL_VERSION = 1
DEFAULT_EXTERNAL_POLL_SECONDS = 0.025


class PlaybackStopped(Exception):
    """Raised in the playback thread when a stop has been requested."""


class PlaybackStopController:
    """Own cancellation state for one player session.

    A physical F10 sets the Event immediately.  When launched from the GUI,
    the controller also observes a token-protected JSON control file so the
    non-elevated GUI can request a graceful stop from an elevated player.
    """

    def __init__(
        self,
        signal_path: Path | None = None,
        token: str | None = None,
        external_poll_seconds: float = DEFAULT_EXTERNAL_POLL_SECONDS,
    ) -> None:
        if external_poll_seconds <= 0:
            raise ValueError("external_poll_seconds must be positive")
        raw_path = signal_path if signal_path is not None else os.environ.get(STOP_FILE_ENV)
        raw_token = token if token is not None else os.environ.get(STOP_TOKEN_ENV)
        self._signal_path = Path(raw_path) if raw_path else None
        self._token = raw_token or ""
        self._event = threading.Event()
        self._external_poll_seconds = float(external_poll_seconds)
        self._hook: Any | None = None

    @property
    def signal_path(self) -> Path | None:
        return self._signal_path

    def reset(self) -> None:
        """Arm a new playback run. Call only before its countdown starts."""
        self._event.clear()

    def request_stop(self) -> None:
        """Fast, idempotent callback safe for a keyboard hook thread."""
        self._event.set()

    def _external_stop_requested(self) -> bool:
        if self._signal_path is None or not self._token:
            return False
        try:
            data = json.loads(self._signal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(data, dict)
            and data.get("version") == CONTROL_PROTOCOL_VERSION
            and data.get("token") == self._token
            and data.get("command") == "stop"
        )

    def is_stop_requested(self) -> bool:
        if self._event.is_set():
            return True
        if self._external_stop_requested():
            self._event.set()
            return True
        return False

    def wait(self, timeout: float | None) -> bool:
        """Wait until a stop is requested or ``timeout`` elapses.

        With a GUI control file, the wait is split into short Event waits so a
        normal-integrity GUI can request a stop within one poll interval while
        physical F10 still wakes immediately.
        """
        if timeout is not None and timeout < 0:
            timeout = 0
        if self.is_stop_requested():
            return True
        if self._signal_path is None or not self._token:
            return self._event.wait(timeout)

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.is_stop_requested():
                return True
            if deadline is None:
                wait_for = self._external_poll_seconds
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self.is_stop_requested()
                wait_for = min(remaining, self._external_poll_seconds)
            if self._event.wait(wait_for):
                return True

    def raise_if_requested(self) -> None:
        if self.is_stop_requested():
            raise PlaybackStopped

    def wait_for(self, seconds: float) -> None:
        if self.wait(max(0.0, seconds)):
            raise PlaybackStopped

    def wait_until(self, target: float, clock: Any = time.perf_counter) -> None:
        self.raise_if_requested()
        remaining = target - clock()
        if remaining > 0 and self.wait(remaining):
            raise PlaybackStopped
        self.raise_if_requested()

    def install_single_key_hook(self, keyboard_module: Any, key: str = "f10") -> Any:
        """Capture the physical F10 independently of currently held music keys."""
        if self._hook is not None:
            raise RuntimeError("stop hook is already installed")

        def on_press(_event: Any) -> None:
            # Do not call PyDirectInput here. This runs in keyboard's hook path.
            self.request_stop()

        self._hook = keyboard_module.on_press_key(key, on_press, suppress=True)
        return self._hook

    def uninstall_hook(self, keyboard_module: Any) -> None:
        if self._hook is None:
            return
        hook, self._hook = self._hook, None
        try:
            keyboard_module.unhook(hook)
        except Exception:
            # Cleanup must never prevent the playback thread from releasing input.
            pass
