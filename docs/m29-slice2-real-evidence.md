# M29 slice 2: evalid live rollout evidence + acceptance (DRAFT — runs in progress)

Refs: epic #241, slice 1 #242 (PR #243, `43faf0b7`), slice 2 #244.
This document is the evidence record for slice 2. It does not declare
acceptance — acceptance is the main round's second-party verdict. Every
number below was read back from the intake ledger, the run `result.json`
files, or GitHub via `gh` (REST only).

## Operator config used for the live runs

- Isolated `$OPENSWARM_HOME` (fresh, nothing from `~/.claude/openswarm`):
  `~/workspace/scratch/m29s2-home`.
  Intake policy `accept_mode=active`,
  `active_scope={repos: [surlymochan/swrm, surlymochan/evalid], users: [surlymochan]}`,
  `repo_policies[surlymochan/evalid]={version: 1, users: [surlymochan]}`,
  `caps={max_dispatches_per_thread: 3, max_budget_minutes_per_thread: 60}`,
  `dispatch={backend: pi, budget_minutes: 10, verify: "node test/live-claims.test.js"}`.
- `policy.local.json` in that home: `pi.require_isolation=false`,
  `pi.default_model=MiniMax-M3.1-Flash-Preview` (the bench model).
- `OPENSWARM_PI_BIN` pointed at an operator-side wrapper (not part of
  this repo, `~/workspace/scratch/m29s2-home/pi-wrapper.sh`) that forwards
  every arg to the bench pi binary (`~/workspace/tools/node22/bin/pi`)
  and inserts `--thinking high` before the prompt, i.e. the bench model
  config (MiniMax-M3.1-Flash-Preview, high thinking). `swrm` itself was
  not changed to force this: `exec_pi` takes no `--thinking` flag.
- Egress for pi went through the bench allowlist proxy on
  `127.0.0.1:9933` (only `api.minimaxi.com:443`).
- Dispatch runs worked from a clean checkout of evalid `main`
  (`8df503f`, v0.5.35); the intake machinery ran from this branch's
  `bin/openswarm`.
- A pre-flight `openswarm run` (smoke task, run
  `20261116-072544-5f34cc`, pi, MiniMax-M3.1-Flash-Preview) proved the pi
  path (provider `minimax-cn`, tool calls executing, quota 77%
  remaining) before any live dispatch. The smoke itself ended
  `budget_exceeded` (3 min budget; the executor spent it running the
  full `npm test` on a trivial read-only task — executor behaviour, not
  a path failure; no branch, no commit, evalid checkout untouched).
  Lesson recorded: 3 min is too small for an executor that runs full
  suites; live dispatches use the M28-proven 10 min budget.
- Shared-quota note: the evalid M16 pilot (`supervise.sh m16s1-pilot`,
  24 cells, jobs 2) runs on the same VM / MiniMax proxy during this
  window. Dispatches share that quota; a 429 / quota hit is recorded
  as a breakpoint, never retried by hammering.

## Candidate qualification (all evalid open issues screened 2026-11-16)

Open evalid issues at screening: #5, #69, #187, #201, #202, #203, #204, #205.

- #201 — QUALIFIED (dispatch 1): real follow-up bug (live claim
  extractor takes a diagnostic sentence as a test claim), small,
  self-contained (`src/core/live-claims.js`), regression oracle exists
  (M15 evidence set: 9 recorded sessions, 15 claims, 15 confirmed;
  session 7 holds the 2 claims incl. the pytest diagnostic sentence).
  No user credentials, no user sign-off item.
- #5 P1-2 first half — QUALIFIED (dispatch 2, via a new trigger
  comment): merge the planning turn and the red-proof turn on the yolo
  track + skip review re-reads of files the gate already hashed.
  P1-3 is already structurally done (PR #34, comment 6692046809) and
  P1-2 second half already landed (PR #35, comment 6693762290), so
  neither is dispatched again; the first half is explicitly "未动".
  Bench-number acceptance (wall-clock ≤1.3x, tokens ≤2.0M, resolved
  ≥4/6) stays pending for the bench rerun, as with P1-3 — the dispatch
  covers code + unit tests only.
- #205 — EXCLUDED: M16 slice 1; the pilot (24 cells) is running on
  this VM right now and main `8df503f` already merged PR #206
  (m16s1-cost-wall). Dispatching it would duplicate live work.
- #203 — EXCLUDED: paused by user order.
- #187 — EXCLUDED: waits on user terminal re-recording (claude
  half, user补录).
- #202 — EXCLUDED as dispatch (queued by user order 2026-11-15, not
  the current milestone); used only for the stop/withdraw probe
  (Ticket + Mandate created with `--no-dispatch`, then stopped —
  no work is performed on the epic itself).
- #204 / #69 — EXCLUDED as dispatch: epics / roadmaps, not single
  dispatchable tasks.
- evalid open PRs at screening: only #153 (stale SARIF CI-evidence
  PR) — not a dispatch candidate.

## Baseline seeding (disclosed, not hidden)

A thread-restricted poll (`--issue N`) fetches the issue body plus ALL
its comments with no `since` filter, so polling a thread with history
would ingest stale bookkeeping comments as fresh Triggers (each
distinct comment is a distinct scope and would dispatch). Before each
probe poll, every pre-existing event on that thread was therefore
marked seen in the isolated intake state (`triggers_seen`), exactly
what the repo-wide cursor does for repo-wide polls. Only the intended
new trigger was left unseeded. Seeded ids are listed per probe below;
no seeded event was ingested, dispatched, or counted.

- #87 (ignore probe): seeded `github:issue:6278283210` (body) and
  `github:comment:6731567649` (surlymochan). Targets left unseeded:
  MAOLVLHL comments 6731930505, 6733392931, 6736537021 (all verified
  to contain no `@openswarm` mention).
- #202 (stop probe): seeded `github:comment:6890955283` (surlymochan
  queue note). The body is the trigger.
- #5 (dispatch 2): seeded `github:issue:5901151179` (body) and all 22
  pre-existing comments (6284929168, 6664234246, 6664235176, 6664236001,
  6664236602, 6666593952, 6667333548, 6692046809, 6693762290, 6695950024,
  6704035354, 6705051602, 6706264141, 6707860592, 6709069598, 6710838387,
  6713163612, 6713345792, 6714541967, 6746842692, 6777018463, 6817214015).
  Only the new P1-2 trigger comment is ingested.
- swrm #244 (cross-repo dedup probe): seeded the slice-2 body event
  (id TBD at probe time) and any comments then present. Only the probe
  comment (verbatim #201 body) is ingested.

## Dispatch 1 — evalid #201 (DONE, with a disclosed platform-kill gap)

- Trigger: issue body `github:issue:6398437849` (author surlymochan,
  listed, unmentioned → inferred; Assessment accept, concrete task).
- Ticket `tkt-013537d14d3b`, Mandate `mnd-281c961730a6`, Assignment
  created, run `20261116-073054-c543cf` (pi, MiniMax-M3.1-Flash-Preview,
  budget 10 min). Ledger: `trigger_received → decision(accept) →
  ticket_created → mandate_sealed → assignment_created →
  dispatch_started`, then the poll process was killed by a VM runtime
  restart (the background exec session has no confirmed final result
  after the restart; no process survived). The run itself kept working
  until the restart: 4936 pi events, and the executor's uncommitted
  work survived in the run worktree
  (`.openswarm-wt-ownrepo-1116-swrm-m29s2-evalid-20261116-073054-c543cf`):
  `src/core/live-claims.js` +101/−2, `test/live-claims.test.js` +69.
- Operator recovery (no `result.json` exists, so nothing is claimed
  from the run status): operator verify in the run worktree —
  `node test/live-claims.test.js` **36 pass, 0 fail** (7 new
  diagnostic-context tests) — then committed `52e00d2`, pushed branch
  `openswarm/20261116-073054-c543cf`, opened **evalid PR #207**.
- Channel: the automatic `channel_posted` ledger entry is missing
  (poll died first); the operator posted the standard channel body as
  recovery on #201, comment **6910472695**, with the restart disclosed
  in the body.
- Ticket closed on verify evidence (`intake close`,
  evidence "run 20261116-073054-c543cf + evalid PR #207").
- Gap honestly recorded: no `dispatch_finished` / `channel_posted`
  ledger entries for this dispatch; the Assignment is left in
  "running" in state (close ends the Ticket only). The M15 evidence-set
  full replay (9 sessions) was not re-run in this round; the new unit
  tests cover the diagnostic case + non-retraction. CI on PR #207 is
  the main round's check.

## Dispatch 2 — evalid #5 P1-2 first half (attempt 1: run done, no PR; attempt 2: same)

- Trigger: comment `6910487910` on #5 (author surlymochan, listed,
  mentioned → explicit; Assessment accept). Baseline: all 23
  pre-existing #5 events seeded seen (see "Baseline seeding").
- Ticket `tkt-2142f188d428`, run `20261116-074402-dde43a` (pi,
  MiniMax-M3.1-Flash-Preview, budget 10 min). Ledger complete:
  `trigger_received → decision(accept, explicit) → ticket_created →
  mandate_sealed → assignment_created → dispatch_started →
  dispatch_finished (status budget_exceeded) → channel_posted
  6910664443`.
- Run outcome: `budget_exceeded` after 603.7 s (rc 124), branch null,
  commit null, patch null; the run worktree was cleaned up, so no
  executor work survived. Events (7674): the executor thrashed early
  (1165 tool calls, many identical `ls`/`cat`/`sed` repeats) and was
  still reasoning about the review re-read side (already landed in
  PR #34/#35) when the budget killed it — the first trigger comment
  had asked for the review re-read skip as well as the merge, i.e. the
  trigger was imprecisely scoped. Quota after the run: pi 74%
  remaining.
- Retry (comment `6910726042`, scoped to the merge only, F2 lite-track
  pattern; review untouched; read yolo.yaml + pass grouping, then
  implement; targeted tests only): Ticket `tkt-e9548c7a86db`, run
  `20261116-075740-35dcbb`, again `budget_exceeded` after 603.6 s
  (rc 124), branch/commit/patch null, 7603 events, 1451 tool calls —
  the executor was still looping identical `ls` commands at the end
  and never converged. Channel posted `6910915260`. Quota after: pi
  71% remaining. Caps on the #5 thread: 2/3 dispatches, 20/60
  budget-minutes (within caps). No third attempt: the pattern is
  systematic (exploratory engine-level task + this executor's
  repeat-command looping under the 10-min budget), and the caps leave
  only 1 dispatch on the thread. The P1-2 first half stays open for a
  follow-up round (larger budget, different backend, or human-scoped
  implementation — main round's call). Both #5 Tickets are left OPEN
  (Mandates active, so a later follow-up can resume them).

## Unauthorized ignore — evalid #87 (DONE)

- Targets: MAOLVLHL comments `6731930505`, `6733392931`,
  `6736537021` on closed issue #87 (real unlisted author, verified to
  contain no `@openswarm` mention). Baseline: issue body
  `github:issue:6278283210` + surlymochan comment `6731567649`
  seeded seen.
- Poll `--issue 87`: 3 new triggers, 2 duplicates, 0 skipped_own.
  All three → decision `ignore`, rule
  `admission:unlisted_unmentioned:ignore`, no Ticket, no Assignment,
  zero runs. Ledger `decision` entries carry the trigger ids.

## Stop / withdraw — evalid #202 (DONE)

- Body trigger `github:issue:6401194642` (epic text, listed author,
  unmentioned → inferred; Assessment accept), polled with
  `--no-dispatch`: Ticket `tkt-ec0b8dd85c6b`, Mandate
  `mnd-5c5e82f3bc03`, dispatched False, 0 Assignments. Baseline: the
  one pre-existing comment `6890955283` seeded seen.
- Stop comment `6910940285` (`@openswarm stop`, own line) → decision
  `stop`, `stopped: true`, `mandate_withdrawn: true`, Ticket ended,
  dispatched False, still 0 Assignments.
- Re-poll of #202: 0 new triggers, 3 duplicates — nothing further
  dispatches under the withdrawn Mandate.

## Cross-repo dedup + idempotent replay (DONE)

- Dedup: the verbatim #201 issue body posted as comment `6910966241`
  on swrm #244 (explanatory comment `6910961063` + #244 body
  `github:issue:6416202220` seeded seen) → poll `--issue 244`
  found 1 trigger, 2 duplicates: `duplicate: True`,
  `cross_repo_duplicate: True`, ticket `tkt-013537d14d3b`, dedup key
  `text:687db0885143fbe9`, no new Ticket, no new Mandate, no
  Assessment, no dispatch; one `cross_repo_duplicate_skipped` ledger
  entry.
- Idempotent replay: re-poll of evalid #201 → 0 new triggers,
  1 duplicate (body), 1 skipped_own (the Channel comment, marker);
  still exactly 1 Assignment for `tkt-013537d14d3b`. Re-poll of #202
  → 0 new triggers, 3 duplicates.

## Guardrails

- Caps: thread #201 used 1 dispatch / 10 m; thread #5 used
  2 dispatches / 20 m; both inside caps (3 / 60 m). No `cap_blocked`
  entry exists, none was needed.
- Health gate: the shipped `routing.health_gate` is off (`enabled:
  false`), so the intake pre-check returned `(None, False)` on every
  live dispatch. The blocked path is covered by the slice-1 unit
  tests (unhealthy backend → `intake_health_gate_blocked`, no
  Assignment); no live unhealthy-backend case was staged, and none is
  claimed. Same accounting as M28 slice 2.
- Non-allowlist default is shadow: no live shadow case was staged in
  this slice (both repos are now in the active scope, so there is no
  in-scope repo left to shadow against). The repo condition was proven
  live by M28 slice 2 (evalid #205, 4 triggers, all
  `active_scope_shadow`, 0 runs) on the same code path, unchanged
  since; the author condition is proven live here by the #87 ignores
  and covered by the slice-1 unit tests. The shipped default
  `accept_mode` is still `shadow`.
- Validator: `intake_m29_slice2_evidence_check` (new in this slice,
  `bin/openswarm`; tests in `tests/test_m29_slice2.py`) run against
  the live ledger (39 lines) + state + policy + operator-verified PR
  map (`tkt-013537d14d3b` → evalid #207): **ok False, 8/10 checks
  pass**. Failures (the honest verdict): `prs_opened_for_two_dispatches`
  (only 1 of 2 finished dispatches opened a PR — the #5 P1-2 runs
  produced no PR) and `dispatch_started_without_finish` (the #201
  dispatch was started but the poll was killed by a platform restart
  before `dispatch_finished`).

## Platform incident (disclosed, affects this slice and the M16 pilot)

- The VM rebooted mid-round (uptime 15 min at 07:55 UTC; the 9933
  allowlist proxy is dead, `ss` shows no listener). It killed the
  dispatch-1 poll at `dispatch_started` (recovered as described
  above) and the M16 pilot `supervise.sh m16s1-pilot` at 14/24 cells
  (last cells timed out at the 1200 s cap). The pilot restart belongs
  to the M16 / main round, not this slice; the bench-env was not
  touched. The dispatches after the reboot reached MiniMax through the
  VM egress proxy (`https_proxy`, endpoint reachable, 404 on a bare
  probe path as expected), not 9933.

## Full test suite

- `python3 -m pytest tests/ -q` on this branch →
  **1235 passed, 6 skipped, 212 subtests passed in 460.55 s**
  (single run; targeted intake files 43 passed in the pre-run check).
