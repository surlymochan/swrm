# M29 slice 1: per-repo intake-policy + evalid allowlist + cross-repo dedup core

Refs: epic #241, slice 1 #242. M28 (`docs/m28-slice1-active-core.md`)
shipped active mode for one repo (`surlymochan/swrm` only). This slice
generalizes intake to a second own repo (`surlymochan/evalid`) and makes
one source Trigger build at most one Mandate across repos. No real
online dispatch happens in this slice (that evidence belongs to slice 2);
the mechanism and its guardrail tests are the deliverable.

## What landed

All in `bin/openswarm`, intake section; tests in
`tests/test_m29_slice1.py` (28 tests). Existing subcommand signatures,
the CLI name, `bin` path, `OPENSWARM_*` variables, the SKILL contract,
weighted / calibration / quota routing and batch / fleet scheduling are
untouched. The shipped default stays `accept_mode=shadow`, so a
non-allowlisted repo still dispatches nothing.

### Per-repo policy (`repo_policies` block, optional)

```json
"repo_policies": {
  "surlymochan/swrm": {
    "version": 2,
    "allowlist": {"users": ["surlymochan", "alice"], "bots": []}
  },
  "surlymochan/evalid": {
    "version": 3,
    "allowlist": {"users": ["surlymochan"], "bots": []}
  }
}
```

`intake_policy_for_repo(policy, repo)` resolves the effective policy
for one Trigger's repo: a `repo_policies` entry (matched
case-insensitively on `owner/name`) overrides the global policy
version and allowlist for that repo only; admission, `active_scope`,
caps, dispatch and assessment stay global. A repo with no entry gets
the policy object unchanged, so swrm behaviour is byte-identical to
the pre-M29 policy (regression test asserts the Gate decision dict
for a swrm Trigger equals the pre-change literal). The resolved
version is what the Gate records and the Mandate seals, so each repo's
Mandates carry that repo's policy version. Validation refuses bad
repo keys, bad versions and bad allowlists, and unknown per-repo keys.

The built-in default policy now carries a per-repo entry for
`surlymochan/evalid` (version 1, users `["surlymochan"]`) -- the
allowlist extension itself. `active_scope` is still unconfigured by
default, so active mode with no operator scope dispatches nothing;
slice 2 configures `active_scope.repos` with both repos in the live
policy file. Because ingest resolves the effective policy per repo,
the `active_scope.users` default (the allowlist users) is also
per-repo: an account authorized in swrm only is in scope in swrm and
nowhere else.

### Cross-repo dedup (`content_seen` in intake state)

One source Trigger builds at most one Mandate across repos. Identity
is content, not delivery: a Trigger claims

- a content key -- `sha1(author.lower + normalized body)`, where the
  body is stripped of the `@openswarm` mention, whitespace-collapsed
  and case-folded; and
- every `owner/repo#number` reference its body cites.

When a Ticket is created, those keys plus the Ticket's own thread as a
`repo#number` reference are recorded in state (`content_seen`,
key -> ticket id). A later Gate-accepted Trigger whose keys hit a
recorded Ticket resolves to that Ticket instead of opening a new one:
result `duplicate=True, cross_repo_duplicate=True`, no new Ticket, no
new Mandate, no Assessment, no dispatch, one
`cross_repo_duplicate_skipped` ledger entry (the Trigger id is
recorded against the existing Ticket, so a re-delivery of that id
resolves through the Trigger-id path).

Scope notes, all covered by tests:

- Cross-repo only: within one repo, two distinct threads with
  identical text are distinct work items (M27 slice 3 opens two such
  Tickets) -- same-repo re-delivery is already covered by the
  Trigger-id dedup.
- A key whose Mandate was withdrawn (human stop) no longer dedups:
  the stop retired that work, so a later task opens a new Ticket.
- A follow-up on a live thread with a different task still goes
  through the normal path (the Trigger's own thread is a registration
  key, not a lookup key).
- Idempotent replay: re-delivering a Trigger id returns the original
  Ticket (`duplicate_skipped`); re-delivering the same source under a
  new id in the other repo returns it too
  (`cross_repo_duplicate_skipped`).

### Guardrails (all reusing the M27/M28 mechanisms, not re-implemented)

- Per-thread caps (`caps.max_dispatches_per_thread`,
  `caps.max_budget_minutes_per_thread`): counted from Assignments on
  the thread, in both repos; a blocked dispatch writes `cap_blocked`
  and creates no Assignment.
- Health-gate pre-check (M24/M25 reuse): an unhealthy (or unknown
  with `on_unknown=block`) backend blocks before any Assignment or
  run exists (`intake_health_gate_blocked` ledger entry).
- Non-allowlist default is shadow: `accept_mode=active` with no
  `active_scope` configured, or a repo / author outside the scope,
  leaves the Ticket shadow with an `active_scope_shadow` trace; an
  unlisted author without a bot mention is Gate-ignored (no Ticket,
  zero runs).

## Operator config for slice 2 (evalid live rollout)

In `$OPENSWARM_HOME/intake-policy.json` (see `intake policy --init`
for the shipped default, which already allowlists evalid per-repo):

```json
{
  "version": 1,
  "accept_mode": "active",
  "active_scope": {
    "repos": ["surlymochan/swrm", "surlymochan/evalid"],
    "users": ["surlymochan"]
  },
  "repo_policies": {
    "surlymochan/evalid": {
      "version": 1,
      "allowlist": {"users": ["surlymochan"], "bots": []}
    }
  },
  "caps": {"max_dispatches_per_thread": 3,
           "max_budget_minutes_per_thread": 60}
}
```

## Not covered here (slice 2)

Real evalid issues dispatched end to end (intake -> Mandate ->
real run -> PR opened + Channel reply, ledger-verified), the
unauthorized-issue ignore and stop/withdraw live cases, and the epic
acceptance check against the live ledger.
