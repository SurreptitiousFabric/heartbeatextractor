from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable, TypeVar


Result = TypeVar("Result")


def atomic_replace(destination: Path, build: Callable[[Path], Result]) -> Result:
    """Build a file beside its destination, fsync it, and atomically replace it."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        result = build(temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
        return result
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def atomic_write_bytes(destination: Path, content: bytes) -> None:
    atomic_replace(destination, lambda temporary: temporary.write_bytes(content))
