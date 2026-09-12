"""Native file-drop support for the DeltaMusic Tk launcher.

Plain ``tkinter`` does not accept files dragged from Windows Explorer.  This
small adapter keeps that optional platform integration in one place.  The
``tkinterdnd2`` package supplies TkDND, which uses Windows' native OLE drag and
drop support and is collected by the PyInstaller hook shipped with the current
build environment.

Only MIDI paths are surfaced to the launcher.  Copying and validating those
files remains the responsibility of the existing safe MIDI importer.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import TclError
from typing import Any, Callable, Iterable

try:  # Keep source-mode startup usable until its optional dependency is installed.
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # pragma: no cover - exercised through the public fallback.
    DND_FILES = None
    TkinterDnD = None


DROP_EVENT = "<<Drop:DND_Files>>"
MIDI_SUFFIXES = frozenset({".mid", ".midi"})


def create_application_root(tk_module: Any) -> tuple[Any, bool]:
    """Return a Tk root and whether native Explorer file drops are available.

    The EXE build always includes ``tkinterdnd2``.  The plain-Tk fallback is
    intentionally retained for source users whose virtual environment has not
    been refreshed yet; all non-drag-and-drop launcher features still work.
    """

    if TkinterDnD is None:
        return tk_module.Tk(), False
    try:
        return TkinterDnD.Tk(), True
    except (tk_module.TclError, RuntimeError):
        # A broken/missing tkdnd binary must not prevent the music launcher
        # from opening.  It simply falls back to its existing file-picker flow.
        return tk_module.Tk(), False


def split_drop_paths(root: Any, raw_data: str) -> tuple[Path, ...]:
    """Parse a Tcl DND file list without breaking paths containing spaces.

    TkDND encodes a multi-file drop as a Tcl list.  ``splitlist`` is the Tcl
    parser, so it correctly handles Chinese names, braces, and spaces where a
    naive ``str.split`` would corrupt paths.
    """

    try:
        values: Iterable[str] = root.tk.splitlist(raw_data)
    except (TclError, TypeError, ValueError):
        return ()
    return tuple(Path(value) for value in values if value)


def midi_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Keep only regular ``.mid``/``.midi`` files, preserving drop order."""

    accepted: list[Path] = []
    for path in paths:
        try:
            if path.suffix.lower() in MIDI_SUFFIXES and path.is_file():
                accepted.append(path)
        except OSError:
            # A file can disappear between dropping it and checking it.  The
            # importer will give a fuller error for any path selected later.
            continue
    return tuple(accepted)


def enable_midi_file_drop(
    widget: Any,
    root: Any,
    on_files: Callable[[tuple[Path, ...]], None],
) -> bool:
    """Register a widget for one-or-more safe MIDI files from Explorer.

    ``on_files`` runs on Tk's UI thread.  It must call the existing safe MIDI
    import flow; this adapter never opens, executes, or copies user files.
    """

    if DND_FILES is None or not hasattr(widget, "drop_target_register"):
        return False

    def on_drop(event: Any) -> str:
        files = midi_paths(split_drop_paths(root, getattr(event, "data", "")))
        if files:
            on_files(files)
        # TkDND requires a requested action response.  Returning the source's
        # action retains the normal Windows copy cursor for Explorer drops.
        return getattr(event, "action", None) or "copy"

    try:
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind(DROP_EVENT, on_drop)
    except (TclError, RuntimeError, AttributeError):
        # Some computers can create a TkDND-enabled root but fail while
        # registering an individual widget (for example, after a partial DLL
        # load).  Treat drag-and-drop as optional so the normal multi-file
        # picker remains usable instead of preventing the launcher from
        # opening.
        return False
    return True
