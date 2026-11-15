# M28 slice 2: real rollout evidence + acceptance (allowlisted scope, live dispatch)

Refs: epic #231, slice 2 #234, slice 1 #232 (PR #233, `23d433e7`).
This document is the evidence record for slice 2. It does not declare
acceptance — acceptance is the main round's second-party verdict. Every
number below was read back from the intake ledger, the run `result.json`
files, or GitHub via `gh api` (REST only; GraphQL was at 0 remaining
during this window).

## Operator config used for the live runs

- Isolated `$OPENSWARM_HOME` (fresh, nothing from `~/.claude/openswarm`):
  intake policy `accept_mode=active`,
  `active_scope={repos: [surlymochan/swrm], users: [surlymochan]}`,
  `caps={max_dispatches_per_thread: 3, max_budget_minutes_per_thread: 60}`,
  `dispatch={backend: pi, budget_minutes: 10,
  verify: "python3 -m pytest tests/test_m27_slice1.py
  tests/test_m27_slice2.py tests/test_m28_slice1.py -q"}`.
- `policy.local.json` in that home: `pi.require_isolation=false`,
  `pi.default_model=MiniMax-M3.1-Flash-Preview` (the bench model).
- `OPENSWARM_PI_BIN` pointed at an operator-side wrapper (not part of
  this repo) that forwards every arg to the bench pi binary and inserts
  `--thinking high` before the prompt, i.e. the bench model config
  (MiniMax-M3.1-Flash-Preview, high thinking). `swrm` itself was not
  changed to force this: `exec_pi` takes no `--thinking` flag.
- Egress for pi went through the bench allowlist proxy on
  `127.0.0.1:9933` (only `api.minimaxi.com:443`).
- Dispatch runs worked from a clean checkout of `main` (`23d433e7`);
  the intake machinery ran from this branch's `bin/openswarm`.
- A pre-flight `openswarm run` (smoke task, run
  `20261115-192657-8cf5bd`, pi, MiniMax-M3.1-Flash-Preview, 27 s,
  branch `openswarm/20261115-192657-8cf5bd`, commit `1df0e25a`)
  proved the pi path before any live dispatch.

## What ran (ledger: 54 lines, isolated home)

Validator: `intake_m28_slice2_evidence_check` (new in this slice,
`bin/openswarm`; tests in `tests/test_m28_slice2.py`) run against the
live ledger + state + policy: **ok, 0 failures, 8/8 checks pass**.
Ledger kinds: `trigger_received` 10, `decision` 10, `ticket_created` 8,
`mandate_sealed` 8, `assignment_created` 3, `dispatch_started` 3,
`dispatch_finished` 3, `channel_posted` 3, `ticket_ended` 1,
`mandate_withdrawn` 1, `active_scope_shadow` 4.

## Acceptance ① — active only inside the allowlist

- In scope, dispatched: #235 (ticket `tkt-1b4df35155c8`, scope verdict
  "repo surlymochan/swrm and author surlymochan are in the active
  scope") and #236 (tickets `tkt-96dee0c03704`, `tkt-0b80e44002a5`).
- Out of scope, shadow: evalid #205 (repo not listed) — 4 triggers
  (issue body + 3 comments), all `accept` + `active_scope_shadow`,
  Ticket only, 0 runs, 0 Assignments. Tickets `tkt-2f3ee2a447b9`,
  `tkt-5b26966b1e27`, `tkt-36463f40aae9`, `tkt-e07b745df88d`.
- The repo condition and the author condition are both enforced by
  `intake_active_scope_check` (slice 1); the live runs prove the repo
  condition end to end, the author condition is covered by the slice-1
  unit tests (no second GitHub account exists to post as).

## Acceptance ② — ≥2 allowlisted issues dispatched end to end

| Issue | Ticket | Run | Status | Branch / commit | PR | Channel |
| --- | --- | --- | --- | --- | --- | --- |
| #235 | `tkt-1b4df35155c8` | `20261115-193436-6f7889` | failed* | `openswarm/20261115-193436-6f7889` / `8413a04a` | #238 | 6900158586 + follow-up 6900188637 |
| #236 | `tkt-0b80e44002a5` | `20261115-194553-72ae75` | done | `openswarm/20261115-194553-72ae75` / `a1544398` | #239 | 6900271120 + follow-up 6900283223 |

- #235 implemented "intake poll prints the dispatch note"
  (`cmd_intake_poll` appends `note=<dispatch_note>`; new test in
  `tests/test_m27_slice2.py`). #236 implemented "thread-restricted
  poll leaves the repo cursor untouched" (`poll_github_source` only
  persists the cursor for full polls; two new tests + one doc line).
- Verify is operator evidence, never executor self-report: on each
  run branch, in a fresh worktree, the operator ran
  `python3 -m pytest tests/test_m27_slice2.py tests/test_m28_slice1.py
  tests/test_m27_slice1.py -q` → **102 passed** (`8413a04a`),
  **103 passed** (`a1544398`). The runs' own `--verify` agreed
  (102 / 103 passed, rc 0).
- *Run `20261115-193436-6f7889` reports status `failed`
  (`exec_error`, `result: null`): the executor did the work and the
  branch + tests verify it, but it emitted no final JSON report, so
  `exec_pi` could not extract a result. The work stands on the branch
  and the operator verify, not on the run status.
- A third dispatch attempt belongs to #236, not to a third issue:
  ticket `tkt-96dee0c03704`, run `20261115-194341-377c6e`, status
  `failed` after 25 s, pi `Connection error`, no branch. Root cause:
  the local 9933 allowlist proxy had died between the two polls
  (no listener on 9933 when checked). Proxy restarted, then a
  follow-up comment on #236 re-triggered the same task as a new
  Trigger (new ticket `tkt-0b80e44002a5`, caps 2/3 dispatches on the
  thread) and dispatched cleanly. The failed attempt stays on the
  ledger (`dispatch_finished` + `channel_posted` 6900203830).
- PR opening is the operator's step, by design: the local runner
  commits on `openswarm/<run-id>` but never pushes
  (`ENFORCED_RISKS` includes push; the executor prompt forbids
  commit/push). The operator verified each branch, pushed it, opened
  the PR, and posted the PR link as a Channel follow-up comment.
  Both PRs are `mergeable: true`, open, awaiting review/merge
  (merge + release are the main round's call, not this slice's).
- CI (read back via `gh api`, check-runs, after both finished):
  PR #238 — 10/10 `success` (9 test legs + secrets);
  PR #239 — 10/10 `success` (9 test legs + secrets).
  Both PRs are `mergeable: true`, open, awaiting review/merge
  (merge + release are the main round's call, not this slice's).

## Acceptance ③ — unauthorized ignore, stop/withdraw

- Gate ignore (zero run): comment `6900103549` on #237, author
  `OpenSwarm` (not allowlisted, no bot mention) → decision `ignore`,
  rule `admission:unlisted_unmentioned:ignore`, no Ticket, no run.
  (The comment itself is a bystander complaint about the handle, not
  a task; the Gate ignored it exactly as designed.)
- Stop/withdraw: #237 (ticket `tkt-0f622c7598eb`, mandate
  `mnd-59784c91c905`, created with `--no-dispatch`). The stop comment
  `6900298929` (`@openswarm stop`, own line) → decision `stop`,
  `stopped: true`, `mandate_withdrawn: true`, `dispatched: false`,
  ticket `ended`, mandate `withdrawn`, **0 Assignments** on the
  ticket. A re-poll of #237 then found 0 new triggers (3 duplicates):
  nothing further dispatches under the withdrawn Mandate.
  #237 was closed by the operator after the sample (comment
  6900322982).
- Note: #237's issue body tripped `draft_only` — the word "withdraw"
  in "stop/withdraw sample" matches the money-action pattern
  (`\bwithdraw\b`). The Ticket + Mandate were still created and the
  stop path still worked; it only ever prevented a dispatch that the
  sample never wanted. Lesson recorded, not hidden: sample-issue
  wording must avoid Gate money/commitment words.

## Acceptance ④ — guardrails

- Caps: thread #235 used 1 dispatch / 10 m; thread #236 used
  2 dispatches / 20 m; both inside caps (3 / 60 m). No `cap_blocked`
  entry exists, none was needed.
- Health gate: the shipped `routing.health_gate` is off (`enabled:
  false`), so the intake pre-check returned `(None, False)` and the
  run children likewise recorded no gate block on every live run.
  The blocked path is covered by the slice-1 unit tests
  (unhealthy backend → `intake_health_gate_blocked`, no Assignment);
  no live unhealthy-backend case was staged, and none is claimed.
- Idempotency: re-poll of #235 after dispatch → 0 new triggers,
  1 duplicate (poll-level dedup), still exactly 1 Assignment for
  `tkt-1b4df35155c8`; re-poll of #237 after the stop → 0 new
  triggers. One Assignment per ticket holds across all 3 dispatches.
- Non-allowlist default is shadow: evalid #205 (see ①); the shipped
  default `accept_mode` is still `shadow`.
- Full test suite: `python3 -m pytest tests/ -q` on this branch →
  **1187 passed, 6 skipped, 212 subtests passed in 539.32 s**
  (single run; targeted intake files 102–103 passed on the run
  branches as well).

## Acceptance ⑤ — evidence on the slice issue

- This document, the ledger validator
  (`intake_m28_slice2_evidence_check` + `tests/test_m28_slice2.py`),
  and the evidence comment on #234 (this slice's PR number, run ids,
  PR numbers, Channel comment ids) are the record. The slice-1 doc
  pointer was fixed in this slice: `docs/m28-slice1-active-core.md`
  now says "Slice 2 (#234)" (it wrongly said #233).

## Deliberate limits (carried from slice 1, still true)

- The health-gate pre-check costs one extra probe per dispatch when
  the gate is enabled; the gate is off by default.
- Caps count allocated budget minutes, not wall clock.
- `intake poll --issue` still advances the repo cursor — no: fixed
  by the #236 dispatch itself (PR #239). A thread-restricted poll
  now leaves `cursors[repo]` untouched.
- The run's `failed` status on a missing final JSON report is an
  executor-reporting gap, not a work gap; the operator verify is the
  record either way.
