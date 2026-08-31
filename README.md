# Heartbeat Extractor

Heartbeat Extractor turns Codex's saved, timestamped progress messages into a
small development diary and lets you browse it in a native GTK4 application.
In plain English: it keeps the useful “what I am doing / what I found / what
passed” updates and leaves prompts, hidden reasoning, commands, and tool output
out.

The source logs are static files on disk. `sync` reads what exists now and
creates or updates the privacy-filtered journals. Run `sync` again later to
consume newly appended events. Unchanged sessions are left byte-for-byte
alone; an append is processed incrementally; a truncated or replaced source is
rebuilt.

> **Privacy warning:** Generated journals can still contain private project
> names, findings, issue numbers, filenames, repository paths, and test
> results. Review every generated journal and every export before committing
> it or pushing it to any remote.

The project is local-first and offline. It has no daemon, hook, automatic Git
commit, API call, external model, summarization service, telemetry, or runtime
network dependency.

## Quick start with Mise

Python and Python-installed development tools must come from the repository's
Mise environment. The project pins Python 3.14.7; do not create this project's
environment from `/usr/bin/python`.

```sh
cd heartbeatextractor
mise install
mise current python
mise run bootstrap
mise exec -- codex-journal sync --timezone Europe/Zurich
mise exec -- codex-journal view
```

`mise current python` must report the project-selected Mise runtime. The
extractor itself has no Python package dependencies. The optional viewer extra
installs PyGObject and expects GTK4, libadwaita, and their introspection data to
be available on the operating system.

## Extractor commands

```sh
mise exec -- codex-journal discover
mise exec -- codex-journal sync --timezone Europe/Zurich
mise exec -- codex-journal sync --session SESSION_ID --timezone Europe/Zurich
mise exec -- codex-journal rebuild --session SESSION_ID --timezone Europe/Zurich
mise exec -- codex-journal verify
```

- `discover` prints safe metadata only: session ID, start timestamp, source
  kind, shortened working directory, repository, and branch. It never prints
  prompts or heartbeat bodies.
- `sync` processes all sessions, or one exact `--session`, and atomically
  updates journals, provenance, and indexes.
- `rebuild` rereads one exact source rather than trusting its incremental
  cache.
- `verify` checks generated structure, processing state, source snapshots,
  provenance, duplicate IDs, and index links without modifying source logs.
  A valid append after the recorded snapshot is a warning; truncation,
  replacement, or a changed recorded prefix is an error.

The state root is `$CODEX_HOME` when set and `~/.codex` otherwise. Source logs
are opened read-only and are never copied into this repository.

## Native viewer

Run `mise exec -- codex-journal view`. The adaptive GTK4/libadwaita interface
provides:

- project, date, branch, lifecycle, source, bookmark, redaction, extraction
  error, and deterministic tag filters;
- local full-text search across generated journal entries only;
- timestamped timelines with expandable provenance and session metadata;
- keyboard-first previous/next session and entry navigation;
- parent/sub-agent links, bookmarks, private notes, exact session comparison,
  and daily/weekly/project activity views;
- manual sync, optional Sync on launch, optional five-minute refresh while the
  window is open, and exact change summaries;
- validated project-directory opening, reviewed copy actions, and atomic
  Markdown or JSON export with private notes excluded by default; and
- remembered window, filter, selection, timeline, theme, and sync-summary
  state.

The viewer reads generated journals and provenance, never rollout JSONL. See
[the viewer guide](docs/VIEWER.md) for workflows, shortcuts, accessibility, and
troubleshooting.

## Data and privacy boundary

| Data | Location | Git policy |
| --- | --- | --- |
| Raw Codex sessions | `$CODEX_HOME/sessions/` or `~/.codex/sessions/` | Never copied here |
| Generated journals and provenance | `journal/YYYY/MM/DD/` | Generated and intentionally left uncommitted |
| Generated indexes | `INDEX.md`, `projects/*.md` | Generated and intentionally left uncommitted |
| Incremental extraction state | `state/journal.sqlite3` | Ignored |
| Viewer search index | `state/viewer.sqlite3` | Ignored and rebuildable |
| Bookmarks, private notes, preferences | `state/annotations.db` | Ignored; never searched; notes export only by explicit opt-in |
| Window/filter/selection state | `state/viewer-state.json` | Ignored; contains no journal text or search query |

Only assistant messages proven by the observed schema to be user-visible
commentary are eligible. User prompts, hidden reasoning, final answers, tool
calls, tool output, environment dumps, and inter-agent traffic fail closed.
Obvious credentials are redacted before generated output is written.

## Development

```sh
mise run check
```

That task runs `compileall`, `tabnanny`, and the complete `unittest` suite in
the Mise-managed environment. Focused GTK tests run when a graphical display
and the optional viewer dependencies are available and skip safely otherwise.

Further documentation:

- [Observed Codex log schema](docs/CODEX-LOG-FORMAT.md)
- [Journal and provenance format](docs/JOURNAL-FORMAT.md)
- [Architecture and trust boundaries](docs/ARCHITECTURE.md)
- [Viewer operation and shortcuts](docs/VIEWER.md)
- [Viewer v1 validation record](docs/VALIDATION.md)
