from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import Callable
from gi.repository import Pango
from .viewer_activity import ActivityBucket, ActivityReport, build_activity_report, fill_daily_range
from .viewer_annotations import AnnotationStore
from .viewer_catalog import CatalogError, JournalCatalog
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
from .viewer_model import ALL
from .viewer_tags import TAGS
from .viewer_ui_support import UIContext, clear_box, selected_text
from .viewer_ui_timeline import TimelineController
@dataclass(frozen=True)
class ReportHost:
    context: UIContext
    main_title: object
    content_box: object
    show_state: Callable[[str], None]
    actions_changed: Callable[[], None]
    show_sidebar: Callable[[], None]
    is_closed: Callable[[], bool]
    def begin(self, title: str, subtitle: str) -> None:
        self.main_title.set_title(title)
        self.main_title.set_subtitle(subtitle)
        clear_box(self.content_box)
class ComparisonController:
    def __init__(
        self,
        host: ReportHost,
        catalog: Callable[[], JournalCatalog],
        timeline: TimelineController,
    ) -> None:
        self.host = host
        self.catalog = catalog
        self.timeline = timeline
        self.recent_session_ids: list[str] = []
        self.report: ComparisonReport | None = None
        self.tag_dropdown: object | None = None
        self.timeline_box: object | None = None
    @property
    def ready(self) -> bool:
        return len(set(self.recent_session_ids[-2:])) == 2
    def note_session(self, session_id: str) -> None:
        if session_id in self.recent_session_ids:
            self.recent_session_ids.remove(session_id)
        self.recent_session_ids.append(session_id)
        self.recent_session_ids = self.recent_session_ids[-10:]
        self.host.actions_changed()
    def open(self) -> None:
        if not self.ready:
            self.timeline.set_action_status(
                "View two different sessions before opening comparison.", warning=True
            )
            return
        left_id, right_id = self.recent_session_ids[-2:]
        try:
            report = compare_details(
                self.catalog().load_detail(left_id), self.catalog().load_detail(right_id)
            )
        except (CatalogError, ValueError):
            self.timeline.set_action_status(
                "One generated session failed comparison validation.", warning=True
            )
            return
        self.report = report
        self.timeline.deactivate()
        self.host.begin("Session comparison", "Two recently viewed generated journals")
        Gtk = self.host.context.Gtk
        title = Gtk.Label(label="Session comparison", xalign=0)
        title.add_css_class("title-1")
        self.host.content_box.append(title)
        explanation = Gtk.Label(
            label=(
                "Exact normalized text only · unchanged rows match exactly · "
                "left-only and right-only rows do not imply causality."
            ),
            xalign=0,
            wrap=True,
        )
        explanation.add_css_class("dim-label")
        self.host.content_box.append(explanation)
        self.host.content_box.append(self._metadata(report))
        filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        filter_row.append(Gtk.Label(label="Timeline tag", xalign=0))
        self.tag_dropdown = Gtk.DropDown.new_from_strings([ALL, *TAGS])
        self.tag_dropdown.connect("notify::selected", self._on_filter)
        filter_row.append(self.tag_dropdown)
        self.host.content_box.append(filter_row)
        self.timeline_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.host.content_box.append(self.timeline_box)
        self._render(None)
        self.host.show_state("content")
        self.host.actions_changed()
    def _metadata(self, report: ComparisonReport) -> object:
        grid = self.host.context.Gtk.Grid(column_spacing=12, row_spacing=7)
        for column, label in enumerate(("Field", "Earlier viewed", "Later viewed")):
            heading = self.host.context.Gtk.Label(label=label, xalign=0)
            heading.add_css_class("heading")
            grid.attach(heading, column, 0, 1, 1)
        for row_index, item in enumerate(report.metadata, 1):
            for column, value in enumerate((item.label, item.left, item.right)):
                label = self.host.context.Gtk.Label(
                    label=value, xalign=0, wrap=True, selectable=True
                )
                label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                label.set_hexpand(column > 0)
                if column == 0:
                    label.add_css_class("dim-label")
                grid.attach(label, column, row_index, 1, 1)
        return grid
    def _on_filter(self, dropdown: object, _pspec: object) -> None:
        self._render(selected_text(dropdown))
    def _render(self, tag: str | None) -> None:
        if self.report is None or self.timeline_box is None:
            return
        clear_box(self.timeline_box)
        rows = filter_timeline(self.report.timeline, None if tag in (None, ALL) else tag)
        if not rows:
            empty = self.host.context.Gtk.Label(
                label="No timeline entries match this deterministic filter.", xalign=0
            )
            empty.add_css_class("dim-label")
            self.timeline_box.append(empty)
            return
        for row in rows:
            grid = self.host.context.Gtk.Grid(column_spacing=12, row_spacing=4)
            grid.add_css_class("card")
            kind = self.host.context.Gtk.Label(label=row.kind, xalign=0)
            kind.add_css_class("caption")
            grid.attach(kind, 0, 0, 2, 1)
            grid.attach(self._entry(row.left, "Left"), 0, 1, 1, 1)
            grid.attach(self._entry(row.right, "Right"), 1, 1, 1, 1)
            self.timeline_box.append(grid)
    def _entry(self, entry: object | None, side: str) -> object:
        if entry is None:
            label = self.host.context.Gtk.Label(label=f"{side}: —", xalign=0)
            label.add_css_class("dim-label")
            return label
        label = self.host.context.Gtk.Label(
            label=f"{entry.display_time}  {entry.text}",
            xalign=0,
            wrap=True,
            selectable=True,
        )
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_hexpand(True)
        return label
class ActivityController:
    def __init__(
        self,
        host: ReportHost,
        catalog: Callable[[], JournalCatalog],
        timeline: TimelineController,
        show_subset: Callable[[tuple[str, ...]], None],
    ) -> None:
        self.host = host
        self.catalog = catalog
        self.timeline = timeline
        self.show_subset = show_subset
        self.report: ActivityReport | None = None
        self.running = False
        self.period_dropdown: object | None = None
        self.project_dropdown: object | None = None
        self.list_box: object | None = None
    def invalidate(self) -> None:
        self.report = None
        self.host.actions_changed()
    def open(self) -> None:
        if self.report is not None:
            self._build()
            return
        if self.running:
            return
        self.running = True
        self.timeline.deactivate()
        self.host.begin("Journal activity", "Generated sessions and visible entries only")
        loading = self.host.context.Adw.StatusPage(
            title="Building journal activity",
            description="Counting generated sessions and visible entries in a worker.",
            icon_name="content-loading-symbolic",
        )
        loading.set_vexpand(True)
        self.host.content_box.append(loading)
        self.host.show_state("content")
        Thread(target=self._worker, daemon=True).start()
    def _worker(self) -> None:
        try:
            catalog = JournalCatalog(self.host.context.repo_root)
            catalog.refresh()
            report = build_activity_report(catalog)
            self.host.context.GLib.idle_add(self._finish, report, None)
        except Exception as exc:  # worker boundary reports type only
            self.host.context.GLib.idle_add(self._finish, None, type(exc).__name__)
    def _finish(self, report: ActivityReport | None, failure: str | None) -> bool:
        self.running = False
        if self.host.is_closed():
            return False
        if report is None:
            clear_box(self.host.content_box)
            self.host.content_box.append(
                self.host.context.Adw.StatusPage(
                    title="Activity view unavailable",
                    description=f"Generated activity failed safely ({failure or 'unknown error'}).",
                    icon_name="dialog-warning-symbolic",
                )
            )
            return False
        self.report = report
        self._build()
        self.host.actions_changed()
        return False
    def _build(self) -> None:
        self.timeline.deactivate()
        self.host.begin("Journal activity", "Generated sessions and visible entries only")
        Gtk = self.host.context.Gtk
        title = Gtk.Label(label="Journal activity", xalign=0)
        title.add_css_class("title-1")
        self.host.content_box.append(title)
        notice = Gtk.Label(
            label=(
                "Deterministic counts from generated sessions and visible entries only. "
                "No productivity score, sentiment, or hidden-activity inference."
            ),
            xalign=0,
            wrap=True,
        )
        notice.add_css_class("dim-label")
        self.host.content_box.append(notice)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        controls.append(Gtk.Label(label="Period", xalign=0))
        self.period_dropdown = Gtk.DropDown.new_from_strings(["Daily", "Weekly"])
        self.period_dropdown.connect("notify::selected", self._on_filter)
        controls.append(self.period_dropdown)
        controls.append(Gtk.Label(label="Project calendar", xalign=0))
        projects = [calendar.project for calendar in self.report.projects]
        self.project_dropdown = Gtk.DropDown.new_from_strings([ALL, *projects])
        self.project_dropdown.connect("notify::selected", self._on_filter)
        controls.append(self.project_dropdown)
        self.host.content_box.append(controls)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.host.content_box.append(self.list_box)
        self._render()
        self.host.show_state("content")
    def _on_filter(self, *_args: object) -> None:
        self._render()
    def _render(self) -> None:
        if self.report is None or self.list_box is None:
            return
        clear_box(self.list_box)
        project = selected_text(self.project_dropdown)
        period = selected_text(self.period_dropdown)
        if project and project != ALL:
            calendar = next(item for item in self.report.projects if item.project == project)
            buckets, heading = calendar.days, f"{project} · project calendar"
        elif period == "Weekly":
            buckets, heading = self.report.weeks, "All projects · ISO weeks"
        elif self.report.days:
            ascending = fill_daily_range(
                self.report, self.report.days[-1].key, self.report.days[0].key
            )
            buckets, heading = tuple(reversed(ascending)), "All projects · calendar days"
        else:
            buckets, heading = (), "No generated activity"
        label = self.host.context.Gtk.Label(label=heading, xalign=0)
        label.add_css_class("title-2")
        self.list_box.append(label)
        if not buckets:
            empty = self.host.context.Gtk.Label(
                label="This period has no generated sessions or visible entries.", xalign=0
            )
            empty.add_css_class("dim-label")
            self.list_box.append(empty)
            return
        for bucket in buckets:
            self.list_box.append(self._row(bucket))
    def _row(self, bucket: ActivityBucket) -> object:
        Gtk = self.host.context.Gtk
        row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_top=8,
            margin_bottom=8,
            margin_start=10,
            margin_end=10,
        )
        row.add_css_class("card")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        body.set_hexpand(True)
        title = Gtk.Label(label=bucket.key, xalign=0)
        title.add_css_class("heading")
        body.append(title)
        statuses = ", ".join(f"{name} {count}" for name, count in bucket.statuses) or "no sessions"
        body.append(
            Gtk.Label(
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
        details = Gtk.Label(
            label=f"Projects: {projects or 'none'}\nTags: {tags or 'none'}",
            xalign=0,
            wrap=True,
        )
        details.add_css_class("caption")
        body.append(details)
        row.append(body)
        button = Gtk.Button(label="View sessions")
        button.set_sensitive(bool(bucket.session_ids))
        button.connect("clicked", lambda _button: self._navigate(bucket))
        row.append(button)
        return row
    def _navigate(self, bucket: ActivityBucket) -> None:
        self.timeline.current_entry_index = 0
        self.show_subset(bucket.session_ids)
        self.host.show_sidebar()
class ExportController:
    def __init__(
        self,
        host: ReportHost,
        catalog: Callable[[], JournalCatalog],
        annotations: AnnotationStore,
        timeline: TimelineController,
        comparison: ComparisonController,
        activity: ActivityController,
        set_status: Callable[[str], None],
    ) -> None:
        self.host = host
        self.catalog = catalog
        self.annotations = annotations
        self.timeline = timeline
        self.comparison = comparison
        self.activity = activity
        self.set_status = set_status
        self.document: ExportDocument | None = None
        self._pending: tuple[Path, bytes] | None = None
        self._format_name = "markdown"
    @property
    def available(self) -> bool:
        return bool(self._scopes())
    def _scopes(self) -> tuple[str, ...]:
        scopes: list[str] = []
        if self.timeline.detail is not None and self.timeline.detail.entries:
            scopes.append("Current entry")
        if self.timeline.detail is not None and self.timeline.selected_indexes:
            scopes.extend(("Checked entries", "Checked time range"))
        if self.comparison.report is not None:
            scopes.append("Comparison results")
        if self.activity.report is not None:
            scopes.append("Activity view")
        return tuple(scopes)
    def open(self) -> None:
        scopes = self._scopes()
        if not scopes:
            self.timeline.set_action_status(
                "Select timeline entries, build a comparison, or open activity before export.",
                warning=True,
            )
            return
        Adw, Gtk = self.host.context.Adw, self.host.context.Gtk
        dialog = Adw.Dialog()
        dialog.set_title("Review export")
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_args: dialog.close())
        header.pack_start(cancel)
        choose = Gtk.Button(label="Choose destination…")
        choose.add_css_class("suggested-action")
        choose.connect("clicked", lambda *_args: self._choose_destination(dialog))
        header.pack_end(choose)
        toolbar.add_top_bar(header)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.append(Gtk.Label(label="Scope"))
        self.scope_dropdown = Gtk.DropDown.new_from_strings(list(scopes))
        self.scope_dropdown.connect("notify::selected", self._update_preview)
        controls.append(self.scope_dropdown)
        controls.append(Gtk.Label(label="Format"))
        self.format_dropdown = Gtk.DropDown.new_from_strings(["Markdown", "JSON"])
        self.format_dropdown.connect("notify::selected", self._update_preview)
        controls.append(self.format_dropdown)
        self.notes_check = Gtk.CheckButton(label="Include private notes (explicit opt-in)")
        self.notes_check.connect("toggled", self._update_preview)
        controls.append(self.notes_check)
        content.append(controls)
        warning = Gtk.Label(
            label=(
                "Generated journals may still contain private project information. "
                "Review every item below before export."
            ),
            xalign=0,
            wrap=True,
        )
        warning.add_css_class("warning")
        content.append(warning)
        self.preview = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            monospace=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=420)
        scroller.set_child(self.preview)
        content.append(scroller)
        toolbar.set_content(content)
        dialog.set_child(toolbar)
        self._update_preview()
        dialog.present(self.host.context.window)
    def _build_document(self) -> ExportDocument:
        scope = selected_text(self.scope_dropdown)
        if scope == "Current entry":
            document = selected_entries_document(
                self.timeline.detail, {self.timeline.current_entry_index}
            )
        elif scope == "Checked entries":
            document = selected_entries_document(
                self.timeline.detail, set(self.timeline.selected_indexes)
            )
        elif scope == "Checked time range":
            document = selected_entries_document(
                self.timeline.detail,
                set(self.timeline.selected_indexes),
                inclusive_range=True,
            )
        elif scope == "Comparison results" and self.comparison.report is not None:
            document = comparison_document(self.comparison.report)
        elif scope == "Activity view" and self.activity.report is not None:
            session_ids = sorted(
                {
                    session_id
                    for bucket in self.activity.report.days
                    for session_id in bucket.session_ids
                }
            )
            details = tuple(
                self.catalog().load_detail(session_id, cache=False)
                for session_id in session_ids
            )
            document = activity_document(self.activity.report, details)
        else:
            raise ValueError("The selected export scope is no longer available.")
        return include_private_notes(document, self.annotations) if self.notes_check.get_active() else document
    def _update_preview(self, *_args: object) -> None:
        try:
            self.document = self._build_document()
            preview = render_preview(self.document)
        except (CatalogError, ValueError) as exc:
            self.document = None
            preview = f"Export unavailable: {exc}"
        self.preview.get_buffer().set_text(preview)
    def _choose_destination(self, preview_dialog: object) -> None:
        self._update_preview()
        if self.document is None:
            return
        format_name = selected_text(self.format_dropdown) or "Markdown"
        self._format_name = format_name.lower()
        extension = ".md" if format_name == "Markdown" else ".json"
        dialog = self.host.context.Gtk.FileDialog(title="Choose reviewed export destination")
        dialog.set_initial_name(f"heartbeat-export{extension}")
        dialog.save(self.host.context.window, None, self._destination_chosen)
        preview_dialog.close()
    def _destination_chosen(self, dialog: object, result: object) -> None:
        try:
            selected = dialog.save_finish(result)
            path_value = selected.get_path()
            if not path_value:
                raise ValueError("Only a local export destination is supported.")
            destination = Path(path_value)
            content = render_export(self.document, self._format_name)
        except Exception:
            return
        if destination.exists():
            self._pending = (destination, content)
            confirmation = self.host.context.Adw.AlertDialog.new(
                "Replace existing export?",
                "The chosen local file already exists. Replacement will be atomic.",
            )
            confirmation.add_response("cancel", "Cancel")
            confirmation.add_response("replace", "Replace")
            confirmation.set_response_appearance(
                "replace", self.host.context.Adw.ResponseAppearance.DESTRUCTIVE
            )
            confirmation.set_default_response("cancel")
            confirmation.set_close_response("cancel")
            confirmation.choose(self.host.context.window, None, self._overwrite_chosen)
            return
        self._write(destination, content, overwrite=False)
    def _overwrite_chosen(self, dialog: object, result: object) -> None:
        try:
            response = dialog.choose_finish(result)
        except Exception:
            response = "cancel"
        pending = self._pending
        self._pending = None
        if response == "replace" and pending is not None:
            self._write(*pending, overwrite=True)
    def _write(self, destination: Path, content: bytes, *, overwrite: bool) -> None:
        try:
            write_export_atomic(destination, content, overwrite=overwrite)
        except (OSError, ValueError):
            self.set_status("Export failed safely; no partial target was retained.")
            return
        self.set_status(
            f"Exported {len(content)} reviewed byte(s) to the chosen local destination."
        )
