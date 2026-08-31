# Viewer v1 validation record

This record contains aggregate, non-private evidence only. No raw rollout,
journal body, prompt, hidden reasoning, tool output, private note, credential,
or machine-specific secret is included.

## Supported environment

- Project runtime: Python 3.14.7 selected by repository-local Mise config.
- Minimum declared Python: 3.11.
- Native test environment: GTK 4.22.4, libadwaita 1.9.3, PyGObject 3.58.
- Core package dependencies: none.
- Optional viewer dependency: `PyGObject>=3.54,<4`.

## Automated gates

`mise run check` runs compileall, tabnanny, and the complete standard-library
`unittest` suite. A fresh Mise-created environment installed the editable core
plus optional viewer, imported GTK/libadwaita, and passed 100 tests, including
three focused tests using real GTK widgets for actions/selection, empty and
malformed states, and non-blocking asynchronous sync.

Privacy regressions cover prompts, hidden reasoning, command arguments, tool
output, final answers, environment dumps, source paths, credentials, private
notes, unknown event types, redaction, and home shortening. Robustness tests
cover malformed and oversized raw records, generated artifacts and state;
append/truncate/replace behavior; atomic writes; duplicate IDs; stable paths;
idempotence; symlink refusal; and bounded comparison/export.

## Feature acceptance matrix

| Capability | Implementation evidence | Test evidence |
| --- | --- | --- |
| Project/date/branch/status browsing | `viewer_catalog.py`, `viewer_model.py`, adaptive sidebar in `viewer_ui.py` | `test_viewer_catalog.py`, `test_viewer_model.py`, GTK selection test |
| Timeline, timestamps, metadata, provenance | `viewer_presenter.py`, timeline/provenance widgets | Presenter DST/date/tag tests and GTK content-state test |
| Full-text search, filters, tags | Generated-only FTS5 index and `viewer_tags.py` | Catalog search/privacy and model composition tests |
| Lifecycle, redaction, extraction-error indicators | Safe front matter and explicit row/detail labels | Model/presenter/catalog malformed-state tests |
| Keyboard and remembered position | 18 window actions, key controller, bounded atomic UI state | GTK action test and `test_viewer_state.py` |
| Parent/sub-agent navigation | Generated parent metadata and relation buttons | Extractor relation and catalog relation tests |
| Manual/launch/periodic sync and summaries | In-process worker, five-minute window timer, snapshot diff | GTK async-sync and `test_viewer_sync.py` |
| Safe project open and copy | Home-contained path resolver and sanitized copy builders | `test_viewer_actions.py` |
| Bookmarks and private notes | Separate ignored annotation SQLite store | `test_viewer_annotations.py` and privacy boundary test |
| Exact comparison | Bounded exact normalized-text sequence comparison | `test_viewer_compare.py` |
| Daily/weekly/project activity | Traceable exact session/entry buckets | `test_viewer_activity.py` |
| Reviewed export | Typed preview, note opt-in, Markdown escaping, atomic local write | `test_viewer_export.py` |
| Offline/no-telemetry privacy boundary | Generated-only viewer imports and no network implementation | Extractor privacy tests and `test_viewer_privacy.py` |

## Aggregate real-data baseline

On 31 August 2026, the generated-only catalog loaded 465 local sessions with
zero catalog diagnostics in 44 ms. It indexed 48,609 sanitized entries in 1.74
seconds, returned a capped 100-result search in 9 ms, and built daily, weekly,
and project activity over the same generated set in 1.74 seconds.

Final `codex-journal verify` enumerated 465 journals and 49,230 entries with zero
errors. Two valid concurrently appended sources produced explicit warnings
after their recorded prefixes passed SHA-256 verification. Fixture tests prove
that changed prefixes and truncation remain errors.

A second all-session sync observed two real appends, so that input was correctly
not labeled unchanged. The idempotence proof then selected one completed source
without displaying its ID: two exact syncs both reported `unchanged=1`, and
SHA-256 manifests for all 938 generated journal, provenance, and index files,
plus the two tracked directory placeholders, were byte-identical.

These are development-machine observations, not guaranteed benchmarks. The
architectural bounds—not those timings—are the compatibility contract.

## Manual and publication boundary

The standalone GTK command remained open under a foreground watchdog until it
was explicitly stopped. A real generated-catalog launch loaded 465 sessions
with zero diagnostics, selected the content state, created 150 timeline widgets,
registered all 18 window actions, and left no sync running. UI behavior was
exercised with sanitized fixtures and real journals only on the local machine.
Invalid compositor captures were deleted. Viewer v1 intentionally ships no
public screenshot; any future public screenshot must be produced from
sanitized fixtures only.

No generated journal, provenance file, generated project index, raw rollout,
private note, ignored viewer state, credential, or screenshot was included in
the implementation commits or pushed to GitHub.
