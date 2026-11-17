# M30 slice 2: fleet-path real-run evidence + acceptance (swrm #251)

Refs: epic #246, slice 1 #247 (PR #248, `4a3a605`), slice 2 #251.
This document is the evidence record for slice 2. It does not declare
acceptance — acceptance is the main round's second-party verdict.
Every number below was read back from the intake ledger, the run
`result.json` files, or the run branches in the scratch checkout
(`git show <branch>:<file>`), never from executor self-report.

## Operator config used for the live runs

- Isolated `$OPENSWARM_HOME` (fresh, nothing from
  `~/.claude/openswarm`):
  `~/workspace/github-maturity/bench-env/m30s2-evidence/home`.
  Intake policy `accept_mode=active`,
  `active_scope={repos: [surlymochan/swrm], users: [surlymochan]}`,
  `caps={max_dispatches_per_thread: 3, max_budget_minutes_per_thread: 60}`,
  `dispatch={backend: pi, budget_minutes: 10}` (F3 uses backend
  `copilot` instead, see below).
- `policy.local.json` in that home: `pi.require_isolation=false`,
  `pi.default_model=MiniMax-M3.1-Flash-Preview`,
  `routing.health_gate={enabled: true, timeout_seconds: 10,
  on_unknown: "allow"}` (gate ON for this run, unlike the shipped
  default off).
- `fleet.json` in that home: local host only, `backends: ["pi"]`,
  `capacity: 2`, repo `surlymochan/swrm` mapped to the scratch
  checkout below.
- `OPENSWARM_PI_BIN` pointed at an operator-side wrapper (not part of
  this repo) that forwards every arg to the bench pi binary and inserts
  `--thinking high` before the prompt, i.e. the bench model config
  (MiniMax-M3.1-Flash-Preview, high thinking).
- Egress for pi went through the bench allowlist proxy on
  `127.0.0.1:9933` (only `api.minimaxi.com:443`).
- Dispatch runs worked from a clean checkout of `main` (`5aa074f`);
  the intake machinery ran from this branch's `bin/openswarm`.
- A pre-flight `openswarm run` (smoke task, run
  `20261117-013414-6e2d49`, pi, MiniMax-M3.1-Flash-Preview, ~26 s,
  branch `openswarm/20261117-013414-6e2d49`, commit `c6ad9717`)
  proved the pi path before any live dispatch: file
  `m30s2-smoke.txt` reads exactly `m30s2 smoke ok` on that branch.

## What ran (ledger: 41 lines, isolated home)

Validator: `intake_m30_slice2_evidence_check` (new in this slice,
`bin/openswarm`; tests in `tests/test_m30_slice2.py`) run against the
live ledger: **ok, 0 failures, 10/10 checks pass**.
Ledger kinds: `trigger_received` 5, `decision` 5, `ticket_created` 5,
`mandate_sealed` 5, `intake_health_gate_blocked` 1,
`assignment_created` 4, `intake_batch_created` 2,
`batch_dispatch_started` 2, `batch_dispatch_finished` 2,
`intake_batch_card_result` 2, `intake_fleet_created` 2,
`fleet_dispatch_started` 2, `fleet_dispatch_finished` 2,
`intake_fleet_card_result` 2.

## Acceptance ② — fleet path dispatches end to end

| Dispatch | Ticket | Fleet | Card run | Status | Branch / commit |
| --- | --- | --- | --- | --- | --- |
| F1 | `tkt-cb4fe22ac624` | `20261117-013740-ee0bae` | `20261117-013741-3ae85a` | done | `openswarm/20261117-013741-3ae85a` / `147e7fac` |
| F2 | `tkt-98b7c99a4233` | `20261117-013759-57688d` | `20261117-013800-5dd57b` | done | `openswarm/20261117-013800-5dd57b` / `fde35da8` |

- Both fleet dispatches went through `intake_dispatch_fleet` via
  `intake_ingest_trigger` (mode `fleet`, real `run_openswarm_fleet`
  runner): cards generated from the Mandate, `intake_fleet_created`
  with topo order, `fleet_dispatch_started` → `fleet_dispatch_finished`,
  one `intake_fleet_card_result` per card with runner `local` and the
  remote run id. Each card was dispatched with
  `fleet_dispatch_once` to the local runner (least-loaded pick from
  `fleet_dispatch_many_plan`).
- Verify is operator evidence, never executor self-report: on each
  run branch, `git show <branch>:m30s2-evidence-fleet-<n>.txt`
  reads exactly `m30s2 fleet evidence <n>` (one file, one line, no
  other file touched — `git show --stat` on each run branch lists
  only that file).

## Acceptance ③ — ≥2 batch runs + ≥2 fleet runs, ≥1 blocked + ≥1 allowed

| Dispatch | Ticket | Batch | Card run | Status | Branch / commit |
| --- | --- | --- | --- | --- | --- |
| B1 | `tkt-c25c30412af4` | `20261117-013629-5a203d` | `20261117-013634-39ba85` | done | `openswarm/20261117-013634-39ba85` / `f54ea6d1` |
| B2 | `tkt-45e8dad2694e` | `20261117-013702-6b8a01` | `20261117-013706-641209` | done | `openswarm/20261117-013706-641209` / `57352d62` |

- Both batch dispatches went through `intake_dispatch_batch` via
  `intake_ingest_trigger` (mode `batch`, real `run_openswarm_batch`
  runner). Branch read-back likewise exact:
  `m30s2-evidence-batch-<n>.txt` reads `m30s2 batch evidence <n>`.
- Blocked (1): F3, ticket `tkt-274ecfdfdcb6`, mode `fleet`, backend
  `copilot` — the intake health-gate pre-check refused it before any
  Assignment existed: `intake_health_gate_blocked`, verdict
  `unhealthy`, reason `no working copilot binary on this host`
  (binary check fail; copilot is genuinely not installed on this
  host). 0 Assignments on that ticket, 0 runs, 0 exec attempts. An
  explicitly configured backend did not exempt the gate.
- Allowed (4): B1, B2, F1, F2 — each with a batch/fleet id and a real
  run id on the ledger. The pi health-gate verdict on all four was
  `unknown` (binary pass, transport pass, quota probe unreadable —
  see the limitation below), allowed per `on_unknown=allow`, with the
  verdict on the record.

## Acceptance ④ — guardrails

- Caps: thread #246 used 2 dispatches / 20 min, thread #251 used
  2 dispatches / 20 min (F3 blocked before dispatching, so it
  consumed no cap); both inside caps (3 / 60 min). No `cap_blocked`
  entry exists, none was needed.
- Health gate: enabled for this run (see config above). The blocked
  path is live (F3, copilot, above); the allowed path is live (pi,
  verdict `unknown`, allowed). Per-card gating inside a fleet
  dispatch is covered by the unit tests (unhealthy backend → blocked
  card with the gate record, no `fleet_dispatch_once` call;
  no-runner plan → blocked card with per-runner reasons;
  dependency failure → successor blocked); no live per-card block
  was staged, and none is claimed.
- Idempotency: one Assignment per ticket holds across all 4
  dispatches (checker `one_assignment_per_ticket` passes); no
  second dispatch was attempted for any ticket.
- Non-allowlist default is shadow: unchanged from M28/M29 (shipped
  `accept_mode` is still `shadow`); the live runs above all used the
  operator's `active` policy with an explicit allowlist.
- Explicit `--backend` does not exempt the gate: F3 (copilot) is the
  live proof; the unit tests cover the same for fleet cards.
- Full test suite: `python3 -m pytest tests/ -q` on this branch →
  **1309 passed, 6 skipped, 212 subtests passed in 741.39 s**
  (single merged run; targeted files: 37 new slice-2 tests + 34
  slice-1 tests pass).

## Acceptance ⑤ — evidence on the slice issue

- This document, the ledger validator
  (`intake_m30_slice2_evidence_check` + `tests/test_m30_slice2.py`),
  and the evidence comment on #251 (run ids, batch/fleet ids,
  ticket ids, branch/commit pointers) are the record. The driver
  script and its isolated home stay outside the repo at
  `~/workspace/github-maturity/bench-env/m30s2-evidence/`
  (`drive.py`, `results.json`, `drive.log`, `home/`); the scratch
  checkout's run branches were never pushed.

## Deliberate limits (disclosed, not hidden)

- The pi quota probe is unreadable on this VM: `quota probe failed:
  MiniMax quota request failed: URLError: <urlopen error Tunnel
  connection failed: 403 Forbidden>` — the builtin quota reader
  cannot reach the MiniMax quota endpoint from here, so every live
  pi dispatch records gate verdict `unknown` and proceeds only
  because `on_unknown=allow`. The binary + transport checks both
  pass. If the quota endpoint becomes readable, the verdict should
  become `healthy` with no code change.
- The live blocked case is a pre-dispatch health-gate block (F3),
  not a per-card block inside a fleet dispatch; per-card blocking is
  proven by unit tests, not by a live card.
- The live dispatches used synthetic triggers (distinct trigger ids
  and task bodies on the real threads #246/#251) with
  `channel_poster=None`, so no per-dispatch chatter was posted to
  GitHub; only the single evidence comment on #251 was posted.
- The scratch checkout's run branches exist only locally and were
  never pushed; the evidence files live only on those branches.
