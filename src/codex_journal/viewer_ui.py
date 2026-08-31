from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .viewer_catalog import (
    CatalogError,
    CatalogSession,
    JournalCatalog,
    JournalSearchIndex,
    SearchFilters,
    SearchHit,
)
from .viewer_model import ALL, SessionBrowserModel, display_start, session_badges
from .viewer_presenter import PresentedEntry, present_timeline
from .viewer_tags import TAGS


class JournalWindow:
    """Native, adaptive browser over generated journal artifacts only."""

    def __init__(self, application: object, repo_root: Path, modules: tuple[Any, ...]) -> None:
        self.Adw, self.Gio, self.GLib, self.Gtk = modules
        self.repo_root = repo_root
        self.catalog = JournalCatalog(repo_root)
        self.model = SessionBrowserModel(self.catalog)
        self._session_rows: dict[object, str] = {}
        self._filter_widgets: dict[str, object] = {}
        self._hits_by_session: dict[str, SearchHit] = {}
        self.search_index: JournalSearchIndex | None = None
        self._loading = False

        self.window = self.Adw.ApplicationWindow(application=application)
        self.window.set_title("Heartbeat Extractor")
        self.window.set_default_size(1180, 760)

        self.split = self.Adw.NavigationSplitView()
        self.split.set_min_sidebar_width(280)
        self.split.set_max_sidebar_width(410)
        self.split.set_sidebar_width_fraction(0.34)
        self.split.set_sidebar(self.Adw.NavigationPage.new(self._build_sidebar(), "Sessions"))
        self.split.set_content(self.Adw.NavigationPage.new(self._build_main(), "Journal"))
        self.window.set_content(self.split)
        self.window.connect("close-request", self._on_close)

        breakpoint = self.Adw.Breakpoint.new(
            self.Adw.BreakpointCondition.parse("max-width: 1000px")
        )
        breakpoint.add_setter(self.split, "collapsed", True)
        self.window.add_breakpoint(breakpoint)

        self._show_state("loading")
        self.GLib.idle_add(self.refresh_catalog)

    def present(self) -> None:
        self.window.present()

    def _build_sidebar(self) -> object:
        toolbar = self.Adw.ToolbarView()
        header = self.Adw.HeaderBar()
        title = self.Adw.WindowTitle(title="Sessions", subtitle="Generated journals only")
        header.set_title_widget(title)
        toolbar.add_top_bar(header)

        outer = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=0)
        self.search_entry = self.Gtk.SearchEntry(
            placeholder_text="Search safe journals",
            margin_top=12,
            margin_start=12,
            margin_end=12,
        )
        self.search_entry.connect("search-changed", self._on_search_changed)
        outer.append(self.search_entry)
        filters = self.Gtk.Box(
            orientation=self.Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        for field, label in (
            ("project", "Project"),
            ("date_from", "From"),
            ("date_to", "To"),
            ("branch", "Branch"),
            ("status", "Status"),
            ("source_kind", "Source"),
            ("tag", "Tag"),
        ):
            row = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=8)
            caption = self.Gtk.Label(label=label, xalign=0)
            caption.set_size_request(58, -1)
            dropdown = self.Gtk.DropDown.new_from_strings([ALL])
            dropdown.set_hexpand(True)
            dropdown.connect("notify::selected", self._on_filter_changed, field)
            self._filter_widgets[field] = dropdown
            row.append(caption)
            row.append(dropdown)
            filters.append(row)
        self.redacted_check = self.Gtk.CheckButton(label="Has redactions")
        self.redacted_check.connect("toggled", self._on_boolean_filter, "redacted_only")
        filters.append(self.redacted_check)
        self.errors_check = self.Gtk.CheckButton(label="Has extraction errors")
        self.errors_check.connect(
            "toggled", self._on_boolean_filter, "extraction_errors_only"
        )
        filters.append(self.errors_check)
        outer.append(filters)

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
        back = self.Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back to sessions")
        back.connect("clicked", lambda *_args: self.split.set_show_content(False))
        header.pack_start(back)
        toolbar.add_top_bar(header)

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
        self.main_stack.set_visible_child_name(name)

    def refresh_catalog(self) -> bool:
        if self._loading:
            return False
        self._loading = True
        try:
            self.catalog.refresh()
            self.model = SessionBrowserModel(self.catalog)
            if self.search_index is not None:
                self.search_index.close()
            self.search_index = JournalSearchIndex(self.repo_root / "state" / "viewer.sqlite3")
            self.search_index.rebuild(self.catalog)
            self._populate_filters()
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
        finally:
            self._loading = False
        return False

    def _on_close(self, *_args: object) -> bool:
        if self.search_index is not None:
            self.search_index.close()
            self.search_index = None
        return False

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
        if self._loading:
            return
        self.model.set_filter(field, self._selected_text(dropdown))
        self._apply_search()
        self._populate_sessions()

    def _on_boolean_filter(self, button: object, field: str) -> None:
        if self._loading:
            return
        self.model.set_filter(field, bool(button.get_active()))
        self._populate_sessions()

    def _on_search_changed(self, _entry: object) -> None:
        if self._loading:
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
            filters=SearchFilters(tags=(tag,) if tag else ()),
            limit=1000,
        ) if active else ()
        self.model.set_search_hits(hits, active=active)
        self._hits_by_session = {}
        for hit in hits:
            self._hits_by_session.setdefault(hit.session_id, hit)

    def _clear_box(self, box: object) -> None:
        while child := box.get_first_child():
            box.remove(child)

    def _populate_sessions(self) -> None:
        self._session_rows.clear()
        self._clear_box(self.session_list)
        sessions = self.model.sessions
        counts = self.model.counts
        self.count_label.set_label(f"{counts.visible} of {counts.total} sessions")
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
        self.model.select(session_id)
        self._render_session(session_id)
        if self.split.get_collapsed():
            self.split.set_show_content(True)

    def _render_session(self, session_id: str) -> None:
        try:
            detail = self.catalog.load_detail(session_id)
        except CatalogError:
            self.error_page.set_title("Generated journal is malformed")
            self.error_page.set_description(
                "The selected generated artifact failed validation and was not displayed. "
                "Private source logs were not opened."
            )
            self._show_state("error")
            return
        self._clear_box(self.content_box)
        session = detail.session
        title = self.Gtk.Label(label=session.project, xalign=0, selectable=True)
        title.add_css_class("title-1")
        self.content_box.append(title)
        subtitle = self.Gtk.Label(
            label=f"{display_start(session)} · {session.branch or 'No branch'} · {session.status}",
            xalign=0,
            selectable=True,
        )
        subtitle.add_css_class("dim-label")
        self.content_box.append(subtitle)
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
                    widget.set_expanded(True)
                    target_widget = widget
                timeline.append(widget)
            self.content_box.append(timeline)
            if target_widget is not None:
                self.GLib.idle_add(target_widget.grab_focus)

        details = self.Gtk.Expander(label="Session details and provenance summary")
        details.set_child(self._details_grid(session))
        self.content_box.append(details)
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

    def _timeline_row(self, session: CatalogSession, presented: PresentedEntry) -> object:
        row = self.Gtk.Expander()
        row.add_css_class("card")
        heading = self.Gtk.Box(
            orientation=self.Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_top=10,
            margin_bottom=10,
            margin_start=12,
            margin_end=12,
        )
        timestamp = self.Gtk.Label(label=presented.display_time, xalign=0, yalign=0)
        timestamp.add_css_class("monospace")
        timestamp.add_css_class("dim-label")
        timestamp.set_size_request(52, -1)
        body = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=5)
        body.set_hexpand(True)
        message = self.Gtk.Label(
            label=presented.entry.text, xalign=0, wrap=True, selectable=True
        )
        body.append(message)
        labels = (*presented.tags, *presented.indicators)
        if labels:
            tags = self.Gtk.Label(label="  ·  ".join(labels), xalign=0)
            tags.add_css_class("caption")
            if "failure" in labels or "security" in labels or "redacted" in labels:
                tags.add_css_class("warning")
            body.append(tags)
        heading.append(timestamp)
        heading.append(body)
        row.set_label_widget(heading)
        row.set_child(self._provenance_grid(session, presented))
        return row

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
            content.set_hexpand(True)
            grid.attach(key, 0, index, 1, 1)
            grid.attach(content, 1, index, 1, 1)
        return grid
