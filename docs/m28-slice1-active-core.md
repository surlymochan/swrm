# M28 slice 1: active-mode core — allowlist scope + caps + idempotency + health-gate pre-check

Refs: epic #231, slice 1 #232. M27 (`docs/m27-slice1-intake-gate.md`,
`docs/m27-slice2-source-assessment.md`) shipped the intake pipeline
end to end but `accept_mode` only ever dispatched in the ungated `on`
mode. This slice adds the controlled-rollout mode `active`: real
dispatch only inside an allowlisted scope, under per-thread caps, and
only when the M24/M25 health gate does not block the effective backend.
No real online dispatch happens in this slice (that evidence belongs
to slice 2); the mechanism and its guardrail tests are the deliverable.

## What landed

All in `bin/openswarm`, intake section (new "intake M28 slice 1"
block; the M27 functions it wires into are untouched in shape).

### `accept_mode: active`

A fourth mode value. Admission (Gate) is unchanged -- the Gate still
decides accept / stop / ignore / unavailable by author rules only.
The scope gate runs at dispatch time, after the Ticket and sealed
Mandate are created, so an out-of-scope Trigger still leaves a Ticket
and a ledger trace (shadow), never a silent drop.

### Allowlist scope (`active_scope` block, optional)

```json
"active_scope": {
  "repos": ["surlymochan/swrm"],
  "users": ["surlymochan"]
}
```

Both conditions must hold: the thread repo is listed in
`active_scope.repos` AND the author is in `active_scope.users`.
Repos are matched case-insensitively (`owner/name`, validated as
exactly one `/` with non-empty parts). `users` defaults to the intake
allowlist users when the block omits it; `repos` defaults to empty --
active mode with no scope configured dispatches nothing (the safe
default). Outside the scope the result carries
`active_scope: {in_scope: false, reason}` and the ledger gets one
`active_scope_shadow` entry; no Assignment, no run, no Channel post.

`accept_mode=on` keeps its M27 meaning (dispatch anything accepted)
and is **not** scope-gated -- it is the operator's explicit choice,
used by manual polls and tests. Caps and the health-gate pre-check
below apply to both dispatching modes.

### Per-thread caps (`caps` block, optional)

```json
"caps": {
  "max_dispatches_per_thread": 3,
  "max_budget_minutes_per_thread": 60
}
```

Each key is independently optional; a missing block (or key) means
that cap is off. Counting is from intake state: every Assignment
whose Ticket sits on the thread counts toward the dispatch count, and
the budget counted is the allocation recorded on each Assignment at
creation (`budget_minutes`, the dispatch budget -- not the wall clock
actually spent, the conservative predictable measure). Assignments
created before the field existed count as the dispatch default (10m).
When a cap would be exceeded the dispatch is refused with a
`cap_blocked` ledger entry (carrying the usage and the limits) and
`cap_blocked: true` on the result; the Ticket still exists.

### Health-gate pre-check (M24/M25 reuse)

`intake_health_gate_check` runs `apply_health_gate` against the swrm
`policy.json` `routing.health_gate` config -- the same config and the
same probe the `openswarm run` child applies after routing. When the
gate is off (shipped default) it returns `(None, False)` and dispatch
proceeds exactly as before. When on, the effective backend is probed:
an explicitly configured dispatch backend, or the backend `route()`
picks for the default run profile (`{"type": "impl", "difficulty": 3}`,
the profile the run child uses). When routing finds no backend (`ok`
False) there is nothing to gate and the check passes, deferring to the
run child, which refuses as unroutable as today. A blocked backend
writes one `intake_health_gate_blocked` ledger entry (with the full
gate record) and the dispatch never creates an Assignment or a run --
the run child would refuse the same backend, but only after creating
both. The allowed/unknown-allowed record rides on the result as
`health_gate` for the record.

### Idempotency (hardening)

Ingest already deduped re-delivered Trigger ids (`duplicate_skipped`,
same Ticket, no new dispatch). This slice adds the second layer:
`intake_dispatch` refuses a second Assignment for a Ticket that
already has one (`ValueError`, raised before creating anything), so
even a direct double call can never double-dispatch. Resume (slice 3)
creates no Assignment, so the guard cannot break it.

## Ledger entries added (slice 1)

`active_scope_shadow`, `cap_blocked`, `intake_health_gate_blocked`.
`assignment_created` / `dispatch_started` now also carry
`budget_minutes`. No existing entry shape changed.

## CLI

No new subcommand. `intake policy` prints the effective active scope
and caps; `intake poll` / `intake ingest` pick the new blocks up
through ingest. The `poll` help text now says `on/active` where it
used to say `on`.

## Tests

28 new tests in `tests/test_m28_slice1.py`: policy validation
(active mode, bad repos/users/caps), scope allow/deny (repo, user,
no-scope-configured, users-default, case-insensitivity, `on` not
gated), caps (count cap, budget cap, no-caps, single-key), idempotency
(re-delivery after a dispatch, direct double dispatch, replay with a
duplicate), health gate (unhealthy blocks before any Assignment,
healthy dispatches with the record, unknown+block blocks,
unknown+allow dispatches, routed backend resolved then gated,
unroutable defers to the run path, gate-off unchanged), and the
`intake policy` display.

## Deliberate limits

- The scope gate, caps, and health pre-check run in ingest only for
  dispatches that would actually happen (`accept` + `on`/`active` +
  not `draft_only` + a runner passed). `draft_only` (money/commitment)
  still never dispatches, in any mode.
- The health pre-check costs one extra probe per dispatch when the
  gate is enabled (the run child probes again); the gate is off by
  default, so the cost is zero until an operator enables it.
- Caps count allocations, not elapsed time: a run that finishes early
  still counts its full allocated budget against the thread.
- Slice 2 (#233, not yet opened at slice-1 time) owns the real
  allowlisted-scope evidence: ≥2 real GitHub issues dispatched end
  to end, plus the unlisted-ignore and stop/withdraw cases.
