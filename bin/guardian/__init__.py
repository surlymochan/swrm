"""swrm guardian — observation-only fleet health (public issue #216, stage 1 of #215).

The package exposes the programmatic API and the command line used by
`bin/swrm-guardian`. Nothing in here dispatches, admits, recovers, drains a
lane or calls a language model: the guardian observes existing outputs and
persists one versioned health snapshot.

    from guardian import observe
    snapshot = observe({"snapshot": "/path/to/fleet-snapshot.json",
                        "quota": "/path/to/quota.json"})
"""
from .observation import (  # noqa: F401
    ConfigError,
    DRAINED,
    DEGRADED,
    DRAIN_FILE,
    DRAIN_SCHEMA,
    DRAIN_SCHEMA_VERSION,
    GuardianError,
    HEALTHY,
    INPUT_KINDS,
    LOCK_FILE,
    MAX_FUTURE_SKEW_SECONDS,
    OBSERVER,
    SCHEMA,
    SCHEMA_VERSION,
    SNAPSHOT_FILE,
    UNHEALTHY,
    UNKNOWN,
    build_snapshot,
    default_state_dir,
    drain,
    load_drain,
    load_snapshot,
    load_thresholds,
    observe,
    read_input,
    save_snapshot,
    undrain,
)

__all__ = [
    "ConfigError",
    "DRAINED",
    "DEGRADED",
    "DRAIN_FILE",
    "DRAIN_SCHEMA",
    "DRAIN_SCHEMA_VERSION",
    "GuardianError",
    "HEALTHY",
    "INPUT_KINDS",
    "LOCK_FILE",
    "MAX_FUTURE_SKEW_SECONDS",
    "OBSERVER",
    "SCHEMA",
    "SCHEMA_VERSION",
    "SNAPSHOT_FILE",
    "UNHEALTHY",
    "UNKNOWN",
    "build_snapshot",
    "default_state_dir",
    "drain",
    "load_drain",
    "load_snapshot",
    "load_thresholds",
    "observe",
    "read_input",
    "save_snapshot",
    "undrain",
]
