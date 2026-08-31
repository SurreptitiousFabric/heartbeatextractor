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
            self.controller: object | None = None

        def do_activate(self) -> None:
            window = self.get_active_window()
            if window is None:
                from .viewer_ui import JournalWindow

                try:
                    self.controller = JournalWindow(
                        self, repo_root, state_root, modules
                    )
                    window = self.controller.window
                except (OSError, ValueError):
                    window = Adw.ApplicationWindow(application=self)
                    window.set_title("Heartbeat Extractor")
                    window.set_default_size(720, 480)
                    window.set_content(
                        Adw.StatusPage(
                            title="Private viewer state is unavailable",
                            description=(
                                "A local state database failed closed. Generated journals and "
                                "source logs were not changed."
                            ),
                            icon_name="dialog-warning-symbolic",
                        )
                    )
            window.present()

    return int(JournalApplication().run([]))
