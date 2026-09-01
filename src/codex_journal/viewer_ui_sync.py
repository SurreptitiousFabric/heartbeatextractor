from __future__ import annotations
from datetime import datetime
from threading import Thread
from typing import Callable
from .engine import JournalEngine
from .viewer_annotations import AnnotationStore
from .viewer_catalog import JournalCatalog
from .viewer_state import ViewerState
from .viewer_sync import CatalogSnapshot, ChangeSummary, compare_snapshots, rebuild_search_index_atomic
from .viewer_ui_support import UIContext, accessible
class SyncController:
    def __init__(
        self,
        context: UIContext,
        annotations: AnnotationStore,
        saved_state: ViewerState,
        catalog: Callable[[], JournalCatalog],
        refresh: Callable[[bool], None],
    ) -> None:
        self.context = context
        self.annotations = annotations
        self.catalog = catalog
        self.refresh = refresh
        self.last_sync_at = saved_state.last_sync_at
        self.last_sync_summary = saved_state.last_sync_summary
        self.running = False
        self.closed = False
        self._periodic_source: int | None = None
        self._launch_started = False
        self._sync_on_launch = annotations.get_preference("sync_on_launch", "false") == "true"
        self._periodic = annotations.get_preference("periodic_sync", "false") == "true"
        self.button: object | None = None
        self.status: object | None = None
        self.sync_on_launch_check: object | None = None
        self.periodic_sync_check: object | None = None
    def create_button(self) -> object:
        button = self.context.Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text="Sync source sessions"
        )
        accessible(self.context, button, "Sync source sessions")
        button.connect("clicked", lambda *_args: self.start())
        self.button = button
        return button
    def create_status(self) -> object:
        status = self.context.Gtk.Label(
            label="Loading generated journals…",
            xalign=0,
            wrap=True,
            margin_start=12,
            margin_end=12,
            margin_bottom=8,
            selectable=True,
        )
        status.add_css_class("caption")
        self.status = status
        return status
    def append_preferences(self, box: object) -> None:
        self.sync_on_launch_check = self.context.Gtk.CheckButton(label="Sync on launch")
        self.sync_on_launch_check.set_active(self._sync_on_launch)
        self.sync_on_launch_check.connect("toggled", self._on_setting_changed)
        box.append(self.sync_on_launch_check)
        self.periodic_sync_check = self.context.Gtk.CheckButton(
            label="Sync every 5 minutes while open"
        )
        self.periodic_sync_check.set_active(self._periodic)
        self.periodic_sync_check.connect("toggled", self._on_setting_changed)
        box.append(self.periodic_sync_check)
    def display_text(self) -> str:
        loaded = len(self.catalog().sessions)
        if not self.last_sync_at:
            return (
                f"Displaying {loaded} generated journal(s). "
                "Viewer sync has not run; source freshness is not claimed."
            )
        return (
            f"Displaying {loaded} generated journal(s). "
            f"Last viewer sync: {self.last_sync_at}\n{self.last_sync_summary or ''}"
        ).strip()
    def update_display(self) -> None:
        if self.status is not None:
            self.status.set_label(self.display_text())
    def set_status(self, text: str) -> None:
        if self.status is not None:
            self.status.set_label(text)
    def catalog_ready(self) -> None:
        self.update_display()
        if self._launch_started:
            return
        self._launch_started = True
        self._configure_periodic()
        if self.sync_on_launch_check.get_active():
            self.context.GLib.idle_add(self.start)
    def start(self) -> bool:
        if self.closed or self.running:
            return False
        self.running = True
        if self.button is not None:
            self.button.set_sensitive(False)
        self.set_status("Sync running… generated journals remain usable.")
        before = CatalogSnapshot.from_catalog(self.catalog())
        Thread(target=self._worker, args=(before,), daemon=True).start()
        return False
    def _worker(self, before: CatalogSnapshot) -> None:
        try:
            result = JournalEngine(self.context.repo_root, self.context.state_root).sync()
            refreshed = JournalCatalog(self.context.repo_root)
            refreshed.refresh()
            summary = compare_snapshots(before, CatalogSnapshot.from_catalog(refreshed))
            rebuild_search_index_atomic(
                refreshed, self.context.repo_root / "state" / "viewer.sqlite3"
            )
            self.context.GLib.idle_add(self._finish, result, summary, None)
        except Exception as exc:  # worker boundary reports type only
            self.context.GLib.idle_add(self._finish, None, None, type(exc).__name__)
    def _finish(
        self,
        result: object | None,
        summary: ChangeSummary | None,
        failure: str | None,
    ) -> bool:
        self.running = False
        if self.closed:
            return False
        if self.button is not None:
            self.button.set_sensitive(True)
        if failure or result is None or summary is None:
            self.set_status(f"Sync failed safely ({failure or 'unknown error'}).")
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
        self.update_display()
        self.refresh(False)
        return False
    def _on_setting_changed(self, _button: object) -> None:
        if self._launch_started:
            self._configure_periodic()
    def _configure_periodic(self) -> None:
        if self._periodic_source is not None:
            self.context.GLib.source_remove(self._periodic_source)
            self._periodic_source = None
        if self.periodic_sync_check.get_active() and not self.closed:
            self._periodic_source = self.context.GLib.timeout_add_seconds(300, self._periodic_tick)
    def _periodic_tick(self) -> bool:
        if self.closed or not self.periodic_sync_check.get_active():
            self._periodic_source = None
            return False
        self.start()
        return True
    def close(self) -> None:
        self.closed = True
        if self._periodic_source is not None:
            self.context.GLib.source_remove(self._periodic_source)
            self._periodic_source = None
        self.annotations.set_preference(
            "sync_on_launch", "true" if self.sync_on_launch_check.get_active() else "false"
        )
        self.annotations.set_preference(
            "periodic_sync", "true" if self.periodic_sync_check.get_active() else "false"
        )
