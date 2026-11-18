# M35 slice 1: Pi event-log growth is bounded (#275, parent #274, incident #218)

## Investigation (measured, not assumed)

- Writer: Pi itself. One-shot runs redirect Pi's `--mode json` stdout
  straight into the run event file (`_run_proc`, `out_path`);
  openswarm only re-reads it incrementally for risk enforcement and
  appends small risk/error records. Lane runs append every child
  message verbatim via `lane_record_event`.
- Growth mechanism (incident evidence, #218 comment 2026-11-14):
  5,984 `message_update` events held 241,046,680 of 242,034,995 bytes
  (99.6%). Each update repeats the full cumulative assistant text twice
  over — as `assistantMessageEvent.partial` and as the top-level
  `message` snapshot — so a message growing to F bytes over N updates
  costs O(N*F) bytes. Newer Pi versions emit delta-only updates, but
  the incident version emits cumulative snapshots, so the fix handles
  the cumulative shape and lets delta-only streams pass through.
- Readers need nothing from `message_update`: `summarize_event` and
  `WatchMessages` ignore it, `pi_usage` reads only `message_end`, and
  the Pi lane branch reads only `message_end`/`agent_end`. The
  authoritative final text and usage always arrive in `message_end`.

## Fix

`PiEventCompactor` in `bin/openswarm` (next to `pi_usage`), wired into
both writers:

- One-shot path: `exec_pi` passes the compactor as `stream_filter` to
  `run_proc`/`_run_proc`, which drains Pi stdout through a pipe on a
  writer thread (backpressure instead of unbounded buffering) and joins
  the thread on every exit path, so the tail — including `message_end`
  — is flushed on success, timeout (124), cancel (130), risk block
  (133), and abort (75). Without a filter the old direct-file path is
  byte-identical.
- Lane path: `lane_run_turn` compacts Pi messages at record time.
- Delta updates are rewritten to carry only the newly appended text
  (other top-level fields, including the latest cumulative `usage`,
  are preserved). `*_end` updates keep their completed block but lose
  the redundant `partial`. A delta is emitted only when the cumulative
  text provably extends the previous snapshot; regeneration, edits,
  non-text payloads, and unknown shapes pass through verbatim.
- No new daemon, no subcommand/flag/SKILL changes, no format migration:
  the file stays JSONL with the same event types. Reconstructing a
  message from a compacted stream: concatenate per-`contentIndex`
  deltas, then replace with `message_end.message` (Pi's own
  assemble-then-replace rule).

## Regression evidence (tests/test_m35_slice1.py, 15 tests)

Sanitized incident-shape fixture (300 updates, 80-char deltas, no real
Pi binary/provider/account): raw 7,392,882 bytes → compacted 124,767
bytes (59.3x); 301 events compacted; delta reconstruction exactly
equals the final text; `pi_usage`, `WatchMessages`, and
`summarize_event` outputs are identical on raw vs compacted streams;
compactor held state peaks at the final text length (24,000 chars),
never the log size. End-to-end fake-Pi runs: filtered run 128 events
in <60 KB with exact usage/final; unfiltered control reproduces
>1 MB; timeout (124) and cancel (130) runs flush a parseable partial
tail whose deltas reconstruct the partial text; error `message_end`
keeps usage and the error receipt.
