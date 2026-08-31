from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType


class ViewerUnavailable(RuntimeError):
    """Raised when the optional native viewer dependencies are unavailable."""


def load_gtk(
    import_module: Callable[[str], ModuleType] = importlib.import_module,
) -> tuple[object, object, object, object]:
    """Load optional GTK modules without affecting extractor-only commands."""

    try:
        gi = import_module("gi")
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        gi.require_version("Pango", "1.0")
        Adw = import_module("gi.repository.Adw")
        Gio = import_module("gi.repository.Gio")
        GLib = import_module("gi.repository.GLib")
        Gtk = import_module("gi.repository.Gtk")
        return Adw, Gio, GLib, Gtk
    except (AttributeError, ImportError, ValueError) as exc:
        raise ViewerUnavailable(
            "The native viewer requires the optional 'viewer' dependencies. "
            "Run 'mise run bootstrap' in the repository and try again."
        ) from exc


def run_viewer(repo_root: Path, state_root: Path) -> int:
    """Start the native generated-journal browser."""

    modules = load_gtk()
    Adw, Gio, _GLib, _Gtk = modules

    class JournalApplication(Adw.Application):
        def __init__(self) -> None:
            super().__init__(
                application_id="com.surreptitiousfabric.HeartbeatExtractor",
                flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            )

        def do_activate(self) -> None:
            window = self.get_active_window()
            if window is None:
                from .viewer_ui import JournalWindow

                window = JournalWindow(self, repo_root, state_root, modules).window
            window.present()

    return int(JournalApplication().run([]))
