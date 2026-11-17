# M30 slice 2: fleet-path intake core + real-run evidence (swrm #251)

M30 takes SuperAgent intake from single-run dispatch (M28/M29) and
batch dispatch (slice 1, #247) to the last dispatch path. Slice 2 is
the fleet path plus the live evidence that closes epic #246
acceptance ②/③/④/⑤.

## How it works

`policy.dispatch.mode` selects the dispatch path for an accepted
Mandate. `"run"` (default, shipped) keeps today's single
`openswarm run`; `"batch"` routes the Mandate through
`intake_dispatch_batch`; `"fleet"` routes it through
`intake_dispatch_fleet` instead. No new subcommand: `intake poll`
picks the mode up from the policy file, and `intake policy` prints
it.

Card generation is shared with the batch path
(`intake_batch_cards`, `intake_write_batch_cards`):

- An unchecked `- [ ]` checklist in the Trigger body becomes one card
  per item, each `depends_on` the previous item (sequential chain), so
  M4 topo order, stacked branches and failure blocking apply unchanged.
- Checked `- [x]` items are already done and skipped.
- A body with no checklist becomes a single card carrying the same
  task text the single-run path hands the executor.
- More than 10 items, or an empty body, is refused loudly instead of
  silently truncating work.
- Cards are written under
  `<OPENSWARM_HOME>/intake-fleets/<ticket_id>/cards/` before the
  runner starts, and the writer refuses to overwrite a non-empty cards
  directory.

Dispatch (`intake_dispatch_fleet`) mirrors `intake_dispatch_batch`:
one Ticket dispatches at most once (a second call raises before
creating anything); the Assignment moves pending -> running -> ended;
a runner failure still ends it with the reason. The default runner
(`run_openswarm_fleet`) dispatches the cards in topo order, one card
at a time, through `fleet_dispatch_once` — the same core `fleet
dispatch` and `fleet dispatch-many` use — so each card lands on the
least-loaded eligible runner (`fleet_dispatch_many_plan`), a failed
or blocked card blocks its M4 dependents, and a successor stacks on
its dependency's commit (`batch_stack_base`) exactly like a batch
card. Each card is health-gated (`fleet_health_gate`, M25 slice 2)
on its effective backend before any exec attempt; an explicitly
configured dispatch backend is gated exactly like a routed pick,
never exempt. A card no runner can take (backend not offered, no repo
mapping, unreachable, or every runner at capacity) is recorded
blocked with the per-runner reasons — never queued silently, never
run locally. A per-card refusal (`FleetDispatchError`) is recorded on
that card and blocks its dependents; it never aborts the remaining
independent cards. The fleet summary is persisted under
`<OPENSWARM_HOME>/fleets/<fleet_id>/summary.json` (mirror of
`batch_save`), and every block also lands in the controller ledger
(`fleet-<id>-<card>`, status `blocked`).

The ingest path reuses its existing gates for fleet mode unchanged:
active-scope shadow (non-allowlist stays shadow), per-thread caps,
and the M24/M25 health-gate pre-check all run before any Assignment
exists. A missing fleet runner means Ticket only, exactly like a
missing single-run runner. Fleet dispatches post to the Channel like
single runs (fleet id + one line per card: runner, status, run id);
batch keeps its slice-1 no-post behaviour.

## Ledger record

- `intake_fleet_created` — cards + topo `order` + cards dir.
- `fleet_dispatch_started` / `fleet_dispatch_finished` /
  `fleet_dispatch_failed` — mirror the single-run triple.
- `intake_fleet_card_result` — one per generated card: runner,
  status, blocked flag, health_gate record when the M25 gate blocked
  it, reason, run_id (None for a card that was never dispatched).
- `channel_posted` / `channel_failed` — carry `fleet_id` alongside
  `run_id` (first dispatched card's run id, None when nothing
  dispatched).

## Evidence check

`intake_m30_slice2_evidence_check(entries, state=None, policy=None)`
validates one fleet-dispatch record plus the slice-2 live bar: at
most one Assignment per ticket, cards created with a topo order
covering exactly those cards, every `fleet_dispatch_started` has a
finish/failure, every generated card has a card result, no blocked
card carries a run_id — and the live bar itself: at least two real
batch runs (`batch_dispatch_finished`), at least two real fleet runs
(distinct run_ids on fleet card results), at least one blocked record
(a blocked card or a pre-dispatch `intake_health_gate_blocked` /
`cap_blocked`), and at least one allowed dispatch. The live numbers
are in `docs/m30-slice2-real-evidence.md`.

## Tests

`tests/test_m30_slice2.py` — 37 tests: dispatch-mode validation
(fleet accepted, bad values refused), fleet dispatch (happy path,
idempotency, runner failure, blocked-card recording, first-run-id),
the default runner (refuses cleanly with no repo checkout, topo-order
dispatch, health-gate block, no-runner block, dependency block),
ingest wiring (fleet runner selection, missing runner stays
Ticket-only, run/batch modes ignore the fleet runner, shadow, caps,
health gate, explicit backend gated, Channel post), the fleet Channel
body, and the evidence checker (good record + one failure per check).
