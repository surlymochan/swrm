# M26 slice 1 — remote runner evidence (VM → gocc)

Slice: M26 slice 1 (#207). Controller: this VM. Runner: gocc (Mac mini).

## Runner transfer

The remote runner needs only the current main code (`run` +
`fleet _snapshot` / `fleet _run-info` predate this slice; the dispatch
logic lives on the controller). Transferred 2026-11-14 ~17:1x SGT by a
plain clone on gocc (gocc reaches GitHub directly):

```sh
git clone https://github.com/surlymochan/swrm.git ~/work/swrm-m26
git -C ~/work/swrm-m26 rev-parse HEAD
# dfdd4d0ce2b80f42a8a77aed1207b42e63b2a65d (main HEAD, clean status)
```

No existing gocc checkout, config, or credential was touched
(`~/work/swrm` left alone; no `backends.json` / `fleet.json` created on
gocc). The runner binary is `~/work/swrm-m26/bin/openswarm`.

## Registry (controller)

`~/.claude/openswarm/fleet.json` on the controller (copied from
`examples/fleet-runners.json`):

```json
{"hosts":[{"kind":"local","name":"local","repos":{"swrm":"~/code/swrm"}},
 {"backends":["codex","pi"],"capacity":2,"kind":"ssh","name":"gocc",
  "openswarm":"~/work/swrm-m26/bin/openswarm",
  "repos":{"swrm":"/Users/surlymo/work/swrm-m26"},"ssh":"gocc"}]}
```

`fleet runners` (2026-11-14, with
`OPENSWARM_SSH_CONFIG=/home/hatch/.ssh/config` — this VM runs as root
with HOME=/home/hatch, so plain ssh reads the empty /root/.ssh/config
instead of the user's config that defines the `gocc` alias):

```
local  local  local  backends=unspecified  capacity=unlimited  reachable  active=0
gocc   ssh    gocc   backends=codex,pi      capacity=2          reachable  active=0
```

## Real dispatch (VM → gocc, codex)

Task card: create `M26-SLICE1-PROOF.md` in the worktree root with
exactly one line `m26-slice1 remote proof`, nothing else.
Verify: `test -f M26-SLICE1-PROOF.md && grep -qx 'm26-slice1 remote proof' M26-SLICE1-PROOF.md`.

Command (controller, from this checkout):

```sh
OPENSWARM_SSH_CONFIG=/home/hatch/.ssh/config ./bin/openswarm fleet dispatch gocc \
  --repo swrm --task-file task-m26-proof.md --backend codex \
  --title "m26 slice 1 remote proof" \
  --verify "test -f M26-SLICE1-PROOF.md && grep -qx 'm26-slice1 remote proof' M26-SLICE1-PROOF.md" \
  --timeout 1500
```

Result (dispatch exit 0; full relayed output in
`~/workspace/github-maturity/bench-env/m26-slice1/dispatch-gocc.log`):

- remote run id: `20261114-172758-5dc642`
- remote status: `done`, remote rc: `0`, verify rc: `0`
- backend: `codex`, model: `gpt-6-luna`, effort: `high`, seconds: `62.453`
- remote base: `dfdd4d0ce2b80f42a8a77aed1207b42e63b2a65d`
- branch: `openswarm/20261114-172758-5dc642`,
  commit: `6850c7a23278dcbf0371ec2e9adf2b58ca6f7797`
- remote worktree: `/Users/surlymo/work/.openswarm-wt-swrm-m26-20261114-172758-5dc642`
- remote run dir: `/Users/surlymo/.claude/openswarm/runs/20261114-172758-5dc642`
  (`result.json`, `status.json`, `task.md`, `diff.patch`,
  `events.1.codex.jsonl` all present on gocc)
- remote ledger row (gocc `ledger.jsonl`): same run id, `done`,
  `codex`, `verify_rc 0`, branch/commit match the result
- remote branch file `M26-SLICE1-PROOF.md` reads exactly
  `m26-slice1 remote proof` (verified via
  `git show openswarm/20261114-172758-5dc642:M26-SLICE1-PROOF.md`)
- controller ledger: line 5, entry `fleet-gocc-a7cfdab8`
  (`fleet_remote_dispatch`, run id / verify rc / branch / commit /
  remote run dir / `_run-info` log pointer all match the remote side)

## Bugs found by the evidence run (fixed in this slice)

1. Card transfer used a `~` path that the remote shell expanded on
   write but the remote Python `open()` did not expand on read
   (`FileNotFoundError: '~/.claude/openswarm/dispatch-*.md'`). The first
   dispatch attempt failed with remote rc 1 and was recorded honestly
   in the controller ledger (line 4, entry `fleet-gocc-12e45bc0`,
   `fetch_error`). Fix: dispatch resolves the runner `$HOME` first and
   transfers/reads absolute paths; regression test
   `test_dispatch_home_unresolvable_refuses_before_run` plus the
   absolute-path assertion in the happy-path test.
2. Plain `ssh gocc` from this VM fails (rc 255): the VM runs as root,
   so ssh reads the empty `/root/.ssh/config` instead of
   `/home/hatch/.ssh/config`. Fix: `OPENSWARM_SSH_CONFIG` env var for
   the ssh transport (unset = byte-identical argv); the evidence runs
   set it. `host_stream` (fleet exec watch) is unchanged and still
   needs the alias resolvable by plain ssh on this VM.
