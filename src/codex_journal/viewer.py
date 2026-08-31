from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType


class ViewerUnavailable(RuntimeError):
    """Raised when the optional native viewer dependencies are unavailable."""


def load_gtk(
    import_module: Callable[[str], ModuleType] = importlib.import_module,
) -> tuple[object, object, object]:
    """Load optional GTK modules without affecting extractor-only commands."""

    try:
        gi = import_module("gi")
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        Adw = import_module("gi.repository.Adw")
        Gio = import_module("gi.repository.Gio")
        Gtk = import_module("gi.repository.Gtk")
        return Adw, Gio, Gtk
    except (AttributeError, ImportError, ValueError) as exc:
        raise ViewerUnavailable(
            "The native viewer requires the optional 'viewer' dependencies. "
            "Run 'mise run bootstrap' in the repository and try again."
        ) from exc


def run_viewer(repo_root: Path, state_root: Path) -> int:
    """Start the native application. Data features arrive in later roadmap issues."""

    Adw, Gio, Gtk = load_gtk()

    class JournalApplication(Adw.Application):
        def __init__(self) -> None:
            super().__init__(
                application_id="com.surreptitiousfabric.HeartbeatExtractor",
                flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            )

        def do_activate(self) -> None:
            window = self.get_active_window()
            if window is None:
                window = Adw.ApplicationWindow(application=self)
                window.set_title("Heartbeat Extractor")
                window.set_default_size(1100, 720)

                toolbar = Adw.ToolbarView()
                toolbar.add_top_bar(Adw.HeaderBar())
                content = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL,
                    spacing=12,
                    margin_top=24,
                    margin_bottom=24,
                    margin_start=24,
                    margin_end=24,
                )
                title = Gtk.Label(label="Heartbeat Extractor")
                title.add_css_class("title-1")
                title.set_halign(Gtk.Align.START)
                content.append(title)

                summary = Gtk.Label(
                    label=(
                        "Native viewer foundation ready. Session browsing is implemented "
                        "through the public roadmap issues."
                    )
                )
                summary.set_wrap(True)
                summary.set_xalign(0)
                content.append(summary)

                paths = Gtk.Label(
                    label=f"Journal: {repo_root}\nCodex state: {state_root}",
                    selectable=True,
                )
                paths.add_css_class("dim-label")
                paths.set_xalign(0)
                content.append(paths)

                toolbar.set_content(content)
                window.set_content(toolbar)
            window.present()

    return int(JournalApplication().run([]))
