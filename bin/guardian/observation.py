"""Observation-only fleet health guardian (public issue #216, stage 1 of #215).

The guardian turns *existing* machine-readable openswarm outputs into one
normalised, versioned health snapshot and persists that snapshot atomically,
so a controller that restarts can read the last observation back.

Inputs (all optional, all read-only, all produced by existing commands):

  snapshot   `openswarm fleet _snapshot`      lanes, providers, active runs
  health     `openswarm fleet health --json`   per-host reachability, run verdicts
  quota      `openswarm quota --json`           remaining quota per backend
  probe      the probe cache (`openswarm probe`) per-backend quota windows
  resources  an existing structured resource-metrics export (memory/disk/network)

This module never dispatches, admits, recovers, drains a lane, restarts a
service or calls a language model. The only writes are two JSON files under
the guardian state directory: the health snapshot and the manual-drain record.
Resource metrics are *accepted* as an existing structured input; the guardian
never collects them, and it never claims a source it did not read.

Two rules shape every verdict:

* Unknown is never healthy. A stale, missing, unsupported or unreadable
  input yields UNKNOWN for the signals it would have carried, so the overall
  verdict can never be `healthy` while evidence is missing.
* A manual drain is durable operator intent, kept in its own file. Observing
  a healthy fleet never clears it.
"""
import datetime
import json
import math
import os
import sys
import time

SCHEMA = "swrm.guardian.health"
SCHEMA_VERSION = 1
DRAIN_SCHEMA = "swrm.guardian.drain"
DRAIN_SCHEMA_VERSION = 1
OBSERVER = "swrm-guardian"

HEALTHY = "healthy"
DEGRADED = "degraded"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"
DRAINED = "drained"
#: A backend with no quota source at all: a known capability gap, not a
#: missing observation. It is reported, but it never folds into a verdict.
NOT_METERED = "not-metered"

#: Severity order used to fold item statuses into a dimension status and
#: dimension statuses into the overall verdict. UNKNOWN outranks HEALTHY so a
#: signal nobody could read can never look healthy.
_SEVERITY = {HEALTHY: 0, DRAINED: 0, UNKNOWN: 1, DEGRADED: 2, UNHEALTHY: 3}
_CONFIDENCE_ORDER = ("none", "low", "medium", "high")
_CONFIDENCE_RANK = {name: index for index, name in enumerate(_CONFIDENCE_ORDER)}

#: The five input slots, in the order they are reported.
INPUT_KINDS = ("snapshot", "health", "quota", "probe", "resources")

SNAPSHOT_FILE = "health.json"
DRAIN_FILE = "drain.json"

#: Explicit provider-side failure markers. A 429 is a provider failure, not a
#: local one, so it is reported as such instead of being folded into "unhealthy".
RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limit", "ratelimit",
                      "too many requests", "throttl", "resource_exhausted")
QUOTA_MARKERS = ("quota exceeded", "quota exhausted", "quota_exhausted",
                 "insufficient_quota", "insufficient quota")

#: Lane states as reported by `openswarm lane status` / `fleet _snapshot`.
LANE_HEALTHY_STATES = ("idle", "busy", "running", "active", "ready", "working")
LANE_DEGRADED_STATES = ("down", "starting", "stopping", "draining", "restarting")
LANE_UNHEALTHY_STATES = ("crashed", "dead", "failed", "quota_exhausted")

DEFAULT_THRESHOLDS = {
    "max_age_seconds": 300.0,
    "memory_warn_pct": 80.0,
    "memory_critical_pct": 90.0,
    "disk_warn_free_pct": 20.0,
    "disk_critical_free_pct": 10.0,
    "network_warn_latency_ms": 2000.0,
    "quota_window_warn_used": 0.8,
    "quota_window_critical_used": 0.95,
    "max_input_bytes": 4 * 1024 * 1024,
    "max_depth": 32,
    "max_records": 500,
}

THRESHOLD_RULES = {
    "max_age_seconds": (1.0, 86400.0),
    "memory_warn_pct": (0.0, 100.0),
    "memory_critical_pct": (0.0, 100.0),
    "disk_warn_free_pct": (0.0, 100.0),
    "disk_critical_free_pct": (0.0, 100.0),
    "network_warn_latency_ms": (0.0, 3600000.0),
    "quota_window_warn_used": (0.0, 1.0),
    "quota_window_critical_used": (0.0, 1.0),
    "max_input_bytes": (1024, 64 * 1024 * 1024),
    "max_depth": (2, 64),
    "max_records": (1, 10000),
}

#: Thresholds that must stay integers (byte and record limits).
INTEGER_THRESHOLDS = ("max_input_bytes", "max_depth", "max_records")


class GuardianError(Exception):
    """Operator-visible failure: bad request, bad config, unreadable state."""


class ConfigError(GuardianError):
    """Invalid threshold configuration."""


# ---------------------------------------------------------------- small helpers

def now_ts():
    """Current wall-clock time (seconds since the epoch)."""
    return time.time()


def iso(ts):
    """UTC ISO-8601 stamp, or None."""
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _short(text, limit=200):
    if text is None:
        return ""
    return str(text)[:limit]


def _num(value):
    """A finite float for a JSON number, else None. Booleans are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _worst(statuses):
    """Fold statuses with the severity order; UNKNOWN beats HEALTHY.

    DRAINED and NOT_METERED are neutral: they never mask a real status, and
    a set that only holds neutral items folds to UNKNOWN rather than to a
    healthy verdict nobody observed.
    """
    real = [status for status in statuses if status not in (DRAINED, NOT_METERED)]
    if not real:
        return UNKNOWN
    worst = None
    for status in real:
        if worst is None or _SEVERITY.get(status, 1) > _SEVERITY.get(worst, 1):
            worst = status
    return worst or UNKNOWN


def _confidence_name(rank):
    return _CONFIDENCE_ORDER[max(0, min(rank, len(_CONFIDENCE_ORDER) - 1))]


def _rate_limit_marker(text):
    """The provider rate-limit marker in *text*, or None."""
    if not text:
        return None
    low = str(text).lower()
    for marker in RATE_LIMIT_MARKERS:
        if marker in low:
            return marker
    return None


def _quota_marker(text):
    if not text:
        return None
    low = str(text).lower()
    for marker in QUOTA_MARKERS:
        if marker in low:
            return marker
    return None


def default_state_dir():
    """Where snapshots and the manual-drain record live by default."""
    for name in ("OPENSWARM_GUARDIAN_HOME", "XSWARM_GUARDIAN_HOME"):
        value = os.environ.get(name)
        if value:
            return value
    for name in ("OPENSWARM_HOME", "XSWARM_HOME"):
        value = os.environ.get(name)
        if value:
            return os.path.join(value, "guardian")
    return os.path.join(os.path.expanduser("~"), ".claude", "openswarm", "guardian")


# ---------------------------------------------------------------- thresholds

def load_thresholds(path=None, overrides=None):
    """Threshold set for one observation. Raises ConfigError on bad input."""
    values = dict(DEFAULT_THRESHOLDS)
    if path:
        raw = _read_config(path)
        section = raw.get("thresholds") if isinstance(raw, dict) else None
        if section is None:
            section = raw
        if not isinstance(section, dict):
            raise ConfigError("threshold config must be a JSON object")
        unknown = sorted(set(section) - set(DEFAULT_THRESHOLDS))
        if unknown:
            raise ConfigError("unknown threshold keys: %s" % ", ".join(unknown))
        values.update(section)
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key not in DEFAULT_THRESHOLDS:
            raise ConfigError("unknown threshold: %s" % key)
        values[key] = value
    for key, (low, high) in THRESHOLD_RULES.items():
        number = _num(values[key])
        if number is None:
            raise ConfigError("threshold %s must be a finite number" % key)
        if not low <= number <= high:
            raise ConfigError("threshold %s must be between %g and %g" % (key, low, high))
        values[key] = number
    for key in INTEGER_THRESHOLDS:
        values[key] = int(values[key])
    if values["memory_critical_pct"] < values["memory_warn_pct"]:
        raise ConfigError("memory_critical_pct must not be below memory_warn_pct")
    if values["disk_critical_free_pct"] > values["disk_warn_free_pct"]:
        raise ConfigError("disk_critical_free_pct must not exceed disk_warn_free_pct")
    if values["quota_window_critical_used"] < values["quota_window_warn_used"]:
        raise ConfigError("quota_window_critical_used must not be below quota_window_warn_used")
    return values


def _read_config(path):
    try:
        with open(path, encoding="utf-8") as stream:
            text = stream.read(1024 * 1024)
    except OSError as error:
        raise ConfigError("cannot read threshold config %s: %s" % (path, error.strerror or error))
    try:
        return json.loads(text)
    except ValueError as error:
        raise ConfigError("threshold config %s is not valid JSON: %s" % (path, error))


# ---------------------------------------------------------------- input reading

class _InputError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def _reject_constant(name):
    raise ValueError("non-finite number %s is not accepted" % name)


def _within_depth(value, limit):
    """True when the parsed document nests no deeper than *limit* levels."""
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            return False
        if isinstance(node, dict):
            for item in node.values():
                stack.append((item, depth + 1))
        elif isinstance(node, list):
            for item in node:
                stack.append((item, depth + 1))
    return True


def _read_text(source, limit):
    """(text, file mtime or None) for a path, or (text, None) for stdin."""
    if source == "-":
        stream = getattr(sys.stdin, "buffer", None)
        if stream is None:
            text = sys.stdin.read(limit + 1)
        else:
            text = stream.read(limit + 1).decode("utf-8", "replace")
        if len(text) > limit:
            raise _InputError("too_large", "input exceeds %d bytes" % limit)
        return text, None
    if not os.path.isfile(source):
        raise _InputError("missing", "input file does not exist")
    try:
        size = os.path.getsize(source)
    except OSError as error:
        raise _InputError("unreadable", "cannot stat input: %s" % (error.strerror or error))
    if size > limit:
        raise _InputError("too_large", "input file is %d bytes (limit %d)" % (size, limit))
    try:
        with open(source, encoding="utf-8") as stream:
            text = stream.read(limit + 1)
    except OSError as error:
        raise _InputError("unreadable", "cannot read input: %s" % (error.strerror or error))
    except UnicodeDecodeError:
        raise _InputError("invalid", "input is not valid UTF-8")
    if len(text) > limit:
        raise _InputError("too_large", "input exceeds %d bytes" % limit)
    try:
        mtime = os.path.getmtime(source)
    except OSError:
        mtime = None
    return text, mtime


def _to_epoch(value):
    """Epoch seconds for a JSON timestamp (number or ISO string), else None."""
    number = _num(value)
    if number is not None:
        if number > 1e11:  # milliseconds
            number = number / 1000.0
        return number
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


_TIMESTAMP_KEYS = ("observed_at", "generated_at", "ts", "timestamp", "as_of")


def _payload_timestamp(payload):
    """The observation timestamp a payload carries, or None."""
    if not isinstance(payload, dict):
        return None
    for key in _TIMESTAMP_KEYS:
        if key in payload:
            stamp = _to_epoch(payload[key])
            if stamp is not None:
                return stamp
    return None


def read_input(kind, source, thresholds, now=None, observed_at=None):
    """Describe one input: what was read, how old it is and how much we trust it.

    *source* is a path ("-" for stdin), an already-parsed payload, or None.
    The returned record never contains the payload of an unusable input.
    """
    now = now if now is not None else now_ts()
    record = {"kind": kind, "path": None, "status": "not_provided", "detail": "",
              "payload": None, "observed_at": None, "timestamp_source": None,
              "age_seconds": None, "freshness": UNKNOWN, "confidence": "none"}
    if source is None:
        record["detail"] = "no input provided"
        return record
    if isinstance(source, (dict, list)):
        record["payload"] = source
        record["path"] = "<in-memory>"
        record["status"] = "ok"
    else:
        source = str(source)
        record["path"] = source
        try:
            text, mtime = _read_text(source, thresholds["max_input_bytes"])
        except _InputError as error:
            record["status"] = error.status
            record["detail"] = error.message
            return record
        try:
            payload = json.loads(text, parse_constant=_reject_constant)
        except ValueError as error:
            record["status"] = "invalid"
            record["detail"] = "input is not valid JSON: %s" % _short(error, 120)
            return record
        if not _within_depth(payload, thresholds["max_depth"]):
            record["status"] = "invalid"
            record["detail"] = "input nests deeper than %d levels" % thresholds["max_depth"]
            return record
        record["payload"] = payload
        record["status"] = "ok"
        if mtime is not None:
            record["_mtime"] = mtime
    stamp = _payload_timestamp(record["payload"])
    mtime = record.pop("_mtime", None)
    if stamp is not None:
        record["observed_at"] = stamp
        record["timestamp_source"] = "payload"
    elif observed_at is not None:
        record["observed_at"] = float(observed_at)
        record["timestamp_source"] = "caller"
    elif mtime is not None:
        record["observed_at"] = mtime
        record["timestamp_source"] = "file_mtime"
    else:
        record["observed_at"] = now
        record["timestamp_source"] = "ingest"
    age = now - record["observed_at"]
    if age < 0:
        age = 0.0
    record["age_seconds"] = round(age, 3)
    if record["timestamp_source"] == "ingest":
        record["freshness"] = UNKNOWN
        record["detail"] = "input carries no timestamp; its age cannot be bounded"
    elif age > thresholds["max_age_seconds"]:
        record["freshness"] = "stale"
        record["detail"] = "observation is %.0fs old (max age %.0fs)" % (age, thresholds["max_age_seconds"])
    else:
        record["freshness"] = "fresh"
    record["confidence"] = _input_confidence(record)
    return record


def _input_confidence(record):
    if record["status"] != "ok":
        return "none"
    if record["freshness"] == "stale":
        return "low"
    if record["freshness"] != "fresh":
        return "low"
    if record["timestamp_source"] in ("payload", "caller"):
        return "high"
    if record["timestamp_source"] == "file_mtime":
        return "medium"
    return "low"


def _observation_view(record):
    """The part of an input record that is safe to publish in a snapshot."""
    return {"kind": record["kind"], "path": record["path"], "status": record["status"],
            "freshness": record["freshness"], "confidence": record["confidence"],
            "observed_at": record["observed_at"], "observed_at_iso": iso(record["observed_at"]),
            "timestamp_source": record["timestamp_source"], "age_seconds": record["age_seconds"],
            "detail": record["detail"]}


# ---------------------------------------------------------------- dimensions

def _shape_ok(kind, payload):
    """True when the payload has the shape this input slot expects."""
    if kind == "snapshot":
        return isinstance(payload, dict) and any(
            isinstance(payload.get(key), (list, dict)) for key in ("lanes", "providers", "active_runs"))
    if kind == "health":
        return isinstance(payload, dict) and any(
            isinstance(payload.get(key), (list, dict)) for key in ("runs", "hosts", "summary"))
    if kind == "quota":
        if isinstance(payload, list):
            return True
        return isinstance(payload, dict) and isinstance(payload.get("backends"), list)
    if kind == "probe":
        return isinstance(payload, dict) and (
            "generated_at" in payload
            or any(key in payload for key in ("claude", "codex", "ocgo", "deepseek", "cursor", "agy", "pi")))
    if kind == "resources":
        if isinstance(payload, dict) and isinstance(payload.get("hosts"), (list, dict)):
            return True
        return isinstance(payload, dict) and any(key in payload for key in ("memory", "disk", "network"))
    return False


def _unknown_dimension(kind, record, detail):
    """A dimension for an input we could not use: status UNKNOWN, no items."""
    return {"status": UNKNOWN, "detail": detail, "items": [], "observation": _observation_view(record)}


def _lane_items(rows, thresholds):
    """One item per lane: state, provider and the lane's own error."""
    items = []
    for row in list(rows)[:thresholds["max_records"]]:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or "").strip().lower()
        error = row.get("last_error")
        error_text = _short(error, 200)
        detail = ""
        if not state:
            status = UNKNOWN
            detail = "lane state was not reported"
        elif state in LANE_UNHEALTHY_STATES:
            status = UNHEALTHY
            detail = "lane state is %s" % state
        elif state in LANE_DEGRADED_STATES:
            status = DEGRADED
            detail = "lane state is %s" % state
        elif state in LANE_HEALTHY_STATES:
            status = HEALTHY
            detail = "lane state is %s" % state
        else:
            status = UNKNOWN
            detail = "lane state %r is not a known state" % state
        signals = []
        if state in ("crashed", "dead", "failed"):
            signals.append("lane_crashed")
        elif state == "quota_exhausted":
            signals.append("quota_exhausted")
        elif state == "down":
            signals.append("lane_down")
        elif state in ("starting", "stopping", "draining", "restarting"):
            signals.append("lane_transitional")
        if error_text:
            signals.append("lane_error")
            if status in (HEALTHY, UNKNOWN):
                status = DEGRADED
            detail = (detail + "; " if detail else "") + "lane reported an error: %s" % error_text
        marker = _rate_limit_marker(error_text)
        if marker:
            signals.append("rate_limited")
            status = UNHEALTHY
            detail = "provider rate-limit signal (%s): %s" % (marker, error_text)
        quota_marker = _quota_marker(error_text)
        if quota_marker:
            signals.append("quota_exhausted")
            status = UNHEALTHY
            detail = "provider quota signal (%s): %s" % (quota_marker, error_text)
        items.append({"name": _short(row.get("name") or "", 64), "status": status, "detail": detail,
                      "state": state, "backend": _short(row.get("backend") or "", 64),
                      "provider": _short(row.get("provider") or "", 64), "signals": signals,
                      "evidence": {"last_error": error_text or None,
                                   "quota_reset_at": row.get("quota_reset_at"),
                                   "current_run": _short(row.get("current_run") or "", 64) or None}})
    return items


def _provider_items(payload, lane_rows, thresholds):
    """Providers we actually observed, with any provider-side failure signal."""
    providers = {}
    unobserved = []

    def entry(name):
        if name not in providers:
            providers[name] = {"name": name, "status": HEALTHY, "verified": False,
                               "detail": "no provider failure signal in the ingested inputs",
                               "signals": [], "evidence": [], "backends": []}
        return providers[name]

    mapping = payload.get("providers") if isinstance(payload, dict) else None
    backend_provider = {}
    if isinstance(mapping, dict):
        for backend, value in mapping.items():
            name = value if isinstance(value, str) else None
            if isinstance(value, dict):
                name = value.get("provider") or value.get("name")
            if not isinstance(name, str) or not name:
                continue
            backend_provider[str(backend)] = name
    for row in list(lane_rows)[:thresholds["max_records"]]:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider")
        backend = row.get("backend")
        if isinstance(provider, str) and provider:
            name = provider
            if isinstance(backend, str) and backend:
                backend_provider.setdefault(backend, name)
            item = entry(name)
            if isinstance(backend, str) and backend and backend not in item["backends"]:
                item["backends"].append(backend)
            state = str(row.get("state") or "").strip().lower()
            error_text = _short(row.get("last_error"), 200)
            marker = _rate_limit_marker(error_text)
            quota_marker = _quota_marker(error_text)
            if marker:
                item["status"] = UNHEALTHY
                item["signals"].append("rate_limited")
                item["evidence"].append("lane %s reported %s" % (_short(row.get("name") or "?", 40), error_text))
                item["detail"] = "provider failure signal: %s" % error_text
            elif quota_marker or state == "quota_exhausted":
                item["status"] = UNHEALTHY
                item["signals"].append("quota_exhausted")
                item["evidence"].append("lane %s is out of quota" % _short(row.get("name") or "?", 40))
                item["detail"] = "provider quota exhausted"
    for backend, name in backend_provider.items():
        if name in providers:
            continue
        unobserved.append(name)
    return [providers[name] for name in sorted(providers)], sorted(set(unobserved))


def _host_items(rows, thresholds):
    items = []
    for row in list(rows)[:thresholds["max_records"]]:
        if not isinstance(row, dict):
            continue
        name = _short(row.get("name") or "", 64)
        status_text = str(row.get("status") or "").strip().lower()
        error = _short(row.get("error"), 200)
        reachable = row.get("reachable")
        paused = bool(row.get("paused"))
        detail = ""
        signals = []
        if status_text == "unreachable" or reachable is False:
            status = UNHEALTHY
            detail = "host is unreachable"
            signals.append("host_unreachable")
        elif status_text == "config invalid":
            status = UNHEALTHY
            detail = "host reports an invalid lane config"
            signals.append("lane_config_invalid")
        elif paused or status_text == "paused":
            status = DEGRADED
            detail = "host is paused"
        elif status_text == "reachable" or reachable is True:
            status = HEALTHY
            detail = "host is reachable"
        else:
            status = UNKNOWN
            detail = "host reachability was not reported"
        if error:
            signals.append("host_error")
            if status in (HEALTHY, UNKNOWN):
                status = UNHEALTHY if not reachable else DEGRADED
            detail = (detail + "; " if detail else "") + error
        items.append({"name": name, "status": status, "detail": detail, "signals": signals,
                      "evidence": {"reachable": reachable, "paused": paused,
                                   "reported_status": status_text or None, "error": error or None}})
    return items


def _run_items(rows, thresholds):
    items = []
    for row in list(rows)[:thresholds["max_records"]]:
        if not isinstance(row, dict):
            continue
        verdict = str(row.get("verdict") or "").strip().lower()
        reason = _short(row.get("reason"), 200)
        if verdict in ("alive", "done"):
            status = HEALTHY
        elif verdict == "zombie":
            status = DEGRADED
        elif verdict == "dead":
            status = UNHEALTHY
        elif verdict == "unknown":
            status = UNKNOWN
        else:
            status = UNKNOWN
        signals = []
        marker = _rate_limit_marker(reason)
        if marker:
            signals.append("rate_limited")
            if status == HEALTHY:
                status = DEGRADED
            reason = (reason + " " if reason else "") + "[%s]" % marker
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        items.append({"name": _short(row.get("id") or "", 64), "status": status,
                      "detail": reason or ("verdict %s" % verdict if verdict else "no verdict reported"),
                      "verdict": verdict or None, "backend": _short(row.get("backend") or "", 64),
                      "signals": signals,
                      "evidence": {"runner_pid": evidence.get("runner_pid"),
                                   "pid_identity": evidence.get("pid_identity"),
                                   "idle_minutes": evidence.get("idle_minutes"),
                                   "last_activity": evidence.get("last_activity"),
                                   "ledger_line": evidence.get("ledger_line")}})
    return items


def _quota_items(payload, thresholds):
    rows = payload.get("backends") if isinstance(payload, dict) else payload
    items = []
    for row in list(rows or [])[:thresholds["max_records"]]:
        if not isinstance(row, dict):
            continue
        backend = _short(row.get("backend") or "", 64)
        status = str(row.get("status") or "").strip().lower()
        remaining = _num(row.get("remaining"))
        detail = _short(row.get("detail"), 200)
        source = _short(row.get("source"), 120)
        signals = []
        if row.get("stale") is True:
            item_status = UNKNOWN
            item_detail = "quota reading is stale: %s" % (detail or "no detail")
            signals.append("stale")
        elif status == "ok":
            item_status = HEALTHY
            item_detail = detail or "remaining above threshold"
            if remaining is not None and remaining <= 0:
                item_status = UNHEALTHY
                item_detail = "quota exhausted (remaining %g)" % remaining
                signals.append("quota_exhausted")
        elif status == "low":
            item_status = DEGRADED
            item_detail = detail or "remaining at or below threshold"
            if remaining is not None and remaining <= 0:
                item_status = UNHEALTHY
                item_detail = "quota exhausted (remaining %g)" % remaining
                signals.append("quota_exhausted")
        elif status == "error":
            item_status = UNHEALTHY
            item_detail = detail or "quota source read failed"
            signals.append("quota_read_error")
        elif status == "unknown":
            if source and not source.lower().startswith("none"):
                item_status = UNKNOWN
                item_detail = detail or "quota source reports no usable reading"
            else:
                item_status = NOT_METERED
                item_detail = detail or "no quota source configured; this backend is not metered"
        else:
            item_status = UNKNOWN
            item_detail = "unrecognised quota status %r" % status
        marker = _rate_limit_marker(detail) or _rate_limit_marker(source)
        if marker:
            item_status = UNHEALTHY
            signals.append("rate_limited")
            item_detail = "provider rate-limit signal (%s): %s" % (marker, detail or source)
        items.append({"name": backend, "status": item_status, "detail": item_detail,
                      "signals": signals, "backend": backend,
                      "evidence": {"remaining": remaining, "reported_status": status or None,
                                   "source": source or None}})
    return items


def _probe_items(payload, thresholds):
    """Quota evidence carried by the probe cache. Backends without any quota
    evidence are reported as unobserved, never as healthy."""
    items = []
    unobserved = []
    claude = payload.get("claude")
    if isinstance(claude, dict):
        used = []
        for key in ("five_hour", "seven_day"):
            window = claude.get(key)
            if isinstance(window, dict):
                value = _num(window.get("used"))
                if value is not None:
                    used.append((key, value))
        evidence = {"provider": claude.get("provider"), "age_minutes": claude.get("age_minutes"),
                    "stale": claude.get("stale"), "windows": {key: value for key, value in used}}
        if claude.get("stale") is True:
            status = UNKNOWN
            detail = "claude quota reading is stale; it cannot be judged"
        elif used:
            peak = max(value for _, value in used)
            if peak >= thresholds["quota_window_critical_used"]:
                status = UNHEALTHY
            elif peak >= thresholds["quota_window_warn_used"]:
                status = DEGRADED
            else:
                status = HEALTHY
            detail = "claude rate-limit windows peak at %.0f%% used" % (peak * 100)
        else:
            status = UNKNOWN
            detail = "claude probe carries no rate-limit window"
        items.append({"name": "claude", "status": status, "detail": detail, "signals": [],
                      "evidence": evidence})
    codex = payload.get("codex")
    if isinstance(codex, dict):
        limits = codex.get("limits") if isinstance(codex.get("limits"), dict) else {}
        windows = []
        for entry in limits.values():
            if not isinstance(entry, dict):
                continue
            for window in (entry.get("windows") or {}).values():
                if isinstance(window, dict):
                    value = _num(window.get("used"))
                    if value is not None:
                        windows.append(value)
        snapshot = codex.get("limits_snapshot") if isinstance(codex.get("limits_snapshot"), dict) else {}
        stale = snapshot.get("stale") is True or codex.get("limits_source") == "snapshot-stale"
        evidence = {"limits_source": codex.get("limits_source"), "snapshot_stale": snapshot.get("stale"),
                    "snapshot_age_minutes": snapshot.get("age_minutes"), "windows": windows}
        if stale:
            status = UNKNOWN
            detail = "codex limits come from a stale snapshot; they cannot be judged"
        elif windows:
            peak = max(windows)
            if peak >= thresholds["quota_window_critical_used"]:
                status = UNHEALTHY
            elif peak >= thresholds["quota_window_warn_used"]:
                status = DEGRADED
            else:
                status = HEALTHY
            detail = "codex limit windows peak at %.0f%% used" % (peak * 100)
        else:
            status = UNKNOWN
            detail = "codex probe carries no usable limit window"
        items.append({"name": "codex", "status": status, "detail": detail, "signals": [],
                      "evidence": evidence})
    deepseek = payload.get("deepseek")
    if isinstance(deepseek, dict):
        error = _short(deepseek.get("error"), 200)
        balance = _num(deepseek.get("balance"))
        if error:
            items.append({"name": "deepseek", "status": DEGRADED,
                          "detail": "balance read failed: %s" % error, "signals": ["balance_error"],
                          "evidence": {"error": error}})
        elif balance is not None:
            items.append({"name": "deepseek",
                          "status": DEGRADED if balance <= 0 else HEALTHY,
                          "detail": "balance is %g" % balance, "signals": [],
                          "evidence": {"balance": balance}})
    for backend in ("ocgo", "cursor", "agy", "pi"):
        entry = payload.get(backend)
        if isinstance(entry, dict) and entry.get("error"):
            items.append({"name": backend, "status": DEGRADED,
                          "detail": "probe reported an error: %s" % _short(entry.get("error"), 160),
                          "signals": ["probe_error"], "evidence": {}})
        else:
            unobserved.append(backend)
    return items, unobserved


def _pct(value):
    number = _num(value)
    return None if number is None else number


def _resource_metric_status(metric, thresholds):
    """(status, detail, signals) for one resource metric."""
    if not isinstance(metric, dict):
        return None
    used = _pct(metric.get("used_pct"))
    if used is None:
        used = _pct(metric.get("used_percent"))
    if used is None:
        used = _pct(metric.get("percent"))
    free = _pct(metric.get("free_pct"))
    if free is None:
        free = _pct(metric.get("free_percent"))
    if free is None:
        free = _pct(metric.get("percent_free"))
    reachable = metric.get("ok")
    if reachable is None:
        reachable = metric.get("reachable")
    error = _short(metric.get("error"), 160)
    latency = _num(metric.get("latency_ms"))
    if used is not None:
        if used >= thresholds["memory_critical_pct"]:
            return (UNHEALTHY, "memory %.0f%% used (critical at %.0f%%)"
                    % (used, thresholds["memory_critical_pct"]), ["resource_pressure"])
        if used >= thresholds["memory_warn_pct"]:
            return (DEGRADED, "memory %.0f%% used (warning at %.0f%%)"
                    % (used, thresholds["memory_warn_pct"]), ["resource_pressure"])
        return (HEALTHY, "memory %.0f%% used" % used, [])
    if free is not None:
        if free <= thresholds["disk_critical_free_pct"]:
            return (UNHEALTHY, "disk %.0f%% free (critical at %.0f%%)"
                    % (free, thresholds["disk_critical_free_pct"]), ["resource_pressure"])
        if free <= thresholds["disk_warn_free_pct"]:
            return (DEGRADED, "disk %.0f%% free (warning at %.0f%%)"
                    % (free, thresholds["disk_warn_free_pct"]), ["resource_pressure"])
        return (HEALTHY, "disk %.0f%% free" % free, [])
    if reachable is False or error:
        return (UNHEALTHY, "network failure: %s" % (error or "reported unreachable"), ["network_failure"])
    if latency is not None and latency >= thresholds["network_warn_latency_ms"]:
        return (DEGRADED, "network latency %.0fms" % latency, ["network_slow"])
    if reachable is True:
        return (HEALTHY, "network reachable", [])
    return None


def _resource_items(payload, thresholds):
    """Resource metrics accepted as an existing structured input."""
    hosts = payload.get("hosts") if isinstance(payload, dict) else None
    rows = []
    if isinstance(hosts, list):
        rows = hosts
    elif isinstance(hosts, dict):
        for name, value in hosts.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("name", name)
                rows.append(row)
    elif isinstance(payload, dict) and any(key in payload for key in ("memory", "disk", "network")):
        row = dict(payload)
        row.setdefault("name", "default")
        rows.append(row)
    items = []
    for row in rows[:thresholds["max_records"]]:
        if not isinstance(row, dict):
            continue
        name = _short(row.get("name") or "", 64)
        metrics = {}
        statuses = []
        details = []
        signals = []
        for kind in ("memory", "disk", "network"):
            metric = row.get(kind)
            outcome = _resource_metric_status(metric, thresholds)
            if outcome is None:
                continue
            status, detail, kind_signals = outcome
            metrics[kind] = {"status": status, "detail": detail,
                              "values": {key: value for key, value in metric.items()
                                         if isinstance(value, (int, float, str, bool)) and not isinstance(value, bool)}}
            statuses.append(status)
            details.append("%s: %s" % (kind, detail))
            signals.extend(kind_signals)
        if not statuses:
            items.append({"name": name, "status": UNKNOWN,
                          "detail": "no memory, disk or network metric was reported for this host",
                          "signals": [], "metrics": {}, "unmeasured": ["memory", "disk", "network"]})
            continue
        unmeasured = [kind for kind in ("memory", "disk", "network") if kind not in metrics]
        items.append({"name": name, "status": _worst(statuses), "detail": "; ".join(details),
                      "signals": sorted(set(signals)), "metrics": metrics, "unmeasured": unmeasured})
    return items


def _dimension(items, detail, observation, extra=None):
    block = {"status": _worst([item["status"] for item in items]) if items else UNKNOWN,
             "detail": detail, "items": items, "observation": _observation_view(observation)}
    if extra:
        block.update(extra)
    return block


def _dimensions_for(observation, thresholds):
    """Turn one usable input into its dimension blocks."""
    kind = observation["kind"]
    if observation["status"] != "ok":
        return {kind: _unknown_dimension(kind, observation,
                                        observation["detail"] or "input is %s" % observation["status"])}
    payload = observation["payload"]
    if not _shape_ok(kind, payload):
        observation["status"] = "unsupported"
        observation["detail"] = "unsupported %s input shape" % kind
        observation["confidence"] = "none"
        return {"%s" % kind: _unknown_dimension(kind, observation, observation["detail"])}
    if kind == "snapshot":
        lanes_raw = payload.get("lanes") if isinstance(payload.get("lanes"), list) else []
        lane_items = _lane_items(lanes_raw, thresholds)
        provider_items, unobserved = _provider_items(payload, lanes_raw, thresholds)
        config_invalid = payload.get("lane_config_valid") is False
        lanes_detail = "no lane rows were reported"
        if lane_items:
            lanes_detail = "%d lane(s) reported" % len(lane_items)
        if config_invalid:
            lanes_detail += "; lane config is invalid: %s" % _short(payload.get("lane_config_error"), 160)
        lanes = {"status": _worst([item["status"] for item in lane_items]) if lane_items else UNKNOWN,
                 "detail": lanes_detail, "items": lane_items,
                 "observation": _observation_view(observation)}
        if config_invalid:
            lanes["status"] = UNHEALTHY
        providers = {"status": _worst([item["status"] for item in provider_items]) if provider_items else UNKNOWN,
                     "detail": "no provider was observed in this snapshot" if not provider_items
                     else "%d provider(s) observed" % len(provider_items),
                     "items": provider_items, "unobserved": unobserved,
                     "observation": _observation_view(observation)}
        return {"snapshot": {"status": _worst([lanes["status"], providers["status"]]),
                             "detail": "fleet snapshot: %s" % lanes_detail,
                             "observation": _observation_view(observation),
                             "lanes": lanes, "providers": providers}}
    if kind == "health":
        hosts_raw = payload.get("hosts") if isinstance(payload.get("hosts"), list) else []
        runs_raw = payload.get("runs") if isinstance(payload.get("runs"), list) else []
        lanes_raw = payload.get("lanes") if isinstance(payload.get("lanes"), list) else []
        blocks = {}
        if hosts_raw or "hosts" in payload:
            items = _host_items(hosts_raw, thresholds)
            blocks["hosts"] = _dimension(items,
                                         "%d host(s) reported" % len(items) if items
                                         else "no host rows were reported", observation)
        if runs_raw or "runs" in payload:
            items = _run_items(runs_raw, thresholds)
            blocks["runs"] = _dimension(items,
                                        "%d run(s) reported" % len(items) if items
                                        else "no run rows were reported", observation)
        if lanes_raw:
            items = _lane_items(lanes_raw, thresholds)
            blocks["lanes"] = _dimension(items,
                                         "%d lane(s) reported" % len(items) if items
                                         else "no lane rows were reported", observation)
        if not blocks:
            blocks["hosts"] = _unknown_dimension("hosts", observation, "health input carried no usable rows")
        status = _worst([block["status"] for block in blocks.values()])
        return {"health": {"status": status, "detail": "fleet health: %d block(s)" % len(blocks),
                           "observation": _observation_view(observation), **blocks}}
    if kind == "quota":
        items = _quota_items(payload, thresholds)
        metered = [item for item in items if item["status"] != NOT_METERED]
        unmetered = [item for item in items if item["status"] == NOT_METERED]
        block = _dimension(metered, "%d backend(s) reported" % len(metered) if metered
                           else "no metered backend reported a quota", observation)
        block["unmetered"] = unmetered
        if unmetered:
            block["detail"] += "; %d backend(s) are not metered" % len(unmetered)
        return {"quota": block}
    if kind == "probe":
        items, unobserved = _probe_items(payload, thresholds)
        block = _dimension(items, "%d backend(s) with quota evidence" % len(items) if items
                           else "the probe state carries no quota evidence", observation)
        block["unobserved"] = unobserved
        return {"probe": block}
    if kind == "resources":
        items = _resource_items(payload, thresholds)
        block = _dimension(items, "%d host(s) reported" % len(items) if items
                           else "no resource rows were reported", observation)
        return {"resources": block}
    return {}


# ---------------------------------------------------------------- snapshot assembly

def _collect_signals(dimensions):
    """Cross-cutting failure signals, so one list shows every fault kind."""
    signals = []
    for name, block in dimensions.items():
        for sub in ("lanes", "hosts", "runs", "providers"):
            inner = block.get(sub)
            if not isinstance(inner, dict):
                continue
            for item in inner.get("items") or []:
                for kind in item.get("signals") or []:
                    signals.append({"kind": kind, "dimension": name, "item": item.get("name"),
                                    "detail": item.get("detail")})
        for item in block.get("items") or []:
            for kind in item.get("signals") or []:
                signals.append({"kind": kind, "dimension": name, "item": item.get("name"),
                                "detail": item.get("detail")})
    return signals


def _attribute_provider_failures(dimensions):
    """A provider failure seen in the quota rows also marks that backend's
    provider, when the snapshot named one."""
    snapshot = dimensions.get("snapshot")
    quota = dimensions.get("quota")
    if not isinstance(snapshot, dict) or not isinstance(quota, dict):
        return
    providers = snapshot.get("providers")
    if not isinstance(providers, dict):
        return
    mapping = {}
    for item in providers.get("items") or []:
        for backend in item.get("backends") or []:
            mapping[backend] = item.get("name")
    if not mapping:
        return
    for item in quota.get("items") or []:
        kinds = item.get("signals") or []
        if not kinds or not ({"rate_limited", "quota_exhausted"} & set(kinds)):
            continue
        name = mapping.get(item.get("backend"))
        if not name:
            continue
        for provider in providers.get("items") or []:
            if provider.get("name") != name:
                continue
            provider["status"] = UNHEALTHY
            provider["signals"] = sorted(set(provider.get("signals") or []) | set(kinds))
            provider["detail"] = "provider failure seen in the quota rows: %s" % item.get("detail")
            provider.setdefault("evidence", []).append("quota row for %s" % item.get("backend"))


def _apply_scoped_drain(dimensions, drain_record):
    """Mark the hosts a manual drain covers as drained.

    A host-scoped drain is operator intent, not a fault: the host keeps its
    own observations but stops counting against the fleet verdict.
    """
    if not drain_record or not drain_record.get("active"):
        return
    if drain_record.get("scope") != "host":
        return
    target = drain_record.get("target")
    if not target:
        return
    health = dimensions.get("health")
    if not isinstance(health, dict):
        return
    hosts = health.get("hosts")
    if not isinstance(hosts, dict):
        return
    if (hosts.get("observation") or {}).get("freshness") != "fresh":
        # the evidence is stale or unbounded: the drain must not turn an
        # unknown into a drained signal
        return
    for item in hosts.get("items") or []:
        if item.get("name") != target:
            continue
        item["status"] = DRAINED
        item["detail"] = "host is manually drained (%s)" % (
            drain_record.get("reason") or "no reason recorded")
        item["signals"] = sorted(set(item.get("signals") or []) | {"manual_drain"})
    hosts["status"] = _worst([item["status"] for item in hosts.get("items") or []])
    health["status"] = _worst([health[block]["status"] for block in ("hosts", "runs", "lanes")
                                if isinstance(health.get(block), dict)])


def _mark_unconfirmed(blocks, observation):
    """A stale (or unbounded) input is reported as UNKNOWN.

    Its items stay in the snapshot as unconfirmed evidence, but they never
    count as a healthy signal and never escalate into a fault: acting on
    evidence nobody can date would be guessing.
    """
    note = "input is %s (%s)" % (observation["freshness"], observation["detail"] or "no detail")
    for block in blocks.values():
        block["status"] = UNKNOWN
        block["detail"] = (block.get("detail") + "; " if block.get("detail") else "") + note
        for sub in ("lanes", "hosts", "runs", "providers"):
            inner = block.get(sub)
            if isinstance(inner, dict):
                inner["status"] = UNKNOWN
                inner["detail"] = (inner.get("detail", "") + "; " if inner.get("detail") else "") + note


def build_snapshot(observations, thresholds, now=None, drain_record=None, required=()):
    """Assemble the versioned health snapshot from the input observations."""
    now = now if now is not None else now_ts()
    dimensions = {}
    for observation in observations:
        if observation["status"] == "not_provided":
            continue
        blocks = _dimensions_for(observation, thresholds)
        if observation["status"] == "ok" and observation["freshness"] != "fresh":
            _mark_unconfirmed(blocks, observation)
        dimensions.update(blocks)
    _apply_scoped_drain(dimensions, drain_record)
    _attribute_provider_failures(dimensions)
    signals = _collect_signals(dimensions)

    usable = {observation["kind"] for observation in observations if observation["status"] == "ok"}
    fresh = {observation["kind"] for observation in observations
             if observation["status"] == "ok" and observation["freshness"] == "fresh"}
    missing_required = []
    for name in required:
        if name == "fleet":
            if not fresh & {"snapshot", "health"}:
                missing_required.append("fleet")
        elif name not in fresh:
            missing_required.append(name)

    evaluated = [block["status"] for block in dimensions.values()]
    transient = _worst(evaluated) if evaluated else UNKNOWN
    reasons = []
    if not dimensions:
        reasons.append("no observation input was provided")
    else:
        for name, block in sorted(dimensions.items()):
            if block["status"] in (DEGRADED, UNHEALTHY, UNKNOWN):
                reasons.append("%s=%s" % (name, block["status"]))
    if missing_required:
        transient = UNKNOWN
        reasons.insert(0, "required input missing: %s" % ", ".join(sorted(missing_required)))
    if drain_record and drain_record.get("active") and drain_record.get("scope") == "fleet":
        overall = DRAINED
    else:
        overall = transient
    confidence = _overall_confidence(observations, dimensions)
    drain_view = _drain_view(drain_record)
    reason = "; ".join(reasons) if reasons else "every observed signal was healthy"
    if overall == DRAINED:
        reason = "manual drain is active (%s); transient status is %s" % (
            drain_view.get("reason") or "no reason recorded", transient)
    coverage = {
        "inputs": [_observation_view(observation) for observation in observations],
        "provided": sorted(usable),
        "not_provided": sorted(observation["kind"] for observation in observations
                               if observation["status"] == "not_provided"),
        "unusable": sorted(observation["kind"] for observation in observations
                           if observation["status"] not in ("ok", "not_provided")),
        "stale": sorted(observation["kind"] for observation in observations
                        if observation["status"] == "ok" and observation["freshness"] != "fresh"),
        "dimensions": sorted(dimensions),
        "missing_required": sorted(missing_required),
    }
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "observer": OBSERVER,
        "generated_at": now,
        "generated_at_iso": iso(now),
        "overall": {"status": overall, "transient_status": transient,
                    "reason": reason, "confidence": confidence},
        "coverage": coverage,
        "thresholds": {key: thresholds[key] for key in sorted(thresholds)},
        "dimensions": dimensions,
        "signals": signals,
        "manual_drain": drain_view,
    }


def _overall_confidence(observations, dimensions):
    ranks = []
    for observation in observations:
        if observation["status"] == "not_provided":
            continue
        rank = _CONFIDENCE_RANK.get(observation["confidence"], 0)
        if rank == 0:
            rank = 1  # an unusable input is low confidence, not no confidence
        ranks.append(rank)
    if not ranks:
        return "none"
    confidence = min(ranks)
    for block in dimensions.values():
        if block["status"] == UNKNOWN:
            confidence = min(confidence, _CONFIDENCE_RANK["medium"])
    if any(observation["status"] == "not_provided" for observation in observations):
        confidence = min(confidence, _CONFIDENCE_RANK["medium"])
    return _confidence_name(confidence)


def _drain_view(record):
    if not record:
        return {"active": False, "scope": None, "target": None, "reason": None,
                "since": None, "since_iso": None, "by": None}
    return {"active": bool(record.get("active")), "scope": record.get("scope"),
            "target": record.get("target"), "reason": record.get("reason"),
            "since": record.get("since"), "since_iso": record.get("since_iso"),
            "by": record.get("by")}


# ---------------------------------------------------------------- persistence

def _atomic_write_json(path, payload):
    """Write JSON so a reader never sees a half-written file."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = os.path.join(directory, ".%s.tmp-%d" % (os.path.basename(path), os.getpid()))
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    try:
        handle = os.open(directory or ".", os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)
    except OSError:
        pass


def save_snapshot(state_dir, snapshot):
    """Persist the snapshot atomically; returns the file path."""
    path = os.path.join(state_dir, SNAPSHOT_FILE)
    _atomic_write_json(path, snapshot)
    return path


def load_snapshot(state_dir):
    """Read the persisted snapshot back (restart-readable)."""
    path = os.path.join(state_dir, SNAPSHOT_FILE)
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
    except FileNotFoundError:
        raise GuardianError("no health snapshot in %s" % state_dir)
    except OSError as error:
        raise GuardianError("health snapshot in %s is unreadable: %s" % (state_dir, error.strerror or error))
    except ValueError as error:
        raise GuardianError("health snapshot in %s is corrupt: %s" % (state_dir, error))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise GuardianError("health snapshot in %s has an unsupported schema" % state_dir)
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise GuardianError("health snapshot schema version %r is not supported (expected %d)"
                            % (version, SCHEMA_VERSION))
    return payload


def load_drain(state_dir):
    """The manual-drain record, or None when no drain was ever recorded."""
    path = os.path.join(state_dir, DRAIN_FILE)
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise GuardianError("manual drain record in %s is unreadable: %s" % (state_dir, error.strerror or error))
    except ValueError as error:
        raise GuardianError("manual drain record in %s is corrupt: %s" % (state_dir, error))
    if not isinstance(payload, dict) or payload.get("schema") != DRAIN_SCHEMA:
        raise GuardianError("manual drain record in %s has an unsupported schema" % state_dir)
    if payload.get("schema_version") != DRAIN_SCHEMA_VERSION:
        raise GuardianError("manual drain record schema version %r is not supported"
                            % payload.get("schema_version"))
    return payload


def drain(state_dir, reason, by=None, host=None, now=None):
    """Record a durable manual drain. Only this call (and undrain) writes it."""
    reason = (reason or "").strip()
    if not reason:
        raise GuardianError("a manual drain needs a reason")
    if len(reason) > 500:
        raise GuardianError("manual drain reason is too long (max 500 characters)")
    now = now if now is not None else now_ts()
    record = {"schema": DRAIN_SCHEMA, "schema_version": DRAIN_SCHEMA_VERSION, "active": True,
              "scope": "host" if host else "fleet", "target": host or None,
              "reason": reason, "by": _short(by or "operator", 64),
              "since": now, "since_iso": iso(now)}
    _atomic_write_json(os.path.join(state_dir, DRAIN_FILE), record)
    return record


def undrain(state_dir, now=None):
    """Clear a manual drain. The record is kept for audit."""
    record = load_drain(state_dir)
    now = now if now is not None else now_ts()
    if record is None:
        return {"active": False, "cleared": False, "detail": "no manual drain was recorded"}
    cleared = dict(record, active=False, cleared_at=now, cleared_at_iso=iso(now))
    _atomic_write_json(os.path.join(state_dir, DRAIN_FILE), cleared)
    return cleared


# ---------------------------------------------------------------- the entry point

def observe(inputs=None, thresholds=None, state_dir=None, now=None, persist=True,
            require=(), require_fleet=True, observed_at=None, drain_record=None):
    """Ingest the given inputs and return one health snapshot.

    *inputs* maps an input slot ("snapshot", "health", "quota", "probe",
    "resources") to a path, "-" for stdin, or an already-parsed payload.
    Nothing is dispatched, admitted or recovered: the only side effect is the
    persisted snapshot (and it never touches the manual-drain record).
    """
    thresholds = thresholds or load_thresholds()
    now = now if now is not None else now_ts()
    state_dir = state_dir or default_state_dir()
    inputs = inputs or {}
    observations = []
    for kind in INPUT_KINDS:
        observations.append(read_input(kind, inputs.get(kind), thresholds, now=now,
                                       observed_at=observed_at))
    if drain_record is None and state_dir:
        drain_record = load_drain(state_dir)
    required = list(require)
    if require_fleet:
        required.append("fleet")
    snapshot = build_snapshot(observations, thresholds, now=now, drain_record=drain_record,
                              required=required)
    if persist:
        save_snapshot(state_dir, snapshot)
    return snapshot
