# Codex Session Journal

Build deterministic development journals from the timestamped progress
messages that Codex actually displayed to the user. The tool reads local
rollout JSONL incrementally and never sends session data to a network service.

> **Privacy warning:** Generated journals can still contain private project
> names, findings, issue numbers, filenames, and test results. Review every
> generated file before committing it or pushing it to any remote.

Only assistant messages explicitly marked as visible commentary are eligible.
User prompts, hidden reasoning, final answers, tool calls, tool output, and
inter-agent traffic fail closed. Obvious credentials are redacted before any
output is written.

## Requirements and installation

Python 3.11 or newer is required. Runtime code uses only the Python standard
library.

```sh
python3 -m venv .venv
.venv/bin/pip install --no-build-isolation -e .
```

No daemon, hook, remote, automatic commit, API call, or external summarizer is
used.

## Commands

```sh
codex-journal discover
codex-journal sync --timezone Europe/Zurich
codex-journal sync --session SESSION_ID --timezone Europe/Zurich
codex-journal rebuild --session SESSION_ID --timezone Europe/Zurich
codex-journal verify
```

`discover` prints safe metadata only: session ID, start timestamp, source kind,
shortened working directory, repository, and branch. It never prints prompt or
heartbeat bodies.

`sync` writes journals beneath `journal/YYYY/MM/DD/`, provenance companions
beside them, a root `INDEX.md`, and project indexes beneath `projects/`.
Incremental processing state lives in the ignored `state/journal.sqlite3`.

`rebuild` discards cached extraction state for exactly one session and rereads
that source. `verify` is read-only and checks metadata, fingerprints,
provenance, duplicate IDs, and index links.

The source root is `$CODEX_HOME` when set and `~/.codex` otherwise. Source logs
are opened read-only and are never copied into this repository. See
[the observed schema](docs/CODEX-LOG-FORMAT.md) and
[journal rules](docs/JOURNAL-FORMAT.md).

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests
```
