# Journal format and compaction rules

Format version 1 creates one Markdown journal and one JSON provenance companion
per source session. Parent and sub-agent sessions remain separate.

Filenames contain the first eight alphanumeric session-ID characters plus a
12-hex SHA-256 suffix derived from the complete session ID. This keeps the
identifier compact and stable while distinguishing UUIDv7 sessions spawned in
the same timestamp window. Verification fails if any two cached sessions map
to one path or if cached, generated, provenance, and indexed inventories differ.

## Selection boundary

Only `response_item/message` records with assistant role, commentary phase, an
`output_text` item, and a valid event timestamp are eligible. Unknown schemas
fail closed. Final answers, prompts, reasoning, tools, outputs, token markers,
and inter-agent records are excluded.

## Deterministic normalization

For each eligible visible message, the tool:

1. hashes the original visible text for provenance;
2. redacts credentials and shortens the home-directory prefix to `~`;
3. rejects environment-dump-shaped messages;
4. collapses whitespace and removes leading bullet/status decoration;
5. splits clear sentence boundaries so each timeline entry is normally one
   sentence;
6. applies only mechanical rewrites whose meaning is stable, including
   “I’m now reviewing …” to “Reviewing …”, “I have found …” to “Found …”, and
   “The agent has corrected …” to “Sub-agent corrected …”.

Text longer than 180 characters is not blindly truncated. If shortening might
alter meaning or an identifier, whitespace-cleaned text is retained.

## Compaction

Entries stay in source order. Exact repeated routine progress and consecutive
same-subject “still working” updates are collapsed. The first focus entry and a
later result are retained.

Any entry containing a failure, security finding, blocker, reviewer objection,
correction, test result, commit/push/issue/PR action, stop condition, or explicit
demonstrated/not-demonstrated distinction is protected from compaction.
Contradictory protected entries are both retained. The tool never invents
causality, completion, timestamps, or outcomes.

## Time

Every `HH:MM` value comes from the exact event timestamp converted with
`zoneinfo`. Multiple entries may share a minute. Untimed visible events are
recorded as extraction errors and omitted; file times and event order never
stand in for a timestamp.

## Metadata and provenance

Front matter records stable identity, lifecycle, original UTC start/end,
rendered timezone, shortened cwd, repository, branch, source fingerprint,
redaction count, and extraction-error count.

The adjacent `*.provenance.json` contains, for every selected line:

- source session ID and source record sequence;
- original UTC event timestamp;
- SHA-256 of the original visible heartbeat text;
- normalized journal text; and
- whether redaction changed the message.

It never contains the original visible body. A redacted secret value is never
stored elsewhere in generated output.
