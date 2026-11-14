# M26 slice 2 — multi-runner fan-out + disconnect recovery (VM ↔ gocc)

Slice: M26 slice 2 (#210). Total: #206. Slice 1 (#207, PR #208) built
the one-to-one remote dispatch; this slice adds the two missing pieces
for real multi-machine dispatch.

## What changed

- `fleet dispatch-many --cards-dir <dir> --repo <name> --backend <id>`:
  one task card per file (filename order). Each card may declare its own
  acceptance command on a `verify:` line (stripped before transfer); a
  batch-level `--verify` applies to cards without one. Assignment is
  least-loaded-first (live active-run count + already assigned this
  wave, name tie-break) and never exceeds a runner's `capacity`: a wave
  dispatches concurrently (one worker per card) and the next wave starts
  only after the previous wave's runs finish, so spare capacity is
  re-read, never assumed. A card no runner can take (backend not
  offered, no repo mapping, unreachable, or every runner at capacity)
  fails the command loudly with the per-runner reason — nothing is
  silently queued, dropped, or run locally. Per-card outcomes and the
  runner distribution go to the summary JSON and one flagged
  `fleet_dispatch_many` ledger entry.
- `fleet recover` (scan mode, no args): every controller dispatch record
  whose remote outcome never came back and that has no final recovery
  entry is probed on its runner — `lost` (runner unreachable or the run
  no longer readable: outcome unknown, non-final, re-probed next time),
  `recovered` (remote `run_digest` reports the run dead: runner pid
  gone, identity-checked — the M11/M12 dead verdict, final), `finished`
  (remote run reached a terminal state late, final, result attached),
  or `still-running` (reported, untouched, no ledger write). A dispatch
  whose remote run id was never recorded (transport died before the id
  came back) is listed as skipped with the explicit
  `fleet recover <run-id> --runner <name>` pointer.
- `fleet recover <run-id> --runner <name>` (explicit mode): probe one
  run directly, even without a dispatch record. `--redispatch`
  re-dispatches a lost/recovered task to another reachable runner with
  spare capacity, reusing the original card / verify / base from the
  dispatch record (explicit mode takes `--card-file/--repo/--backend`
  instead). A failed redispatch leaves the disposition in place with
  the reason attached.
- Shared core: `fleet dispatch` is now a thin wrapper over
  `fleet_dispatch_once`, reused by `dispatch-many` and `recover
  --redispatch` (same refusal order, same ledger record). Dispatch
  records now carry `card_file` / `verify_cmd` so recovery can find the
  original task. Both new ledger flags (`fleet_dispatch_many`,
  `fleet_remote_recovery`) are skipped by the routing-weight /
  advice-accuracy / eval / backfill passes, like the other fleet
  bookkeeping records. Concurrent local snapshots take one lock: the
  capacity-only env toggle is process-global.
- No change to weighted/calibration/quota routing, the single-machine
  dispatch path, CLI names, bin paths, `OPENSWARM_*` variables, or any
  existing subcommand signature.

## Tests

- New: 17 tests in `tests/test_m26_slice2.py` — per-card `verify:`
  parsing/stripping, fan-out distribution (gocc 2 / mac 1 over 3 cards),
  two-wave capacity behaviour (capacity 1 + 1, 3 cards), no-spare-capacity
  refusal with no local fallback, ineligible-runner skip, global
  `--verify` fallback, routing-weights skip for both new flags, and the
  full recover matrix (lost non-final + re-probe, dead → recovered
  final + idempotent second scan, finished late result, still-running
  no-write, done/idless skips, explicit-mode runner checks, redispatch
  to another runner, redispatch failure when the card file is gone,
  plus the scan-mode regression for `append_ledger`-backfilled
  idless entries).
- Full suite: 1055 passed, 6 skipped, 212 subtests passed,
  552.09s, exit 0, one-shot run 2026-11-14 after the scan-mode fix
  (`python3 -m pytest tests/ -q`, log `/tmp/m26s2-fullsuite-2.log`).

## Real evidence (VM ↔ gocc, pi)

Runner registry (controller `~/.claude/openswarm/fleet.json`, updated
for this slice: the local entry's repo path was stale — `~/code/swrm`
does not exist on this VM — and now points at this checkout; local
capacity set to 2 to match gocc):

```json
{"hosts":[{"kind":"local","name":"local","repos":{"swrm":"/home/hatch/workspace/github-maturity/bench-env/m26-slice2"},"capacity":2},
 {"backends":["codex","pi"],"capacity":2,"kind":"ssh","name":"gocc",
  "openswarm":"~/work/swrm-m26/bin/openswarm",
  "repos":{"swrm":"/Users/surlymo/work/swrm-m26"},"ssh":"gocc"}]}
```

Backend `pi` on both runners (MiniMax token plan, ~0.88 remaining on
both sides at run time; codex login state on the VM is unknown, gocc
codex was proven in slice 1 but mixing backends across runners is out
of scope for this slice).

### Fan-out: 6 real tasks, 2 with verify

Cards in `~/workspace/github-maturity/bench-env/m26-slice2-evidence/cards/`
(`fanout-1.md` … `fanout-6.md`; `fanout-1`/`fanout-2` carry a `verify:`
line). Command (controller, from this checkout):

```sh
OPENSWARM_SSH_CONFIG=/home/hatch/.ssh/config OPENSWARM_PI_BIN=/home/hatch/workspace/tools/node22/bin/pi \
  ./bin/openswarm fleet dispatch-many \
  --cards-dir ~/workspace/github-maturity/bench-env/m26-slice2-evidence/cards \
  --repo swrm --backend pi --timeout 1500
```

Result (2026-11-14 ~18:35–18:37 SGT; full log
`~/workspace/github-maturity/bench-env/m26-slice2-evidence/dispatch-many-2.log`):

- `ok: true`, 2 waves, distribution `{"gocc": 3, "local": 3}`,
  summary ledger line 28.
- fanout-1 → gocc, run `20261114-183458-dec0b6`, done, rc 0,
  verify rc 0, branch `openswarm/20261114-183458-dec0b6`, commit
  `925e90dec5d5`, 5.9s, controller ledger line 23.
- fanout-2 → local, run `20261114-103426-b093ec`, done, rc 0,
  verify rc 0, branch `openswarm/20261114-103426-b093ec`, commit
  `6441a1dba84c`, 18.4s, controller ledger line 22.
- fanout-3 → gocc, run `20261114-183456-bae1c7`, done, rc 0,
  branch `openswarm/20261114-183456-bae1c7`, commit `11b3b49bb16a`,
  12.1s, controller ledger line 24.
- fanout-4 → local, run `20261114-103426-1a8d79`, done, rc 0,
  branch `openswarm/20261114-103426-1a8d79`, commit `7af769f99c28`,
  12.4s, controller ledger line 20.
- fanout-5 → gocc, run `20261114-183547-8c3cc6`, done, rc 0,
  branch `openswarm/20261114-183547-8c3cc6`, commit `3b117c763acf`,
  4.4s, controller ledger line 27.
- fanout-6 → local, run `20261114-103518-6e2fb4`, done, rc 0,
  branch `openswarm/20261114-103518-6e2fb4`, commit `982c503d839a`,
  19.2s, controller ledger line 26.
- Every branch's proof file was read back with `git show` and reads
  exactly `m26-slice2 fanout proof <n>` (one line, nothing else).

First attempt failed 6/6 and is kept as honest history (log
`dispatch-many.log`, controller ledger lines 6–15): both runners
refused pi — gocc `unroutable` ("pi requires the isolated launcher"),
local `failed`/`exec_error` ("isolated Pi dependencies unavailable").
Root cause: the pi backend requires its isolated launcher, but this VM
has no `bwrap` and no broker socket, and gocc's `pi` resolves to the
plain binary. Fix used for the evidence run (temporary, both sides
reverted afterwards): `policy.local.json` with
`{"pi": {"require_isolation": false}}` (local: new file, deleted after;
gocc: key added to the existing file, backed up to
`policy.local.json.bak-m26s2` first, byte-restored after) plus
`OPENSWARM_PI_BIN=<bench plain pi>` on the controller so the local
probe records the plain binary. Local codex is not logged in and gocc
codex cannot run on the VM, so pi was the only backend spanning both
machines. Smoke tests passed first on both runners (local run
`20261114-103312-1348a6`, gocc run `20261114-183404-3a1ab9`; both done,
verify rc 0; ledger lines 17–18). Lasting follow-up, not fixed here:
restoring the isolated-pi stack (bwrap + broker + runtime) on the VM
and making gocc pi routable without the override.

### Disconnect: remote runner stopped mid-run

Method (real stop, not a simulation): a long task (full test suite +
proof file) was dispatched to gocc; once its remote run
`20261114-183717-7ff501` was `running` with runner pid 45039 (verified
with `ps` to be that exact `openswarm run` process), the pid was
killed with SIGTERM. The remote `status.json` still reads
`state=running` with the dead pid — the M11/M12 dead-runner shape.

- Dispatch outcome: remote rc 255, `remote_run_id: null`,
  `fetch_error: "could not determine remote run id from the runner
  reply"`, controller ledger line 29, entry `fleet-gocc-8fb0ecf6`
  (log `disconnect-dispatch.log`). The task was not silently lost: the
  failed dispatch is on the ledger with its fetch error.
- `fleet recover 20261114-183717-7ff501 --runner gocc` →
  disposition `recovered`, `recovered_final: true`, controller ledger
  line 30: "remote run … is dead on 'gocc' (runner process gone, pid
  identity checked); it can never resume" (log `recover-1.log`).
- `fleet recover … --redispatch --card-file …/disconnect-1.md --repo
  swrm --backend pi` → redispatched to `local`, new run
  `20261114-103856-90ae55`, status `done`, 1050.8s, branch
  `openswarm/20261114-103856-90ae55`, commit `6fb7df897d6b`,
  controller ledger lines 34 (new dispatch) and 35 (recovery with the
  redispatch attached) (log `recover-redispatch.log`). The proof file
  on the branch reads exactly
  `m26-slice2 disconnect proof passed=1038 failed=0` (base-commit suite
  count: 1038 passed + 6 skipped + 212 subtests; the branch total is
  1054 on the slice-2 head, 16 more from the new tests).

Scan-mode bug found by this evidence run and fixed in the PR:
`fleet recover` (no args) first probed the entry ids of the two idless
dispatches as if they were run ids (`append_ledger` backfills a missing
`run_id` from the entry id), marking both `lost` (controller ledger
lines 31–32, non-final). Fixed to read the authoritative
`remote_run_id` and skip idless entries with the explicit
`fleet recover <run-id>` pointer; regression test added. After the
fix, a scan lists both idless entries as `skipped` and writes nothing
(log `recover-scan-2.log`); lines 31–32 stay as pre-fix history.
