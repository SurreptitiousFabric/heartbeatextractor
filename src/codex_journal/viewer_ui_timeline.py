from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from gi.repository import Pango
from .viewer_actions import ProjectPathError, copy_one_entry, copy_selected_range, project_directory_uri
from .viewer_annotations import AnnotationStore, AnnotationTarget
from .viewer_catalog import CatalogError, CatalogSession, JournalCatalog
from .viewer_model import SessionBrowserModel, display_start
from .viewer_presenter import PresentedEntry, present_timeline, safe_inline_markup
from .viewer_ui_support import UIContext, accessible, clear_box
@dataclass(frozen=True)
class TimelineCallbacks:
    show_state: Callable[[str], None]
    navigate: Callable[[str], None]
    refresh_browser: Callable[[], None]
    actions_changed: Callable[[], None]
class TimelineController:
    def __init__(
        self,
        context: UIContext,
        annotations: AnnotationStore,
        catalog: Callable[[], JournalCatalog],
        model: Callable[[], SessionBrowserModel],
        main_title: object,
        error_page: object,
        content_box: object,
        callbacks: TimelineCallbacks,
        *,
        entry_index: int,
        density: str,
    ) -> None:
        self.context = context
        self.annotations = annotations
        self.catalog = catalog
        self.model = model
        self.main_title = main_title
        self.error_page = error_page
        self.content_box = content_box
        self.callbacks = callbacks
        self.current_entry_index = entry_index
        self.density = density
        self.detail: object | None = None
        self.details_expander: object | None = None
        self.action_status: object | None = None
        self.selection_mode = False
        self._updating_selection = False
        self._selected_indexes: set[int] = set()
        self._rows: dict[int, object] = {}
        self._checks: dict[int, object] = {}
        self._pending_note_delete: AnnotationTarget | None = None
        self.open_project_button: object | None = None
        self.select_mode_button: object | None = None
        self.more_actions_button: object | None = None
        self.selection_bar: object | None = None
        self.selection_count: object | None = None
        self.copy_selection_button: object | None = None
        self.density_dropdown: object | None = None
    @property
    def selected_indexes(self) -> frozenset[int]:
        return frozenset(self._selected_indexes)
    @property
    def indexes(self) -> tuple[int, ...]:
        return tuple(sorted(self._rows))
    def row(self, index: int) -> object:
        return self._rows[index]
    def selection_checkbox(self, index: int) -> object:
        return self._checks[index]
    def attach_header(self, header: object, journal_menu: object) -> None:
        Gtk = self.context.Gtk
        self.open_project_button = Gtk.Button(
            icon_name="folder-open-symbolic", tooltip_text="Open validated project directory"
        )
        accessible(self.context, self.open_project_button, "Open validated project directory")
        self.open_project_button.connect("clicked", lambda *_args: self.open_project())
        header.pack_end(self.open_project_button)
        self.select_mode_button = Gtk.ToggleButton(label="Select")
        self.select_mode_button.set_tooltip_text("Select a sanitized timeline range")
        self.select_mode_button.connect("toggled", self._on_selection_mode_toggled)
        header.pack_end(self.select_mode_button)
        self.more_actions_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic", tooltip_text="More journal actions"
        )
        self.more_actions_button.set_menu_model(journal_menu)
        accessible(self.context, self.more_actions_button, "Open more journal actions")
        header.pack_end(self.more_actions_button)
    def build_selection_bar(self) -> object:
        Gtk = self.context.Gtk
        self.selection_bar = Gtk.Revealer()
        actions = Gtk.ActionBar()
        self.selection_count = Gtk.Label(label="0 selected")
        actions.pack_start(self.selection_count)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_args: self.set_selection_mode(False))
        actions.pack_end(cancel)
        self.copy_selection_button = Gtk.Button(label="Copy selected")
        self.copy_selection_button.add_css_class("suggested-action")
        self.copy_selection_button.set_sensitive(False)
        self.copy_selection_button.connect("clicked", lambda *_args: self.copy_selected_range())
        actions.pack_end(self.copy_selection_button)
        self.selection_bar.set_child(actions)
        return self.selection_bar
    def create_density_control(self) -> object:
        dropdown = self.context.Gtk.DropDown.new_from_strings(["Comfortable", "Compact"])
        dropdown.set_selected(1 if self.density == "compact" else 0)
        dropdown.connect("notify::selected", self._on_density_changed)
        accessible(self.context, dropdown, "Choose timeline density")
        self.density_dropdown = dropdown
        return dropdown
    def select_session(self, session_id: str, changed: bool) -> None:
        if changed:
            self.current_entry_index = 0
        self.set_selection_mode(False)
        self.render(session_id)
    def render(self, session_id: str) -> None:
        try:
            detail = self.catalog().load_detail(session_id)
        except (CatalogError, ValueError):
            self.error_page.set_title("Generated journal is malformed")
            self.error_page.set_description(
                "The selected generated artifact failed validation and was not displayed. "
                "Private source logs were not opened."
            )
            self.callbacks.show_state("error")
            return
        clear_box(self.content_box)
        self._rows.clear()
        self._checks.clear()
        self.detail = detail
        session = detail.session
        self.main_title.set_title(session.project)
        self.main_title.set_subtitle(
            f"{display_start(session)} · {session.branch or 'No branch'} · {session.status}"
        )
        self.action_status = self.context.Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
        self.action_status.add_css_class("caption")
        self.content_box.append(self.action_status)
        if session.redaction_count or session.extraction_error_count:
            warning = self.context.Adw.Banner.new(
                f"{session.redaction_count} redaction(s) · "
                f"{session.extraction_error_count} extraction error(s)"
            )
            warning.set_revealed(True)
            self.content_box.append(warning)
        heading = self.context.Gtk.Label(label="Timeline", xalign=0)
        heading.add_css_class("title-2")
        self.content_box.append(heading)
        if not detail.entries:
            empty = self.context.Adw.StatusPage(
                title="No user-visible heartbeats",
                description="This session still has a journal, but no eligible progress entries were found.",
                icon_name="dialog-information-symbolic",
            )
            empty.set_vexpand(False)
            self.content_box.append(empty)
        else:
            self._render_entries(detail, session)
        details = self.context.Gtk.Expander(label="Session details and provenance summary")
        details.set_child(self._details_grid(session))
        self.details_expander = details
        self.content_box.append(details)
        relationships = self._relationship_box(session)
        if relationships is not None:
            self.content_box.append(relationships)
        self.content_box.append(self._notes_expander(detail))
        self._append_extraction_errors(detail)
        self.callbacks.show_state("content")
    def _render_entries(self, detail: object, session: CatalogSession) -> None:
        timeline = self.context.Gtk.Box(
            orientation=self.context.Gtk.Orientation.VERTICAL, spacing=8
        )
        previous_date = None
        target_index = self.model().matching_entry(session.session_id)
        if target_index is None:
            target_index = min(self.current_entry_index, max(0, len(detail.entries) - 1))
        target_widget = None
        for presented in present_timeline(detail):
            if presented.local_date != previous_date:
                date = self.context.Gtk.Label(label=presented.date_label, xalign=0)
                date.add_css_class("heading")
                date.set_margin_top(8)
                timeline.append(date)
                previous_date = presented.local_date
            widget = self._timeline_row(session, presented)
            if presented.entry.index == target_index:
                widget.add_css_class("accent")
                target_widget = widget
            self._rows[presented.entry.index] = widget
            timeline.append(widget)
        self.content_box.append(timeline)
        if target_widget is not None:
            self.context.GLib.idle_add(target_widget.grab_focus)
    def _append_extraction_errors(self, detail: object) -> None:
        if not detail.extraction_errors:
            return
        errors = self.context.Gtk.Expander(
            label=f"Extraction errors ({len(detail.extraction_errors)})"
        )
        box = self.context.Gtk.Box(
            orientation=self.context.Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=8,
            margin_bottom=8,
            margin_start=10,
            margin_end=10,
        )
        for error in detail.extraction_errors:
            label = self.context.Gtk.Label(
                label=f"Record {error.sequence}: {error.code}", xalign=0, selectable=True
            )
            label.add_css_class("warning")
            box.append(label)
        errors.set_child(box)
        self.content_box.append(errors)
    def deactivate(self) -> None:
        self.detail = None
        self.details_expander = None
        self.set_selection_mode(False)
    def move(self, delta: int) -> None:
        if not self._rows:
            return
        indexes = sorted(self._rows)
        try:
            current = indexes.index(self.current_entry_index)
        except ValueError:
            current = 0
        self.focus(indexes[max(0, min(len(indexes) - 1, current + delta))])
    def focus(self, index: int) -> None:
        widget = self._rows.get(index)
        if widget is None:
            return
        for candidate_index, candidate in self._rows.items():
            if candidate_index == index:
                candidate.add_css_class("accent")
            else:
                candidate.remove_css_class("accent")
        self.current_entry_index = index
        self._load_entry_note()
        widget.grab_focus()
    def toggle_details(self) -> None:
        if self.details_expander is not None:
            self.details_expander.set_expanded(not self.details_expander.get_expanded())
            self.details_expander.grab_focus()
    def _on_density_changed(self, dropdown: object, _pspec: object) -> None:
        self.density = "compact" if dropdown.get_selected() == 1 else "comfortable"
        if self.detail is not None:
            self.render(self.detail.session.session_id)
    def _timeline_row(self, session: CatalogSession, presented: PresentedEntry) -> object:
        Gtk = self.context.Gtk
        row = Gtk.Expander()
        row.add_css_class("card")
        compact = self.density == "compact"
        heading = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8 if compact else 12,
            margin_top=5 if compact else 10,
            margin_bottom=5 if compact else 10,
            margin_start=12,
            margin_end=12,
        )
        selected = Gtk.CheckButton()
        accessible(
            self.context,
            selected,
            f"Include journal entry at {presented.display_time} in copy range",
        )
        selected.connect("toggled", self._on_entry_selected, presented.entry.index)
        selected.set_visible(self.selection_mode)
        self._checks[presented.entry.index] = selected
        timestamp = Gtk.Label(label=presented.display_time, xalign=0, yalign=0)
        timestamp.add_css_class("monospace")
        timestamp.add_css_class("dim-label")
        timestamp.set_size_request(52, -1)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        body.set_hexpand(True)
        message = Gtk.Label(xalign=0, wrap=True, selectable=True)
        message.set_markup(safe_inline_markup(presented.entry.text))
        message.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        body.append(message)
        self._append_badges(body, session, presented)
        heading.append(selected)
        heading.append(timestamp)
        heading.append(body)
        row.set_label_widget(heading)
        row.set_child(self._provenance_grid(session, presented))
        row.connect("notify::expanded", self._on_entry_expanded, presented.entry.index)
        return row
    def _append_badges(
        self, body: object, session: CatalogSession, presented: PresentedEntry
    ) -> None:
        labels = (*presented.tags, *presented.indicators)
        target = AnnotationTarget(session.session_id, presented.entry.source_event_sequence)
        if self.annotations.is_bookmarked(target):
            labels = (*labels, "★ bookmarked")
        if not labels:
            return
        Gtk = self.context.Gtk
        badges = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            column_spacing=5,
            row_spacing=5,
            max_children_per_line=8,
            min_children_per_line=1,
            homogeneous=False,
            halign=Gtk.Align.START,
        )
        for value in labels:
            badge = Gtk.Label(label=value)
            badge.add_css_class("caption")
            badge.set_margin_top(2)
            badge.set_margin_bottom(2)
            badge.set_margin_start(6)
            badge.set_margin_end(6)
            if value in {"failure", "security", "blocker", "stop", "redacted"}:
                badge.add_css_class("warning")
            elif value in {"correction", "test"}:
                badge.add_css_class("success")
            frame = Gtk.Frame()
            frame.add_css_class("card")
            frame.set_child(badge)
            badges.append(frame)
        body.append(badges)
    def _on_entry_selected(self, button: object, entry_index: int) -> None:
        if self._updating_selection:
            return
        if button.get_active():
            self._selected_indexes.add(entry_index)
        else:
            self._selected_indexes.discard(entry_index)
        self._update_selection_count()
    def _on_selection_mode_toggled(self, button: object) -> None:
        if not self._updating_selection:
            self.set_selection_mode(bool(button.get_active()))
    def set_selection_mode(self, enabled: bool) -> None:
        enabled = bool(enabled and self.detail is not None and self.detail.entries)
        self.selection_mode = enabled
        self._updating_selection = True
        try:
            if self.select_mode_button is not None and self.select_mode_button.get_active() != enabled:
                self.select_mode_button.set_active(enabled)
            for checkbox in self._checks.values():
                checkbox.set_visible(enabled)
                if not enabled:
                    checkbox.set_active(False)
            if not enabled:
                self._selected_indexes.clear()
        finally:
            self._updating_selection = False
        if self.selection_bar is not None:
            self.selection_bar.set_reveal_child(enabled)
        self._update_selection_count()
    def _update_selection_count(self) -> None:
        count = len(self._selected_indexes)
        if self.selection_count is not None:
            self.selection_count.set_label(f"{count} selected")
        if self.copy_selection_button is not None:
            self.copy_selection_button.set_sensitive(count > 0)
    def set_action_status(self, message: str, *, warning: bool = False) -> None:
        if self.action_status is None:
            return
        self.action_status.set_label(message)
        if warning:
            self.action_status.add_css_class("warning")
        else:
            self.action_status.remove_css_class("warning")
    def open_project(self) -> None:
        session = self.model().selected
        try:
            uri = project_directory_uri(
                session.working_directory if session else None, home=Path.home()
            )
            self.context.Gio.AppInfo.launch_default_for_uri(uri, None)
            self.set_action_status("Opened the validated local project directory.")
        except ProjectPathError as exc:
            self.set_action_status(str(exc), warning=True)
        except Exception:
            self.set_action_status(
                "The desktop file manager could not open the directory.", warning=True
            )
    def copy_current_entry(self) -> None:
        detail = self.detail
        if detail is None or not detail.entries:
            self.set_action_status("No sanitized timeline entry is available to copy.", warning=True)
            return
        entry = next(
            (item for item in detail.entries if item.index == self.current_entry_index),
            detail.entries[0],
        )
        payload = copy_one_entry(entry)
        self.context.window.get_clipboard().set(payload.text)
        self.set_action_status("Copied 1 sanitized journal entry with its timestamp.")
    def copy_selected_range(self) -> None:
        detail = self.detail
        if detail is not None and not self._selected_indexes:
            self.set_selection_mode(True)
            self.set_action_status("Select the journal entries to copy, then choose Copy selected.")
            return
        try:
            if detail is None:
                raise ValueError("No timeline entries are selected.")
            payload = copy_selected_range(detail.entries, self._selected_indexes)
        except ValueError as exc:
            self.set_action_status(str(exc), warning=True)
            return
        self.context.window.get_clipboard().set(payload.text)
        self.set_action_status(
            f"Copied {payload.entry_count} sanitized journal entries with timestamps."
        )
        self.set_selection_mode(False)
    def current_entry_target(self) -> AnnotationTarget | None:
        if self.detail is None:
            return None
        entry = next(
            (item for item in self.detail.entries if item.index == self.current_entry_index), None
        )
        return (
            AnnotationTarget(self.detail.session.session_id, entry.source_event_sequence)
            if entry is not None
            else None
        )
    def toggle_entry_bookmark(self) -> None:
        target = self.current_entry_target()
        if target is None:
            self.set_action_status("No exact timeline entry is available to bookmark.", warning=True)
            return
        bookmarked = self.annotations.toggle_bookmark(target)
        self._after_bookmark_change(
            "Bookmarked current timeline entry." if bookmarked else "Removed entry bookmark."
        )
    def toggle_session_bookmark(self) -> None:
        session = self.model().selected
        if session is None:
            self.set_action_status("No session is available to bookmark.", warning=True)
            return
        bookmarked = self.annotations.toggle_bookmark(AnnotationTarget(session.session_id))
        self._after_bookmark_change(
            "Bookmarked current session." if bookmarked else "Removed session bookmark."
        )
    def _after_bookmark_change(self, message: str) -> None:
        self.callbacks.refresh_browser()
        self.set_action_status(message)
    def _notes_expander(self, detail: object) -> object:
        Gtk = self.context.Gtk
        expander = Gtk.Expander(label="Private notes · local annotation database only")
        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=10,
            margin_bottom=10,
            margin_start=10,
            margin_end=10,
        )
        session_note = self.annotations.get_note(AnnotationTarget(detail.session.session_id))
        self.session_note_view = self._note_editor(session_note.text if session_note else "")
        outer.append(Gtk.Label(label="Session note", xalign=0))
        outer.append(self.session_note_view)
        outer.append(self._note_buttons("session"))
        self.entry_note_label = Gtk.Label(label="Current entry note", xalign=0)
        outer.append(self.entry_note_label)
        self.entry_note_view = self._note_editor("")
        outer.append(self.entry_note_view)
        outer.append(self._note_buttons("entry"))
        expander.set_child(outer)
        self._load_entry_note()
        return expander
    def _note_editor(self, text: str) -> object:
        view = self.context.Gtk.TextView(
            wrap_mode=self.context.Gtk.WrapMode.WORD_CHAR,
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
        buttons = self.context.Gtk.Box(
            orientation=self.context.Gtk.Orientation.HORIZONTAL, spacing=8
        )
        save = self.context.Gtk.Button(label=f"Save {scope} note")
        save.connect("clicked", lambda _button: self._save_note(scope))
        delete = self.context.Gtk.Button(label=f"Delete {scope} note")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda button: self._delete_note(scope, button))
        buttons.append(save)
        buttons.append(delete)
        return buttons
    def _note_target(self, scope: str) -> AnnotationTarget | None:
        session = self.model().selected
        if session is None:
            return None
        return AnnotationTarget(session.session_id) if scope == "session" else self.current_entry_target()
    def _note_view(self, scope: str) -> object:
        return self.session_note_view if scope == "session" else self.entry_note_view
    def _save_note(self, scope: str) -> None:
        target = self._note_target(scope)
        if target is None:
            self.set_action_status("No exact annotation target is available.", warning=True)
            return
        buffer = self._note_view(scope).get_buffer()
        start, end = buffer.get_bounds()
        try:
            note = self.annotations.save_note(target, buffer.get_text(start, end, True))
        except ValueError as exc:
            self.set_action_status(str(exc), warning=True)
            return
        buffer.set_text(note.text)
        self._pending_note_delete = None
        self.set_action_status(f"Saved private {scope} note locally.")
    def _delete_note(self, scope: str, button: object) -> None:
        target = self._note_target(scope)
        if target is None or self.annotations.get_note(target) is None:
            self.set_action_status(f"No private {scope} note exists to delete.", warning=True)
            return
        if self._pending_note_delete != target:
            self._pending_note_delete = target
            button.set_label("Confirm delete")
            self.set_action_status(
                f"Press Confirm delete to remove this exact private {scope} note.", warning=True
            )
            return
        self.annotations.delete_note(target)
        self._note_view(scope).get_buffer().set_text("")
        self._pending_note_delete = None
        button.set_label(f"Delete {scope} note")
        self.set_action_status(f"Deleted private {scope} note.")
    def _load_entry_note(self) -> None:
        if not hasattr(self, "entry_note_view"):
            return
        target = self.current_entry_target()
        note = self.annotations.get_note(target) if target else None
        self.entry_note_view.get_buffer().set_text(note.text if note else "")
        self.entry_note_view.set_sensitive(target is not None)
        self._pending_note_delete = None
    def _relationship_box(self, session: CatalogSession) -> object | None:
        parent = self.catalog().parent_of(session.session_id)
        children = self.catalog().children_of(session.session_id)
        if parent is None and not children:
            return None
        section = self.context.Gtk.Box(
            orientation=self.context.Gtk.Orientation.VERTICAL, spacing=6
        )
        heading = self.context.Gtk.Label(label="Related sessions", xalign=0)
        heading.add_css_class("heading")
        section.append(heading)
        related_sessions = ([] if parent is None else [("Parent", parent)]) + [
            ("Sub-agent", child) for child in children
        ]
        for label, related in related_sessions:
            button = self.context.Gtk.Button(label=f"{label} · {related.session_id}")
            button.set_halign(self.context.Gtk.Align.START)
            button.connect(
                "clicked",
                lambda _button, target=related.session_id: self.callbacks.navigate(target),
            )
            section.append(button)
        return section
    def _on_entry_expanded(self, row: object, _pspec: object, entry_index: int) -> None:
        if row.get_expanded():
            self.current_entry_index = entry_index
            self._load_entry_note()
    def _provenance_grid(self, session: CatalogSession, presented: PresentedEntry) -> object:
        entry = presented.entry
        return self._key_value_grid(
            (
                ("Source session", session.session_id),
                ("Event sequence", str(entry.source_event_sequence)),
                ("Original UTC", entry.original_timestamp_utc),
                ("Original-text SHA-256", entry.original_text_sha256),
                ("Normalized text", entry.text),
                ("Redacted", "yes" if entry.redacted else "no"),
            )
        )
    def _details_grid(self, session: CatalogSession) -> object:
        parent = self.catalog().parent_of(session.session_id)
        children = self.catalog().children_of(session.session_id)
        return self._key_value_grid(
            (
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
        )
    def _key_value_grid(self, values: tuple[tuple[str, str], ...]) -> object:
        grid = self.context.Gtk.Grid(
            column_spacing=16,
            row_spacing=8,
            margin_top=10,
            margin_bottom=10,
            margin_start=10,
            margin_end=10,
        )
        for index, (label, value) in enumerate(values):
            key = self.context.Gtk.Label(label=label, xalign=1, yalign=0)
            key.add_css_class("dim-label")
            content = self.context.Gtk.Label(label=value, xalign=0, wrap=True, selectable=True)
            content.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            content.set_hexpand(True)
            grid.attach(key, 0, index, 1, 1)
            grid.attach(content, 1, index, 1, 1)
        return grid
    def update_availability(self, session_ready: bool, detail_ready: bool) -> None:
        if self.open_project_button is not None:
            self.open_project_button.set_sensitive(session_ready)
        if self.select_mode_button is not None:
            self.select_mode_button.set_sensitive(detail_ready)
