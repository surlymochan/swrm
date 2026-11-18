# M35 slice 2: real-run evidence + thinking/toolcall compaction (#277, parent #274)

Slice 1 (#275, PR #276, merged `65d9aec2`) compacted plain-text deltas
only. This slice ran real Pi runs against that merged main and found
the same growth mechanism in the other two delta kinds, fixed it, and
re-evidenced the fix on real runs. No subcommand/flag/SKILL changes, no
format migration, no new daemon.

## Defect found on merged main (real run, not a fixture)

- Run `20261118-152400-fd37e7` (`openswarm run --backend pi --model
  MiniMax-M3.1-Flash-Preview`, task: write a 1200+ word architecture
  review of a small fixture repo). The run was cut short after ~60 s
  when the operator shell was killed by a service restart (pi died
  with EPIPE on the closed stdout pipe; not a product defect), so the
  run has no `result.json` — but its event file is complete evidence.
- Event file `events.1.pi.jsonl`: 2,953 lines, **31,270,608 bytes in
  ~60 seconds**. `thinking_delta`: 2,273 events, 27,561,742 bytes
  (88%); `toolcall_delta`: 454 events, 3,289,808 bytes (10.5%).
  `text_delta`: 3 events, 449 bytes, all compacted by slice 1.
- Root cause (code, not speculation): `PiEventCompactor._text_content`
  required every content block to be plain text, so thinking blocks
  (`thinking` string) and toolCall blocks (`partialJson` string) made
  it return `None` and `compact_event` passed the whole cumulative
  snapshot through verbatim. On a reasoning model the thinking stream
  dominates, so the incident's O(N*F) growth reproduced exactly.

## Second shape found in the same stream: batched snapshots

Of the 2,273 thinking deltas, 344 did not satisfy slice 1's strict
rule (snapshot suffix must equal the delta). Inspection of consecutive
events showed why: Pi sometimes batches two chunks into one snapshot,
so a delta's snapshot already contains the NEXT delta too (the new
suffix starts with the delta instead of equalling it), and the
following event re-emits the same snapshot with the next delta. The
strict rule then failed that event AND the next one (knock-on), even
though dropping either snapshot is safe.

## Fix (`PiEventCompactor`, `bin/openswarm`)

- The cumulative string is now read from the streaming block selected
  by `contentIndex`: a text block's `text`, a thinking block's
  `thinking`, a toolCall block's `partialJson`. Anything else still
  passes through verbatim.
- The compactor tracks the *consumed prefix* (text already accounted
  for by emitted deltas) instead of the last snapshot. A delta
  compacts when the snapshot advance equals it (classic append) or
  starts with it (batched snapshot — the prefix advances by the delta
  only, the remainder is picked up by the following deltas). A delta
  matching neither passes through with the prefix untouched, so a
  later event can still match; a snapshot that no longer starts with
  the prefix (regeneration/edit) re-baselines.
- Reconstruction stays exact as long as the stream keeps flowing; on
  an interrupted tail it may lag the last snapshot by at most one
  batched jump. The authoritative text still arrives in `message_end`
  (kept verbatim), and tool receipts still arrive in `toolcall_end`
  and `tool_execution_*` (kept verbatim).

## Evidence

- Offline replay of the real 31.3 MB stream through the fixed
  compactor: **31,270,608 → 832,651 bytes (37.6x)**; all 2,730 delta
  events compacted (2,273 thinking + 454 toolcall + 3 text); held
  state peaks at 16,257 chars (the live thinking block, never the
  log); end-block reconstruction 24/24 (thinking_end content and
  toolcall_end arguments both equal the concatenated deltas);
  `pi_usage` identical on raw vs compacted
  (`input 85,831 / output 6,374 / cacheRead 719,146`); `WatchMessages`
  and `summarize_event` sequences identical (27 rows).
- Tests: `tests/test_m35_slice2.py`, 10 new tests (thinking/toolcall
  fixtures in the real wire shape, batched-snapshot and mismatch
  cases, reader compatibility, fake-Pi `exec_pi` end-to-end). Full
  local suite: **1390 passed, 6 skipped, 212 subtests passed**
  (1396 collected).
- Live re-runs on the fix branch (run ids and ledger/result reads in
  the evidence comment on #277).
