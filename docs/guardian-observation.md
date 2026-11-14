# Guardian observation — observe-only fleet health

Public issue **#216**, the first stage of **#215**. The guardian reads the
machine-readable outputs that `openswarm` already produces, normalises them
into **one versioned health snapshot**, and persists that snapshot atomically
so a controller that restarts can read the last observation back.

This stage is **observe-only**. It is a sensor, not a controller.

## What it does

* Ingests existing outputs: a fleet snapshot, a fleet health report, a quota
  report, the probe cache, and an existing structured resource-metrics export.
* Normalises them into one snapshot with timestamps, per-observation
  freshness and confidence, and an explicit `unknown` verdict for every
  stale, missing or unsupported input.
* Folds the observations into one fleet verdict (`healthy`, `degraded`,
  `unhealthy`, `unknown`, `drained`) with per-dimension detail and a list of
  failure signals.
* Persists the snapshot atomically (unique scratch file + `os.replace` +
  `fsync`) into the guardian state directory, under a lock that serialises
  the state updates of concurrent threads and processes.
* Re-evaluates the persisted snapshot when it is read back: the freshness of
  its inputs and the current manual drain are checked again, so a restarted
  controller never acts on evidence that went stale and never reports a drain
  that was already lifted.

## What it never does

* No dispatch, no admission, no capacity reservation, no reroute.
* No recovery, no reaping, no lane restart, no service change.
* No language model anywhere in the pipeline.
* No second fleet collector: it never walks hosts, lanes or runs itself; it
  only reads the outputs the existing commands already produce.
* No capacity or free-slot inference. An idle lane is reported as an idle
  lane; it is never turned into "free capacity", and nothing is re-dispatched
  because of what was observed.
* No resource collection. Memory/disk/network numbers are *accepted* as an
  existing structured input; when no such input is given, the guardian says
  so instead of claiming a source it never read.

## Commands

```bash
# 1. produce the inputs with the existing commands (read-only)
openswarm fleet _snapshot  > fleet-snapshot.json
openswarm fleet health --json > fleet-health.json
openswarm quota --json      > quota.json
openswarm probe --quiet     # writes the probe cache the guardian can read

# 2. observe, normalise and persist one health snapshot
bin/swrm-guardian observe \
    --snapshot fleet-snapshot.json \
    --health   fleet-health.json \
    --quota    quota.json \
    --state-dir "$OPENSWARM_HOME/guardian"

# 3. read the persisted snapshot back (this is what a restarted controller does)
bin/swrm-guardian status --state-dir "$OPENSWARM_HOME/guardian"

# 4. hold the fleet (durable, operator intent, independent of observations)
bin/swrm-guardian drain --reason "host under maintenance" --by operator
bin/swrm-guardian undrain
```

Useful flags:

| Flag | Meaning |
| --- | --- |
| `--state-dir DIR` | Where `health.json` and `drain.json` live. Default: `$OPENSWARM_GUARDIAN_HOME`, else `$OPENSWARM_HOME/guardian`, else `~/.claude/openswarm/guardian`. |
| `--config FILE` | Threshold config JSON (see below). |
| `--max-age SECONDS` | Freshness window. Default 300 s. |
| `--observed-at EPOCH` | Timestamp to assume for inputs that carry no timestamp of their own (for example a capture piped through stdin). |
| `--require NAME` | Extra input slots that must be usable (`snapshot`, `health`, `quota`, `probe`, `resources`). Repeatable, comma-separated. |
| `--no-require-fleet` | Do not require a fleet snapshot or fleet health input. |
| `--no-persist` | Build the snapshot without writing it. |
| `--format json\|text` | Output format. `json` (default) prints the whole snapshot. |
| `--fail-on never\|unhealthy\|not-healthy` | Exit 2 when the verdict matches. Default `never`: the exit status reports whether the *command* worked, not how healthy the fleet is. |

Any input slot accepts `-` to read one document from stdin (one slot only).

## Inputs

The guardian accepts the outputs of the existing commands as they are; it
does not re-derive them.

| Slot | Produced by | What the guardian reads |
| --- | --- | --- |
| `--snapshot` | `openswarm fleet _snapshot` | `lanes[]` (state, backend, provider, `last_error`), `providers{}` (backend → provider), `lane_config_valid` |
| `--health` | `openswarm fleet health --json` | `hosts[]` (reachable, paused, status, error) with the per-host `health{}` payload (its own `lanes[]` and `runs[]`) and/or top-level `runs[]` (verdict, reason, evidence) and `lanes[]` |
| `--quota` | `openswarm quota --json` | `backends[]` (backend, remaining, status, detail, source) |
| `--probe` | the probe cache written by `openswarm probe` | `claude` / `codex` rate-limit windows, their staleness flags, `deepseek` balance |
| `--resources` | any existing structured resource export | `hosts[]` with `memory` / `disk` / `network` metrics |

Resource metrics are read as given. The accepted shape is:

```json
{
  "observed_at": 1730000000,
  "hosts": [
    {"name": "worker-1",
     "memory": {"used_pct": 71.0},
     "disk": {"free_pct": 34.0},
     "network": {"ok": true, "latency_ms": 25}}
  ]
}
```

`memory` accepts `used_pct` (or `used_percent`/`percent`), `disk` accepts
`free_pct` (or `free_percent`/`percent_free`), `network` accepts `ok` /
`reachable`, `error` and `latency_ms`. A host with no metric at all is
reported `unknown`, never healthy. Metrics that were not supplied are listed
under `unmeasured` — the guardian never pretends to have measured them.

## The verdict

Statuses, folded with a severity order in which **unknown outranks healthy**:

`unhealthy` > `degraded` > `unknown` > `healthy` > `drained`

A dimension status is the worst status among its items; the overall status is
the worst dimension status. `drained` is neutral: it never masks a real
status.

* **Lanes** — `crashed`/`dead`/`failed` are `unhealthy`; `down`,
  `starting`/`stopping` are `degraded`; `idle`/`busy`/`running` are healthy;
  an unrecognised state is `unknown`. A lane error is at least `degraded`. An
  invalid lane config is `unhealthy`.
* **Hosts** — unreachable or config-invalid hosts are `unhealthy`, a paused
  host is `degraded`, a reachable host is healthy. `openswarm fleet health
  --json` nests each host's own lanes and runs under the host, and that
  nested evidence belongs to the host: a crashed lane, a dead run or a
  provider rate limit (a `429` marker) under a host makes that host
  `unhealthy`, even when the host itself answers. The nested lanes and runs
  are also reported on their own (`dimensions.health.lanes` /
  `dimensions.health.runs`), tagged with the host they belong to.
* **Runs** — `alive`/`done` are healthy, `zombie` is `degraded`, `dead` is
  `unhealthy`, `unknown` (no runner pid recorded) stays `unknown`. A run
  that is in flight (`alive`) is healthy: the guardian never reports
  in-flight work as a fault.
* **Providers** — a provider is `unhealthy` when an explicit failure signal
  names it: a `429`/rate-limit marker in a lane error, or a lane that is out
  of quota. Providers with no signal are reported as "no provider failure
  signal in the ingested inputs" — the guardian does not claim to have probed
  them.
* **Quota** — `ok` is healthy, `low` is `degraded`, `error` is `unhealthy`,
  `unknown` stays `unknown`; a remaining count of zero is `unhealthy`
  (quota exhausted) and also marks the backend's provider. A backend with no
  quota source at all (source `none (unknown)`) is reported as **not metered**:
  it is listed under `dimensions.quota.unmetered` and never folds into the
  verdict, because an unmetered backend is a known gap, not a missing
  observation.
* **Resources** — over the memory/disk/network thresholds below.

### Freshness and confidence

Every input is recorded with the timestamp it was read at, how old it is and
how much the guardian trusts it:

| Freshness | When | Verdict effect |
| --- | --- | --- |
| `fresh` | age ≤ `--max-age` | the signals count normally |
| `stale` | age > `--max-age` | the dimension is `unknown`; the items are kept as unconfirmed evidence but never counted as healthy and never escalated into a fault |
| `unknown` | the input carries no timestamp (for example stdin without one) | the dimension is `unknown` |

Confidence is `high` when the payload carried its own timestamp, `medium` when
only the file mtime was available, `low` for stale or undated inputs and
`none` for an input that could not be read. The overall confidence is the
weakest observation, capped at `medium` while any input slot is missing.

A missing, unsupported, unreadable, oversized or too deeply nested input is
recorded as such and its dimension is `unknown`. **The overall verdict is
never `healthy` while evidence is missing.**

An observation that claims to be from the **future** is refused: it is
recorded as `invalid` (a broken clock is not evidence), so it can never be
aged into "fresh for ever".

### Missing evidence never erases a verified fault

A required input that is missing (`--require`, or the fleet input that is
required by default) makes the verdict `unknown` **only when nothing else
was verified**. A fleet that was seen to be unhealthy stays `unhealthy`, and
the missing input is reported next to it:

```
"overall": {"status": "unhealthy", "reason": "required input missing: quota (a verified status is never erased by missing evidence); snapshot=unhealthy"}
```

### Stale evidence never poisons fresh evidence

A stale (or undated) input is reported as `unknown` and its items are marked
`unconfirmed`: they keep the status they had in `unconfirmed_status`, but
they never count as healthy and never escalate into a fault. A stale quota
report therefore cannot mark a provider `unhealthy`, and a stale fleet
report cannot turn a healthy provider into a fault. The signals of an
unconfirmed item are listed with `"confirmed": false`.

### Truncated inputs are incomplete, never silently short

`max_records` bounds how many rows of one dimension are reported. When it
bites, the dimension says so: it is marked `incomplete`, its detail names how
many rows were not reported, and its status becomes `unknown` — the rows
that were not read could hold anything. The limit never conceals a verified
fault: a `crashed` lane, an unreachable host or a rate limit among the rows
that were not reported is listed in `truncated_faults` and keeps the
dimension `unhealthy`.

### Thresholds

Defaults (override with `--config`):

```json
{
  "thresholds": {
    "max_age_seconds": 300,
    "memory_warn_pct": 80,
    "memory_critical_pct": 90,
    "disk_warn_free_pct": 20,
    "disk_critical_free_pct": 10,
    "network_warn_latency_ms": 2000,
    "quota_window_warn_used": 0.8,
    "quota_window_critical_used": 0.95,
    "max_input_bytes": 4194304,
    "max_depth": 32,
    "max_records": 500
  }
}
```

Unknown keys are rejected, so a typo fails loudly instead of silently
changing nothing. `max_input_bytes`, `max_depth` and `max_records` bound
what a single observation may cost.

## Manual drain

A manual drain is **durable operator intent** and lives in its own file
(`drain.json`), separate from the transient observations:

```bash
bin/swrm-guardian drain --reason "disk rebuild on worker-1" --by operator
bin/swrm-guardian drain --reason "worker-1 rebuild" --host worker-1   # one host
bin/swrm-guardian undrain
```

* A fleet-wide drain makes the overall status `drained`; the transient status
  is still reported next to it (`overall.transient_status`).
* A host-scoped drain marks that host `drained` and leaves the rest of the
  fleet alone. The lanes and runs nested under that host are drained with it:
  they belong to the host the operator held.
* **Observing a healthy fleet never clears a drain.** Only `undrain` does.
* The drain record is kept (with `active: false`) after `undrain`, so the
  history of a drain is auditable.
* Reading the persisted snapshot back re-checks the drain: a drain that was
  lifted while the controller was down is not reported as active any more.

## Persisted state

`health.json` holds one versioned snapshot:

```json
{
  "schema": "swrm.guardian.health",
  "schema_version": 1,
  "observer": "swrm-guardian",
  "generated_at": 1730000000.0,
  "generated_at_iso": "2026-11-14T20:00:00Z",
  "overall": {"status": "degraded", "transient_status": "degraded",
              "reason": "quota=degraded", "confidence": "medium"},
  "coverage": {"inputs": [], "provided": [], "not_provided": [],
               "unusable": [], "dimensions": [], "missing_required": [],
               "required": ["fleet"], "truncated": []},
  "thresholds": {},
  "dimensions": {},
  "signals": [],
  "manual_drain": {"active": false}
}
```

The file is written atomically: the payload goes into a *unique* scratch file
(created with `mkstemp`, so two writers never share one), it is `fsync`ed and
then renamed onto the target, so a reader never sees a half-written snapshot
and a crashed write leaves the previous snapshot intact. The state directory
is guarded by an advisory lock (`state.lock`), so concurrent observers and
drain updates - in threads and in processes - cannot interleave.

Reading the snapshot back re-evaluates it: the freshness of every input is
recomputed against the current time (a snapshot whose inputs went stale while
the controller was down is reported `unknown`, never `healthy`) and the
current manual drain is applied again. The result carries `reevaluated_at`
(the time of that check) next to `generated_at` (the time of the
observation). A snapshot written by a newer schema version is refused with a
clear error rather than misread.

## Programmatic use

```python
import sys
sys.path.insert(0, "bin")            # the guardian package lives next to the CLI
from guardian import observe

snapshot = observe({"snapshot": "fleet-snapshot.json", "quota": "quota.json"},
                   state_dir="/var/tmp/guardian-state")
print(snapshot["overall"]["status"])
```

`observe()` accepts a path, `"-"` for stdin or an already-parsed payload per
slot, and never dispatches, admits or recovers anything.

## Limitations

* The guardian can only judge what the ingested inputs contain. A dimension
  with no observation is omitted from the verdict and listed in
  `coverage.not_provided`; it is never assumed healthy.
* Resource metrics are not collected by this tool. Without a `--resources`
  input there is no memory/disk/network verdict at all.
* Provider health is inferred from failure *signals* in the ingested outputs
  (rate-limit markers, exhausted lanes, quota rows). There is no direct
  provider probe, so "no failure signal" means "nothing in these inputs
  failed", not "the provider was verified healthy".
* Lane states are recognised by name. A state this version does not know is
  reported `unknown` on purpose, not guessed.
* Only one input can be read from stdin per invocation.
* The snapshot is a point-in-time verdict. It carries no history and no
  trend; a later stage may add one. What is persisted is re-evaluated when
  it is read back, so the verdict a controller reads is the verdict for
  *now*, not a frozen one.
* `max_records` bounds how many rows one dimension reports. The rows that
  were not reported are not read into the verdict, so a dimension that hit
  the limit is `unknown` (incomplete) even when the rows it did read were
  healthy.
* Nothing here is a gate. The verdict is not wired into dispatch or
  admission; that is the next stage.

## Tests

```bash
python3 -m unittest discover -s tests -p test_guardian_observation.py
```

The suite drives the real module and the real CLI: memory/disk/network
deterioration, unreachable hosts, crashed lanes, 429 and quota exhaustion,
host-scoped health (lanes and runs nested under a host), stale and
unsupported quota, missing inputs, truncated inputs, future timestamps, the
durable manual drain, a controller restart that reads the state back, a
corrupt snapshot, the input bounds, concurrent threads and processes writing
the same state directory, and a write that is killed half way through.
