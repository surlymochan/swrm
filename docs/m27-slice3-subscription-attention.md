# M27 slice 3: Subscription resume / stop + attention CLI + 验收总成

Refs: epic #212, slice 3 #228. Fusion plan: `docs/dsh-superagent-fusion.md`.
Slice 1 (`docs/m27-slice1-intake-gate.md`): domain core, ledger, Gate, shadow.
Slice 2 (`docs/m27-slice2-source-assessment.md`): GitHub Source, Assessment,
real dispatch, Channel.

## What landed

```
human follow-up on an ended Ticket's thread
        -> policy re-check (same Mandate, current policy) -> resume, no new Eval
human stop on a thread
        -> Ticket ended (if open) + Mandate withdrawn, Ticket takes no more messages
openswarm attention            read-only list: open / ended awaiting review / stale
openswarm intake close         operator close on verify evidence (Mandate stays active)
```

New concepts (all in `bin/openswarm`, intake section):

| Concept | Meaning |
| --- | --- |
| Subscription resume | `intake_try_resume` runs inside ingest right after the Gate accepts and before Assessment: when the thread has an ended Ticket with an active Mandate and no open Ticket, a human follow-up with an actionable body reopens that Ticket under the same Mandate (same ticket id, same mandate id, Mandate fields untouched). No new Decision entry is written (no new Eval), no new Ticket/Mandate, no dispatch. Ledger: `subscription_followup` (trigger, mandate, policy version, rules checked, body excerpt) + `ticket_resumed`. |
| Policy re-check | `intake_resume_check`: Mandate still active, author human, author still on the current allowlist, thread kind still in scope, accept_mode not off, follow-up actionable (>= 10 chars after stripping the bot mention, the same floor as the default Assessment). Any failure falls through to the normal path (a greeting is ignored by Assessment; a withdrawn Mandate means a new Ticket). |
| Mandate withdrawal | `withdraw_intake_mandate`: a lifecycle transition, not a scope edit -- the seal still forbids changing policy_version / scope / strength / rules / source_identity; only `status` moves `active -> withdrawn`, once (a second withdrawal is refused). Mandates sealed before slice 3 carry no status field and read as active. A stop withdraws the Mandate of the thread's open Ticket, or of its most recently ended Ticket when no open Ticket exists. Ledger: `mandate_withdrawn`. A withdrawn Mandate can never resume its Ticket; a later task on the thread opens a new Ticket with a new Mandate. |
| Standalone stop | `intake_is_human_stop` replaces the slice 1 regex: a stop counts only when it is its own instruction on a line (line starts with `@openswarm stop` or a bare stop word) and the line is not quoted (`>`), not a table row (`|`), and not inside a fenced code block. Fixes the slice 2 real-data case where an evidence table quoting `@openswarm stop` (gh comment 6878371088) stopped work. |
| Operator close | `intake close --ticket TKT [--reason R] [--evidence E]`: ends an open Ticket on verify evidence. The Mandate stays active, so a later human follow-up can still resume the Ticket -- closing is the verify path, withdrawing (human stop) is the only path that retires the Mandate. Closing an ended or unknown Ticket is refused. Ledger: `ticket_ended` with `via: close` (+ evidence pointer); stops write `via: stop`. |
| attention CLI | `openswarm attention [--json] [--stale-days N]` (default 7): read-only. Buckets: `open` (needs work), `ended_unreviewed` (ended Tickets, listed with close reason -- there is no review marker yet, so every ended Ticket is listed), `stale` (open Tickets older than N days, plus ended Tickets ended that long ago without review). Each row carries Mandate status and latest Assignment status. Loads state only; writes nothing (no state save, no ledger append -- covered by a test that diffs both files). |

## What resume does NOT do (deliberate limits)

- Resume reopens the Ticket; it creates no new Assignment and dispatches
  nothing. The follow-up text is on the ledger (`subscription_followup`
  body excerpt) for the next dispatch. Wiring follow-up dispatch is a
  later slice, not this one.
- A follow-up on a thread with a live open Ticket takes the normal path
  (a distinct task opens a new Ticket; a same-scope repeat is ignored by
  Assessment). Resume only ever reopens an *ended* Ticket, and only when
  no open Ticket exists on the thread, so ingest never has to guess which
  Ticket a message belongs to.
- Resume does not re-check whether the follow-up stays inside the sealed
  Mandate scope (not deterministically decidable); the body excerpt in
  the ledger is the audit trail for a human to spot scope drift.

## CLI (new; existing commands untouched)

```
openswarm intake close --ticket TKT-... [--reason R] [--evidence E]
openswarm attention [--json] [--stale-days N]
```

## Tests

19 new tests in `tests/test_m27_slice3.py`: stop tightening (table /
fence / quote / mid-sentence quoted stops ignored, standalone stops
still stop), resume (same ticket + mandate, no new Decision entry,
ledger entries, greeting / bot / delisted-author refusals, open-ticket
normal path), stop withdrawal (open + ended-only threads, second stop
is a no-op, withdrawn Mandate blocks resume and forces a new Ticket,
double withdrawal refused, pre-slice-3 Mandates read active), operator
close (Mandate stays active, then a follow-up resumes; refused when
ended/unknown), attention buckets + staleness + read-only proof, CLI
smoke (`attention --json`, `intake close`, text output).

## Real acceptance evidence (2026-11-15, controlled repo surlymochan/swrm, accept mode=on)

Probe issue #229 (temporary, now closed), all comments posted as
@surlymochan via `gh`, polled with `intake poll --issue 229` against
an isolated `$OPENSWARM_HOME` (echo backend, so the dispatch is real
but writes nothing to the repo). Ticket `tkt-99876ab42595`, Mandate
`mnd-ab3f93ccef3f`, policy v1, accept_mode=on.

- Accept + dispatch (poll 1): issue body trigger
  `github:issue:6401318030` -> accept / explicit -> run
  `20261115-072546-80c516` status `done` (backend echo, branch
  `openswarm/20261115-072546-80c516`, commit `9e0e8a7517`,
  `echo-output.txt` on the branch) -> Channel comment
  `6891044123`. Ledger seq 1-8.
- Operator close: `intake close --ticket tkt-99876ab42595` with
  `--evidence runs/20261115-072546-80c516/result.json` -> ticket
  `ended`, Mandate still `active`. Ledger seq 9 (`ticket_ended`,
  `via: close`).
- Resume (poll 2): human follow-up comment `6891047609`
  ("@openswarm follow-up: the echo output is on file; please resume
  this ticket ...") -> `resumed: True`, same ticket, same Mandate,
  `dispatched: False`, **no new Decision entry** (decision entries
  total still 1). Ledger seq 10 `trigger_received`, 11
  `subscription_followup` (rules checked: `mandate-active`,
  `author-still-allowlisted`, `scope-still-admitted`,
  `follow-up-actionable`), 12 `ticket_resumed`. Ticket back to `open`.
- Stop (poll 3): human stop comment `6891050729` ("@openswarm stop")
  -> decision `stop`, `stopped: True`, `mandate_withdrawn: True`.
  Ledger seq 13-16 (`trigger_received`, `decision`,
  `ticket_ended` `via: stop`, `mandate_withdrawn`). Ticket `ended`,
  Mandate `withdrawn`.
- `openswarm attention` after the run: `0 open, 1 ended unreviewed,
  0 stale`, the ended row showing `mandate=withdrawn`,
  `assignments=1(ended)`.
- Probe issue #229 closed afterwards (comment `6891055398`).
- Evidence dir: `~/workspace/scratch/ownrepo-1115-swrm-m27s3-evidence/`
  (ledger + state + `poll1-accept-dispatch.json`,
  `poll2-resume.json`, `poll3-stop.json`). Evidence comment on #228
  carries the same numbers with review pointers. Full-suite result is
  recorded in that #228 comment, not claimed here.

## Epic #212 acceptance status after this slice

- ① Domain core: done (slice 1).
- ② Real end-to-end accept: done (slice 2; re-verified in this slice's
  acceptance run through the initial dispatch).
- ③ Resume / stop cases >= 1 each: this slice (see evidence above).
- ④ accept_mode defaults to shadow (built-in policy v1, verified by
  `intake policy` on a fresh home: `accept_mode=shadow`); full-suite
  result is recorded in the evidence comment on #228; release v0.4.46
  notes the MIT source and the close-on-verify divergence (release is
  a main-wheel step after this PR merges, not part of this PR).
