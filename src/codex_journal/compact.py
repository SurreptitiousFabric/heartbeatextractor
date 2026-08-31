from __future__ import annotations

import re

from .model import Candidate, JournalEntry


SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9#`~])")
DECORATION_RE = re.compile(r"^(?:[-*•✓✔⏳🔍🛠]+\s*)+")
WHITESPACE_RE = re.compile(r"\s+")
NOISE_RE = re.compile(
    r"^(?:Ran \d+ commands?|Explored\b|Read\b|Token count\b|Use (?:up|down) arrows\b)",
    re.IGNORECASE,
)
EMPTY_WAIT_RE = re.compile(r"^Waiting for (?:agents?|sub-agents?)\.?$", re.IGNORECASE)
ROUTINE_RE = re.compile(
    r"^(?:I(?:'m| am) )?(?:still |currently )?(?:working|continuing|checking|reviewing|waiting)\b",
    re.IGNORECASE,
)
PROTECTED_RE = re.compile(
    r"\b(?:fail(?:ed|ure|ing)?|error|defect|bug|security|vulnerab|blocker|blocked|objection|reviewer|correct(?:ed|ion)|fix(?:ed)?|pass(?:ed|ing)?|test(?:s|ed|ing)?|commit(?:ted)?|push(?:ed)?|issue|pull request|\bPR\b|stop(?:ped|ping)?|uncommitted|unintegrated|unexecuted|not demonstrated|demonstrated|withdrawn|fail-open|credential|secret|token|private key)\b",
    re.IGNORECASE,
)
RESULT_RE = re.compile(
    r"\b(?:found|confirmed|completed|created|updated|wrote|generated|validated|verified|exposed|revealed|identified|resolved|removed|added|changed|passed|failed|corrected|fixed)\b",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(r"[A-Za-z0-9_#./:+-]+")
STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "for",
    "i",
    "im",
    "is",
    "it",
    "now",
    "of",
    "on",
    "still",
    "the",
    "this",
    "to",
    "we",
}


def normalize_sentence(sentence: str) -> str:
    text = WHITESPACE_RE.sub(" ", sentence).strip()
    text = DECORATION_RE.sub("", text).strip()
    text = re.sub(r"^I(?:'m| am) now (reviewing|checking|running|validating|verifying|building|implementing|documenting)\b", lambda m: m.group(1).capitalize(), text, flags=re.IGNORECASE)
    text = re.sub(r"^I(?:'m| am) (reviewing|checking|running|validating|verifying|building|implementing|documenting)\b", lambda m: m.group(1).capitalize(), text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:I have|I've) found\b", "Found", text, flags=re.IGNORECASE)
    text = re.sub(r"^The (?:sub-)?agent has corrected\b", "Sub-agent corrected", text, flags=re.IGNORECASE)
    if text:
        text = text[0].upper() + text[1:]
    return text


def split_sentences(text: str) -> list[str]:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    for block in re.split(r"\n+", cleaned):
        block = block.strip()
        if not block:
            continue
        parts.extend(SENTENCE_RE.split(block))
    return [normalized for part in parts if (normalized := normalize_sentence(part))]


def _subject_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in TOKEN_RE.findall(text)
        if token.lower() not in STOP_WORDS and len(token) > 1
    }


def _same_subject(left: str, right: str) -> bool:
    a = _subject_tokens(left)
    b = _subject_tokens(right)
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.65


def compact_candidates(candidates: list[Candidate]) -> list[JournalEntry]:
    entries: list[JournalEntry] = []
    for candidate in candidates:
        for sentence in split_sentences(candidate.text):
            if NOISE_RE.search(sentence) or EMPTY_WAIT_RE.search(sentence):
                continue
            protected = bool(PROTECTED_RE.search(sentence))
            has_result = bool(RESULT_RE.search(sentence))
            if entries and not protected:
                previous = entries[-1].text
                if sentence.casefold() == previous.casefold():
                    continue
                if ROUTINE_RE.search(sentence) and not has_result and _same_subject(previous, sentence):
                    continue
            entries.append(
                JournalEntry(
                    sequence=candidate.sequence,
                    timestamp_utc=candidate.timestamp_utc,
                    text=sentence,
                    original_text_sha256=candidate.original_text_sha256,
                    redacted=candidate.redacted,
                )
            )
    return entries
