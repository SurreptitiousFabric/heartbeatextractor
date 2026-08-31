# Observed Codex log format

This document describes the local format observed on 31 August 2026. It is an
implementation detail, not a public compatibility promise. Official OpenAI
documentation found during discovery did not specify this on-disk schema.

## State root and files

`CODEX_HOME` was unset on the inspected machine, so the state root resolved to
`~/.codex`.

- `sessions/YYYY/MM/DD/rollout-<local-date-time>-<session-id>.jsonl` contains
  the authoritative event stream. Each rollout is read incrementally as JSONL.
- `session_index.jsonl` contains `id`, `thread_name`, and `updated_at` records.
  The title is not needed and is never printed or journaled. The bounded sample
  contained one NUL-filled malformed record, so the index is not authoritative.
- `history.jsonl` contains user history and is deliberately excluded.
- SQLite databases, shell snapshots, logs, caches, and authentication files
  are deliberately excluded from extraction.

Discovery reads rollout files only after locating the state layout. It never
modifies, locks, normalizes, renames, copies, truncates, or deletes source
state.

## Bounded discovery sample

The investigation examined record shapes, discriminators, and safe metadata
from:

- a completed ordinary CLI session ending in `task_complete`;
- a sub-agent session whose source records a parent thread; and
- the current CLI session while it was being appended.

Message bodies were suppressed during shape discovery. As a visibility check,
the SHA-256 hash of a known TUI commentary message matched exactly one
persisted assistant commentary record and its timestamp.

## JSONL envelope

Every valid observed record is a JSON object with:

- `timestamp`: an RFC 3339 UTC timestamp for that exact event;
- `type`: the outer record discriminator; and
- `payload`: an object whose schema depends on `type`.

Record order supplies a zero-based source sequence number for provenance. The
sequence number is not used to invent time.

## Session metadata

The first `type: session_meta` record has a payload containing:

- `id` and `session_id`: the stable session ID (equal in observed records);
- `timestamp`: the session start timestamp in UTC;
- `cwd`: the working directory;
- `source`: `cli` for ordinary sessions, or a structured sub-agent object;
- `git.branch`, `git.commit_hash`, and `git.repository_url`, when Codex
  captured Git metadata;
- version/provider/context fields that are not needed for journals.

Some long-lived streams contain another `session_meta` after compaction. The
first valid metadata record is authoritative for identity and start time.

For sub-agents, the observed parent relationship is:

```text
payload.source.subagent.thread_spawn.parent_thread_id
payload.source.subagent.thread_spawn.depth
payload.source.subagent.thread_spawn.agent_path
```

Inter-agent delivery records are not user heartbeats and are excluded.

## Visible and excluded messages

The proven user-visible heartbeat record is exactly:

```text
type = response_item
payload.type = message
payload.role = assistant
payload.phase = commentary
payload.content[*].type = output_text
```

The content item's `text` is the visible commentary body. The envelope
`timestamp` is the heartbeat time.

Final responses use the same message shape with
`payload.phase = final_answer`. They are recognized for schema validation but excluded from
version 1 journals.

The following observed records are always excluded:

- `response_item` with `payload.type = reasoning`;
- messages with `role = user` or `role = developer`;
- `custom_tool_call`, `custom_tool_call_output`, `function_call`, and
  `function_call_output`;
- `agent_message` and `inter_agent_communication_metadata`;
- `event_msg` UI/control records such as `item_completed` and `token_count`;
- unknown outer types, payload types, phases, roles, and content types.

This allowlist is intentionally fail-closed. A field named `reasoning` is
never treated as a substitute for visible commentary.

## Lifecycle and timestamps

Observed `event_msg.payload.type` values include:

- `task_started`: a turn began;
- `task_complete`: a turn completed after a final answer;
- `turn_aborted`: a turn stopped without normal completion;
- `thread_settings_applied`, `item_completed`, and `token_count`: non-journal
  control or UI events.

The latest lifecycle event determines status:

- `task_started` means `active`;
- `task_complete` means `completed`;
- `turn_aborted` means `incomplete`;
- no proven lifecycle event means `incomplete`.

`ended_at_utc` is the exact timestamp of the latest `task_complete` or
`turn_aborted` only when that event is the latest lifecycle state. Active and
unclassified sessions have no end time. File modification time is never used
as an event, start, or end timestamp.

## Change detection and errors

The source fingerprint is SHA-256 over the rollout bytes. Incremental state
also stores the processed byte offset and SHA-256 of that exact processed
prefix. A larger file with a matching processed prefix is append-only. A
shorter file or prefix mismatch is a truncation/replacement and is rebuilt.

Records are read with a fixed maximum size. Malformed JSON, non-object JSON,
oversized lines, missing timestamps, and invalid visible-message shapes become
recorded extraction errors. They are never silently converted into journal
text. A final unterminated line is left unprocessed until it is completed by a
later append.
