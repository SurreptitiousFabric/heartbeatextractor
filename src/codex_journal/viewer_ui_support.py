from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
@dataclass(frozen=True)
class UIContext:
    Adw: Any
    Gio: Any
    GLib: Any
    Gtk: Any
    application: object
    window: object
    repo_root: Path
    state_root: Path
def accessible(context: UIContext, widget: object, label: str) -> None:
    widget.update_property([context.Gtk.AccessibleProperty.LABEL], [label])
def clear_box(box: object) -> None:
    while child := box.get_first_child():
        box.remove(child)
def selected_text(dropdown: object) -> str | None:
    item = dropdown.get_selected_item()
    return item.get_string() if item is not None else None
def select_dropdown_value(dropdown: object, value: str) -> None:
    model = dropdown.get_model()
    if model is None:
        return
    for index in range(model.get_n_items()):
        item = model.get_item(index)
        if item is not None and item.get_string() == value:
            dropdown.set_selected(index)
            return
