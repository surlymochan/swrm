# Pi one-shot event compatibility (#218)

At base `ba925861bddc6132bf9091e583f5a26e098f4af1`, `exec_pi` calls
`run_proc` / `_run_proc`, with child stdout directed to the event file and
incremental risk checks reading that file. The incident owner's deployment
description confirms this call pattern, but no verified deployed Git commit
or deployed writer source comparison is available. A release directory label
does not establish source identity. This change does not establish a production
fix. The supplied comparison found identical `pi_usage` implementations.

The new one-shot path selects `_run_pi_proc` through `run_proc(pi_events=True)`.
It reads a pipe in 64 KiB pieces, checks original events for risk, and flushes
complete records to the existing append-only JSONL path. RPC lanes are unchanged.
Only assistant text/thinking strings in `message_update.message.content` and
`assistantMessageEvent.partial.content` receive prefix coding. Each update keeps
its metadata, original event delta, tool arguments, and other evidence. Rewrites
receive prefix zero; unknown shapes pass through. No updates are sampled away.

`swrm_pi_compaction.version=1` lists snapshot paths and prefix lengths in Python
Unicode characters. The string at each path is the suffix; its full value is the
preceding value at that path sliced to `prefix`, plus the suffix. Replay with
`PiEventCompactor.restore` from the file start. Message start/end and agent end
reset references. Files/attempts have independent state. Truncating or filtering
updates prevents full replay; these files must remain append-only.

Native message/tool/turn end events, errors, cancellation evidence and uncertain
commit receipts are retained. Official usage and the final result still come
only from `message_end`. Legacy Pi usage/watch readers ignore the extra update
field and still read native final/tool events. New watch readers show text
suffixes without replay state, including after a saved byte cursor. Report and
resume continue to use existing status/result paths; session continuation flags
and session directories are unchanged.

Growth is linear in retained unique snapshot text plus per-event metadata, not
a fixed total byte cap. Repeated full deltas, unknown shapes, tool output and
other unique evidence can still grow without bound. Parser state is limited to
one record (4 MiB plus a read chunk) and the preceding update. Oversized records
are retained raw and fail the attempt because risk inspection is unavailable;
usage extraction drains oversized records and explicitly reports incomplete
usage/final evidence. stderr tail reads are bounded. Existing external readers
may still allocate an entire oversized raw record. A torn trailing record is
retained and flagged. A writer SIGKILL/power loss can lose an incomplete pipe
record; completed records are flushed, without a per-record fsync guarantee.
