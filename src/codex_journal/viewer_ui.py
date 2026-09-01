from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from threading import Thread
from typing import Any

from gi.repository import Pango

from .engine import JournalEngine
from .viewer_actions import (
    ProjectPathError,
    copy_one_entry,
    copy_selected_range,
    project_directory_uri,
)
from .viewer_activity import ActivityBucket, ActivityReport, build_activity_report, fill_daily_range
from .viewer_annotations import AnnotationStore, AnnotationTarget
from .viewer_catalog import (
    CatalogError,
    CatalogSession,
    JournalCatalog,
    JournalSearchIndex,
    SearchHit,
)
from .viewer_compare import ComparisonReport, compare_details, filter_timeline
from .viewer_export import (
    ExportDocument,
    activity_document,
    comparison_document,
    include_private_notes,
    render_export,
    render_preview,
    selected_entries_document,
    write_export_atomic,
)
from .viewer_model import ALL, BrowserFilters, SessionBrowserModel, display_start, session_badges
from .viewer_presenter import (
    PresentedEntry,
    concise_session_summary,
    present_timeline,
    safe_inline_markup,
)
from .viewer_tags import TAGS
from .viewer_state import ViewerState, ViewerStateStore
from .viewer_sync import (
    CatalogSnapshot,
    ChangeSummary,
    compare_snapshots,
    rebuild_search_index_atomic,
)


class JournalWindow:
    """Native, adaptive browser over generated journal artifacts only."""

    def __init__(
        self,
        application: object,
        repo_root: Path,
        state_root: Path,
        modules: tuple[Any, ...],
    ) -> None:
        self.Adw, self.Gio, self.GLib, self.Gtk = modules
        self.repo_root = repo_root
        self.state_root = state_root
        self.application = application
        self.catalog = JournalCatalog(repo_root)
        self.model = SessionBrowserModel(self.catalog)
        self.state_store = ViewerStateStore(repo_root / "state" / "viewer-state.json")
        self.saved_state = self.state_store.load()
        self.annotations = AnnotationStore(repo_root / "state" / "annotations.db")
        stored_theme = self.annotations.get_preference("theme", "system")
        self.theme = stored_theme if stored_theme in {"system", "light", "dark"} else "system"
        self.sync_on_launch_preference = (
            self.annotations.get_preference("sync_on_launch", "false") == "true"
        )
        self.periodic_sync_preference = (
            self.annotations.get_preference("periodic_sync", "false") == "true"
        )
        self.last_sync_at = self.saved_state.last_sync_at
        self.last_sync_summary = self.saved_state.last_sync_summary
        self.current_entry_index = self.saved_state.timeline_entry_index
        self.timeline_density = self.saved_state.timeline_density
        self._state_restored = False
        self._timeline_widgets: dict[int, object] = {}
        self._selection_checks: dict[int, object] = {}
        self.selection_mode = False
        self._updating_selection = False
        self.details_expander: object | None = None
        self.current_detail: object | None = None
        self._selected_entry_indexes: set[int] = set()
        self.action_status: object | None = None
        self._pending_note_delete: AnnotationTarget | None = None
        self._recent_session_ids: list[str] = []
        self._comparison_report: ComparisonReport | None = None
        self._activity_report: ActivityReport | None = None
        self._activity_running = False
        self._export_document: ExportDocument | None = None
        self._pending_export: tuple[Path, bytes] | None = None
        self._session_rows: dict[object, str] = {}
        self._filter_widgets: dict[str, object] = {}
        self._hits_by_session: dict[str, SearchHit] = {}
        self._session_summaries: dict[str, str] = {}
        self.search_index: JournalSearchIndex | None = None
        self._loading = False
        self._updating_filters = False
        self._sync_running = False
        self._closed = False
        self._periodic_source: int | None = None
        self._launch_sync_started = False

        self.window = self.Adw.ApplicationWindow(application=application)
        self.window.set_title("Heartbeat Extractor")
        self.window.set_default_size(
            self.saved_state.window_width, self.saved_state.window_height
        )

        self.split = self.Adw.NavigationSplitView()
        self.split.set_min_sidebar_width(220)
        self.split.set_max_sidebar_width(380)
        self.split.set_sidebar_width_fraction(0.28)
        self.split.set_sidebar(self.Adw.NavigationPage.new(self._build_sidebar(), "Sessions"))
        self.split.set_content(self.Adw.NavigationPage.new(self._build_main(), "Journal"))
        self.window.set_content(self.split)
        self.window.connect("close-request", self._on_close)
        self._install_actions()
        self._apply_theme()

        breakpoint = self.Adw.Breakpoint.new(
            self.Adw.BreakpointCondition.parse("max-width: 1000px")
        )
        self._narrow_breakpoint = breakpoint
        self.split.set_collapsed(True)
        self.window.add_breakpoint(breakpoint)
        self.window.connect("notify::current-breakpoint", self._on_breakpoint_changed)

        keys = self.Gtk.EventControllerKey.new()
        keys.connect("key-pressed", self._on_key_pressed)
        self.window.add_controller(keys)

        self._show_state("loading")
        self.GLib.idle_add(self.refresh_catalog)

    def present(self) -> None:
        self.window.present()
        self.GLib.idle_add(self._sync_breakpoint)

    def _on_breakpoint_changed(self, *_args: object) -> None:
        self._sync_breakpoint()

    def _sync_breakpoint(self) -> bool:
        self.split.set_collapsed(
            self.window.get_current_breakpoint() is self._narrow_breakpoint
        )
        return False

    def _install_actions(self) -> None:
        actions: tuple[tuple[str, Callable[..., None], tuple[str, ...]], ...] = (
            ("previous-session", lambda *_args: self._move_session(-1), ("<Ctrl>Page_Up",)),
            ("next-session", lambda *_args: self._move_session(1), ("<Ctrl>Page_Down",)),
            ("previous-entry", lambda *_args: self._move_entry(-1), ("<Alt>Up",)),
            ("next-entry", lambda *_args: self._move_entry(1), ("<Alt>Down",)),
            ("focus-search", lambda *_args: self.search_entry.grab_focus(), ("<Ctrl>f", "slash")),
            ("refresh", lambda *_args: self.refresh_catalog(), ("F5",)),
            ("sync", lambda *_args: self._start_sync(), ("<Ctrl>r",)),
            ("open-project", lambda *_args: self._open_project(), ("<Ctrl>o",)),
            ("copy-entry", lambda *_args: self._copy_current_entry(), ("<Ctrl><Alt>c",)),
            ("selection-mode", lambda *_args: self._set_selection_mode(True), ("<Ctrl><Shift>s",)),
            (
                "copy-range",
                lambda *_args: self._copy_selected_range(),
                ("<Ctrl><Alt><Shift>c",),
            ),
            ("bookmark", lambda *_args: self._toggle_entry_bookmark(), ("<Ctrl>b",)),
            (
                "bookmark-session",
                lambda *_args: self._toggle_session_bookmark(),
                ("<Ctrl><Shift>b",),
            ),
            ("compare", lambda *_args: self._compare_recent(), ("<Ctrl><Shift>c",)),
            ("activity", lambda *_args: self._open_activity(), ("<Ctrl><Shift>a",)),
            ("export", lambda *_args: self._open_export_preview(), ("<Ctrl>e",)),
            ("toggle-details", lambda *_args: self._toggle_details(), ("<Ctrl>d",)),
            ("help", lambda *_args: self._show_shortcuts(), ("<Ctrl><Shift>slash",)),
            ("cycle-theme", lambda *_args: self._cycle_theme(), ("<Ctrl><Shift>t",)),
        )
        for name, callback, accelerators in actions:
            action = self.Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.window.add_action(action)
            self.application.set_accels_for_action(f"win.{name}", list(accelerators))

    def _on_key_pressed(
        self,
        _controller: object,
        keyval: int,
        _keycode: int,
        state: object,
    ) -> bool:
        focus = self.window.get_focus()
        if isinstance(focus, (self.Gtk.Entry, self.Gtk.SearchEntry, self.Gtk.TextView)):
            return False
        modifiers = int(state) & int(
            self.Gtk.accelerator_get_default_mod_mask()
        )
        if modifiers:
            return False
        if keyval in (ord("j"), ord("J"), 0xFF54):
            self._move_entry(1)
            return True
        if keyval in (ord("k"), ord("K"), 0xFF52):
            self._move_entry(-1)
            return True
        return False

    def _move_session(self, delta: int) -> None:
        sessions = self.model.sessions
        if not sessions:
            return
        ids = [session.session_id for session in sessions]
        try:
            current = ids.index(self.model.selected_session_id)
        except ValueError:
            current = 0
        target_id = ids[max(0, min(len(ids) - 1, current + delta))]
        row = next(
            (item for item, session_id in self._session_rows.items() if session_id == target_id),
            None,
        )
        if row is not None:
            self.session_list.select_row(row)
            row.grab_focus()

    def _move_entry(self, delta: int) -> None:
        if not self._timeline_widgets:
            return
        indexes = sorted(self._timeline_widgets)
        try:
            current = indexes.index(self.current_entry_index)
        except ValueError:
            current = 0
        index = indexes[max(0, min(len(indexes) - 1, current + delta))]
        self._focus_entry(index)

    def _focus_entry(self, index: int) -> None:
        widget = self._timeline_widgets.get(index)
        if widget is None:
            return
        for candidate_index, candidate in self._timeline_widgets.items():
            if candidate_index == index:
                candidate.add_css_class("accent")
            else:
                candidate.remove_css_class("accent")
        self.current_entry_index = index
        self._load_entry_note()
        widget.grab_focus()

    def _toggle_details(self) -> None:
        if self.details_expander is not None:
            self.details_expander.set_expanded(not self.details_expander.get_expanded())
            self.details_expander.grab_focus()

    def _apply_theme(self) -> None:
        schemes = {
            "system": self.Adw.ColorScheme.DEFAULT,
            "light": self.Adw.ColorScheme.FORCE_LIGHT,
            "dark": self.Adw.ColorScheme.FORCE_DARK,
        }
        self.Adw.StyleManager.get_default().set_color_scheme(schemes[self.theme])

    def _cycle_theme(self) -> None:
        choices = ("system", "light", "dark")
        self.theme = choices[(choices.index(self.theme) + 1) % len(choices)]
        self._apply_theme()

    def _on_density_changed(self, dropdown: object, _pspec: object) -> None:
        self.timeline_density = "compact" if dropdown.get_selected() == 1 else "comfortable"
        detail = self.current_detail
        if detail is not None and self._state_restored:
            self._render_session(detail.session.session_id)

    def _show_shortcuts(self) -> None:
        window = self.Gtk.ShortcutsWindow(transient_for=self.window, modal=True)
        window.set_title("Heartbeat Extractor shortcuts")
        section = self.Gtk.ShortcutsSection(section_name="journal", title="Journal browsing")
        group = self.Gtk.ShortcutsGroup(title="Navigation and actions")
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
                self.Gtk.ShortcutsShortcut(title=title, accelerator=accelerator)
            )
        section.add_group(group)
        window.add_section(section)
        window.present()

    def _build_sidebar(self) -> object:
        toolbar = self.Adw.ToolbarView()
        header = self.Adw.HeaderBar()
        title = self.Adw.WindowTitle(title="Sessions", subtitle="Generated journals only")
        header.set_title_widget(title)
        settings_button = self.Gtk.MenuButton(
            icon_name="open-menu-symbolic", tooltip_text="Viewer preferences and help"
        )
        self._accessible(settings_button, "Open viewer preferences and help")
        preferences = self.Gtk.Popover()
        preferences_box = self.Gtk.Box(
            orientation=self.Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        preference_heading = self.Gtk.Label(label="Viewer preferences", xalign=0)
        preference_heading.add_css_class("heading")
        preferences_box.append(preference_heading)
        self.sync_on_launch_check = self.Gtk.CheckButton(label="Sync on launch")
        self.sync_on_launch_check.connect("toggled", self._on_sync_setting_changed)
        preferences_box.append(self.sync_on_launch_check)
        self.periodic_sync_check = self.Gtk.CheckButton(
            label="Sync every 5 minutes while open"
        )
        self.periodic_sync_check.connect("toggled", self._on_sync_setting_changed)
        preferences_box.append(self.periodic_sync_check)
        density_row = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=8)
        density_row.append(self.Gtk.Label(label="Timeline density", xalign=0))
        self.density_dropdown = self.Gtk.DropDown.new_from_strings(
            ["Comfortable", "Compact"]
        )
        self.density_dropdown.set_selected(1 if self.timeline_density == "compact" else 0)
        self.density_dropdown.connect("notify::selected", self._on_density_changed)
        self._accessible(self.density_dropdown, "Choose timeline density")
        density_row.append(self.density_dropdown)
        preferences_box.append(density_row)
        theme_button = self.Gtk.Button(label="Cycle color theme")
        theme_button.connect("clicked", lambda *_args: self._cycle_theme())
        preferences_box.append(theme_button)
        self.shortcuts_button = self.Gtk.Button(label="Keyboard shortcuts")
        self._accessible(self.shortcuts_button, "Show keyboard shortcuts")
        self.shortcuts_button.connect("clicked", lambda *_args: self._show_shortcuts())
        preferences_box.append(self.shortcuts_button)
        preferences.set_child(preferences_box)
        settings_button.set_popover(preferences)
        header.pack_end(settings_button)
        self.sync_button = self.Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text="Sync source sessions"
        )
        self._accessible(self.sync_button, "Sync source sessions")
        self.sync_button.connect("clicked", lambda *_args: self._start_sync())
        header.pack_end(self.sync_button)
        toolbar.add_top_bar(header)

        outer = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=0)
        self.search_entry = self.Gtk.SearchEntry(
            placeholder_text="Search safe journals",
            margin_top=12,
            margin_start=12,
            margin_end=12,
        )
        self.search_entry.connect("search-changed", self._on_search_changed)
        self._accessible(self.search_entry, "Search generated journals")
        outer.append(self.search_entry)
        filters = self.Gtk.Box(
            orientation=self.Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        advanced = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=8)
        for field, label, destination in (
            ("project", "Project", filters),
            ("status", "Status", filters),
            ("date_from", "From", advanced),
            ("date_to", "To", advanced),
            ("branch", "Branch", advanced),
            ("source_kind", "Source", advanced),
            ("tag", "Tag", advanced),
        ):
            row = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=8)
            caption = self.Gtk.Label(label=label, xalign=0)
            caption.set_size_request(58, -1)
            dropdown = self.Gtk.DropDown.new_from_strings([ALL])
            dropdown.set_hexpand(True)
            dropdown.connect("notify::selected", self._on_filter_changed, field)
            self._accessible(dropdown, f"Filter by {label.lower()}")
            self._filter_widgets[field] = dropdown
            row.append(caption)
            row.append(dropdown)
            destination.append(row)
        self.bookmarks_check = self.Gtk.CheckButton(label="Bookmarked sessions")
        self.bookmarks_check.connect("toggled", self._on_boolean_filter, "bookmarked_only")
        filters.append(self.bookmarks_check)
        self.redacted_check = self.Gtk.CheckButton(label="Has redactions")
        self.redacted_check.connect("toggled", self._on_boolean_filter, "redacted_only")
        advanced.append(self.redacted_check)
        self.errors_check = self.Gtk.CheckButton(label="Has extraction errors")
        self.errors_check.connect(
            "toggled", self._on_boolean_filter, "extraction_errors_only"
        )
        advanced.append(self.errors_check)
        self.advanced_filters = self.Gtk.Expander(label="Advanced filters")
        self.advanced_filters.set_child(advanced)
        filters.append(self.advanced_filters)
        outer.append(filters)

        feedback = self.Gtk.Box(
            orientation=self.Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_start=12,
            margin_end=12,
            margin_bottom=8,
        )
        self.filter_status = self.Gtk.Label(label="", xalign=0, hexpand=True)
        self.filter_status.add_css_class("caption")
        feedback.append(self.filter_status)
        self.clear_filters_button = self.Gtk.Button(label="Clear all")
        self.clear_filters_button.add_css_class("flat")
        self.clear_filters_button.connect("clicked", self._clear_filters)
        self.clear_filters_button.set_visible(False)
        feedback.append(self.clear_filters_button)
        outer.append(feedback)

        self.sync_status = self.Gtk.Label(
            label="Loading generated journals…",
            xalign=0,
            wrap=True,
            margin_start=12,
            margin_end=12,
            margin_bottom=8,
            selectable=True,
        )
        self.sync_status.add_css_class("caption")
        outer.append(self.sync_status)

        self.count_label = self.Gtk.Label(
            label="Loading…", xalign=0, margin_start=12, margin_end=12, margin_bottom=8
        )
        self.count_label.add_css_class("dim-label")
        outer.append(self.count_label)

        self.session_list = self.Gtk.ListBox()
        self.session_list.set_selection_mode(self.Gtk.SelectionMode.SINGLE)
        self.session_list.add_css_class("navigation-sidebar")
        self.session_list.connect("row-selected", self._on_session_selected)
        scroller = self.Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(self.Gtk.PolicyType.NEVER, self.Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.session_list)
        outer.append(scroller)
        toolbar.set_content(outer)
        return toolbar

    def _build_main(self) -> object:
        toolbar = self.Adw.ToolbarView()
        header = self.Adw.HeaderBar()
        self.main_title = self.Adw.WindowTitle(
            title="Journal", subtitle="Choose a generated session"
        )
        header.set_title_widget(self.main_title)
        self.open_project_button = self.Gtk.Button(
            icon_name="folder-open-symbolic", tooltip_text="Open validated project directory"
        )
        self._accessible(self.open_project_button, "Open validated project directory")
        self.open_project_button.connect("clicked", lambda *_args: self._open_project())
        header.pack_end(self.open_project_button)
        self.select_mode_button = self.Gtk.ToggleButton(label="Select")
        self.select_mode_button.set_tooltip_text("Select a sanitized timeline range")
        self.select_mode_button.connect("toggled", self._on_selection_mode_toggled)
        header.pack_end(self.select_mode_button)
        journal_menu = self.Gio.Menu()
        journal_menu.append("Copy current entry", "win.copy-entry")
        journal_menu.append("Bookmark current entry", "win.bookmark")
        journal_menu.append("Bookmark current session", "win.bookmark-session")
        journal_menu.append("Compare recent sessions", "win.compare")
        journal_menu.append("Journal activity", "win.activity")
        journal_menu.append("Preview and export…", "win.export")
        journal_menu.append("Session details", "win.toggle-details")
        self.more_actions_button = self.Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            tooltip_text="More journal actions",
        )
        self.more_actions_button.set_menu_model(journal_menu)
        self._accessible(self.more_actions_button, "Open more journal actions")
        header.pack_end(self.more_actions_button)
        toolbar.add_top_bar(header)

        self.selection_bar = self.Gtk.Revealer()
        selection_actions = self.Gtk.ActionBar()
        self.selection_count = self.Gtk.Label(label="0 selected")
        selection_actions.pack_start(self.selection_count)
        cancel_selection = self.Gtk.Button(label="Cancel")
        cancel_selection.connect("clicked", lambda *_args: self._set_selection_mode(False))
        selection_actions.pack_end(cancel_selection)
        self.copy_selection_button = self.Gtk.Button(label="Copy selected")
        self.copy_selection_button.add_css_class("suggested-action")
        self.copy_selection_button.set_sensitive(False)
        self.copy_selection_button.connect(
            "clicked", lambda *_args: self._copy_selected_range()
        )
        selection_actions.pack_end(self.copy_selection_button)
        self.selection_bar.set_child(selection_actions)
        toolbar.add_top_bar(self.selection_bar)

        self.main_stack = self.Gtk.Stack()
        self.main_stack.set_transition_type(self.Gtk.StackTransitionType.CROSSFADE)
        self.main_stack.add_named(
            self.Adw.StatusPage(
                title="Loading journals",
                description="Reading bounded metadata from generated journal files.",
                icon_name="content-loading-symbolic",
            ),
            "loading",
        )
        self.main_stack.add_named(
            self.Adw.StatusPage(
                title="No journals yet",
                description="Run Sync to create privacy-filtered journals, then refresh this view.",
                icon_name="folder-open-symbolic",
            ),
            "empty",
        )
        self.main_stack.add_named(
            self.Adw.StatusPage(
                title="Choose a session",
                description="Select a generated session journal from the sidebar.",
                icon_name="document-open-symbolic",
            ),
            "unselected",
        )
        self.error_page = self.Adw.StatusPage(
            title="Journal unavailable",
            description="This generated artifact failed closed.",
            icon_name="dialog-warning-symbolic",
        )
        self.main_stack.add_named(self.error_page, "error")
        self.content_box = self.Gtk.Box(
            orientation=self.Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        self.content_scroller = self.Gtk.ScrolledWindow(vexpand=True)
        self.content_scroller.set_policy(self.Gtk.PolicyType.NEVER, self.Gtk.PolicyType.AUTOMATIC)
        self.content_scroller.set_child(self.content_box)
        self.main_stack.add_named(self.content_scroller, "content")
        toolbar.set_content(self.main_stack)
        return toolbar

    def _show_state(self, name: str) -> None:
        if name != "content":
            self.current_detail = None
            self.details_expander = None
            self._set_selection_mode(False)
        self.main_stack.set_visible_child_name(name)
        if name == "loading":
            self.main_title.set_title("Journal")
            self.main_title.set_subtitle("Loading generated journals")
        elif name == "empty":
            self.main_title.set_title("Journal")
            self.main_title.set_subtitle("No generated journals")
        elif name == "unselected":
            self.main_title.set_title("Journal")
            self.main_title.set_subtitle("Choose a generated session")
        elif name == "error":
            self.main_title.set_title("Journal unavailable")
            self.main_title.set_subtitle("Generated artifact failed validation")
        self._update_action_availability()

    def _update_action_availability(self) -> None:
        detail_ready = bool(self.current_detail and self.current_detail.entries)
        session_ready = self.current_detail is not None and self.model.selected is not None
        states = {
            "open-project": session_ready,
            "copy-entry": detail_ready,
            "copy-range": detail_ready,
            "selection-mode": detail_ready,
            "bookmark": detail_ready,
            "bookmark-session": session_ready,
            "compare": len(set(self._recent_session_ids[-2:])) == 2,
            "activity": bool(self.catalog.sessions),
            "export": bool(
                detail_ready or self._comparison_report is not None or self._activity_report is not None
            ),
            "toggle-details": self.details_expander is not None,
        }
        for name, enabled in states.items():
            action = self.window.lookup_action(name)
            if action is not None:
                action.set_enabled(enabled)
        if hasattr(self, "open_project_button"):
            self.open_project_button.set_sensitive(session_ready)
        if hasattr(self, "select_mode_button"):
            self.select_mode_button.set_sensitive(detail_ready)

    def refresh_catalog(self, *, rebuild_index: bool = True) -> bool:
        if self._loading:
            return False
        if self._state_restored:
            self.saved_state = self._capture_state()
        self._loading = True
        try:
            self.catalog.refresh()
            self._activity_report = None
            self.model = SessionBrowserModel(self.catalog)
            self.model.set_bookmarked_session_ids(self.annotations.bookmarked_session_ids())
            if self.search_index is not None:
                self.search_index.close()
            search_path = self.repo_root / "state" / "viewer.sqlite3"
            try:
                self.search_index = JournalSearchIndex(search_path)
            except CatalogError:
                rebuild_search_index_atomic(self.catalog, search_path)
                self.search_index = JournalSearchIndex(search_path)
            if rebuild_index:
                self.search_index.rebuild(self.catalog)
            self._session_summaries = self.search_index.session_summaries()
            self._populate_filters()
            self._restore_state()
            self._state_restored = True
            self._apply_search()
            self._populate_sessions()
            if not self.catalog.sessions:
                if self.catalog.diagnostics:
                    self._show_catalog_error()
                else:
                    self._show_state("empty")
            else:
                first = self.session_list.get_row_at_index(0)
                if first is not None:
                    self.session_list.select_row(first)
            if not self._launch_sync_started:
                self._launch_sync_started = True
                self._configure_periodic()
                if self.sync_on_launch_check.get_active():
                    self.GLib.idle_add(self._start_sync)
        finally:
            self._loading = False
        return False

    def _on_close(self, *_args: object) -> bool:
        if self._closed:
            return False
        self._closed = True
        if self._periodic_source is not None:
            self.GLib.source_remove(self._periodic_source)
            self._periodic_source = None
        try:
            self.state_store.save(self._capture_state())
        except (OSError, ValueError):
            pass
        try:
            self.annotations.set_preference("theme", self.theme)
            self.annotations.set_preference(
                "sync_on_launch", "true" if self.sync_on_launch_check.get_active() else "false"
            )
            self.annotations.set_preference(
                "periodic_sync", "true" if self.periodic_sync_check.get_active() else "false"
            )
        except (OSError, ValueError):
            pass
        self.annotations.close()
        if self.search_index is not None:
            self.search_index.close()
            self.search_index = None
        return False

    def _capture_state(self) -> ViewerState:
        filters = {
            key: value
            for key, value in asdict(self.model.filters).items()
            if value not in (None, False, "")
        }
        return ViewerState(
            selected_session_id=self.model.selected_session_id,
            filters=filters,
            window_width=max(480, self.window.get_width()),
            window_height=max(480, self.window.get_height()),
            content_visible=self.split.get_show_content(),
            timeline_entry_index=self.current_entry_index,
            timeline_density=self.timeline_density,
            last_sync_at=self.last_sync_at,
            last_sync_summary=self.last_sync_summary,
        )

    def _accessible(self, widget: object, label: str) -> None:
        widget.update_property([self.Gtk.AccessibleProperty.LABEL], [label])

    def _restore_state(self) -> None:
        restored_advanced = False
        for field, value in self.saved_state.filters.items():
            if field in self._filter_widgets and isinstance(value, str):
                self._select_dropdown_value(self._filter_widgets[field], value)
                self.model.set_filter(field, value)
                restored_advanced = restored_advanced or field not in {"project", "status"}
            elif field == "redacted_only" and isinstance(value, bool):
                self.redacted_check.set_active(value)
                self.model.set_filter(field, value)
                restored_advanced = restored_advanced or value
            elif field == "extraction_errors_only" and isinstance(value, bool):
                self.errors_check.set_active(value)
                self.model.set_filter(field, value)
                restored_advanced = restored_advanced or value
            elif field == "bookmarked_only" and isinstance(value, bool):
                self.bookmarks_check.set_active(value)
                self.model.set_filter(field, value)
        self.advanced_filters.set_expanded(restored_advanced)
        session_id = self.saved_state.selected_session_id
        if session_id and any(item.session_id == session_id for item in self.model.sessions):
            self.model.select(session_id)
        self.split.set_show_content(self.saved_state.content_visible)
        self.sync_on_launch_check.set_active(self.sync_on_launch_preference)
        self.periodic_sync_check.set_active(self.periodic_sync_preference)

    def _select_dropdown_value(self, dropdown: object, value: str) -> None:
        model = dropdown.get_model()
        if model is None:
            return
        for index in range(model.get_n_items()):
            item = model.get_item(index)
            if item is not None and item.get_string() == value:
                dropdown.set_selected(index)
                return

    def _show_catalog_error(self) -> None:
        count = len(self.catalog.diagnostics)
        self.error_page.set_title("Generated journal catalog is malformed")
        self.error_page.set_description(
            f"{count} generated artifact error(s) were recorded. Private source logs were not opened."
        )
        self._show_state("error")

    def _filter_values(self, field: str) -> tuple[str, ...]:
        return {
            "project": self.model.projects,
            "date_from": tuple(reversed(self.model.dates)),
            "date_to": tuple(reversed(self.model.dates)),
            "branch": self.model.branches,
            "status": self.model.statuses,
            "source_kind": self.model.source_kinds,
            "tag": TAGS,
        }[field]

    def _populate_filters(self) -> None:
        for field, dropdown in self._filter_widgets.items():
            dropdown.set_model(self.Gtk.StringList.new([ALL, *self._filter_values(field)]))
            dropdown.set_selected(0)

    def _selected_text(self, dropdown: object) -> str | None:
        item = dropdown.get_selected_item()
        return item.get_string() if item is not None else None

    def _on_filter_changed(self, dropdown: object, _pspec: object, field: str) -> None:
        if self._loading or self._updating_filters:
            return
        self.model.set_filter(field, self._selected_text(dropdown))
        self._apply_search()
        self._populate_sessions()

    def _on_boolean_filter(self, button: object, field: str) -> None:
        if self._loading or self._updating_filters:
            return
        self.model.set_filter(field, bool(button.get_active()))
        self._populate_sessions()

    def _stored_sync_status(self) -> str:
        loaded = len(self.catalog.sessions)
        if not self.last_sync_at:
            return (
                f"Displaying {loaded} generated journal(s). "
                "Viewer sync has not run; source freshness is not claimed."
            )
        return (
            f"Displaying {loaded} generated journal(s). "
            f"Last viewer sync: {self.last_sync_at}\n{self.last_sync_summary or ''}"
        ).strip()

    def _on_sync_setting_changed(self, _button: object) -> None:
        if not self._loading and self._launch_sync_started:
            self._configure_periodic()

    def _configure_periodic(self) -> None:
        if self._periodic_source is not None:
            self.GLib.source_remove(self._periodic_source)
            self._periodic_source = None
        if self.periodic_sync_check.get_active() and not self._closed:
            self._periodic_source = self.GLib.timeout_add_seconds(300, self._periodic_tick)

    def _periodic_tick(self) -> bool:
        if self._closed or not self.periodic_sync_check.get_active():
            self._periodic_source = None
            return False
        self._start_sync()
        return True

    def _start_sync(self) -> bool:
        if self._closed or self._sync_running:
            return False
        self._sync_running = True
        self.sync_button.set_sensitive(False)
        self.sync_status.set_label("Sync running… generated journals remain usable.")
        before = CatalogSnapshot.from_catalog(self.catalog)
        Thread(target=self._sync_worker, args=(before,), daemon=True).start()
        return False

    def _sync_worker(self, before: CatalogSnapshot) -> None:
        try:
            result = JournalEngine(self.repo_root, self.state_root).sync()
            refreshed = JournalCatalog(self.repo_root)
            refreshed.refresh()
            summary = compare_snapshots(before, CatalogSnapshot.from_catalog(refreshed))
            rebuild_search_index_atomic(refreshed, self.repo_root / "state" / "viewer.sqlite3")
            self.GLib.idle_add(self._finish_sync, result, summary, None)
        except Exception as exc:  # worker boundary: report type only, never a private path
            self.GLib.idle_add(self._finish_sync, None, None, type(exc).__name__)

    def _finish_sync(
        self,
        result: object | None,
        summary: ChangeSummary | None,
        failure: str | None,
    ) -> bool:
        self._sync_running = False
        if self._closed:
            return False
        self.sync_button.set_sensitive(True)
        if failure or result is None or summary is None:
            self.sync_status.set_label(f"Sync failed safely ({failure or 'unknown error'}).")
            return False
        self.last_sync_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.last_sync_summary = (
            f"discovered={result.discovered} unchanged={result.unchanged} "
            f"appended={result.appended} rebuilt={result.rebuilt} "
            f"no-heartbeats={result.no_heartbeats} "
            f"active/incomplete={result.active_or_incomplete} "
            f"sessions-with-errors={result.sessions_with_errors} run-errors={len(result.errors)}. "
            f"{summary.describe()}"
        )
        self.sync_status.set_label(self._stored_sync_status())
        self.refresh_catalog(rebuild_index=False)
        return False

    def _on_search_changed(self, _entry: object) -> None:
        if self._loading or self._updating_filters:
            return
        self._apply_search()
        self._populate_sessions()

    def _apply_search(self) -> None:
        if self.search_index is None:
            return
        query = self.search_entry.get_text()
        tag = self.model.filters.tag
        active = bool(query.strip() or tag)
        hits = self.search_index.search(
            query,
            tags=(tag,) if tag else (),
            limit=1000,
        ) if active else ()
        self.model.set_search_hits(hits, active=active)
        self._hits_by_session = {}
        for hit in hits:
            self._hits_by_session.setdefault(hit.session_id, hit)

    def _clear_box(self, box: object) -> None:
        while child := box.get_first_child():
            box.remove(child)

    def _clear_filters(self, *_args: object) -> None:
        self._updating_filters = True
        try:
            self.search_entry.set_text("")
            for dropdown in self._filter_widgets.values():
                dropdown.set_selected(0)
            self.redacted_check.set_active(False)
            self.errors_check.set_active(False)
            self.bookmarks_check.set_active(False)
            self.model.filters = BrowserFilters()
            self.model.set_search_hits((), active=False)
            self.model.set_session_subset(None)
            self._hits_by_session.clear()
        finally:
            self._updating_filters = False
        self._populate_sessions()

    def _filter_count(self) -> int:
        values = asdict(self.model.filters)
        return sum(value not in (None, False, "") for value in values.values()) + int(
            bool(self.search_entry.get_text().strip())
        )

    def _update_filter_feedback(self) -> None:
        count = self._filter_count()
        self.clear_filters_button.set_visible(bool(count))
        self.filter_status.set_label(
            f"{count} active filter{'s' if count != 1 else ''}" if count else ""
        )

    def _populate_sessions(self) -> None:
        self._session_rows.clear()
        self._clear_box(self.session_list)
        sessions = self.model.sessions
        counts = self.model.counts
        self.count_label.set_label(f"{counts.visible} of {counts.total} sessions")
        self.sync_status.set_label(self._stored_sync_status())
        self._update_filter_feedback()
        for session in sessions:
            row = self.Gtk.ListBoxRow()
            row.set_activatable(True)
            row.set_child(self._session_row_content(session))
            self.session_list.append(row)
            self._session_rows[row] = session.session_id
        if not sessions:
            self.model.select(None)
            self._show_state("unselected" if self.catalog.sessions else "empty")
            return
        selected_id = self.model.selected_session_id
        selected = next(
            (row for row, session_id in self._session_rows.items() if session_id == selected_id),
            self.session_list.get_row_at_index(0),
        )
        self.session_list.select_row(selected)

    def _session_row_content(self, session: CatalogSession) -> object:
        box = self.Gtk.Box(
            orientation=self.Gtk.Orientation.VERTICAL,
            spacing=4,
            margin_top=10,
            margin_bottom=10,
            margin_start=12,
            margin_end=12,
        )
        project = self.Gtk.Label(label=session.project, xalign=0, ellipsize=3)
        project.add_css_class("heading")
        box.append(project)
        branch = f" · {session.branch}" if session.branch else ""
        meta = self.Gtk.Label(
            label=f"{display_start(session)}{branch} · {session.entry_count} entries",
            xalign=0,
            ellipsize=3,
        )
        meta.add_css_class("dim-label")
        box.append(meta)
        badges = self.Gtk.Label(label="  ·  ".join(session_badges(session)), xalign=0, ellipsize=3)
        badges.add_css_class("caption")
        if session.extraction_error_count or session.redaction_count:
            badges.add_css_class("warning")
        box.append(badges)
        summary_text = self._session_summaries.get(session.session_id)
        if summary_text:
            summary = concise_session_summary(summary_text)
            focus = self.Gtk.Label(label=summary, xalign=0, ellipsize=3)
            focus.add_css_class("caption")
            focus.set_tooltip_text(summary_text)
            box.append(focus)
        if self.annotations.is_bookmarked(AnnotationTarget(session.session_id)):
            bookmarked = self.Gtk.Label(label="★ session bookmark", xalign=0)
            bookmarked.add_css_class("accent")
            box.append(bookmarked)
        hit = self._hits_by_session.get(session.session_id)
        if hit is not None:
            context = self.Gtk.Label(label=f"Match: {hit.text}", xalign=0, ellipsize=3)
            context.add_css_class("accent")
            context.set_tooltip_text(hit.text)
            box.append(context)
        return box

    def _on_session_selected(self, _list: object, row: object | None) -> None:
        if row is None:
            return
        session_id = self._session_rows.get(row)
        if session_id is None:
            return
        if session_id != self.model.selected_session_id:
            self.current_entry_index = 0
        self.model.select(session_id)
        if session_id in self._recent_session_ids:
            self._recent_session_ids.remove(session_id)
        self._recent_session_ids.append(session_id)
        self._recent_session_ids = self._recent_session_ids[-10:]
        self._set_selection_mode(False)
        self._render_session(session_id)
        if self.split.get_collapsed():
            self.split.set_show_content(True)

    def _render_session(self, session_id: str) -> None:
        try:
            detail = self.catalog.load_detail(session_id)
        except (CatalogError, ValueError):
            self.main_title.set_title("Journal unavailable")
            self.main_title.set_subtitle("Generated artifact failed validation")
            self.error_page.set_title("Generated journal is malformed")
            self.error_page.set_description(
                "The selected generated artifact failed validation and was not displayed. "
                "Private source logs were not opened."
            )
            self._show_state("error")
            return
        self._clear_box(self.content_box)
        self._timeline_widgets.clear()
        self._selection_checks.clear()
        self.current_detail = detail
        session = detail.session
        self.main_title.set_title(session.project)
        self.main_title.set_subtitle(
            f"{display_start(session)} · {session.branch or 'No branch'} · {session.status}"
        )
        self.action_status = self.Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
        self.action_status.add_css_class("caption")
        self.content_box.append(self.action_status)
        if session.redaction_count or session.extraction_error_count:
            warning = self.Adw.Banner.new(
                f"{session.redaction_count} redaction(s) · "
                f"{session.extraction_error_count} extraction error(s)"
            )
            warning.set_revealed(True)
            self.content_box.append(warning)

        timeline_heading = self.Gtk.Label(label="Timeline", xalign=0)
        timeline_heading.add_css_class("title-2")
        self.content_box.append(timeline_heading)
        if not detail.entries:
            empty = self.Adw.StatusPage(
                title="No user-visible heartbeats",
                description="This session still has a journal, but no eligible progress entries were found.",
                icon_name="dialog-information-symbolic",
            )
            empty.set_vexpand(False)
            self.content_box.append(empty)
        else:
            timeline = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=8)
            previous_date = None
            target_index = self.model.matching_entry(session.session_id)
            if target_index is None:
                target_index = min(self.current_entry_index, max(0, len(detail.entries) - 1))
            target_widget = None
            for presented in present_timeline(detail):
                if presented.local_date != previous_date:
                    date = self.Gtk.Label(label=presented.date_label, xalign=0)
                    date.add_css_class("heading")
                    date.set_margin_top(8)
                    timeline.append(date)
                    previous_date = presented.local_date
                widget = self._timeline_row(session, presented)
                if presented.entry.index == target_index:
                    widget.add_css_class("accent")
                    target_widget = widget
                self._timeline_widgets[presented.entry.index] = widget
                timeline.append(widget)
            self.content_box.append(timeline)
            if target_widget is not None:
                self.GLib.idle_add(target_widget.grab_focus)

        details = self.Gtk.Expander(label="Session details and provenance summary")
        details.set_child(self._details_grid(session))
        self.details_expander = details
        self.content_box.append(details)
        relationships = self._relationship_box(session)
        if relationships is not None:
            self.content_box.append(relationships)
        self.content_box.append(self._notes_expander(detail))
        if detail.extraction_errors:
            errors = self.Gtk.Expander(
                label=f"Extraction errors ({len(detail.extraction_errors)})"
            )
            error_box = self.Gtk.Box(
                orientation=self.Gtk.Orientation.VERTICAL,
                spacing=6,
                margin_top=8,
                margin_bottom=8,
                margin_start=10,
                margin_end=10,
            )
            for error in detail.extraction_errors:
                label = self.Gtk.Label(
                    label=f"Record {error.sequence}: {error.code}", xalign=0, selectable=True
                )
                label.add_css_class("warning")
                error_box.append(label)
            errors.set_child(error_box)
            self.content_box.append(errors)
        self._show_state("content")

    def _compare_recent(self) -> None:
        if len(self._recent_session_ids) < 2:
            self._set_action_status(
                "View two different sessions before opening comparison.", warning=True
            )
            return
        left_id, right_id = self._recent_session_ids[-2:]
        if left_id == right_id:
            self._set_action_status("Comparison requires two different sessions.", warning=True)
            return
        try:
            report = compare_details(
                self.catalog.load_detail(left_id), self.catalog.load_detail(right_id)
            )
        except (CatalogError, ValueError):
            self._set_action_status("One generated session failed comparison validation.", warning=True)
            return
        self._comparison_report = report
        self._set_selection_mode(False)
        self.current_detail = None
        self.details_expander = None
        self.main_title.set_title("Session comparison")
        self.main_title.set_subtitle("Two recently viewed generated journals")
        self._clear_box(self.content_box)
        title = self.Gtk.Label(label="Session comparison", xalign=0)
        title.add_css_class("title-1")
        self.content_box.append(title)
        explanation = self.Gtk.Label(
            label=(
                "Exact normalized text only · unchanged rows match exactly · "
                "left-only and right-only rows do not imply causality."
            ),
            xalign=0,
            wrap=True,
        )
        explanation.add_css_class("dim-label")
        self.content_box.append(explanation)
        self.content_box.append(self._comparison_metadata(report))
        filter_row = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=8)
        filter_row.append(self.Gtk.Label(label="Timeline tag", xalign=0))
        self.comparison_tag = self.Gtk.DropDown.new_from_strings([ALL, *TAGS])
        self.comparison_tag.connect("notify::selected", self._on_comparison_filter)
        filter_row.append(self.comparison_tag)
        self.content_box.append(filter_row)
        self.comparison_timeline = self.Gtk.Box(
            orientation=self.Gtk.Orientation.VERTICAL, spacing=8
        )
        self.content_box.append(self.comparison_timeline)
        self._render_comparison_timeline(None)
        self._show_state("content")

    def _comparison_metadata(self, report: ComparisonReport) -> object:
        grid = self.Gtk.Grid(column_spacing=12, row_spacing=7)
        for column, label in enumerate(("Field", "Earlier viewed", "Later viewed")):
            heading = self.Gtk.Label(label=label, xalign=0)
            heading.add_css_class("heading")
            grid.attach(heading, column, 0, 1, 1)
        for row_index, item in enumerate(report.metadata, 1):
            for column, value in enumerate((item.label, item.left, item.right)):
                label = self.Gtk.Label(label=value, xalign=0, wrap=True, selectable=True)
                label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                label.set_hexpand(column > 0)
                if column == 0:
                    label.add_css_class("dim-label")
                grid.attach(label, column, row_index, 1, 1)
        return grid

    def _on_comparison_filter(self, dropdown: object, _pspec: object) -> None:
        self._render_comparison_timeline(self._selected_text(dropdown))

    def _render_comparison_timeline(self, tag: str | None) -> None:
        report = self._comparison_report
        if report is None or not hasattr(self, "comparison_timeline"):
            return
        self._clear_box(self.comparison_timeline)
        rows = filter_timeline(report.timeline, None if tag in (None, ALL) else tag)
        if not rows:
            empty = self.Gtk.Label(
                label="No timeline entries match this deterministic filter.", xalign=0
            )
            empty.add_css_class("dim-label")
            self.comparison_timeline.append(empty)
            return
        for row in rows:
            grid = self.Gtk.Grid(column_spacing=12, row_spacing=4)
            grid.add_css_class("card")
            kind = self.Gtk.Label(label=row.kind, xalign=0)
            kind.add_css_class("caption")
            grid.attach(kind, 0, 0, 2, 1)
            grid.attach(self._comparison_entry(row.left, "Left"), 0, 1, 1, 1)
            grid.attach(self._comparison_entry(row.right, "Right"), 1, 1, 1, 1)
            self.comparison_timeline.append(grid)

    def _comparison_entry(self, entry: object | None, side: str) -> object:
        if entry is None:
            label = self.Gtk.Label(label=f"{side}: —", xalign=0)
            label.add_css_class("dim-label")
            return label
        label = self.Gtk.Label(
            label=f"{entry.display_time}  {entry.text}",
            xalign=0,
            wrap=True,
            selectable=True,
        )
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_hexpand(True)
        return label

    def _open_activity(self) -> None:
        if self._activity_report is None:
            if self._activity_running:
                return
            self._activity_running = True
            self._set_selection_mode(False)
            self.current_detail = None
            self.details_expander = None
            self.main_title.set_title("Journal activity")
            self.main_title.set_subtitle("Generated sessions and visible entries only")
            self._clear_box(self.content_box)
            loading = self.Adw.StatusPage(
                title="Building journal activity",
                description="Counting generated sessions and visible entries in a worker.",
                icon_name="content-loading-symbolic",
            )
            loading.set_vexpand(True)
            self.content_box.append(loading)
            self._show_state("content")
            Thread(target=self._activity_worker, daemon=True).start()
            return
        self._build_activity_view()

    def _activity_worker(self) -> None:
        try:
            catalog = JournalCatalog(self.repo_root)
            catalog.refresh()
            report = build_activity_report(catalog)
            self.GLib.idle_add(self._finish_activity, report, None)
        except Exception as exc:  # worker boundary reports no private path or content
            self.GLib.idle_add(self._finish_activity, None, type(exc).__name__)

    def _finish_activity(
        self, report: ActivityReport | None, failure: str | None
    ) -> bool:
        self._activity_running = False
        if self._closed:
            return False
        if report is None:
            self._clear_box(self.content_box)
            error = self.Adw.StatusPage(
                title="Activity view unavailable",
                description=f"Generated activity failed safely ({failure or 'unknown error'}).",
                icon_name="dialog-warning-symbolic",
            )
            self.content_box.append(error)
            return False
        self._activity_report = report
        self._build_activity_view()
        return False

    def _build_activity_view(self) -> None:
        self._set_selection_mode(False)
        self.current_detail = None
        self.details_expander = None
        self.main_title.set_title("Journal activity")
        self.main_title.set_subtitle("Generated sessions and visible entries only")
        self._clear_box(self.content_box)
        title = self.Gtk.Label(label="Journal activity", xalign=0)
        title.add_css_class("title-1")
        self.content_box.append(title)
        notice = self.Gtk.Label(
            label=(
                "Deterministic counts from generated sessions and visible entries only. "
                "No productivity score, sentiment, or hidden-activity inference."
            ),
            xalign=0,
            wrap=True,
        )
        notice.add_css_class("dim-label")
        self.content_box.append(notice)
        controls = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=10)
        controls.append(self.Gtk.Label(label="Period", xalign=0))
        self.activity_period = self.Gtk.DropDown.new_from_strings(["Daily", "Weekly"])
        self.activity_period.connect("notify::selected", self._on_activity_filter)
        controls.append(self.activity_period)
        controls.append(self.Gtk.Label(label="Project calendar", xalign=0))
        projects = [calendar.project for calendar in self._activity_report.projects]
        self.activity_project = self.Gtk.DropDown.new_from_strings([ALL, *projects])
        self.activity_project.connect("notify::selected", self._on_activity_filter)
        controls.append(self.activity_project)
        self.content_box.append(controls)
        self.activity_list = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=8)
        self.content_box.append(self.activity_list)
        self._render_activity()
        self._show_state("content")

    def _on_activity_filter(self, *_args: object) -> None:
        self._render_activity()

    def _render_activity(self) -> None:
        report = self._activity_report
        if report is None or not hasattr(self, "activity_list"):
            return
        self._clear_box(self.activity_list)
        project = self._selected_text(self.activity_project)
        period = self._selected_text(self.activity_period)
        if project and project != ALL:
            calendar = next(item for item in report.projects if item.project == project)
            buckets = calendar.days
            heading = f"{project} · project calendar"
        elif period == "Weekly":
            buckets = report.weeks
            heading = "All projects · ISO weeks"
        elif report.days:
            ascending = fill_daily_range(report, report.days[-1].key, report.days[0].key)
            buckets = tuple(reversed(ascending))
            heading = "All projects · calendar days"
        else:
            buckets = ()
            heading = "No generated activity"
        label = self.Gtk.Label(label=heading, xalign=0)
        label.add_css_class("title-2")
        self.activity_list.append(label)
        if not buckets:
            empty = self.Gtk.Label(
                label="This period has no generated sessions or visible entries.", xalign=0
            )
            empty.add_css_class("dim-label")
            self.activity_list.append(empty)
            return
        for bucket in buckets:
            self.activity_list.append(self._activity_row(bucket))

    def _activity_row(self, bucket: ActivityBucket) -> object:
        row = self.Gtk.Box(
            orientation=self.Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_top=8,
            margin_bottom=8,
            margin_start=10,
            margin_end=10,
        )
        row.add_css_class("card")
        body = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=4)
        body.set_hexpand(True)
        title = self.Gtk.Label(label=bucket.key, xalign=0)
        title.add_css_class("heading")
        body.append(title)
        statuses = ", ".join(f"{name} {count}" for name, count in bucket.statuses) or "no sessions"
        body.append(
            self.Gtk.Label(
                label=(
                    f"{len(bucket.session_ids)} session(s) · {bucket.entries} visible entries · "
                    f"{statuses}"
                ),
                xalign=0,
                wrap=True,
            )
        )
        projects = ", ".join(f"{name} {count}" for name, count in bucket.projects)
        tags = ", ".join(f"{name} {count}" for name, count in bucket.tags)
        details = self.Gtk.Label(
            label=f"Projects: {projects or 'none'}\nTags: {tags or 'none'}",
            xalign=0,
            wrap=True,
        )
        details.add_css_class("caption")
        body.append(details)
        row.append(body)
        open_button = self.Gtk.Button(label="View sessions")
        open_button.set_sensitive(bool(bucket.session_ids))
        open_button.connect("clicked", lambda _button, item=bucket: self._navigate_activity(item))
        row.append(open_button)
        return row

    def _navigate_activity(self, bucket: ActivityBucket) -> None:
        self.current_entry_index = 0
        self.search_entry.set_text("")
        self.model = SessionBrowserModel(self.catalog)
        self.model.set_bookmarked_session_ids(self.annotations.bookmarked_session_ids())
        self.model.set_session_subset(frozenset(bucket.session_ids))
        self._hits_by_session.clear()
        self._populate_sessions()
        if self.split.get_collapsed():
            self.split.set_show_content(False)

    def _available_export_scopes(self) -> tuple[str, ...]:
        scopes: list[str] = []
        if self.current_detail is not None and self.current_detail.entries:
            scopes.append("Current entry")
        if self.current_detail is not None and self._selected_entry_indexes:
            scopes.extend(("Checked entries", "Checked time range"))
        if self._comparison_report is not None:
            scopes.append("Comparison results")
        if self._activity_report is not None:
            scopes.append("Activity view")
        return tuple(scopes)

    def _open_export_preview(self) -> None:
        scopes = self._available_export_scopes()
        if not scopes:
            self._set_action_status(
                "Select timeline entries, build a comparison, or open activity before export.",
                warning=True,
            )
            return
        dialog = self.Adw.Dialog()
        dialog.set_title("Review export")
        toolbar = self.Adw.ToolbarView()
        header = self.Adw.HeaderBar()
        cancel = self.Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_args: dialog.close())
        header.pack_start(cancel)
        choose = self.Gtk.Button(label="Choose destination…")
        choose.add_css_class("suggested-action")
        choose.connect("clicked", lambda *_args: self._choose_export_destination(dialog))
        header.pack_end(choose)
        toolbar.add_top_bar(header)
        content = self.Gtk.Box(
            orientation=self.Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        controls = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.append(self.Gtk.Label(label="Scope"))
        self.export_scope = self.Gtk.DropDown.new_from_strings(list(scopes))
        self.export_scope.connect("notify::selected", self._update_export_preview)
        controls.append(self.export_scope)
        controls.append(self.Gtk.Label(label="Format"))
        self.export_format = self.Gtk.DropDown.new_from_strings(["Markdown", "JSON"])
        self.export_format.connect("notify::selected", self._update_export_preview)
        controls.append(self.export_format)
        self.export_notes = self.Gtk.CheckButton(
            label="Include private notes (explicit opt-in)"
        )
        self.export_notes.connect("toggled", self._update_export_preview)
        controls.append(self.export_notes)
        content.append(controls)
        warning = self.Gtk.Label(
            label=(
                "Generated journals may still contain private project information. "
                "Review every item below before export."
            ),
            xalign=0,
            wrap=True,
        )
        warning.add_css_class("warning")
        content.append(warning)
        self.export_preview = self.Gtk.TextView(
            editable=False,
            cursor_visible=False,
            monospace=True,
            wrap_mode=self.Gtk.WrapMode.WORD_CHAR,
        )
        preview_scroll = self.Gtk.ScrolledWindow(vexpand=True, min_content_height=420)
        preview_scroll.set_child(self.export_preview)
        content.append(preview_scroll)
        toolbar.set_content(content)
        dialog.set_child(toolbar)
        self._update_export_preview()
        dialog.present(self.window)

    def _build_export_document(self) -> ExportDocument:
        scope = self._selected_text(self.export_scope)
        if scope == "Current entry":
            document = selected_entries_document(
                self.current_detail, {self.current_entry_index}
            )
        elif scope == "Checked entries":
            document = selected_entries_document(
                self.current_detail, set(self._selected_entry_indexes)
            )
        elif scope == "Checked time range":
            document = selected_entries_document(
                self.current_detail,
                set(self._selected_entry_indexes),
                inclusive_range=True,
            )
        elif scope == "Comparison results" and self._comparison_report is not None:
            document = comparison_document(self._comparison_report)
        elif scope == "Activity view" and self._activity_report is not None:
            session_ids = sorted(
                {
                    session_id
                    for bucket in self._activity_report.days
                    for session_id in bucket.session_ids
                }
            )
            details = tuple(self.catalog.load_detail(session_id, cache=False) for session_id in session_ids)
            document = activity_document(self._activity_report, details)
        else:
            raise ValueError("The selected export scope is no longer available.")
        return (
            include_private_notes(document, self.annotations)
            if self.export_notes.get_active()
            else document
        )

    def _update_export_preview(self, *_args: object) -> None:
        try:
            self._export_document = self._build_export_document()
            preview = render_preview(self._export_document)
        except (CatalogError, ValueError) as exc:
            self._export_document = None
            preview = f"Export unavailable: {exc}"
        self.export_preview.get_buffer().set_text(preview)

    def _choose_export_destination(self, preview_dialog: object) -> None:
        self._update_export_preview()
        if self._export_document is None:
            return
        format_name = self._selected_text(self.export_format) or "Markdown"
        self._export_format_name = format_name.lower()
        extension = ".md" if format_name == "Markdown" else ".json"
        file_dialog = self.Gtk.FileDialog(title="Choose reviewed export destination")
        file_dialog.set_initial_name(f"heartbeat-export{extension}")
        file_dialog.save(self.window, None, self._export_destination_chosen)
        preview_dialog.close()

    def _export_destination_chosen(self, dialog: object, result: object) -> None:
        try:
            selected = dialog.save_finish(result)
            path_value = selected.get_path()
            if not path_value:
                raise ValueError("Only a local export destination is supported.")
            destination = Path(path_value)
            content = render_export(self._export_document, self._export_format_name)
        except Exception:
            return
        if destination.exists():
            self._pending_export = (destination, content)
            confirmation = self.Adw.AlertDialog.new(
                "Replace existing export?",
                "The chosen local file already exists. Replacement will be atomic.",
            )
            confirmation.add_response("cancel", "Cancel")
            confirmation.add_response("replace", "Replace")
            confirmation.set_response_appearance(
                "replace", self.Adw.ResponseAppearance.DESTRUCTIVE
            )
            confirmation.set_default_response("cancel")
            confirmation.set_close_response("cancel")
            confirmation.choose(self.window, None, self._export_overwrite_chosen)
            return
        self._write_export(destination, content, overwrite=False)

    def _export_overwrite_chosen(self, dialog: object, result: object) -> None:
        try:
            response = dialog.choose_finish(result)
        except Exception:
            response = "cancel"
        pending = self._pending_export
        self._pending_export = None
        if response == "replace" and pending is not None:
            self._write_export(*pending, overwrite=True)

    def _write_export(self, destination: Path, content: bytes, *, overwrite: bool) -> None:
        try:
            write_export_atomic(destination, content, overwrite=overwrite)
        except (OSError, ValueError):
            self.sync_status.set_label("Export failed safely; no partial target was retained.")
            return
        self.sync_status.set_label(
            f"Exported {len(content)} reviewed byte(s) to the chosen local destination."
        )

    def _timeline_row(self, session: CatalogSession, presented: PresentedEntry) -> object:
        row = self.Gtk.Expander()
        row.add_css_class("card")
        compact = self.timeline_density == "compact"
        heading = self.Gtk.Box(
            orientation=self.Gtk.Orientation.HORIZONTAL,
            spacing=8 if compact else 12,
            margin_top=5 if compact else 10,
            margin_bottom=5 if compact else 10,
            margin_start=12,
            margin_end=12,
        )
        selected = self.Gtk.CheckButton()
        self._accessible(selected, f"Include journal entry at {presented.display_time} in copy range")
        selected.connect("toggled", self._on_entry_selected, presented.entry.index)
        selected.set_visible(self.selection_mode)
        self._selection_checks[presented.entry.index] = selected
        timestamp = self.Gtk.Label(label=presented.display_time, xalign=0, yalign=0)
        timestamp.add_css_class("monospace")
        timestamp.add_css_class("dim-label")
        timestamp.set_size_request(52, -1)
        body = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=5)
        body.set_hexpand(True)
        message = self.Gtk.Label(
            xalign=0, wrap=True, selectable=True
        )
        message.set_markup(safe_inline_markup(presented.entry.text))
        message.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        body.append(message)
        labels = (*presented.tags, *presented.indicators)
        target = AnnotationTarget(session.session_id, presented.entry.source_event_sequence)
        if self.annotations.is_bookmarked(target):
            labels = (*labels, "★ bookmarked")
        if labels:
            badges = self.Gtk.FlowBox(
                selection_mode=self.Gtk.SelectionMode.NONE,
                column_spacing=5,
                row_spacing=5,
                max_children_per_line=8,
                min_children_per_line=1,
                homogeneous=False,
                halign=self.Gtk.Align.START,
            )
            for value in labels:
                badge = self.Gtk.Label(label=value)
                badge.add_css_class("caption")
                badge.set_margin_top(2)
                badge.set_margin_bottom(2)
                badge.set_margin_start(6)
                badge.set_margin_end(6)
                if value in {"failure", "security", "blocker", "stop", "redacted"}:
                    badge.add_css_class("warning")
                elif value in {"correction", "test"}:
                    badge.add_css_class("success")
                frame = self.Gtk.Frame()
                frame.add_css_class("card")
                frame.set_child(badge)
                badges.append(frame)
            body.append(badges)
        heading.append(selected)
        heading.append(timestamp)
        heading.append(body)
        row.set_label_widget(heading)
        row.set_child(self._provenance_grid(session, presented))
        row.connect("notify::expanded", self._on_entry_expanded, presented.entry.index)
        return row

    def _on_entry_selected(self, button: object, entry_index: int) -> None:
        if self._updating_selection:
            return
        if button.get_active():
            self._selected_entry_indexes.add(entry_index)
        else:
            self._selected_entry_indexes.discard(entry_index)
        self._update_selection_count()

    def _on_selection_mode_toggled(self, button: object) -> None:
        if not self._updating_selection:
            self._set_selection_mode(bool(button.get_active()))

    def _set_selection_mode(self, enabled: bool) -> None:
        enabled = bool(enabled and self.current_detail is not None and self.current_detail.entries)
        self.selection_mode = enabled
        self._updating_selection = True
        try:
            if self.select_mode_button.get_active() != enabled:
                self.select_mode_button.set_active(enabled)
            for checkbox in self._selection_checks.values():
                checkbox.set_visible(enabled)
                if not enabled:
                    checkbox.set_active(False)
            if not enabled:
                self._selected_entry_indexes.clear()
        finally:
            self._updating_selection = False
        self.selection_bar.set_reveal_child(enabled)
        self._update_selection_count()

    def _update_selection_count(self) -> None:
        count = len(self._selected_entry_indexes)
        self.selection_count.set_label(f"{count} selected")
        self.copy_selection_button.set_sensitive(count > 0)

    def _set_action_status(self, message: str, *, warning: bool = False) -> None:
        if self.action_status is None:
            return
        self.action_status.set_label(message)
        if warning:
            self.action_status.add_css_class("warning")
        else:
            self.action_status.remove_css_class("warning")

    def _open_project(self) -> None:
        session = self.model.selected
        try:
            uri = project_directory_uri(
                session.working_directory if session else None, home=Path.home()
            )
            self.Gio.AppInfo.launch_default_for_uri(uri, None)
            self._set_action_status("Opened the validated local project directory.")
        except ProjectPathError as exc:
            self._set_action_status(str(exc), warning=True)
        except Exception:
            self._set_action_status("The desktop file manager could not open the directory.", warning=True)

    def _copy_current_entry(self) -> None:
        detail = self.current_detail
        if detail is None or not detail.entries:
            self._set_action_status("No sanitized timeline entry is available to copy.", warning=True)
            return
        entry = next(
            (item for item in detail.entries if item.index == self.current_entry_index),
            detail.entries[0],
        )
        payload = copy_one_entry(entry)
        self.window.get_clipboard().set(payload.text)
        self._set_action_status("Copied 1 sanitized journal entry with its timestamp.")

    def _copy_selected_range(self) -> None:
        detail = self.current_detail
        if detail is not None and not self._selected_entry_indexes:
            self._set_selection_mode(True)
            self._set_action_status("Select the journal entries to copy, then choose Copy selected.")
            return
        try:
            if detail is None:
                raise ValueError("No timeline entries are selected.")
            payload = copy_selected_range(detail.entries, self._selected_entry_indexes)
        except ValueError as exc:
            self._set_action_status(str(exc), warning=True)
            return
        self.window.get_clipboard().set(payload.text)
        self._set_action_status(
            f"Copied {payload.entry_count} sanitized journal entries with timestamps."
        )
        self._set_selection_mode(False)

    def _current_entry_target(self) -> AnnotationTarget | None:
        detail = self.current_detail
        if detail is None:
            return None
        entry = next(
            (item for item in detail.entries if item.index == self.current_entry_index), None
        )
        return (
            AnnotationTarget(detail.session.session_id, entry.source_event_sequence)
            if entry is not None
            else None
        )

    def _toggle_entry_bookmark(self) -> None:
        target = self._current_entry_target()
        if target is None:
            self._set_action_status("No exact timeline entry is available to bookmark.", warning=True)
            return
        bookmarked = self.annotations.toggle_bookmark(target)
        self._after_bookmark_change(
            "Bookmarked current timeline entry." if bookmarked else "Removed entry bookmark."
        )

    def _toggle_session_bookmark(self) -> None:
        session = self.model.selected
        if session is None:
            self._set_action_status("No session is available to bookmark.", warning=True)
            return
        bookmarked = self.annotations.toggle_bookmark(AnnotationTarget(session.session_id))
        self._after_bookmark_change(
            "Bookmarked current session." if bookmarked else "Removed session bookmark."
        )

    def _after_bookmark_change(self, message: str) -> None:
        selected = self.model.selected_session_id
        self.model.set_bookmarked_session_ids(self.annotations.bookmarked_session_ids())
        self._populate_sessions()
        if selected and selected in {item.session_id for item in self.model.sessions}:
            self.model.select(selected)
        self._set_action_status(message)

    def _notes_expander(self, detail: object) -> object:
        expander = self.Gtk.Expander(label="Private notes · local annotation database only")
        outer = self.Gtk.Box(
            orientation=self.Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=10,
            margin_bottom=10,
            margin_start=10,
            margin_end=10,
        )
        session_target = AnnotationTarget(detail.session.session_id)
        session_note = self.annotations.get_note(session_target)
        self.session_note_view = self._note_editor(session_note.text if session_note else "")
        outer.append(self.Gtk.Label(label="Session note", xalign=0))
        outer.append(self.session_note_view)
        outer.append(self._note_buttons("session"))

        self.entry_note_label = self.Gtk.Label(label="Current entry note", xalign=0)
        outer.append(self.entry_note_label)
        self.entry_note_view = self._note_editor("")
        outer.append(self.entry_note_view)
        outer.append(self._note_buttons("entry"))
        expander.set_child(outer)
        self._load_entry_note()
        return expander

    def _note_editor(self, text: str) -> object:
        view = self.Gtk.TextView(
            wrap_mode=self.Gtk.WrapMode.WORD_CHAR,
            top_margin=8,
            bottom_margin=8,
            left_margin=8,
            right_margin=8,
            height_request=84,
        )
        view.get_buffer().set_text(text)
        view.add_css_class("card")
        return view

    def _note_buttons(self, scope: str) -> object:
        buttons = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=8)
        save = self.Gtk.Button(label=f"Save {scope} note")
        save.connect("clicked", lambda _button: self._save_note(scope))
        delete = self.Gtk.Button(label=f"Delete {scope} note")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda button: self._delete_note(scope, button))
        buttons.append(save)
        buttons.append(delete)
        return buttons

    def _note_target(self, scope: str) -> AnnotationTarget | None:
        session = self.model.selected
        if session is None:
            return None
        return AnnotationTarget(session.session_id) if scope == "session" else self._current_entry_target()

    def _note_view(self, scope: str) -> object:
        return self.session_note_view if scope == "session" else self.entry_note_view

    def _note_text(self, scope: str) -> str:
        buffer = self._note_view(scope).get_buffer()
        start, end = buffer.get_bounds()
        return buffer.get_text(start, end, True)

    def _save_note(self, scope: str) -> None:
        target = self._note_target(scope)
        if target is None:
            self._set_action_status("No exact annotation target is available.", warning=True)
            return
        try:
            note = self.annotations.save_note(target, self._note_text(scope))
        except ValueError as exc:
            self._set_action_status(str(exc), warning=True)
            return
        self._note_view(scope).get_buffer().set_text(note.text)
        self._pending_note_delete = None
        self._set_action_status(f"Saved private {scope} note locally.")

    def _delete_note(self, scope: str, button: object) -> None:
        target = self._note_target(scope)
        if target is None or self.annotations.get_note(target) is None:
            self._set_action_status(f"No private {scope} note exists to delete.", warning=True)
            return
        if self._pending_note_delete != target:
            self._pending_note_delete = target
            button.set_label("Confirm delete")
            self._set_action_status(
                f"Press Confirm delete to remove this exact private {scope} note.", warning=True
            )
            return
        self.annotations.delete_note(target)
        self._note_view(scope).get_buffer().set_text("")
        self._pending_note_delete = None
        button.set_label(f"Delete {scope} note")
        self._set_action_status(f"Deleted private {scope} note.")

    def _load_entry_note(self) -> None:
        if not hasattr(self, "entry_note_view"):
            return
        target = self._current_entry_target()
        note = self.annotations.get_note(target) if target else None
        self.entry_note_view.get_buffer().set_text(note.text if note else "")
        self.entry_note_view.set_sensitive(target is not None)
        self._pending_note_delete = None

    def _relationship_box(self, session: CatalogSession) -> object | None:
        parent = self.catalog.parent_of(session.session_id)
        children = self.catalog.children_of(session.session_id)
        if parent is None and not children:
            return None
        section = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=6)
        heading = self.Gtk.Label(label="Related sessions", xalign=0)
        heading.add_css_class("heading")
        section.append(heading)
        if parent is not None:
            button = self.Gtk.Button(label=f"Parent · {parent.session_id}")
            button.set_halign(self.Gtk.Align.START)
            button.connect("clicked", lambda _button, target=parent.session_id: self._navigate_related(target))
            section.append(button)
        for child in children:
            button = self.Gtk.Button(label=f"Sub-agent · {child.session_id}")
            button.set_halign(self.Gtk.Align.START)
            button.connect("clicked", lambda _button, target=child.session_id: self._navigate_related(target))
            section.append(button)
        return section

    def _navigate_related(self, session_id: str) -> None:
        self.current_entry_index = 0
        self.search_entry.set_text("")
        for dropdown in self._filter_widgets.values():
            dropdown.set_selected(0)
        self.redacted_check.set_active(False)
        self.errors_check.set_active(False)
        self.model = SessionBrowserModel(self.catalog)
        self.model.set_bookmarked_session_ids(self.annotations.bookmarked_session_ids())
        self._hits_by_session.clear()
        self.model.select(session_id)
        self._populate_sessions()

    def _on_entry_expanded(
        self, row: object, _pspec: object, entry_index: int
    ) -> None:
        if row.get_expanded():
            self.current_entry_index = entry_index
            self._load_entry_note()

    def _provenance_grid(self, session: CatalogSession, presented: PresentedEntry) -> object:
        entry = presented.entry
        values = (
            ("Source session", session.session_id),
            ("Event sequence", str(entry.source_event_sequence)),
            ("Original UTC", entry.original_timestamp_utc),
            ("Original-text SHA-256", entry.original_text_sha256),
            ("Normalized text", entry.text),
            ("Redacted", "yes" if entry.redacted else "no"),
        )
        return self._key_value_grid(values)

    def _details_grid(self, session: CatalogSession) -> object:
        parent = self.catalog.parent_of(session.session_id)
        children = self.catalog.children_of(session.session_id)
        values = (
            ("Session", session.session_id),
            ("Status", session.status),
            ("Started", session.started_at_utc),
            ("Ended", session.ended_at_utc or "Not recorded"),
            ("Timezone", session.rendered_timezone),
            ("Working directory", session.working_directory or "Not recorded"),
            ("Repository", session.repository or "Not recorded"),
            ("Branch", session.branch or "Not recorded"),
            ("Source kind", session.source_kind),
            ("Parent session", parent.session_id if parent else "Not linked"),
            (
                "Child sessions",
                ", ".join(child.session_id for child in children) if children else "None linked",
            ),
            ("Timeline entries", str(session.entry_count)),
            ("Redactions", str(session.redaction_count)),
            ("Extraction errors", str(session.extraction_error_count)),
            ("Fingerprint", session.source_fingerprint),
        )
        return self._key_value_grid(values)

    def _key_value_grid(self, values: tuple[tuple[str, str], ...]) -> object:
        grid = self.Gtk.Grid(
            column_spacing=16,
            row_spacing=8,
            margin_top=10,
            margin_bottom=10,
            margin_start=10,
            margin_end=10,
        )
        for index, (label, value) in enumerate(values):
            key = self.Gtk.Label(label=label, xalign=1, yalign=0)
            key.add_css_class("dim-label")
            content = self.Gtk.Label(label=value, xalign=0, wrap=True, selectable=True)
            content.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            content.set_hexpand(True)
            grid.attach(key, 0, index, 1, 1)
            grid.attach(content, 1, index, 1, 1)
        return grid
