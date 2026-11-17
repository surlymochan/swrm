"""M31 slice 1 (#254): executor-side health probe + per-host gate decision.

M24/M25 gated fleet and batch dispatches on the CONTROLLER's probe state:
a healthy remote host was mis-blocked when the controller lacked the
backend or the login, and an unhealthy remote host could be waved through
when the controller happened to be healthy. This slice moves the probe to
the executor host — `openswarm health-probe` runs where the work would
run (in-process for a local host, over the transport for a remote one) —
and the gate decides per host, with the verdict recorded in the ledger
(fleet_health_gate entry, host dimension: probe_host / probe_side).

Pinned behaviour (the tests below pin each one):
- `health-probe` is read-only: one backend, JSON or human output, exit 0
  whenever the probe ran (even for an unhealthy verdict); it writes no
  ledger entry and no state beyond get_state's own read path.
- A local host is probed in-process; a remote host is probed via
  `openswarm health-probe --backend … --json` over its transport.
- A probe that cannot be read back (transport timeout rc 124, non-zero
  remote exit, unparseable output, no usable verdict) is verdict
  "unknown" — never a silent allow — and on_unknown decides.
- Controller state no longer decides a remote host: a controller that
  probes unhealthy with a remote host that probes healthy allows the
  dispatch (and vice versa).
- An explicit --backend is gated exactly like a routed pick; the gate
  flags are not forwarded to the remote run.
- Gate off (shipped default, routing.health_gate.enabled=false) changes
  nothing: no probe, no transport call, no gate entry.
- --skip-health-gate bypasses the probe and the bypass itself is recorded.
- The single-run gate is untouched: `openswarm run` still probes this
  host in-process (it IS the executor for a single run).
- Ledger fleet_health_gate entries carry probe_host / probe_side; batch
  and fleet block entries carry the host.

Run: python3 -m unittest tests.test_m31_slice1
     (or: python3 tests/test_m31_slice1.py)
"""
import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402
import test_fleet  # noqa: E402  (fake fleet stub + _StdoutWithBuffer)

xs = _load.load()
xs.breakers = lambda: {}  # isolate from any breakers on disk

POLICY = xs.jload(f"{xs.SKILL_DIR}/policy.json", {})
with open(_load.FIXTURE_STATE) as f:
    BASE = json.load(f)

STUB = test_fleet.STUB

SSH_HOST = {"name": "gocc", "kind": "ssh", "ssh": "gocc",
            "openswarm": "/tmp/swrm-m31s1-openswarm"}
LOCAL_HOST = {"name": "local", "kind": "local"}


def codex_state():
    """Fixture probe state with a usable codex binary and readable limits."""
    st = copy.deepcopy(BASE)
    st["codex"]["binary"] = sys.executable
    return st


def probe_record(verdict, reason, status="fail"):
    """Canned health_probe_backend record (health_gate_decide fills in
    enabled/source/stage/on_unknown/action around it)."""
    return {"backend": "codex", "verdict": verdict, "reason": reason,
            "checks": [{"name": "login", "status": status, "detail": reason,
                        "evidence": "command: codex login status"}],
            "seconds": 0.01, "timeout_seconds": 10}


UNHEALTHY = probe_record("unhealthy", "codex is not logged in (codex login status says 'Not logged in')")
HEALTHY = probe_record("healthy", "all 3 applicable check(s) passed", status="pass")
UNKNOWN = probe_record("unknown", "codex login status could not be classified", status="unknown")


def gate_policy(on_unknown="allow"):
    policy = copy.deepcopy(POLICY)
    policy["routing"]["health_gate"] = {"enabled": True, "timeout_seconds": 10,
                                        "on_unknown": on_unknown}
    return policy


def gate_args(**overrides):
    args = {"health_gate": True, "no_health_gate": False, "skip_health_gate": False}
    args.update(overrides)
    return argparse.Namespace(**args)


class FleetProbeHostHealthTests(unittest.TestCase):
    def test_local_host_probes_in_process(self):
        with mock.patch.object(xs, "get_state", return_value=codex_state()), \
                mock.patch.object(xs, "_health_codex_login",
                                  return_value=xs._health_check("login", "pass", "live session", "cmd")), \
                mock.patch.object(xs, "host_run",
                                  side_effect=AssertionError("local probe must not use the transport")):
            record = xs.fleet_probe_host_health(LOCAL_HOST, "codex", POLICY, 10)
        self.assertEqual(record["verdict"], "healthy")
        self.assertEqual(record["probe_host"], "local")
        self.assertEqual(record["probe_side"], "executor")

    def test_remote_host_probes_over_transport(self):
        remote = copy.deepcopy(HEALTHY)
        with mock.patch.object(xs, "host_run",
                               return_value=(0, json.dumps(remote).encode(), b"")) as transport:
            record = xs.fleet_probe_host_health(SSH_HOST, "codex", POLICY, 10)
        self.assertEqual(record["verdict"], "healthy")
        self.assertEqual(record["probe_host"], "gocc")
        self.assertEqual(record["probe_side"], "executor")
        command = transport.call_args[0][1]
        self.assertIn("health-probe", command)
        self.assertIn("codex", command)

    def test_remote_unhealthy_verdict_passes_through(self):
        with mock.patch.object(xs, "host_run",
                               return_value=(0, json.dumps(UNHEALTHY).encode(), b"")):
            record = xs.fleet_probe_host_health(SSH_HOST, "codex", POLICY, 10)
        self.assertEqual(record["verdict"], "unhealthy")
        self.assertEqual(record["probe_host"], "gocc")
        self.assertEqual(record["probe_side"], "executor")

    def test_remote_timeout_is_unknown_not_allow(self):
        with mock.patch.object(xs, "host_run", return_value=(124, b"", b"transport timed out")):
            record = xs.fleet_probe_host_health(SSH_HOST, "codex", POLICY, 10)
        self.assertEqual(record["verdict"], "unknown")
        self.assertEqual(record["checks"][0]["name"], "transport")
        self.assertEqual(record["checks"][0]["status"], "timeout")
        self.assertIn("timed out", record["reason"])

    def test_remote_nonzero_exit_is_unknown(self):
        with mock.patch.object(xs, "host_run",
                               return_value=(1, b"", b"ssh: connect to host gocc port 22: timed out")):
            record = xs.fleet_probe_host_health(SSH_HOST, "codex", POLICY, 10)
        self.assertEqual(record["verdict"], "unknown")
        self.assertIn("rc=1", record["reason"])

    def test_remote_garbage_output_is_unknown(self):
        with mock.patch.object(xs, "host_run", return_value=(0, b"not json at all", b"")):
            record = xs.fleet_probe_host_health(SSH_HOST, "codex", POLICY, 10)
        self.assertEqual(record["verdict"], "unknown")
        self.assertIn("unparseable", record["reason"])

    def test_remote_record_without_verdict_is_unknown(self):
        with mock.patch.object(xs, "host_run", return_value=(0, b'{"backend": "codex"}', b"")):
            record = xs.fleet_probe_host_health(SSH_HOST, "codex", POLICY, 10)
        self.assertEqual(record["verdict"], "unknown")

    def test_remote_backends_file_is_forwarded(self):
        with mock.patch.object(xs, "host_run",
                               return_value=(0, json.dumps(HEALTHY).encode(), b"")) as transport:
            xs.fleet_probe_host_health(SSH_HOST, "echo", POLICY, 10,
                                       remote_backends_file="/tmp/remote-backends.json")
        command = transport.call_args[0][1]
        self.assertIn("--backends-file", command)
        self.assertIn("/tmp/remote-backends.json", command)


class FleetHealthGatePerHostTests(unittest.TestCase):
    def test_controller_unhealthy_remote_healthy_allows(self):
        """The core M31 fix: the controller's state must not mis-block a
        healthy remote host. Controller probes unhealthy (no codex
        binary); the remote probe says healthy; the gate allows."""
        policy = gate_policy()
        with mock.patch.object(xs, "get_state", return_value=copy.deepcopy(BASE)), \
                mock.patch.object(xs, "host_run",
                                  return_value=(0, json.dumps(HEALTHY).encode(), b"")):
            record, blocked = xs.fleet_health_gate(gate_args(), policy, "codex", host=SSH_HOST)
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "healthy")
        self.assertEqual(record["action"], "allowed")
        self.assertEqual(record["probe_host"], "gocc")
        self.assertEqual(record["probe_side"], "executor")

    def test_controller_healthy_remote_unhealthy_blocks(self):
        policy = gate_policy()
        with mock.patch.object(xs, "get_state", return_value=codex_state()), \
                mock.patch.object(xs, "_health_codex_login",
                                  return_value=xs._health_check("login", "pass", "live session", "cmd")), \
                mock.patch.object(xs, "host_run",
                                  return_value=(0, json.dumps(UNHEALTHY).encode(), b"")):
            record, blocked = xs.fleet_health_gate(gate_args(), policy, "codex", host=SSH_HOST)
        self.assertTrue(blocked)
        self.assertEqual(record["verdict"], "unhealthy")
        self.assertEqual(record["action"], "blocked")

    def test_remote_timeout_blocks_when_on_unknown_block(self):
        policy = gate_policy(on_unknown="block")
        with mock.patch.object(xs, "host_run", return_value=(124, b"", b"transport timed out")):
            record, blocked = xs.fleet_health_gate(gate_args(), policy, "codex", host=SSH_HOST)
        self.assertTrue(blocked)
        self.assertEqual(record["verdict"], "unknown")
        self.assertEqual(record["action"], "blocked")

    def test_remote_timeout_allows_when_on_unknown_allow(self):
        policy = gate_policy(on_unknown="allow")
        with mock.patch.object(xs, "host_run", return_value=(124, b"", b"transport timed out")):
            record, blocked = xs.fleet_health_gate(gate_args(), policy, "codex", host=SSH_HOST)
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "unknown")
        self.assertEqual(record["action"], "allowed")
        self.assertIn("on_unknown=allow", record["reason"])

    def test_gate_off_never_touches_transport(self):
        with mock.patch.object(xs, "host_run",
                               side_effect=AssertionError("gate off must not probe")):
            self.assertEqual(xs.fleet_health_gate(gate_args(health_gate=False), POLICY,
                                                  "codex", host=SSH_HOST), (None, False))

    def test_skip_bypasses_without_probing(self):
        policy = gate_policy()
        with mock.patch.object(xs, "host_run",
                               side_effect=AssertionError("bypass must not probe")):
            record, blocked = xs.fleet_health_gate(gate_args(skip_health_gate=True), policy,
                                                   "codex", host=SSH_HOST)
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "bypassed")
        self.assertEqual(record["action"], "bypassed")

    def test_no_host_falls_back_to_controller_probe(self):
        policy = gate_policy()
        with mock.patch.object(xs, "get_state", return_value=codex_state()), \
                mock.patch.object(xs, "_health_codex_login",
                                  return_value=xs._health_check("login", "pass", "live session", "cmd")), \
                mock.patch.object(xs, "host_run",
                                  side_effect=AssertionError("fallback must not use the transport")):
            record, blocked = xs.fleet_health_gate(gate_args(), policy, "codex")
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "healthy")
        self.assertNotIn("probe_host", record)


class BatchHealthGatePerHostTests(unittest.TestCase):
    def test_remote_healthy_allows_despite_controller(self):
        policy = gate_policy()
        spec = {"health_gate": True, "backends_file": None}
        with mock.patch.object(xs, "get_state", return_value=copy.deepcopy(BASE)), \
                mock.patch.object(xs, "host_run",
                                  return_value=(0, json.dumps(HEALTHY).encode(), b"")):
            record, blocked = xs.batch_health_gate(spec, policy, "codex", host=SSH_HOST)
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "healthy")
        self.assertEqual(record["probe_host"], "gocc")

    def test_remote_unhealthy_blocks_card(self):
        policy = gate_policy()
        spec = {"health_gate": True, "backends_file": None}
        with mock.patch.object(xs, "host_run",
                               return_value=(0, json.dumps(UNHEALTHY).encode(), b"")):
            record, blocked = xs.batch_health_gate(spec, policy, "codex", host=SSH_HOST)
        self.assertTrue(blocked)
        self.assertEqual(record["verdict"], "unhealthy")

    def test_no_host_falls_back_to_controller_probe(self):
        policy = gate_policy()
        spec = {"health_gate": True, "backends_file": None}
        with mock.patch.object(xs, "get_state", return_value=codex_state()), \
                mock.patch.object(xs, "_health_codex_login",
                                  return_value=xs._health_check("login", "pass", "live session", "cmd")), \
                mock.patch.object(xs, "host_run",
                                  side_effect=AssertionError("fallback must not use the transport")):
            record, blocked = xs.batch_health_gate(spec, policy, "codex")
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "healthy")


class FleetHealthGateExecPerHostTests(unittest.TestCase):
    """In-process `fleet exec -- run ...` through xs.cmd_fleet, with the
    transport mocked: host_run is the exec attempt, so asserting it is
    (not) called is asserting the run was (not) forwarded. Mirrors the
    M25 exec tests; the probe now runs per host."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m31-gate-")
        self.root = Path(self.tmp.name)
        self.task = self.root / "task.md"
        self.task.write_text("task one")
        with open(_load.FIXTURE_STATE) as handle:
            state = json.load(handle)
        now = time.time()
        state["codex"]["limits"] = {"codex": {"windows": {"seven_day": xs.window_stats(30, 10080, now + 100 * 3600)}}}
        state["claude"].update({"provider": "official", "metered_by_anthropic": True, "stale": False,
                                "five_hour": xs.window_stats(20, 300, now + 3 * 3600),
                                "seven_day": xs.window_stats(30, 10080, now + 100 * 3600)})
        state["deepseek"]["balance"] = 55.0
        for model in state["ocgo"]["models"]:
            model["usage"] = {"5h": 0.0, "7d": 0.0, "30d": 0.0}
        self.state = state
        (self.root / "state.json").write_text(json.dumps(state))
        STUB.chmod(0o755)
        (self.root / "fleet.json").write_text(json.dumps(
            {"hosts": [{"name": "local", "kind": "local", "openswarm": str(STUB)}]}))
        self.patch = mock.patch.object(xs, "DATA", str(self.root))
        self.patch.start()
        self.ledger_patch = mock.patch.object(xs, "LEDGER", str(self.root / "ledger.jsonl"))
        self.ledger_patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.ledger_patch.stop)

    def write_policy(self, **routing):
        local = {"codex": {"require_default_model": False}}
        if routing:
            local["routing"] = routing
        (self.root / "policy.local.json").write_text(json.dumps(local))

    def ledger_entries(self):
        path = self.root / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def gate_entries(self):
        return [e for e in self.ledger_entries() if e.get("fleet_health_gate")]

    def exec_run(self, *extra, health_gate=False, no_health_gate=False, skip_health_gate=False):
        namespace = argparse.Namespace(
            fleet_cmd="exec", host="local", timeout=300,
            health_gate=health_gate, no_health_gate=no_health_gate,
            skip_health_gate=skip_health_gate,
            args=["--", "run", "--task-file", str(self.task), "--card-type", "fix",
                  "--cwd", str(self.root), "--budget-minutes", "5", *extra])
        policy = xs.load_policy()
        stdout = test_fleet._StdoutWithBuffer()
        with mock.patch.object(xs, "get_state", return_value=self.state), \
                mock.patch.object(xs, "host_run", return_value=(0, b'{"id": "fake-abc"}', b"")) as transport, \
                mock.patch.object(sys, "stdout", stdout):
            try:
                xs.cmd_fleet(namespace, policy)
            except SystemExit as exited:
                return exited.code, transport, stdout.buffer.getvalue()
        return 0, transport, stdout.buffer.getvalue()

    def test_allowed_entry_carries_probe_host_dimension(self):
        self.write_policy(weighted=False)
        with mock.patch.object(xs, "health_probe_backend", return_value=copy.deepcopy(HEALTHY)):
            code, transport, _ = self.exec_run("--backend", "codex", health_gate=True)
        self.assertEqual(code, 0)
        transport.assert_called_once()
        entries = self.gate_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "dispatched")
        self.assertEqual(entries[0]["probe_host"], "local")
        self.assertEqual(entries[0]["probe_side"], "executor")
        self.assertEqual(entries[0]["health_gate"]["probe_host"], "local")
        self.assertEqual(entries[0]["health_gate"]["probe_side"], "executor")

    def test_blocked_entry_carries_probe_host_dimension(self):
        self.write_policy(weighted=False)
        namespace = argparse.Namespace(
            fleet_cmd="exec", host="local", timeout=300,
            health_gate=True, no_health_gate=False, skip_health_gate=False,
            args=["--", "run", "--task-file", str(self.task), "--card-type", "fix",
                  "--cwd", str(self.root), "--budget-minutes", "5", "--backend", "codex"])
        policy = xs.load_policy()
        stdout = test_fleet._StdoutWithBuffer()
        with mock.patch.object(xs, "get_state", return_value=self.state), \
                mock.patch.object(xs, "health_probe_backend", return_value=copy.deepcopy(UNHEALTHY)), \
                mock.patch.object(xs, "host_run", return_value=(0, b'{"id": "fake-abc"}', b"")) as transport, \
                mock.patch.object(sys, "stdout", stdout):
            with self.assertRaises(ValueError):
                xs.cmd_fleet(namespace, policy)
        transport.assert_not_called()
        entries = self.gate_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "blocked")
        self.assertEqual(entries[0]["probe_host"], "local")
        self.assertEqual(entries[0]["probe_side"], "executor")


class HealthProbeCliTests(unittest.TestCase):
    """Real CLI: `openswarm health-probe` end to end with the fixture
    state (codex binary /usr/bin/codex is absent on this host, so codex
    probes unhealthy; the echo custom backend probes healthy)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m31-probe-cli-")
        self.root = Path(self.tmp.name)
        with open(_load.FIXTURE_STATE) as handle:
            state = json.load(handle)
        (self.root / "state.json").write_text(json.dumps(state))
        echo_src = Path(_load.ROOT) / "examples" / "echo-cli.py"
        (self.root / "echo-cli.py").write_text(echo_src.read_text())
        (self.root / "echo-cli.py").chmod(0o755)
        (self.root / "backends.json").write_text(json.dumps({"backends": [
            {"command": ["python3", "echo-cli.py", "--prompt", "{prompt}",
                         "--workdir", "{worktree}", "--model", "{model}"],
             "env": {"ECHO_MODEL": "echo-1"}, "id": "echo", "model": "echo-1",
             "name": "Echo CLI", "quota_source": "none"}]}))
        (self.root / "policy.local.json").write_text(json.dumps(
            {"codex": {"require_default_model": False}}))
        self.addCleanup(self.tmp.cleanup)

    def env(self):
        return {**os.environ, "OPENSWARM_HOME": str(self.root),
                "OPENSWARM_STATE_FILE": str(self.root / "state.json")}

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, str(Path(_load.ROOT) / "bin" / "openswarm"), *argv],
                              env=self.env(), capture_output=True, text=True, timeout=120)

    def test_health_probe_echo_is_healthy_json(self):
        proc = self.run_cli("health-probe", "--backend", "echo",
                            "--backends-file", str(self.root / "backends.json"), "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        record = json.loads(proc.stdout)
        self.assertEqual(record["backend"], "echo")
        self.assertEqual(record["verdict"], "healthy")
        self.assertEqual(record["probe_side"], "executor")
        self.assertTrue(all(c["status"] == "pass" for c in record["checks"]))

    def test_health_probe_codex_is_unhealthy_json(self):
        proc = self.run_cli("health-probe", "--backend", "codex", "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        record = json.loads(proc.stdout)
        self.assertEqual(record["verdict"], "unhealthy")
        self.assertEqual(record["checks"][0]["name"], "binary")
        self.assertEqual(record["checks"][0]["status"], "fail")

    def test_health_probe_human_output_names_verdict(self):
        proc = self.run_cli("health-probe", "--backend", "codex")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("codex", proc.stdout)
        self.assertIn("unhealthy", proc.stdout)

    def test_health_probe_writes_no_ledger(self):
        self.run_cli("health-probe", "--backend", "codex", "--json")
        self.assertFalse((self.root / "ledger.jsonl").exists())


class FleetExecPerHostCliTests(unittest.TestCase):
    """Real CLI: `fleet exec --health-gate local -- run ...` end to end
    against the fake fleet stub, with the real probe reading the fixture
    state — now per host, executor-side."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m31-exec-cli-")
        self.root = Path(self.tmp.name)
        self.task = self.root / "task.md"
        self.task.write_text("task one")
        with open(_load.FIXTURE_STATE) as handle:
            state = json.load(handle)
        (self.root / "state.json").write_text(json.dumps(state))
        STUB.chmod(0o755)
        (self.root / "fleet.json").write_text(json.dumps(
            {"hosts": [{"name": "local", "kind": "local", "openswarm": str(STUB)}]}))
        echo_src = Path(_load.ROOT) / "examples" / "echo-cli.py"
        (self.root / "echo-cli.py").write_text(echo_src.read_text())
        (self.root / "echo-cli.py").chmod(0o755)
        (self.root / "backends.json").write_text(json.dumps({"backends": [
            {"command": ["python3", "echo-cli.py", "--prompt", "{prompt}",
                         "--workdir", "{worktree}", "--model", "{model}"],
             "env": {"ECHO_MODEL": "echo-1"}, "id": "echo", "model": "echo-1",
             "name": "Echo CLI", "quota_source": "none"}]}))
        (self.root / "policy.local.json").write_text(json.dumps(
            {"codex": {"require_default_model": False}}))
        self.addCleanup(self.tmp.cleanup)

    def env(self):
        return {**os.environ, "OPENSWARM_HOME": str(self.root),
                "OPENSWARM_STATE_FILE": str(self.root / "state.json")}

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, str(Path(_load.ROOT) / "bin" / "openswarm"), *argv],
                              env=self.env(), capture_output=True, text=True, timeout=120)

    def start_events(self):
        path = self.root / "calls.jsonl"
        if not path.exists():
            return []
        events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [event for event in events if event["event"] == "start"]

    def ledger_entries(self):
        path = self.root / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def exec_run(self, *extra, health_gate=False):
        command = ["run", "--task-file", str(self.task), "--card-type", "fix",
                   "--cwd", str(self.root), "--budget-minutes", "5", *extra]
        argv = ["fleet", "exec"]
        if health_gate:
            argv.append("--health-gate")
        argv += ["local", "--", *command]
        return self.run_cli(*argv)

    def test_cli_gate_on_allows_healthy_echo_with_probe_dimension(self):
        proc = self.exec_run("--backend", "echo", "--backends-file", str(self.root / "backends.json"),
                             health_gate=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.start_events()), 1)
        allowed = [e for e in self.ledger_entries() if e.get("fleet_health_gate")]
        self.assertEqual(len(allowed), 1)
        self.assertEqual(allowed[0]["status"], "dispatched")
        self.assertEqual(allowed[0]["health_gate"]["verdict"], "healthy")
        self.assertEqual(allowed[0]["probe_host"], "local")
        self.assertEqual(allowed[0]["probe_side"], "executor")

    def test_cli_gate_on_blocks_unhealthy_codex_without_start(self):
        proc = self.exec_run("--backend", "codex", health_gate=True)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("health gate blocked dispatch", proc.stderr)
        self.assertEqual(self.start_events(), [])
        blocked = [e for e in self.ledger_entries() if e.get("fleet_health_gate")]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["status"], "blocked")
        self.assertEqual(blocked[0]["probe_host"], "local")
        self.assertEqual(blocked[0]["probe_side"], "executor")


if __name__ == "__main__":
    unittest.main(verbosity=2)
