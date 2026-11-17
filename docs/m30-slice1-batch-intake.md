# M30 slice 1: batch-path intake core (swrm #247)

M30 takes SuperAgent intake from single-run dispatch (M28/M29) to the
remaining two dispatch paths. Slice 1 is the batch path; slice 2 is
fleet + real-run evidence + acceptance.

## How it works

`policy.dispatch.mode` selects the dispatch path for an accepted
Mandate. `"run"` (default, shipped) keeps today's single
`openswarm run`; `"batch"` routes the Mandate through
`intake_dispatch_batch` instead. `"fleet"` (M30 slice 2, #251)
routes it through `intake_dispatch_fleet`: the same cards,
dispatched in topo order across the fleet runners via
`fleet_dispatch_once`, each card health-gated before any exec
attempt. No new subcommand: `intake poll` picks the mode up
from the policy file, and `intake policy` prints it.

Card generation (`intake_batch_cards`) is deterministic and
inspectable:

- An unchecked `- [ ]` checklist in the Trigger body becomes one card
  per item, each `depends_on` the previous item (sequential chain), so
  M4 topo order, stacked branches and failure blocking apply unchanged.
- Checked `- [x]` items are already done and skipped.
- A body with no checklist becomes a single card carrying the same
  task text the single-run path hands the executor.
- More than 10 items, or an empty body, is refused loudly instead of
  silently truncating work.
- Cards are written under
  `<OPENSWARM_HOME>/intake-batches/<ticket_id>/cards/` before the
  runner starts, and the writer refuses to overwrite a non-empty cards
  directory.

Dispatch (`intake_dispatch_batch`) mirrors `intake_dispatch`: one
Ticket dispatches at most once (a second call raises before creating
anything); the Assignment moves pending -> running -> ended; a runner
failure still ends it with the reason. The default runner
(`run_openswarm_batch`) builds the batch in-process with the same spec
and summary construction `openswarm batch` uses, on the local host,
and runs `execute_batch` — so per-card weighted/quota routing
(M7/M18) and the M25 health gate apply card by card. A blocked card
never starts an exec attempt, and its M4 dependents block like any
other non-done dependency. An explicitly configured dispatch backend is
passed through as the batch backend and is gated per card exactly like
a routed pick, never exempt.

The ingest path reuses its existing gates for batch mode unchanged:
active-scope shadow (non-allowlist stays shadow), per-thread caps,
and the M24/M25 health-gate pre-check all run before any Assignment
exists. A missing batch runner means Ticket only, exactly like a
missing single-run runner. Batch dispatches do not post to the Channel
(slice 2 adds the completion post for fleet only; batch keeps the
slice-1 behaviour): the batch summary is the record.

## Ledger record

- `intake_batch_created` — cards + topo `order` + cards dir.
- `batch_dispatch_started` / `batch_dispatch_finished` /
  `batch_dispatch_failed` — mirror the single-run triple.
- `intake_batch_card_result` — one per generated card: status,
  blocked flag, health_gate record when the M25 gate blocked it,
  reason, run_id (None for a card that was never dispatched).

## Evidence check

`intake_m30_slice1_evidence_check(entries, state=None, policy=None)`
validates one batch-dispatch record: at most one Assignment per
ticket, cards created with a topo order covering exactly those cards,
every `batch_dispatch_started` has a finish/failure, every generated
card has a card result, and no blocked card carries a run_id.
Real-run evidence (>=2 batch runs with >=1 blocked + >=1 allowed) is
slice 2's job.

## Tests

`tests/test_m30_slice1.py` — 34 tests: dispatch-mode validation,
card generation, batch dispatch (happy path, idempotency, runner
failure, blocked-card recording), ingest wiring (shadow, caps,
health gate, explicit backend gated), the evidence checker, and the
default runner refusing cleanly with no repo checkout.
