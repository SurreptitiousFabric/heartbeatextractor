# Architecture and trust boundaries

Heartbeat Extractor is two connected local programs: a deterministic extractor
and a native viewer. The viewer never becomes a second raw-log parser.

```text
read-only Codex rollout JSONL
            |
            v
bounded allowlisted extractor ----> ignored incremental SQLite state
            |
            v
generated Markdown + JSON provenance + indexes
            |
            v
bounded viewer catalog ----> ignored, rebuildable FTS5 index
            |
            v
browser model + presenter ----> GTK4/libadwaita widgets

ignored annotation SQLite --------> bookmarks / private notes / preferences
ignored atomic JSON state --------> window / filters / selection / sync summary
```

## Layer responsibilities

### Raw discovery and extraction

`parser.py`, `engine.py`, `compact.py`, and `redact.py` locate rollout JSONL
beneath the configured Codex state root. They stream bounded records, allowlist
only proven visible assistant commentary, use the event's own timestamp,
redact before persistence, and compact mechanically. Unknown schemas fail
closed. The extractor never modifies or locks the source.

`state.py` stores incremental offsets, prefix hashes, and extracted safe
candidates in ignored `state/journal.sqlite3`. A matching prefix plus a larger
file is append-only. A shorter file or changed prefix rebuilds the session.

### Generated artifacts

`render.py` atomically writes one Markdown journal and one JSON provenance file
per session, plus root and project indexes. Provenance stores the source event
sequence, exact UTC timestamp, hash of the original visible text, normalized
text, and redaction flag—not the original unredacted body.

Generated artifacts are useful but not automatically public. They remain
uncommitted until a person reviews them.

### Viewer catalog and search

`viewer_catalog.py` reads bounded front matter eagerly and journal/provenance
details lazily. It rejects malformed, oversized, duplicate, unsupported, or
unknown data. It never imports or calls the raw parser.

The FTS5 index in ignored `state/viewer.sqlite3` contains only generated safe
entry text and safe metadata. It can be rebuilt atomically. Private notes and
raw source fields are not indexed.

`viewer_tags.py` adds mechanical labels for failure, test, security, blocker,
correction, commit, issue/PR, stop, and filename patterns. Tags do not rewrite
or infer meaning.

### Presentation and GTK

`viewer_model.py` composes deterministic filters and selection.
`viewer_presenter.py` converts stored event timestamps into the journal's
recorded timezone and prepares date groups and visible indicators.
`viewer_ui.py` owns the adaptive GTK widgets and starts potentially expensive
sync or activity work in background threads. UI callbacks receive safe counts
or error types rather than raw exception payloads.

The application retains its Python controller for the whole window lifetime.
Closing the window atomically stores bounded safe UI state and closes local
databases.

### Private local state

`viewer_annotations.py` stores bookmarks, private notes, and three preferences
in ignored `state/annotations.db`. Notes are limited to 64 KiB, excluded from
search, separate from journals, and excluded from export unless the person
checks the explicit opt-in control.

`viewer_state.py` stores only bounded selection, filter, window, entry index,
and sync-summary values in ignored `state/viewer-state.json`. It deliberately
does not store search queries or journal text.

### Local actions and export

`viewer_actions.py` opens a project directory only after resolving it beneath
the user's home and rejecting missing paths and symlink escapes. Copy actions
use sanitized visible entry text only.

`viewer_compare.py` compares exact normalized text without semantic matching or
causal claims. `viewer_activity.py` counts exact generated session and entry
references without productivity scoring. `viewer_export.py` builds typed,
bounded Markdown or JSON exports, shows exact scope before writing, escapes
Markdown, requires a local absolute destination, confirms replacement, and
writes atomically.

## Invariants

- No viewer module reads rollout JSONL.
- No prompt, hidden reasoning, final answer, tool call, tool output, or
  inter-agent payload is an extraction fallback.
- No event timestamp is inferred from order, duration, or file modification
  time.
- No network or telemetry path exists in extraction, viewing, or export.
- No background daemon or automatic Git action exists.
- Unknown data types and malformed private state fail closed.
- Append warnings never hide a changed recorded prefix, truncation, or
  replacement error.
- Generated journals, raw logs, and private annotation state are never included
  in implementation commits.

## Bounded-resource policy

Individual raw JSONL records default to 4 MiB. Viewer metadata is capped at 64
KiB, a generated journal at 8 MiB, provenance at 32 MiB, UI state at 256 KiB,
one note at 64 KiB, comparison at 10,000 entries per side, export preview at 4
MiB, and final export at 16 MiB. Large session files are streamed; catalog
details are lazy; FTS results are capped.

The measured local v1 baseline and reproducible checks are recorded in
[VALIDATION.md](VALIDATION.md).
