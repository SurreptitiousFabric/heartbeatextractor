# Viewer guide

## Launch and refresh model

```sh
mise exec -- codex-journal view
```

On launch, the viewer reads existing generated journals only. Press Refresh to
reread generated files without touching raw sessions. Press Sync to run the
extractor in-process, update journals atomically, rebuild safe search, and show
counts for new sessions, entries, lifecycle changes, and extraction errors.
The timeline remains usable while sync runs.

The sidebar offers two opt-in modes:

- **Sync on launch** runs one sync after the initial generated catalog appears.
- **Sync every 5 minutes while open** schedules in-process sync only while the
  window exists. This is not a daemon and does not survive application exit.

The last successful time and change summary are remembered locally.

## Browse and search

Sessions are newest first and show project, local start, branch, lifecycle,
source kind, entry count, and warning indicators. Combine full-text search with
project, from/to date, branch, lifecycle status, source kind, bookmark,
redaction, extraction-error, and deterministic tag filters.

Search indexes generated normalized entries only. The deterministic tags are:
`failure`, `test`, `security`, `blocker`, `correction`, `commit`, `issue/PR`,
`stop`, and `filename`. They are regex classifications, not model judgments.

Selecting a session opens its timeline. Entries retain their exact stored UTC
timestamp and render in the journal's recorded timezone. Expand an entry to see
its source sequence, original UTC timestamp, original-text SHA-256, normalized
text, and redaction flag. Session details show lifecycle, working directory,
repository, branch, source kind, relation IDs, counts, and source fingerprint.

Redaction and extraction-error badges say that sanitization or parsing trouble
occurred; they never reveal removed values. An extraction-error expander shows
only record sequence and safe error code.

## Relationships, bookmarks, and notes

Reliable parent metadata creates buttons between parent and sub-agent journals;
sessions remain separate rather than being merged.

Bookmarks can target a whole session or the currently expanded entry. The
sidebar can filter to bookmarked sessions. Bookmarks and private notes live in
ignored `state/annotations.db`, not in generated journals. A note can target the
session or current entry. Deletion requires a second confirmation click.

Private notes are never full-text indexed. They are never exported unless
**Include private notes (explicit opt-in)** is selected in the export review.

## Comparison and activity

View two different sessions, then choose Compare. Metadata appears side by
side. Timeline rows are `unchanged`, `left-only`, or `right-only` based on exact
normalized text. A deterministic tag can filter the rows. The viewer does not
infer causality or reconcile contradictions.

Activity provides exact daily and ISO-week counts from generated sessions and
visible entries, plus one calendar per project. Empty days are explicit. Each
bucket links back to the exact session subset. Activity is not a productivity,
sentiment, duration, or hidden-work score.

## Safe actions and export

Open Project resolves the recorded working directory and permits it only when
it is an existing directory beneath the current user's home without a symlink
escape.

Copy Entry copies one rendered timestamp plus sanitized entry text. Check
timeline rows to copy either the checked entries or the inclusive range between
the first and last check. These actions never copy provenance hashes, raw
source fields, private notes, or tool output.

Export supports the current entry, checked entries, checked time range, latest
comparison, or current activity view. Choose Markdown or JSON, inspect the
scope preview, decide explicitly whether to attach relevant private notes, and
then choose a local path. Existing targets require confirmation and replacement
is atomic. Every export includes a private-information warning.

## Keyboard reference

| Shortcut | Action |
| --- | --- |
| `Ctrl+Page Up` / `Ctrl+Page Down` | Previous / next filtered session |
| `Alt+Up` / `Alt+Down` | Previous / next timeline entry |
| `K` / `J` or arrow up / down | Previous / next entry when not editing text |
| `Ctrl+F` or `/` | Focus generated-journal search |
| `F5` | Refresh generated journal files only |
| `Ctrl+R` | Sync source sessions in-process |
| `Ctrl+O` | Open validated project directory |
| `Ctrl+Alt+C` | Copy current sanitized entry |
| `Ctrl+Alt+Shift+C` | Copy checked entries/range |
| `Ctrl+B` | Bookmark current entry |
| `Ctrl+Shift+B` | Bookmark current session |
| `Ctrl+D` | Toggle session details |
| `Ctrl+Shift+C` | Compare two most recently viewed sessions |
| `Ctrl+Shift+A` | Open activity views |
| `Ctrl+E` | Review and export selected material |
| `Ctrl+Shift+T` | Cycle system, light, and dark themes |
| `Ctrl+Shift+/` | Open the in-app shortcut reference |

`J`, `K`, `/`, and unmodified arrow navigation are disabled while an entry,
search field, or private-note editor owns focus so normal text editing wins.

## Accessibility

The application uses native GTK controls, semantic headings, selectable text,
visible keyboard focus, tooltips, accessible labels for icon-only controls,
system/light/dark color schemes, and an adaptive split view. The sidebar
collapses on narrow windows and exposes a Back button. Status pages distinguish
loading, empty, unselected, and malformed states without depending on color
alone.

Timeline text and metadata wrap rather than being clipped. Buttons remain
available alongside shortcuts. Redaction, extraction errors, and action results
have textual labels.

## Troubleshooting

- **No journals yet:** run `mise exec -- codex-journal sync --timezone ZONE`,
  then press Refresh.
- **Viewer dependency unavailable:** confirm `mise current python`, run
  `mise run bootstrap`, and verify the operating system provides GTK4,
  libadwaita, and introspection data.
- **Generated catalog malformed:** run `mise exec -- codex-journal verify`.
  The viewer will fail closed without reading raw logs as a fallback.
- **Append warning from verify:** the recorded source prefix is valid but that
  active session has newer bytes. Run Sync to catch up. A truncation, changed
  prefix, or replacement remains an error.
- **Private state unavailable:** the viewer shows a fail-closed status page and
  leaves generated journals and source logs unchanged. Back up private notes
  before intentionally replacing annotation state.

The generated journals themselves may contain private project information.
Review them and every export before any remote upload.
