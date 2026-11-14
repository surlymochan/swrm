"""Command line for the observation-only fleet health guardian (#216).

    swrm-guardian observe --snapshot fleet.json --quota quota.json
    swrm-guardian status
    swrm-guardian drain --reason "host under maintenance"
    swrm-guardian undrain

Every command is read-only with respect to the fleet: the guardian observes
existing outputs, persists one health snapshot and keeps a durable manual
drain. It never dispatches, admits, recovers or restarts anything.
"""
import argparse
import json
import sys

from .observation import (
    DEGRADED,
    DRAINED,
    GuardianError,
    INPUT_KINDS,
    UNHEALTHY,
    UNKNOWN,
    default_state_dir,
    drain,
    load_snapshot,
    load_thresholds,
    observe,
    undrain,
)

FORMATS = ("json", "text")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="swrm-guardian",
        description="observe-only fleet health guardian: normalise existing outputs "
                    "into one versioned health snapshot (no dispatch, no admission).")
    sub = parser.add_subparsers(dest="command", required=True)

    watcher = sub.add_parser("observe", help="ingest existing outputs and persist one health snapshot")
    watcher.add_argument("--snapshot", help="path to an `openswarm fleet _snapshot` JSON ('-' for stdin)")
    watcher.add_argument("--health", help="path to an `openswarm fleet health --json` JSON ('-' for stdin)")
    watcher.add_argument("--quota", help="path to an `openswarm quota --json` JSON ('-' for stdin)")
    watcher.add_argument("--probe", help="path to the probe cache JSON written by `openswarm probe`")
    watcher.add_argument("--resources", help="path to an existing structured resource-metrics JSON")
    watcher.add_argument("--state-dir", help="where the snapshot and drain record live")
    watcher.add_argument("--config", help="threshold config JSON")
    watcher.add_argument("--max-age", type=float, help="freshness window in seconds (default 300)")
    watcher.add_argument("--observed-at", type=float,
                         help="epoch seconds to assume for inputs that carry no timestamp of their own")
    watcher.add_argument("--require", action="append", default=[],
                         help="extra input slots that must be usable, e.g. --require quota")
    watcher.add_argument("--no-require-fleet", action="store_true",
                         help="do not require a fleet snapshot or fleet health input")
    watcher.add_argument("--no-persist", action="store_true", help="build the snapshot without writing it")
    watcher.add_argument("--format", choices=FORMATS, default="json")
    watcher.add_argument("--fail-on", choices=("never", "unhealthy", "not-healthy"), default="never",
                         help="exit 2 when the verdict matches (default: never)")

    reader = sub.add_parser("status", help="print the persisted health snapshot")
    reader.add_argument("--state-dir", help="where the snapshot and drain record live")
    reader.add_argument("--format", choices=FORMATS, default="json")

    drainer = sub.add_parser("drain", help="record a durable manual drain")
    drainer.add_argument("--reason", required=True, help="why the fleet is held")
    drainer.add_argument("--by", help="who recorded the drain")
    drainer.add_argument("--host", help="drain one host instead of the whole fleet")
    drainer.add_argument("--state-dir", help="where the snapshot and drain record live")
    drainer.add_argument("--format", choices=FORMATS, default="json")

    clearer = sub.add_parser("undrain", help="clear the manual drain")
    clearer.add_argument("--state-dir", help="where the snapshot and drain record live")
    clearer.add_argument("--format", choices=FORMATS, default="json")
    return parser


def _state_dir(args):
    return getattr(args, "state_dir", None) or default_state_dir()


def _required(args):
    names = []
    for value in getattr(args, "require", None) or []:
        for name in str(value).split(","):
            name = name.strip()
            if name and name not in names:
                names.append(name)
    for name in names:
        if name not in INPUT_KINDS and name != "fleet":
            raise GuardianError("unknown required input: %s" % name)
    return names


def _inputs(args):
    values = {}
    for kind in INPUT_KINDS:
        value = getattr(args, kind, None)
        if value:
            values[kind] = value
    streams = [kind for kind, value in values.items() if value == "-"]
    if len(streams) > 1:
        raise GuardianError("only one input can be read from stdin (got %s)" % ", ".join(sorted(streams)))
    return values


def _render_text(snapshot):
    if "overall" not in snapshot:
        lines = ["%s: %s" % (key, value) for key, value in sorted(snapshot.items())
                 if not isinstance(value, (dict, list))]
        return "\n".join(lines)
    overall = snapshot.get("overall") or {}
    lines = ["guardian %s @ %s" % (snapshot.get("observer", "swrm-guardian"),
                                   snapshot.get("generated_at_iso") or "-"),
             "overall: %s (transient: %s) confidence: %s"
             % (overall.get("status"), overall.get("transient_status"), overall.get("confidence")),
             "  reason: %s" % (overall.get("reason") or "-")]
    lines.append("inputs:")
    for record in (snapshot.get("coverage") or {}).get("inputs") or []:
        lines.append("  %-10s %-9s %-8s %-7s age %s"
                     % (record.get("kind"), record.get("status"), record.get("freshness"),
                        record.get("confidence"),
                        "-" if record.get("age_seconds") is None else "%.0fs" % record["age_seconds"]))
    lines.append("dimensions:")
    for name, block in (snapshot.get("dimensions") or {}).items():
        lines.append("  %-10s %-9s %s" % (name, block.get("status"), block.get("detail") or ""))
        for sub in ("lanes", "hosts", "runs", "providers"):
            inner = block.get(sub)
            if not isinstance(inner, dict):
                continue
            for item in inner.get("items") or []:
                lines.append("    %-10s %-9s %s" % (item.get("name"), item.get("status"),
                                                   item.get("detail") or ""))
        for item in block.get("items") or []:
            lines.append("    %-10s %-9s %s" % (item.get("name"), item.get("status"),
                                               item.get("detail") or ""))
    drain = snapshot.get("manual_drain") or {}
    if drain.get("active"):
        lines.append("manual drain: active scope=%s target=%s since=%s reason=%s"
                     % (drain.get("scope"), drain.get("target") or "-",
                        drain.get("since_iso") or "-", drain.get("reason") or "-"))
    else:
        lines.append("manual drain: inactive")
    signals = snapshot.get("signals") or []
    if signals:
        lines.append("signals:")
        for signal in signals:
            lines.append("  %-18s %-10s %s" % (signal.get("kind"), signal.get("item") or "-",
                                               signal.get("detail") or ""))
    return "\n".join(lines)


def _emit(payload, fmt):
    if fmt == "text":
        print(_render_text(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _fail_code(snapshot, mode):
    if mode == "never":
        return 0
    status = ((snapshot.get("overall") or {}).get("status"))
    if mode == "unhealthy" and status == UNHEALTHY:
        return 2
    if mode == "not-healthy" and status in (DEGRADED, UNHEALTHY, UNKNOWN, DRAINED):
        return 2
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "observe":
            thresholds = load_thresholds(args.config, {"max_age_seconds": args.max_age})
            state_dir = _state_dir(args)
            snapshot = observe(_inputs(args), thresholds=thresholds, state_dir=state_dir,
                               persist=not args.no_persist,
                               require=_required(args),
                               require_fleet=not args.no_require_fleet,
                               observed_at=args.observed_at)
            _emit(snapshot, args.format)
            return _fail_code(snapshot, args.fail_on)
        if args.command == "status":
            snapshot = load_snapshot(_state_dir(args))
            _emit(snapshot, args.format)
            return 0
        if args.command == "drain":
            record = drain(_state_dir(args), args.reason, by=args.by, host=args.host)
            _emit(record, args.format)
            return 0
        if args.command == "undrain":
            record = undrain(_state_dir(args))
            _emit(record, args.format)
            return 0
    except (GuardianError, ValueError) as error:
        print("swrm-guardian: %s" % error, file=sys.stderr)
        return 1
    except OSError as error:
        print("swrm-guardian: %s" % (error.strerror or error), file=sys.stderr)
        return 1
    parser.error("unknown command")
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
