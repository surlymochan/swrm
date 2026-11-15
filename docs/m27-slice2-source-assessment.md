# M27 slice 2: GitHub Source + Assessment + real dispatch + Channel

Refs: epic #212, slice 2 #224. Fusion plan: `docs/dsh-superagent-fusion.md`.
Slice 1 (`docs/m27-slice1-intake-gate.md`): domain core, ledger, Gate, shadow.

## What landed

The intake pipeline is now end to end for one repo at a time:

```
gh poll -> Trigger -> Gate (slice 1, deterministic)
        -> Assessment (model judgement, second layer)
        -> accept + mode on + not draft_only -> Assignment -> `openswarm run`
        -> Channel comment back to the source thread
```

New concepts (all in `bin/openswarm`, intake section):

| Concept | Meaning |
| --- | --- |
| GitHub Source | `intake poll --repo owner/name` fetches issues + issue comments via `gh api` (one page each, newest first). `--issue N` / `--pr N` restricts the poll to a single thread -- use this for acceptance runs so a poll cannot dispatch on unrelated threads. |
| Read cursor | `state.cursors[repo].since` = max `updated_at` seen; persisted in `intake-state.json`, so a restart never replays the window. Dedup also lives in `triggers_seen` (Trigger id -> Ticket id), so a re-poll of the same window yields only duplicates. A thread-restricted poll (`--issue N` / `--pr N`) leaves the cursor untouched: it only saw one thread, so advancing the repo-wide cursor would hide other threads updated before it. |
| Own-post filter | The Channel posts with the marker `<!-- openswarm-intake -->`; the translator drops any event whose body contains it. This matters because Channel posts as the allowlisted user account -- the marker, not the author, is what keeps them out. Messages from `openswarm[bot]` are dropped too. |
| Assessment | Second layer after the Gate. It can only downgrade an accept to ignore; it never admits what the Gate ignored, stopped, or marked unavailable. The recorded Decision is the final one; `gate_decision` preserves the Gate outcome. |
| Model hook | When a model command is configured (CLI `--model-command`, policy `assessment.command`, or `$OPENSWARM_ASSESS_CMD`), Assessment runs it with Trigger + Gate Decision + thread context JSON on stdin and reads `{"verdict": "accept"\|"ignore", "reason": ...}` on stdout. Any failure (exit code, bad JSON, bad verdict, timeout) falls back to the deterministic default and records `fallback_error` -- a down model never fails the poll. |
| Deterministic default | No model configured: explicit mandates are confirmed without a call; inferred candidates are accepted when the body (minus the bot mention) is a concrete task (>= 10 chars) and not a repeat of an already-open Ticket on the same thread (same 120-char Mandate scope); otherwise ignored with a reason. |
| Real dispatch | `intake_dispatch` creates an Assignment (`pending -> running -> ended`), then runs one `openswarm run` in a subprocess in the same `$OPENSWARM_HOME`, so quota routing, health gating and verify are exactly the production path. A runner failure still ends the Assignment with the reason and writes one `dispatch_failed` ledger entry. The run id and `result.json` are the evidence. Dispatch timeout = budget + 3 min grace; the executor is killed, never waited on forever. |
| Channel | After a dispatch (success or failure) the Channel posts a comment to the source thread via `gh api`, with the marker, ticket id, run id/status, branch/commit when the result carries them, and the evidence pointer. A Channel failure is recorded (`channel_failed`), never raised. |
| Dispatch config | Policy `dispatch` block (all optional): `backend` (null = routed), `backends_file`, `verify`, `budget_minutes` (default 10). CLI flags on `intake poll` override per run. |

## What still does not dispatch

- `accept_mode` is not `on` (default `shadow`): Ticket only.
- `draft_only` (money / commitment, Gate rule): Ticket only, in any mode.
- No dispatch runner passed (plain `intake ingest`): Ticket only.
  `intake ingest` is the shadow path; real dispatch is wired through
  `intake poll` only.

## CLI (new subcommands; existing commands untouched)

```
openswarm intake assess --trigger-file T.json [--policy-file P.json] [--model-command CMD...]
openswarm intake poll --repo owner/name [--issue N | --pr N] [--since ISO] [--limit N]
    [--dry-run] [--policy-file P.json] [--model-command CMD...]
    [--backend B] [--backends-file F] [--verify CMD] [--budget-minutes N]
    [--no-dispatch] [--no-channel] [--cwd DIR] [--json]
```

`intake policy` now also prints the effective dispatch and assessment config.

## Ledger entries added (slice 2)

`assessment` is embedded in the `decision` entry (with `gate_decision`
and the final `decision`); new kinds: `assignment_created`,
`dispatch_started`, `dispatch_finished`, `dispatch_failed`,
`channel_posted`, `channel_failed`.

## Tests

35 new tests in `tests/test_m27_slice2.py`: translator (issues, PRs,
comments, marker, own-bot, null body, malformed), polling (cursor,
dedup, thread filter), Assessment (gate precedence, explicit fast
path, default rules, duplicate-of-open-ticket, model hook accept /
ignore / veto / failure fallback), ingest wiring (shadow no-dispatch,
on-mode dispatch + channel, draft_only block, dispatch/channel
failure recording, assessment downgrade, no-runner ticket-only),
policy validation for the new blocks, CLI smoke (`assess`, `poll
--dry-run` with a fake `gh`), and one real end-to-end dispatch
through the subprocess `run` path with the echo custom backend
(`result.json` status `done`, branch file verified via `git show`).

## Real-data shadow poll (2026-11-15, isolated $OPENSWARM_HOME)

One read-only `intake poll --repo surlymochan/swrm --since
2026-11-14T00:00:00Z` (default shadow policy, no dispatch, no
Channel) against the live repo: **59 triggers** ingested, **56
accept / 2 ignore / 1 stop**, 56 Tickets + 56 sealed Mandates,
**0 Assignments**, 230 ledger lines, cursor persisted at
`2026-11-14T19:13:32Z`. Both ignores are real
`chatgpt-codex-connector[bot]` comments (`bot-message`); the stop is
the quoted-`@openswarm stop` case noted under limits above. Evidence:
workspace `swrm-m27-slice2/evidence-shadow/` (ledger + state; the
stdout JSON was truncated by a local `head` pipe, the ledger is the
complete record).

## Deliberate divergences and limits (slice 2)

- **No in-repo LLM client**: swrm has no model-call helper (the
  "planner" is routing logic, not a model invocation), so Assessment
  ships as deterministic rules + an external command hook. Wiring a
  real model is a config point (command + timeout), not a new
  dependency; stdlib-only is unchanged.
- **Explicit mandates skip the model** unless a model command is
  configured (the model may still veto). A listed author @-mentioning
  the bot is unambiguous intent; a model second-guessing it adds
  failure modes for no gain.
- **Comment threads**: a comment event alone cannot tell a PR thread
  from an issue thread; the poll resolves the kind from the parent
  issue in the same fetch (`pull_request` key) and defaults to
  `issue` when the parent was not fetched.
- **One page per fetch**: `poll` reads one page (<= `--limit`, default
  100) of issues and one of comments per run; long-quiet repos are
  fully covered by the cursor, busy repos need frequent polls.
- **Stop matches quoted text**: the Gate's human-stop rule fires on any
  `@openswarm stop` (or line-leading stop word) anywhere in the body,
  including a quoted mention inside a long evidence comment. Real-data
  proof: a 2026-11-14 shadow poll decided `stop` on comment 6878371088
  because its table quotes the synthetic S3 case ("`@openswarm stop`").
  Slice 1 behaviour, kept as-is here; tightening it (e.g. only a
  standalone instruction counts) belongs to slice 3, not this slice.
- Subscription / resume / stop / `attention` CLI belong to slice 3.
