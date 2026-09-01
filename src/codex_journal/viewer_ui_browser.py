from __future__ import annotations
from dataclasses import asdict, dataclass
from typing import Callable
from .viewer_annotations import AnnotationStore, AnnotationTarget
from .viewer_catalog import CatalogError, CatalogSession, JournalCatalog, JournalSearchIndex, SearchHit
from .viewer_model import ALL, BrowserFilters, SessionBrowserModel, display_start, session_badges
from .viewer_presenter import concise_session_summary
from .viewer_state import ViewerState
from .viewer_sync import rebuild_search_index_atomic
from .viewer_tags import TAGS
from .viewer_ui_support import UIContext, accessible, clear_box, select_dropdown_value, selected_text
@dataclass(frozen=True)
class BrowserCallbacks:
    session_selected: Callable[[str, bool], None]
    show_state: Callable[[str], None]
    show_error: Callable[[int], None]
    ready: Callable[[], None]
    status_text: Callable[[], str]
class BrowserController:
    def __init__(
        self,
        context: UIContext,
        annotations: AnnotationStore,
        saved_state: ViewerState,
        callbacks: BrowserCallbacks,
    ) -> None:
        self.context = context
        self.annotations = annotations
        self.callbacks = callbacks
        self.catalog = JournalCatalog(context.repo_root)
        self.model = SessionBrowserModel(self.catalog)
        self.search_index: JournalSearchIndex | None = None
        self.loading = False
        self.restored = False
        self._saved_filters = dict(saved_state.filters)
        self._saved_session_id = saved_state.selected_session_id
        self._session_rows: dict[object, str] = {}
        self._filter_widgets: dict[str, object] = {}
        self._hits_by_session: dict[str, SearchHit] = {}
        self._summaries: dict[str, str] = {}
        self.toolbar: object | None = None
        self.search_entry: object | None = None
        self.session_list: object | None = None
        self.filter_status: object | None = None
        self.clear_filters_button: object | None = None
        self.count_label: object | None = None
        self.advanced_filters: object | None = None
        self.bookmarks_check: object | None = None
        self.redacted_check: object | None = None
        self.errors_check: object | None = None
        self.sync_status: object | None = None
    @property
    def ready(self) -> bool:
        return self.restored and not self.loading
    def build_sidebar(
        self, settings_button: object, sync_button: object, sync_status: object
    ) -> object:
        Gtk, Adw = self.context.Gtk, self.context.Adw
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Sessions", subtitle="Generated journals only"))
        header.pack_end(settings_button)
        header.pack_end(sync_button)
        toolbar.add_top_bar(header)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.search_entry = Gtk.SearchEntry(
            placeholder_text="Search safe journals",
            margin_top=12,
            margin_start=12,
            margin_end=12,
        )
        self.search_entry.connect("search-changed", self._on_search_changed)
        accessible(self.context, self.search_entry, "Search generated journals")
        outer.append(self.search_entry)
        filters = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        advanced = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for field, label, destination in (
            ("project", "Project", filters),
            ("status", "Status", filters),
            ("date_from", "From", advanced),
            ("date_to", "To", advanced),
            ("branch", "Branch", advanced),
            ("source_kind", "Source", advanced),
            ("tag", "Tag", advanced),
        ):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            caption = Gtk.Label(label=label, xalign=0)
            caption.set_size_request(58, -1)
            dropdown = Gtk.DropDown.new_from_strings([ALL])
            dropdown.set_hexpand(True)
            dropdown.connect("notify::selected", self._on_filter_changed, field)
            accessible(self.context, dropdown, f"Filter by {label.lower()}")
            self._filter_widgets[field] = dropdown
            row.append(caption)
            row.append(dropdown)
            destination.append(row)
        self.bookmarks_check = Gtk.CheckButton(label="Bookmarked sessions")
        self.bookmarks_check.connect("toggled", self._on_boolean_filter, "bookmarked_only")
        filters.append(self.bookmarks_check)
        self.redacted_check = Gtk.CheckButton(label="Has redactions")
        self.redacted_check.connect("toggled", self._on_boolean_filter, "redacted_only")
        advanced.append(self.redacted_check)
        self.errors_check = Gtk.CheckButton(label="Has extraction errors")
        self.errors_check.connect("toggled", self._on_boolean_filter, "extraction_errors_only")
        advanced.append(self.errors_check)
        self.advanced_filters = Gtk.Expander(label="Advanced filters")
        self.advanced_filters.set_child(advanced)
        filters.append(self.advanced_filters)
        outer.append(filters)
        feedback = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_start=12,
            margin_end=12,
            margin_bottom=8,
        )
        self.filter_status = Gtk.Label(label="", xalign=0, hexpand=True)
        self.filter_status.add_css_class("caption")
        feedback.append(self.filter_status)
        self.clear_filters_button = Gtk.Button(label="Clear all")
        self.clear_filters_button.add_css_class("flat")
        self.clear_filters_button.connect("clicked", self.clear_filters)
        self.clear_filters_button.set_visible(False)
        feedback.append(self.clear_filters_button)
        outer.append(feedback)
        self.sync_status = sync_status
        outer.append(sync_status)
        self.count_label = Gtk.Label(
            label="Loading…", xalign=0, margin_start=12, margin_end=12, margin_bottom=8
        )
        self.count_label.add_css_class("dim-label")
        outer.append(self.count_label)
        self.session_list = Gtk.ListBox()
        self.session_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.session_list.add_css_class("navigation-sidebar")
        self.session_list.connect("row-selected", self._on_session_selected)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.session_list)
        outer.append(scroller)
        toolbar.set_content(outer)
        self.toolbar = toolbar
        return toolbar
    def refresh(self, *, rebuild_index: bool = True) -> bool:
        if self.loading:
            return False
        if self.restored:
            self._saved_filters = {
                key: value
                for key, value in asdict(self.model.filters).items()
                if value not in (None, False, "")
            }
            self._saved_session_id = self.model.selected_session_id
        self.loading = True
        try:
            self.catalog.refresh()
            self.model = SessionBrowserModel(self.catalog)
            self.model.set_bookmarked_session_ids(self.annotations.bookmarked_session_ids())
            self._open_search_index(rebuild_index)
            self._populate_filters()
            self._restore()
            self.restored = True
            self._apply_search()
            self._populate_sessions()
            if not self.catalog.sessions:
                if self.catalog.diagnostics:
                    self.callbacks.show_error(len(self.catalog.diagnostics))
                else:
                    self.callbacks.show_state("empty")
        finally:
            self.loading = False
        self.callbacks.ready()
        return False
    def _open_search_index(self, rebuild: bool) -> None:
        if self.search_index is not None:
            self.search_index.close()
        path = self.context.repo_root / "state" / "viewer.sqlite3"
        try:
            self.search_index = JournalSearchIndex(path)
        except CatalogError:
            rebuild_search_index_atomic(self.catalog, path)
            self.search_index = JournalSearchIndex(path)
        if rebuild:
            self.search_index.rebuild(self.catalog)
        self._summaries = self.search_index.session_summaries()
    def _restore(self) -> None:
        restored_advanced = False
        for field, value in self._saved_filters.items():
            if field in self._filter_widgets and isinstance(value, str):
                select_dropdown_value(self._filter_widgets[field], value)
                self.model.set_filter(field, value)
                restored_advanced = restored_advanced or field not in {"project", "status"}
            elif field in {"redacted_only", "extraction_errors_only", "bookmarked_only"} and isinstance(value, bool):
                widget = {
                    "redacted_only": self.redacted_check,
                    "extraction_errors_only": self.errors_check,
                    "bookmarked_only": self.bookmarks_check,
                }[field]
                widget.set_active(value)
                self.model.set_filter(field, value)
                restored_advanced = restored_advanced or value
        self.advanced_filters.set_expanded(restored_advanced)
        if self._saved_session_id and any(
            item.session_id == self._saved_session_id for item in self.model.sessions
        ):
            self.model.select(self._saved_session_id)
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
            dropdown.set_model(
                self.context.Gtk.StringList.new([ALL, *self._filter_values(field)])
            )
            dropdown.set_selected(0)
    def _on_filter_changed(self, dropdown: object, _pspec: object, field: str) -> None:
        if self.loading:
            return
        self.model.set_filter(field, selected_text(dropdown))
        self._apply_search()
        self._populate_sessions()
    def _on_boolean_filter(self, button: object, field: str) -> None:
        if self.loading:
            return
        self.model.set_filter(field, bool(button.get_active()))
        self._populate_sessions()
    def _on_search_changed(self, _entry: object) -> None:
        if not self.loading:
            self._apply_search()
            self._populate_sessions()
    def _apply_search(self) -> None:
        if self.search_index is None:
            return
        query = self.search_entry.get_text()
        tag = self.model.filters.tag
        active = bool(query.strip() or tag)
        hits = self.search_index.search(
            query, tags=(tag,) if tag else (), limit=1000
        ) if active else ()
        self.model.set_search_hits(hits, active=active)
        self._hits_by_session = {}
        for hit in hits:
            self._hits_by_session.setdefault(hit.session_id, hit)
    def clear_filters(self, *_args: object) -> None:
        self.loading = True
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
            self.loading = False
        self._populate_sessions()
    def _populate_sessions(self) -> None:
        self._session_rows.clear()
        clear_box(self.session_list)
        sessions = self.model.sessions
        counts = self.model.counts
        self.count_label.set_label(f"{counts.visible} of {counts.total} sessions")
        self.sync_status.set_label(self.callbacks.status_text())
        count = sum(
            value not in (None, False, "") for value in asdict(self.model.filters).values()
        ) + int(bool(self.search_entry.get_text().strip()))
        self.clear_filters_button.set_visible(bool(count))
        self.filter_status.set_label(
            f"{count} active filter{'s' if count != 1 else ''}" if count else ""
        )
        for session in sessions:
            row = self.context.Gtk.ListBoxRow()
            row.set_activatable(True)
            row.set_child(self._session_row(session))
            self.session_list.append(row)
            self._session_rows[row] = session.session_id
        if not sessions:
            self.model.select(None)
            self.callbacks.show_state("unselected" if self.catalog.sessions else "empty")
            return
        selected = next(
            (
                row
                for row, session_id in self._session_rows.items()
                if session_id == self.model.selected_session_id
            ),
            self.session_list.get_row_at_index(0),
        )
        self.session_list.select_row(selected)
    def _session_row(self, session: CatalogSession) -> object:
        Gtk = self.context.Gtk
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
            margin_top=10,
            margin_bottom=10,
            margin_start=12,
            margin_end=12,
        )
        project = Gtk.Label(label=session.project, xalign=0, ellipsize=3)
        project.add_css_class("heading")
        box.append(project)
        branch = f" · {session.branch}" if session.branch else ""
        meta = Gtk.Label(
            label=f"{display_start(session)}{branch} · {session.entry_count} entries",
            xalign=0,
            ellipsize=3,
        )
        meta.add_css_class("dim-label")
        box.append(meta)
        badges = Gtk.Label(label="  ·  ".join(session_badges(session)), xalign=0, ellipsize=3)
        badges.add_css_class("caption")
        if session.extraction_error_count or session.redaction_count:
            badges.add_css_class("warning")
        box.append(badges)
        summary_text = self._summaries.get(session.session_id)
        if summary_text:
            summary = Gtk.Label(label=concise_session_summary(summary_text), xalign=0, ellipsize=3)
            summary.add_css_class("caption")
            summary.set_tooltip_text(summary_text)
            box.append(summary)
        if self.annotations.is_bookmarked(AnnotationTarget(session.session_id)):
            bookmarked = Gtk.Label(label="★ session bookmark", xalign=0)
            bookmarked.add_css_class("accent")
            box.append(bookmarked)
        hit = self._hits_by_session.get(session.session_id)
        if hit is not None:
            context = Gtk.Label(label=f"Match: {hit.text}", xalign=0, ellipsize=3)
            context.add_css_class("accent")
            context.set_tooltip_text(hit.text)
            box.append(context)
        return box
    def _on_session_selected(self, _list: object, row: object | None) -> None:
        session_id = self._session_rows.get(row) if row is not None else None
        if session_id is None:
            return
        changed = session_id != self.model.selected_session_id
        self.model.select(session_id)
        self.callbacks.session_selected(session_id, changed)
    def move(self, delta: int) -> None:
        sessions = self.model.sessions
        if not sessions:
            return
        session_ids = [session.session_id for session in sessions]
        try:
            current = session_ids.index(self.model.selected_session_id)
        except ValueError:
            current = 0
        target_id = session_ids[max(0, min(len(session_ids) - 1, current + delta))]
        row = next(
            (item for item, session_id in self._session_rows.items() if session_id == target_id),
            None,
        )
        if row is not None:
            self.session_list.select_row(row)
            row.grab_focus()
    def show_subset(self, session_ids: tuple[str, ...]) -> None:
        self.search_entry.set_text("")
        self.model = SessionBrowserModel(self.catalog)
        self.model.set_bookmarked_session_ids(self.annotations.bookmarked_session_ids())
        self.model.set_session_subset(frozenset(session_ids))
        self._hits_by_session.clear()
        self._populate_sessions()
    def navigate(self, session_id: str) -> None:
        self.clear_filters()
        if self.catalog.get(session_id) is None:
            return
        self.model.select(session_id)
        self._populate_sessions()
    def refresh_bookmarks(self) -> None:
        selected = self.model.selected_session_id
        self.model.set_bookmarked_session_ids(self.annotations.bookmarked_session_ids())
        self._populate_sessions()
        if selected and selected in {item.session_id for item in self.model.sessions}:
            self.model.select(selected)
    def filter_widget(self, name: str) -> object:
        return self._filter_widgets[name]
    def summary(self, session_id: str) -> str | None:
        return self._summaries.get(session_id)
    def capture_filters(self) -> dict[str, str | bool | None]:
        return {
            key: value
            for key, value in asdict(self.model.filters).items()
            if value not in (None, False, "")
        }
    def close(self) -> None:
        if self.search_index is not None:
            self.search_index.close()
            self.search_index = None
