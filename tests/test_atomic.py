from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_journal.atomic import atomic_replace, atomic_write_bytes


class AtomicReplacementTests(unittest.TestCase):
    def test_bytes_are_replaced_once_without_a_temporary_left_behind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "nested" / "result.txt"
            with patch("codex_journal.atomic.os.replace", wraps=os.replace) as replace:
                atomic_write_bytes(destination, b"complete\n")
            self.assertEqual(destination.read_bytes(), b"complete\n")
            replace.assert_called_once()
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*.tmp")), [])

    def test_builder_result_is_returned_and_failure_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "result.db"

            def build(path: Path) -> int:
                path.write_bytes(b"database")
                return 42

            self.assertEqual(atomic_replace(destination, build), 42)
            with patch("codex_journal.atomic.os.replace", side_effect=OSError("failure")):
                with self.assertRaises(OSError):
                    atomic_write_bytes(destination, b"replacement")
            self.assertEqual(destination.read_bytes(), b"database")
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
