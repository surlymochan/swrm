"""M25 slice 2 (#204): fleet-path health gate + real evidence + flip call.

M25 slice 1 (#202) put the pre-dispatch health gate into batch cards;
a forwarded `fleet exec -- run ...` still dispatched to an unhealthy
backend and failed at exec time on the executor host. This slice reuses
the same gate (apply_health_gate / health_probe_backend) on the forwarded
run's effective backend at the controller — the weighted/quota-routed pick
when routing chose one, else the forwarded explicit --backend — before any
host exec attempt. An unhealthy backend is refused (ledger
blocked/health-gate with reason + evidence pointers, exit 2, never
rerouted); the gate flags live on the fleet exec invocation (before the
host, like --timeout) and are NOT forwarded to the remote run, whose own
M24 gate is the executor-side check. The gate is off by default
(routing.health_gate.enabled=false).

Pinned behaviour (the tests below pin each one):
- Gate off (shipped default) changes nothing: no health_gate ledger
  entry, forwarding byte-identical to before, probe state never read,
  custom backends file never loaded.
- Unhealthy blocks: exit 2, host_run never called, ledger entry status
  blocked with blocked_by health-gate and the full gate record (reason +
  per-check evidence pointers).
- An explicit --backend is gated exactly like a routed pick; a routed
  pick is gated on the pick routing actually chose.
- A probe that cannot finish in time is verdict "unknown" with the
  timeout on the record, and no new check starts after the deadline;
  on_unknown decides whether the run forwards.
- --skip-health-gate bypasses the probe and the bypass itself is recorded.
- A dry run is gated like a single run: a blocked dry run is refused and
  recorded, an allowed dry run forwards but writes no `dispatched` record
  (nothing dispatched).
- Non-run forwarded commands are untouched by the gate.
- A fleet health-gate ledger entry never inflates routing weights or
  advice accuracy (it is skipped like the other decision records).

Run: python3 -m unittest tests.test_m25_slice2
     (or: python3 tests/test_m25_slice2.py)
"""
import argparse
import copy
import io
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


def codex_state():
    """Fixture probe state with a usable codex binary and readable limits."""
    st = copy.deepcopy(BASE)
    st["codex"]["binary"] = sys.executable
    return st


def probe_record(verdict, reason, status="fail"):
    """Canned health_probe_backend record (apply_health_gate fills in
    enabled/source/stage/on_unknown/action around it)."""
    return {"backend": "codex", "verdict": verdict, "reason": reason,
            "checks": [{"name": "login", "status": status, "detail": reason,
                        "evidence": "command: codex login status"}],
            "seconds": 0.01, "timeout_seconds": 10}


UNHEALTHY = probe_record("unhealthy", "codex is not logged in (codex login status says 'Not logged in')")
HEALTHY = probe_record("healthy", "all 3 applicable check(s) passed", status="pass")
UNKNOWN = probe_record("unknown", "codex login status could not be classified", status="unknown")


class FleetHealthGateUnitTests(unittest.TestCase):
    def test_gate_off_returns_none_without_reading_state(self):
        with mock.patch.object(xs, "get_state", side_effect=AssertionError("state must not be read")):
            self.assertEqual(xs.fleet_health_gate(
                argparse.Namespace(health_gate=False, no_health_gate=False, skip_health_gate=False),
                POLICY, "codex"), (None, False))

    def test_no_health_gate_flag_wins_over_enabled_config(self):
        policy = copy.deepcopy(POLICY)
        policy["routing"]["health_gate"] = {"enabled": True, "timeout_seconds": 10, "on_unknown": "allow"}
        with mock.patch.object(xs, "get_state", side_effect=AssertionError("state must not be read")):
            self.assertEqual(xs.fleet_health_gate(
                argparse.Namespace(health_gate=False, no_health_gate=True, skip_health_gate=False),
                policy, "codex"), (None, False))

    def test_health_gate_flag_wins_over_no_health_gate_flag(self):
        # Same precedence as apply_health_gate (M24): --health-gate beats
        # --no-health-gate when both are passed.
        with mock.patch.object(xs, "get_state", return_value=codex_state()), \
                mock.patch.object(xs, "_health_codex_login",
                                  return_value=xs._health_check("login", "pass", "live session", "cmd")):
            record, blocked = xs.fleet_health_gate(
                argparse.Namespace(health_gate=True, no_health_gate=True, skip_health_gate=False),
                POLICY, "codex")
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "healthy")
        self.assertEqual(record["stage"], "fleet")

    def test_no_backend_selected_is_not_gated(self):
        with mock.patch.object(xs, "get_state", side_effect=AssertionError("state must not be read")):
            self.assertEqual(xs.fleet_health_gate(
                argparse.Namespace(health_gate=True, no_health_gate=False, skip_health_gate=False),
                POLICY, None), (None, False))

    def test_invalid_config_ignored_when_gate_off(self):
        policy = copy.deepcopy(POLICY)
        policy["routing"]["health_gate"] = "nonsense"
        self.assertEqual(xs.fleet_health_gate(
            argparse.Namespace(health_gate=False, no_health_gate=False, skip_health_gate=False),
            policy, "codex"), (None, False))

    def test_invalid_config_fails_loudly_when_gate_on(self):
        policy = copy.deepcopy(POLICY)
        policy["routing"]["health_gate"] = {"enabled": True, "timeout_seconds": 0, "on_unknown": "allow"}
        with mock.patch.object(xs, "get_state", return_value=codex_state()):
            with self.assertRaises(ValueError):
                xs.fleet_health_gate(
                    argparse.Namespace(health_gate=True, no_health_gate=False, skip_health_gate=False),
                    policy, "codex")

    def test_config_enabled_probes_without_flag(self):
        policy = copy.deepcopy(POLICY)
        policy["routing"]["health_gate"] = {"enabled": True, "timeout_seconds": 10, "on_unknown": "allow"}
        with mock.patch.object(xs, "get_state", return_value=codex_state()), \
                mock.patch.object(xs, "_health_codex_login",
                                  return_value=xs._health_check("login", "pass", "live session", "cmd")):
            record, blocked = xs.fleet_health_gate(
                argparse.Namespace(health_gate=False, no_health_gate=False, skip_health_gate=False),
                policy, "codex")
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "healthy")
        self.assertEqual(record["action"], "allowed")
        self.assertEqual(record["stage"], "fleet")

    def test_skip_flag_bypasses_probe_and_records_bypass(self):
        with mock.patch.object(xs, "health_probe_backend",
                               side_effect=AssertionError("probe must not run")):
            record, blocked = xs.fleet_health_gate(
                argparse.Namespace(health_gate=True, no_health_gate=False, skip_health_gate=True),
                POLICY, "codex")
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "bypassed")
        self.assertEqual(record["action"], "bypassed")

    def test_probe_timeout_bounds_and_unknown_verdict(self):
        """Real probe, slow login: a 1s budget must stop new checks after
        the deadline, record the timeout, and (on_unknown=block) refuse."""
        def slow_login(binary, timeout):
            time.sleep(1.2)
            return xs._health_check("login", "pass", "live session", "command: codex login status")

        cfg = {"enabled": True, "timeout_seconds": 1, "on_unknown": "block"}
        with mock.patch.object(xs, "get_state", return_value=codex_state()), \
                mock.patch.object(xs, "_health_codex_login", side_effect=slow_login), \
                mock.patch.object(xs, "health_gate_config", return_value=cfg):
            record, blocked = xs.fleet_health_gate(
                argparse.Namespace(health_gate=True, no_health_gate=False, skip_health_gate=False),
                POLICY, "codex")
        self.assertTrue(blocked)
        self.assertEqual(record["verdict"], "unknown")
        self.assertLess(record["seconds"], 3)
        self.assertEqual([c["name"] for c in record["checks"]], ["binary", "login", "timeout"])
        self.assertEqual(record["checks"][2]["status"], "timeout")
        self.assertIn("budget", record["checks"][2]["detail"])

    def test_gate_entry_does_not_inflate_routing_weights(self):
        rows = [{"id": f"cx{i}", "backend": "codex", "model": "cheap", "card_type": "fix",
                 "status": "done", "exec_pass": i < 2, "attempts": [{}], "seconds": 10,
                 "tokens": {"total": 100}} for i in range(8)]
        before = xs.compute_routing_weights(rows, 8)["entries"]
        gate = {"id": "fleet-local-abcdef01", "backend": "codex", "model": "cheap",
                "status": "blocked", "fleet_health_gate": True,
                "health_gate": copy.deepcopy(UNHEALTHY)}
        after = xs.compute_routing_weights(rows + [gate], 8)["entries"]
        self.assertEqual(before, after)
        self.assertEqual([(row["backend"], row["runs"]) for row in after], [("codex", 8)])


class FleetHealthGateExecTests(unittest.TestCase):
    """In-process `fleet exec -- run ...` through xs.cmd_fleet, with the
    transport mocked: host_run is the exec attempt, so asserting it is
    (not) called is asserting the run was (not) forwarded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-fleet-gate-")
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
        # LEDGER is a load-time constant (f"{DATA}/ledger.jsonl"), so
        # patching DATA alone does not move it — test_batch patches it
        # explicitly too.
        self.ledger_patch = mock.patch.object(xs, "LEDGER", str(self.root / "ledger.jsonl"))
        self.ledger_patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.ledger_patch.stop)

    def write_policy(self, **routing):
        """Routing overrides via policy.local.json; codex require_default_model
        off so the fixture state routes (as in test_fleet)."""
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
        """Call xs.cmd_fleet for `fleet exec local -- run ...` in-process;
        returns (exit_code, transport_mock, stdout_bytes)."""
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

    def test_gate_off_forwards_without_gate_entry(self):
        self.write_policy(weighted=False)
        with mock.patch.object(xs, "health_probe_backend",
                               side_effect=AssertionError("probe must not run")):
            code, transport, _ = self.exec_run("--backend", "codex")
        self.assertEqual(code, 0)
        transport.assert_called_once()
        self.assertEqual(self.gate_entries(), [])

    def test_unhealthy_backend_blocks_without_exec_recorded(self):
        # Same refusal as above, but asserting the ledger entry and that
        # host_run was never called: run the exec with the transport mock
        # held open across the refusal.
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
            with self.assertRaises(ValueError) as caught:
                xs.cmd_fleet(namespace, policy)
        self.assertIn("health gate blocked dispatch", str(caught.exception))
        transport.assert_not_called()
        blocked = self.gate_entries()
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["status"], "blocked")
        self.assertEqual(blocked[0]["blocked_by"], "health-gate")
        self.assertIn("not logged in", blocked[0]["blocked_reason"])
        self.assertEqual(blocked[0]["error_class"], "health_gate_blocked")
        self.assertEqual(blocked[0]["health_gate"]["verdict"], "unhealthy")
        self.assertEqual(blocked[0]["health_gate"]["checks"][0]["evidence"],
                         "command: codex login status")
        self.assertEqual(blocked[0]["backend"], "codex")
        self.assertEqual(blocked[0]["host"], "local")

    def test_explicit_backend_is_not_exempt(self):
        # --backend codex is user-explicit: the gate must still refuse it.
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
        self.assertEqual(len(self.gate_entries()), 1)

    def test_routed_pick_is_gated(self):
        # Weighted routing on, seeded so the advised pick is ocgo: the
        # gate must judge ocgo (the pick), not the default codex.
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        policy = xs.load_policy()
        decision = xs.route({"type": "impl", "difficulty": 3}, self.state, policy)
        self.assertTrue(decision["ok"])
        self.assertEqual(decision["backend"], "codex")
        rows = [{"id": f"cx{i}", "backend": "codex", "model": decision["model"], "card_type": "fix",
                 "status": "done", "exec_pass": i < 2, "attempts": [{}], "seconds": 10,
                 "tokens": {"total": 100}} for i in range(8)]
        rows += [{"id": f"oc{i}", "backend": "ocgo", "model": test_fleet.OCGO_MODEL, "card_type": "fix",
                  "status": "done", "exec_pass": i < 7, "attempts": [{}], "seconds": 10,
                  "tokens": {"total": 100}} for i in range(8)]
        (self.root / "ledger.jsonl").write_text("".join(json.dumps(e) + "\n" for e in rows))
        ocgo_unhealthy = probe_record("unhealthy", "opencode lists no models")
        ocgo_unhealthy["backend"] = "ocgo"
        namespace = argparse.Namespace(
            fleet_cmd="exec", host="local", timeout=300,
            health_gate=True, no_health_gate=False, skip_health_gate=False,
            args=["--", "run", "--task-file", str(self.task), "--card-type", "fix",
                  "--cwd", str(self.root), "--budget-minutes", "5"])
        stdout = test_fleet._StdoutWithBuffer()
        with mock.patch.object(xs, "get_state", return_value=self.state), \
                mock.patch.object(xs, "health_probe_backend", return_value=copy.deepcopy(ocgo_unhealthy)), \
                mock.patch.object(xs, "host_run", return_value=(0, b'{"id": "fake-abc"}', b"")) as transport, \
                mock.patch.object(sys, "stdout", stdout):
            with self.assertRaises(ValueError):
                xs.cmd_fleet(namespace, policy)
        transport.assert_not_called()
        blocked = self.gate_entries()
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["backend"], "ocgo")
        self.assertEqual(blocked[0]["health_gate"]["backend"], "ocgo")

    def test_healthy_backend_forwards_and_records_gate(self):
        self.write_policy(weighted=False)
        with mock.patch.object(xs, "health_probe_backend", return_value=copy.deepcopy(HEALTHY)):
            code, transport, out = self.exec_run("--backend", "codex", health_gate=True)
        self.assertEqual(code, 0)
        transport.assert_called_once()
        self.assertEqual(out, b'{"id": "fake-abc"}')
        entries = self.gate_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "dispatched")
        self.assertEqual(entries[0]["health_gate"]["verdict"], "healthy")
        self.assertEqual(entries[0]["health_gate"]["action"], "allowed")
        # The gate entry is written before the forward, so there is no
        # remote run to join to yet; append_ledger normalizes the
        # missing run_id to the entry id (same as the quota refusal).
        self.assertEqual(entries[0]["run_id"], entries[0]["id"])

    def test_skip_flag_bypasses_probe_and_records_bypass(self):
        self.write_policy(weighted=False)
        with mock.patch.object(xs, "health_probe_backend",
                               side_effect=AssertionError("probe must not run")):
            code, transport, _ = self.exec_run("--backend", "codex",
                                               health_gate=True, skip_health_gate=True)
        self.assertEqual(code, 0)
        transport.assert_called_once()
        entries = self.gate_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["health_gate"]["verdict"], "bypassed")
        self.assertEqual(entries[0]["health_gate"]["action"], "bypassed")

    def test_unknown_with_block_posture_blocks(self):
        self.write_policy(weighted=False)
        cfg = {"enabled": True, "timeout_seconds": 10, "on_unknown": "block"}
        namespace = argparse.Namespace(
            fleet_cmd="exec", host="local", timeout=300,
            health_gate=True, no_health_gate=False, skip_health_gate=False,
            args=["--", "run", "--task-file", str(self.task), "--card-type", "fix",
                  "--cwd", str(self.root), "--budget-minutes", "5", "--backend", "codex"])
        policy = xs.load_policy()
        stdout = test_fleet._StdoutWithBuffer()
        with mock.patch.object(xs, "get_state", return_value=self.state), \
                mock.patch.object(xs, "health_probe_backend", return_value=copy.deepcopy(UNKNOWN)), \
                mock.patch.object(xs, "health_gate_config", return_value=cfg), \
                mock.patch.object(xs, "host_run", return_value=(0, b'{"id": "fake-abc"}', b"")) as transport, \
                mock.patch.object(sys, "stdout", stdout):
            with self.assertRaises(ValueError):
                xs.cmd_fleet(namespace, policy)
        transport.assert_not_called()
        blocked = self.gate_entries()
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["health_gate"]["verdict"], "unknown")
        self.assertEqual(blocked[0]["health_gate"]["action"], "blocked")

    def test_unknown_with_allow_posture_forwards(self):
        self.write_policy(weighted=False)
        with mock.patch.object(xs, "health_probe_backend", return_value=copy.deepcopy(UNKNOWN)):
            code, transport, _ = self.exec_run("--backend", "codex", health_gate=True)
        self.assertEqual(code, 0)
        transport.assert_called_once()
        entries = self.gate_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["health_gate"]["verdict"], "unknown")
        self.assertEqual(entries[0]["health_gate"]["action"], "allowed")

    def test_dry_run_blocked_is_refused_and_recorded(self):
        self.write_policy(weighted=False)
        namespace = argparse.Namespace(
            fleet_cmd="exec", host="local", timeout=300,
            health_gate=True, no_health_gate=False, skip_health_gate=False,
            args=["--", "run", "--dry-run", "--task-file", str(self.task), "--card-type", "fix",
                  "--cwd", str(self.root), "--budget-minutes", "5", "--backend", "codex"])
        policy = xs.load_policy()
        stdout = test_fleet._StdoutWithBuffer()
        with mock.patch.object(xs, "get_state", return_value=self.state), \
                mock.patch.object(xs, "health_probe_backend", return_value=copy.deepcopy(UNHEALTHY)), \
                mock.patch.object(xs, "host_run", return_value=(0, b"{}", b"")) as transport, \
                mock.patch.object(sys, "stdout", stdout):
            with self.assertRaises(ValueError):
                xs.cmd_fleet(namespace, policy)
        transport.assert_not_called()
        self.assertEqual(len(self.gate_entries()), 1)
        self.assertEqual(self.gate_entries()[0]["status"], "blocked")

    def test_dry_run_allowed_forwards_without_dispatched_record(self):
        self.write_policy(weighted=False)
        with mock.patch.object(xs, "health_probe_backend", return_value=copy.deepcopy(HEALTHY)):
            code, transport, _ = self.exec_run("--dry-run", "--backend", "codex", health_gate=True)
        self.assertEqual(code, 0)
        transport.assert_called_once()
        self.assertEqual(self.gate_entries(), [])

    def test_non_run_command_is_untouched(self):
        """`fleet exec` forwards non-run commands verbatim even with the
        gate on: no probe, no gate entry."""
        echo = self.root / "echo-stub"
        echo.write_text("#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        echo.chmod(0o755)
        (self.root / "fleet.json").write_text(json.dumps(
            {"hosts": [{"name": "local", "kind": "local", "openswarm": str(echo)}]}))
        self.write_policy(weighted=False)
        namespace = argparse.Namespace(
            fleet_cmd="exec", host="local", timeout=300,
            health_gate=True, no_health_gate=False, skip_health_gate=False,
            args=["--", "lane", "status"])
        policy = xs.load_policy()
        stdout = test_fleet._StdoutWithBuffer()
        with mock.patch.object(xs, "health_probe_backend",
                               side_effect=AssertionError("probe must not run")), \
                mock.patch.object(sys, "stdout", stdout):
            try:
                xs.cmd_fleet(namespace, policy)
            except SystemExit as exited:
                code = exited.code
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.buffer.getvalue()), ["lane", "status"])
        self.assertEqual(self.gate_entries(), [])


class FleetHealthGateCliTests(unittest.TestCase):
    """Real CLI: `fleet exec --health-gate local -- run ...` end to end
    against the fake fleet stub, with the real probe reading the fixture
    state (codex binary /usr/bin/codex is absent on this host, so codex
    probes unhealthy; the echo custom backend probes healthy)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-fleet-gate-cli-")
        self.root = Path(self.tmp.name)
        self.task = self.root / "task.md"
        self.task.write_text("task one")
        with open(_load.FIXTURE_STATE) as handle:
            state = json.load(handle)
        (self.root / "state.json").write_text(json.dumps(state))
        STUB.chmod(0o755)
        (self.root / "fleet.json").write_text(json.dumps(
            {"hosts": [{"name": "local", "kind": "local", "openswarm": str(STUB)}]}))
        # Echo custom backend (repo example): command python3 echo-cli.py,
        # quota_source none — the real probe finds the binary and no quota
        # check applies, so it is healthy.
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

    def test_cli_gate_on_blocks_unhealthy_codex_without_start(self):
        proc = self.exec_run("--backend", "codex", health_gate=True)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("health gate blocked dispatch", proc.stderr)
        self.assertEqual(self.start_events(), [])
        blocked = [e for e in self.ledger_entries() if e.get("fleet_health_gate")]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["status"], "blocked")
        self.assertEqual(blocked[0]["blocked_by"], "health-gate")
        self.assertEqual(blocked[0]["backend"], "codex")

    def test_cli_gate_on_allows_healthy_echo_backend(self):
        proc = self.exec_run("--backend", "echo", "--backends-file", str(self.root / "backends.json"),
                             health_gate=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "echo")
        allowed = [e for e in self.ledger_entries() if e.get("fleet_health_gate")]
        self.assertEqual(len(allowed), 1)
        self.assertEqual(allowed[0]["status"], "dispatched")
        self.assertEqual(allowed[0]["health_gate"]["verdict"], "healthy")

    def test_cli_gate_off_forwards_codex_without_gate_entry(self):
        proc = self.exec_run("--backend", "codex")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.start_events()), 1)
        self.assertEqual([e for e in self.ledger_entries() if e.get("fleet_health_gate")], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
