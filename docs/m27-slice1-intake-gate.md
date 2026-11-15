# M27 slice 1: intake domain core + append-only ledger + Gate (shadow)

Refs: epic #212, slice 1 #213. Fusion plan: `docs/dsh-superagent-fusion.md`.

## What landed

A Python (stdlib-only) port of the **domain model** of
`deepseek-ai/dsh-superagent-prototype` (MIT) — no TypeScript code moved, no
DSH runtime introduced. The Executor layer is the existing swrm dispatch
(`run` / `batch` / `fleet` / M26 remote runners); slice 1 is **shadow-only**:
an accepted Trigger creates a Ticket and a sealed Mandate but never an
Assignment and never dispatches.

New concepts (all in `bin/openswarm`, section "intake"):

| Concept | Meaning |
| --- | --- |
| Source / Trigger / Thread | One GitHub comment (or issue/PR event) on one thread. A Trigger is normalized strictly: missing `id` / `thread.repo` / `thread.kind` / `thread.number` / `author` / `body` is refused, never guessed at. |
| Gate | Pure deterministic admission: Trigger + intake policy -> Decision (`accept` / `stop` / `ignore` / `unavailable`). No I/O, no model calls. Assessment (model judgement) is slice 2. |
| Decision | `{decision, strength, rules, reason, draft_only, policy_version, trigger_id, accept_mode}`. Every Decision is appended to the intake ledger. |
| Ticket | `open -> ended -> open` (resume transition validated here; the resume *path* lands in slice 3). One Ticket per Trigger; independent of any single run. |
| Mandate | Sealed at Ticket creation: policy version, scope (thread + 120-char summary), strength, hit rules, source identity. `_sealed: true`; `intake_update_mandate` refuses any update — growing scope means a new Ticket. |
| Assignment | Binds a Ticket to one Executor instance: `pending -> running -> ended` (pending may also cancel to `ended`). Slice 1 never creates one. |

## Intake policy (`$OPENSWARM_HOME/intake-policy.json`, default v1)

Separate from `policy.json` (quota/routing) by name on purpose. Strict
validation: an unknown `accept_mode`, a bad admission value, a missing
version, or malformed JSON refuses loudly instead of silently admitting
(or ignoring) work. No policy file -> the built-in v1 policy is used and
reported as source `default`.

```json
{
  "version": 1,
  "accept_mode": "shadow",
  "scopes": ["issue", "pr"],
  "allowlist": {"users": ["surlymochan"], "bots": []},
  "admission": {
    "listed_mentioned": "explicit",
    "listed_unmentioned": "inferred",
    "unlisted_mentioned": "inferred",
    "unlisted_unmentioned": "ignore"
  },
  "money_draft_only": true
}
```

### Admission table

| Author | @openswarm mentioned | Outcome |
| --- | --- | --- |
| listed user | yes | `accept`, strength `explicit` |
| listed user | no | `accept`, strength `inferred` |
| unlisted user | yes | `accept`, strength `inferred` |
| unlisted user | no | `ignore` |
| bot, not allowlisted | — | `ignore` (`bot-message`; bot messages are context only) |
| bot, allowlisted | — | `accept`, strength `inferred` at most, never `explicit` |
| the intake bot itself (`openswarm[bot]`) | — | `ignore` (`own-bot-message`) |
| any, thread kind outside `scopes` | — | `ignore` (`scope-outside`) |
| any, `accept_mode` is `off` | — | `unavailable` (`accept-mode-off`) |

### Extra Gate rules (applied after admission)

- **Human stop**: a listed author writing a stop (`@openswarm stop`, or a
  leading `stop` / `停止` / `别做了` / `取消任务` / `不要做了`) yields
  Decision `stop`; ingest ends the thread's open Ticket if one exists,
  otherwise records the stop with no Ticket. An unlisted author saying
  "stop" does **not** stop anything.
- **Money / commitment draft-only**: a body matching a money action
  (buy/sell/pay/transfer/… or 买/卖/付款/转账/充值/提现/…) or a currency
  (USDT/USDC/BTC/ETH/SOL/USD/$, ¥, 美元/人民币) or an outward commitment
  (对外承诺 / promise to / guarantee / commit to deliver…) is still
  accepted, but with `draft_only: true` and rule `money-draft-only` /
  `commitment-draft-only`: a draft may be generated, it is never
  auto-executed. This holds even for an allowlisted author.

## Ledger and state

- **Ledger** `$OPENSWARM_HOME/intake-ledger.jsonl` — append-only. Entries
  (`trigger_received`, `decision`, `ticket_created`, `mandate_sealed`,
  `ticket_ended`, `duplicate_skipped`) carry a contiguous `seq` under the
  same lock file the run ledger uses; nothing ever rewrites it in place.
- **State** `$OPENSWARM_HOME/intake-state.json` — `triggers_seen`
  (Trigger id -> Ticket id or null), tickets, mandates, assignments,
  source cursors (reserved for the slice 2 Source). Written atomically
  via `jsave`.
- **Idempotency**: a repeated delivery of the same Trigger id returns the
  original Ticket and writes only one `duplicate_skipped` ledger line.

## CLI (new subcommand family; existing commands untouched)

```
openswarm intake policy [--init [--force] | --json]   # show / write v1 policy
openswarm intake ingest --trigger-file T.json [--policy-file P.json]
openswarm intake replay --fixture F.json [--policy-file P.json] [--check] [--json]
openswarm intake tickets [--json]
openswarm intake decisions [--n N] [--json]
```

## Replay evidence (fixture `tests/fixtures/m27-replay.json`)

The fixture replays the **real** `surlymochan/swrm` comment stream:
6 verbatim comments fetched 2026-11-14 via `gh api` (ids and URLs in each
trigger's `provenance`), plus 4 labelled synthetic boundary cases the real
stream does not cover (unlisted author, stop, money) and 1 duplicate
re-delivery of R1. Expected Decisions were derived by hand from the table
above **before** running the replay; `intake replay --check` compares all
of them.

| # | Trigger (real unless noted) | Expected | Actual |
| --- | --- | --- | --- |
| R1 | surlymochan `@codex review`, PR #13, comment 6510574536 | accept / inferred | accept / inferred, ticket `tkt-9d4dbc92c596` |
| R2 | `chatgpt-codex-connector[bot]` review summary, PR #13, 6510579421 | ignore (`bot-message`) | ignore |
| R3 | surlymochan scoped-wrapper note, PR #16, 6512478180 | accept / inferred | accept / inferred, ticket `tkt-44f4d25e2d23` |
| R4 | surlymochan scheduling note, issue #212, 6876814735 | accept / inferred | accept / inferred, ticket `tkt-40c4005f9edb` |
| R5 | `chatgpt-codex-connector[bot]` review receipt, PR #13, 6510592583 | ignore (`bot-message`) | ignore |
| R6 | surlymochan M26 slice 2 evidence, issue #210, 6876463120 | accept / inferred | accept / inferred, ticket `tkt-1af08e1051b8` |
| S1 | synthetic unlisted `@openswarm` request, issue #999 | accept / inferred | accept / inferred, ticket `tkt-0ef84a6eb733` |
| S2 | synthetic unlisted, no mention, issue #999 | ignore | ignore |
| S3 | synthetic surlymochan `@openswarm stop`, PR #13 | stop (ends R1's ticket) | stop, R1 ticket `ended` |
| S4 | synthetic surlymochan `@openswarm 帮我买 10 USDT 的 BTC`, issue #998 | accept / explicit, draft_only | accept / explicit, draft_only, ticket `tkt-9eebc788091e` |
| R1′ | duplicate re-delivery of R1 (same trigger id) | duplicate, same ticket | duplicate, ticket `tkt-9d4dbc92c596` |

Replay totals: 11 triggers, 6 Tickets (5 open, 1 ended by the stop),
6 sealed Mandates, **0 Assignments**, 34 ledger lines
(`trigger_received` 10 + `decision` 10 + `ticket_created` 6 +
`mandate_sealed` 6 + `ticket_ended` 1 + `duplicate_skipped` 1).
`replay check OK: 11 triggers, all decisions match expected`.

Note the real stream contains no `@openswarm` mention and no unlisted
author — that is why all four real surlymochan comments land as
`inferred`, and why the explicit / unlisted / stop / money paths are
covered by labelled synthetic cases instead of invented "real" ones.

## Tests

33 new tests in `tests/test_m27_slice1.py` (admission table, bots, scope,
mode off, money/commitment/stop, policy validation, Mandate sealing,
both state machines, idempotency, shadow no-Assignment, append-only
ledger with contiguous seq, policy-version pinning, the replay fixture
end to end, CLI smoke for all five subcommands).

## Deliberate divergences and limits (slice 1)

- `accept_mode: on` is accepted by policy validation but behaves like
  shadow in this slice: a Ticket is created, no Assignment, no dispatch.
  Real dispatch lands in slice 2; the decision records this.
- Ticket closing still requires swrm verify + evidence (2026-11-07
  standing rule); dsh's executor-self-report close is intentionally
  **not** ported.
- The resume transition is validated in the model, but no resume path
  exists yet (slice 3, Subscription).
- Slice 2 (M27 #224: GitHub Source + Assessment + real dispatch + Channel)
  is in flight: [#224](https://github.com/surlymochan/swrm/issues/224).
