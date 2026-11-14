"""Frozen pi_usage from ba925861, for old-reader compatibility assertions."""
import json

PI_USAGE_KEYS = ("input", "output", "cacheRead", "cacheWrite")


def pi_usage(events_path):
    usage = {k: 0 for k in PI_USAGE_KEYS}
    final, errors = None, []
    try:
        fh = open(events_path, errors="ignore")
    except OSError:
        return usage, final, errors
    with fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type") != "message_end":
                continue
            m = ev.get("message") or {}
            if m.get("role") != "assistant":
                continue
            u = m.get("usage") or {}
            for k in PI_USAGE_KEYS:
                try:
                    usage[k] += int(u.get(k) or 0)
                except (TypeError, ValueError):
                    pass
            if m.get("stopReason") == "error" or m.get("errorMessage"):
                errors.append(str(m.get("errorMessage") or "error")[:500])
            text = "".join(b.get("text", "") for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                final = text
    return usage, final, errors
