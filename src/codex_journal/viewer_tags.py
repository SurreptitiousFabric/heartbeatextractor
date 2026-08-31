from __future__ import annotations

import re


TAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("failure", re.compile(r"\b(?:fail(?:ed|ure)?|error|defect|broken)\b", re.IGNORECASE)),
    ("test", re.compile(r"\b(?:test(?:s|ed|ing)?|validation|contract(?:s)?|pass(?:ed|ing)?)\b", re.IGNORECASE)),
    ("security", re.compile(r"\b(?:security|credential|secret|token|permission|fail-open|vulnerab\w*)\b", re.IGNORECASE)),
    ("blocker", re.compile(r"\b(?:blocker(?:s)?|blocked|objection(?:s)?)\b", re.IGNORECASE)),
    ("correction", re.compile(r"\b(?:correct(?:ed|ion)?|fix(?:ed)?|withdrawn|retracted)\b", re.IGNORECASE)),
    ("commit", re.compile(r"(?:\b(?:commit(?:ted)?|push(?:ed)?)\b|\b[0-9a-f]{7,40}\b)", re.IGNORECASE)),
    ("issue/PR", re.compile(r"(?:\bissue(?:s)?\b|\bpull request(?:s)?\b|\bPRs?\b|#[0-9]+)", re.IGNORECASE)),
    ("stop", re.compile(r"\b(?:stop(?:ped|ping)?|paused|halted|remains un(?:committed|integrated|executed))\b", re.IGNORECASE)),
    ("filename", re.compile(r"(?:^|\s)[\w./-]+\.(?:py|rs|go|js|ts|tsx|jsx|md|toml|yaml|yml|json|sh|c|h|cpp)(?=\s|[,:;.)]|$)", re.IGNORECASE)),
)

TAGS = tuple(label for label, _pattern in TAG_PATTERNS)


def classify_entry(text: str) -> tuple[str, ...]:
    """Return stable mechanical labels without rewriting the visible text."""

    return tuple(label for label, pattern in TAG_PATTERNS if pattern.search(text))
