from __future__ import annotations
from collections.abc import Callable
from pathlib import Path
from typing import Any
from .viewer_annotations import AnnotationStore
from .viewer_state import ViewerState, ViewerStateStore
from .viewer_ui_browser import BrowserCallbacks, BrowserController
from .viewer_ui_reports import ActivityController, ComparisonController, ExportController, ReportHost
from .viewer_ui_support import UIContext, accessible
from .viewer_ui_sync import SyncController
from .viewer_ui_timeline import TimelineCallbacks, TimelineController
class JournalWindow:
    """Composition shell for the generated-journal viewer."""
    def __init__(
        self,
        application: object,
        repo_root: Path,
        state_root: Path,
        modules: tuple[Any, ...],
    ) -> None:
        Adw, Gio, GLib, Gtk = modules
        self.application = application
        self.repo_root = repo_root
        self.state_root = state_root
        self.window = Adw.ApplicationWindow(application=application)
        self.window.set_title("Heartbeat Extractor")
        self.context = UIContext(
            Adw, Gio, GLib, Gtk, application, self.window, repo_root, state_root
        )
        self.state_store = ViewerStateStore(repo_root / "state" / "viewer-state.json")
        self.saved_state = self.state_store.load()
        self.annotations = AnnotationStore(repo_root / "state" / "annotations.db")
        stored_theme = self.annotations.get_preference("theme", "system")
        self.theme = stored_theme if stored_theme in {"system", "light", "dark"} else "system"
        self.closed = False
        self._ready_once = False
        self.window.set_default_size(
            self.saved_state.window_width, self.saved_state.window_height
        )
        main, header = self._build_main()
        self.browser = BrowserController(
            self.context,
            self.annotations,
            self.saved_state,
            BrowserCallbacks(
                self._session_selected,
                self._show_state,
                self._show_catalog_error,
                self._browser_ready,
                lambda: self.sync.display_text(),
            ),
        )
        self.timeline = TimelineController(
            self.context,
            self.annotations,
            lambda: self.browser.catalog,
            lambda: self.browser.model,
            self.main_title,
            self.error_page,
            self.content_box,
            TimelineCallbacks(
                self._show_state,
                self._navigate,
                self.browser.refresh_bookmarks,
                self._update_actions,
            ),
            entry_index=self.saved_state.timeline_entry_index,
            density=self.saved_state.timeline_density,
        )
        self.timeline.attach_header(header, self._journal_menu())
        main.add_top_bar(self.timeline.build_selection_bar())
        host = ReportHost(
            self.context,
            self.main_title,
            self.content_box,
            self._show_state,
            self._update_actions,
            self._show_sidebar,
            lambda: self.closed,
        )
        self.comparison = ComparisonController(host, lambda: self.browser.catalog, self.timeline)
        self.activity = ActivityController(
            host,
            lambda: self.browser.catalog,
            self.timeline,
            self.browser.show_subset,
        )
        self.sync = SyncController(
            self.context,
            self.annotations,
            self.saved_state,
            lambda: self.browser.catalog,
            lambda rebuild: self.browser.refresh(rebuild_index=rebuild),
        )
        self.export = ExportController(
            host,
            lambda: self.browser.catalog,
            self.annotations,
            self.timeline,
            self.comparison,
            self.activity,
            self.sync.set_status,
        )
        sidebar = self.browser.build_sidebar(
            self._settings_button(), self.sync.create_button(), self.sync.create_status()
        )
        self.split = Adw.NavigationSplitView()
        self.split.set_min_sidebar_width(220)
        self.split.set_max_sidebar_width(380)
        self.split.set_sidebar_width_fraction(0.28)
        self.split.set_sidebar(Adw.NavigationPage.new(sidebar, "Sessions"))
        self.split.set_content(Adw.NavigationPage.new(main, "Journal"))
        self.window.set_content(self.split)
        self.window.connect("close-request", self.close)
        self._install_actions()
        self._apply_theme()
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1000px"))
        self._narrow_breakpoint = breakpoint
        self.split.set_collapsed(True)
        self.window.add_breakpoint(breakpoint)
        self.window.connect("notify::current-breakpoint", self._breakpoint_changed)
        keys = Gtk.EventControllerKey.new()
        keys.connect("key-pressed", self._key_pressed)
        self.window.add_controller(keys)
        self._show_state("loading")
        GLib.idle_add(self.browser.refresh)
    @property
    def ready(self) -> bool:
        return self.browser.ready
    @property
    def visible_state(self) -> str:
        return self.main_stack.get_visible_child_name()
    def present(self) -> None:
        self.window.present()
        self.context.GLib.idle_add(self._sync_breakpoint)
    def refresh(self) -> bool:
        return self.browser.refresh()
    def close(self, *_args: object) -> bool:
        if self.closed:
            return False
        self.closed = True
        try:
            self.state_store.save(self._capture_state())
        except (OSError, ValueError):
            pass
        try:
            self.annotations.set_preference("theme", self.theme)
            self.sync.close()
        except (OSError, ValueError):
            pass
        self.browser.close()
        self.annotations.close()
        return False
    def _build_main(self) -> tuple[object, object]:
        Adw, Gtk = self.context.Adw, self.context.Gtk
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.main_title = Adw.WindowTitle(
            title="Journal", subtitle="Choose a generated session"
        )
        header.set_title_widget(self.main_title)
        toolbar.add_top_bar(header)
        self.main_stack = Gtk.Stack()
        self.main_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        for name, title, description, icon in (
            ("loading", "Loading journals", "Reading bounded metadata from generated journal files.", "content-loading-symbolic"),
            ("empty", "No journals yet", "Run Sync to create privacy-filtered journals, then refresh this view.", "folder-open-symbolic"),
            ("unselected", "Choose a session", "Select a generated session journal from the sidebar.", "document-open-symbolic"),
        ):
            self.main_stack.add_named(
                Adw.StatusPage(title=title, description=description, icon_name=icon), name
            )
        self.error_page = Adw.StatusPage(
            title="Journal unavailable",
            description="This generated artifact failed closed.",
            icon_name="dialog-warning-symbolic",
        )
        self.main_stack.add_named(self.error_page, "error")
        self.content_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.content_box)
        self.main_stack.add_named(scroller, "content")
        toolbar.set_content(self.main_stack)
        return toolbar, header
    def _journal_menu(self) -> object:
        menu = self.context.Gio.Menu()
        for label, action in (
            ("Copy current entry", "copy-entry"),
            ("Bookmark current entry", "bookmark"),
            ("Bookmark current session", "bookmark-session"),
            ("Compare recent sessions", "compare"),
            ("Journal activity", "activity"),
            ("Preview and export…", "export"),
            ("Session details", "toggle-details"),
        ):
            menu.append(label, f"win.{action}")
        return menu
    def _settings_button(self) -> object:
        Gtk = self.context.Gtk
        button = Gtk.MenuButton(
            icon_name="open-menu-symbolic", tooltip_text="Viewer preferences and help"
        )
        accessible(self.context, button, "Open viewer preferences and help")
        popover = Gtk.Popover()
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        heading = Gtk.Label(label="Viewer preferences", xalign=0)
        heading.add_css_class("heading")
        box.append(heading)
        self.sync.append_preferences(box)
        density = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        density.append(Gtk.Label(label="Timeline density", xalign=0))
        density.append(self.timeline.create_density_control())
        box.append(density)
        theme = Gtk.Button(label="Cycle color theme")
        theme.connect("clicked", lambda *_args: self._cycle_theme())
        box.append(theme)
        self.shortcuts_button = Gtk.Button(label="Keyboard shortcuts")
        accessible(self.context, self.shortcuts_button, "Show keyboard shortcuts")
        self.shortcuts_button.connect("clicked", lambda *_args: self._show_shortcuts())
        box.append(self.shortcuts_button)
        popover.set_child(box)
        button.set_popover(popover)
        return button
    def _install_actions(self) -> None:
        actions: tuple[tuple[str, Callable[..., None], tuple[str, ...]], ...] = (
            ("previous-session", lambda *_args: self.browser.move(-1), ("<Ctrl>Page_Up",)),
            ("next-session", lambda *_args: self.browser.move(1), ("<Ctrl>Page_Down",)),
            ("previous-entry", lambda *_args: self.timeline.move(-1), ("<Alt>Up",)),
            ("next-entry", lambda *_args: self.timeline.move(1), ("<Alt>Down",)),
            ("focus-search", lambda *_args: self.browser.search_entry.grab_focus(), ("<Ctrl>f", "slash")),
            ("refresh", lambda *_args: self.refresh(), ("F5",)),
            ("sync", lambda *_args: self.sync.start(), ("<Ctrl>r",)),
            ("open-project", lambda *_args: self.timeline.open_project(), ("<Ctrl>o",)),
            ("copy-entry", lambda *_args: self.timeline.copy_current_entry(), ("<Ctrl><Alt>c",)),
            ("selection-mode", lambda *_args: self.timeline.set_selection_mode(True), ("<Ctrl><Shift>s",)),
            ("copy-range", lambda *_args: self.timeline.copy_selected_range(), ("<Ctrl><Alt><Shift>c",)),
            ("bookmark", lambda *_args: self.timeline.toggle_entry_bookmark(), ("<Ctrl>b",)),
            ("bookmark-session", lambda *_args: self.timeline.toggle_session_bookmark(), ("<Ctrl><Shift>b",)),
            ("compare", lambda *_args: self.comparison.open(), ("<Ctrl><Shift>c",)),
            ("activity", lambda *_args: self.activity.open(), ("<Ctrl><Shift>a",)),
            ("export", lambda *_args: self.export.open(), ("<Ctrl>e",)),
            ("toggle-details", lambda *_args: self.timeline.toggle_details(), ("<Ctrl>d",)),
            ("help", lambda *_args: self._show_shortcuts(), ("<Ctrl><Shift>slash",)),
            ("cycle-theme", lambda *_args: self._cycle_theme(), ("<Ctrl><Shift>t",)),
        )
        for name, callback, accelerators in actions:
            action = self.context.Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.window.add_action(action)
            self.application.set_accels_for_action(f"win.{name}", list(accelerators))
    def _session_selected(self, session_id: str, changed: bool) -> None:
        self.comparison.note_session(session_id)
        self.timeline.select_session(session_id, changed)
        if self.split.get_collapsed():
            self.split.set_show_content(True)
        self._update_actions()
    def _browser_ready(self) -> None:
        self.activity.invalidate()
        self.sync.catalog_ready()
        if not self._ready_once:
            self._ready_once = True
            self.split.set_show_content(self.saved_state.content_visible)
        self._update_actions()
    def _navigate(self, session_id: str) -> None:
        self.timeline.current_entry_index = 0
        self.browser.navigate(session_id)
    def _show_sidebar(self) -> None:
        if self.split.get_collapsed():
            self.split.set_show_content(False)
    def _show_catalog_error(self, count: int) -> None:
        self.error_page.set_title("Generated journal catalog is malformed")
        self.error_page.set_description(
            f"{count} generated artifact error(s) were recorded. Private source logs were not opened."
        )
        self._show_state("error")
    def _show_state(self, name: str) -> None:
        if name != "content" and hasattr(self, "timeline"):
            self.timeline.deactivate()
        self.main_stack.set_visible_child_name(name)
        titles = {
            "loading": ("Journal", "Loading generated journals"),
            "empty": ("Journal", "No generated journals"),
            "unselected": ("Journal", "Choose a generated session"),
            "error": ("Journal unavailable", "Generated artifact failed validation"),
        }
        if name in titles:
            self.main_title.set_title(titles[name][0])
            self.main_title.set_subtitle(titles[name][1])
        if hasattr(self, "timeline"):
            self._update_actions()
    def _update_actions(self) -> None:
        if not hasattr(self, "export"):
            return
        detail_ready = bool(self.timeline.detail and self.timeline.detail.entries)
        session_ready = self.timeline.detail is not None and self.browser.model.selected is not None
        states = {
            "open-project": session_ready,
            "copy-entry": detail_ready,
            "copy-range": detail_ready,
            "selection-mode": detail_ready,
            "bookmark": detail_ready,
            "bookmark-session": session_ready,
            "compare": self.comparison.ready,
            "activity": bool(self.browser.catalog.sessions),
            "export": self.export.available,
            "toggle-details": self.timeline.details_expander is not None,
        }
        for name, enabled in states.items():
            action = self.window.lookup_action(name)
            if action is not None:
                action.set_enabled(enabled)
        self.timeline.update_availability(session_ready, detail_ready)
    def _capture_state(self) -> ViewerState:
        return ViewerState(
            selected_session_id=self.browser.model.selected_session_id,
            filters=self.browser.capture_filters(),
            window_width=max(480, self.window.get_width()),
            window_height=max(480, self.window.get_height()),
            content_visible=self.split.get_show_content(),
            timeline_entry_index=self.timeline.current_entry_index,
            timeline_density=self.timeline.density,
            last_sync_at=self.sync.last_sync_at,
            last_sync_summary=self.sync.last_sync_summary,
        )
    def _key_pressed(
        self, _controller: object, keyval: int, _keycode: int, state: object
    ) -> bool:
        focus = self.window.get_focus()
        if isinstance(
            focus,
            (self.context.Gtk.Entry, self.context.Gtk.SearchEntry, self.context.Gtk.TextView),
        ):
            return False
        modifiers = int(state) & int(self.context.Gtk.accelerator_get_default_mod_mask())
        if modifiers:
            return False
        if keyval in (ord("j"), ord("J"), 0xFF54):
            self.timeline.move(1)
            return True
        if keyval in (ord("k"), ord("K"), 0xFF52):
            self.timeline.move(-1)
            return True
        return False
    def _show_shortcuts(self) -> None:
        window = self.context.Gtk.ShortcutsWindow(transient_for=self.window, modal=True)
        window.set_title("Heartbeat Extractor shortcuts")
        section = self.context.Gtk.ShortcutsSection(
            section_name="journal", title="Journal browsing"
        )
        group = self.context.Gtk.ShortcutsGroup(title="Navigation and actions")
        for title, accelerator in (
            ("Previous session", "<Ctrl>Page_Up"),
            ("Next session", "<Ctrl>Page_Down"),
            ("Previous entry (K also works)", "<Alt>Up"),
            ("Next entry (J also works)", "<Alt>Down"),
            ("Focus search (/ also works)", "<Ctrl>f"),
            ("Refresh generated journals", "F5"),
            ("Sync source sessions", "<Ctrl>r"),
            ("Open validated project directory", "<Ctrl>o"),
            ("Copy current sanitized entry", "<Ctrl><Alt>c"),
            ("Enter timeline selection mode", "<Ctrl><Shift>s"),
            ("Copy selected sanitized range", "<Ctrl><Alt><Shift>c"),
            ("Bookmark current entry", "<Ctrl>b"),
            ("Bookmark current session", "<Ctrl><Shift>b"),
            ("Toggle session details", "<Ctrl>d"),
            ("Cycle system/light/dark theme", "<Ctrl><Shift>t"),
            ("Compare two most recently viewed sessions", "<Ctrl><Shift>c"),
            ("Open daily, weekly, and project activity", "<Ctrl><Shift>a"),
            ("Preview and export reviewed material", "<Ctrl>e"),
            ("Show this help", "<Ctrl><Shift>slash"),
        ):
            group.add_shortcut(
                self.context.Gtk.ShortcutsShortcut(title=title, accelerator=accelerator)
            )
        section.add_group(group)
        window.add_section(section)
        window.present()
    def _apply_theme(self) -> None:
        schemes = {
            "system": self.context.Adw.ColorScheme.DEFAULT,
            "light": self.context.Adw.ColorScheme.FORCE_LIGHT,
            "dark": self.context.Adw.ColorScheme.FORCE_DARK,
        }
        self.context.Adw.StyleManager.get_default().set_color_scheme(schemes[self.theme])
    def _cycle_theme(self) -> None:
        choices = ("system", "light", "dark")
        self.theme = choices[(choices.index(self.theme) + 1) % len(choices)]
        self._apply_theme()
    def _breakpoint_changed(self, *_args: object) -> None:
        self._sync_breakpoint()
    def _sync_breakpoint(self) -> bool:
        self.split.set_collapsed(
            self.window.get_current_breakpoint() is self._narrow_breakpoint
        )
        return False
