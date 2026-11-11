import argparse
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _load

xs = _load.load()


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-fleet-")
        self.root = pathlib.Path(self.tmp.name)
        self.patch = mock.patch.object(xs, "DATA", str(self.root))
        self.patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.patch.stop)

    def config(self, hosts):
        (self.root / "fleet.json").write_text(json.dumps({"hosts": hosts}))

    def test_status_does_not_contact_or_wake_paused_instabox(self):
        self.config([{"name": "local", "kind": "local"}, {"name": "box", "kind": "instabox", "box": "task-box"},
                     {"name": "ssh", "kind": "ssh", "ssh": "host-alias"}])
        def status(host, command, **kw):
            self.assertNotEqual(host["name"], "box")
            if host["name"] == "ssh":
                return 255, b"", b"unreachable"
            return 0, json.dumps({"lanes": [{"name": "L1", "state": "idle", "model": "m", "current_run": None}],
                                  "active_runs": [{"id": "r1", "state": "running"}]}).encode(), b""
        output = io.StringIO()
        with mock.patch.object(xs, "sh", return_value=(0, "task-box  Paused\n", "")) as ls, \
                mock.patch.object(xs, "host_run", side_effect=status) as transport, contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=True))
        rows = json.loads(output.getvalue())["hosts"]
        self.assertTrue(rows[0]["reachable"])
        self.assertEqual(rows[0]["lanes"][0]["model"], "m")
        self.assertTrue(rows[1]["paused"])
        self.assertFalse(rows[1]["reachable"])
        self.assertFalse(rows[2]["reachable"])
        self.assertEqual(transport.call_count, 2)
        ls.assert_called_once_with(["instabox", "ls"], timeout=20)

    def test_unknown_instabox_state_does_not_open_ssh(self):
        self.config([{"name": "box", "kind": "instabox", "box": "task-box"}])
        with mock.patch.object(xs, "sh", return_value=(1, "", "offline")), \
                mock.patch.object(xs, "host_run") as transport, contextlib.redirect_stdout(io.StringIO()):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=False))
        transport.assert_not_called()

    def test_text_status_uses_reachable_snapshot_without_lanes(self):
        self.config([{"name": "box", "kind": "instabox", "box": "task-box"}])
        output = io.StringIO()
        with mock.patch.object(xs, "sh", return_value=(0, "task-box  Running\n", "")), \
                mock.patch.object(xs, "host_run", return_value=(0, b'{"lanes": [], "active_runs": []}', b"")), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=False))
        self.assertEqual(output.getvalue(), "box  no lanes  lanes=0 active=0\n")

    def test_empty_lane_config_is_valid(self):
        config = self.root / "lanes.json"
        config.write_text('{"lanes": []}')
        with mock.patch.object(xs, "envvar", side_effect=lambda name, default=None:
                               str(config) if name == "LANES_FILE" else default):
            self.assertEqual(xs.lane_config(), [])

    def test_fleet_snapshot_preserves_invalid_lane_config_reason(self):
        config = self.root / "lanes.json"
        config.write_text('{"lanes": [')
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(xs, "envvar", side_effect=lambda name, default=None:
                                                  str(config) if name == "LANES_FILE" else default))
            stack.enter_context(mock.patch.object(xs, "execution_provider", return_value="provider"))
            stack.enter_context(mock.patch.object(xs, "load_policy", return_value={}))
            stack.enter_context(mock.patch.object(xs, "agy_status", return_value={}))
            snapshot = xs.fleet_snapshot()
        self.assertEqual(snapshot["lanes"], [])
        self.assertFalse(snapshot["lane_config_valid"])
        self.assertIn("invalid lane config", snapshot["lane_config_error"])
        self.assertIn("lanes.json", snapshot["lane_config_error"])

    def test_capacity_snapshot_skips_model_probe_but_keeps_capacity_fields(self):
        with mock.patch.dict(os.environ, {"OPENSWARM_FLEET_CAPACITY_ONLY": "1"}), \
                mock.patch.object(xs, "agy_status") as status:
            snapshot = xs.fleet_snapshot()
        status.assert_not_called()
        self.assertIn("active_runs", snapshot)
        self.assertIn("providers", snapshot)
        self.assertEqual(snapshot["backends"], {})

    def test_capacity_snapshot_skips_completed_run_logs(self):
        runs = self.root / "runs"
        for name, state in (("done", "finished"), ("active", "running")):
            directory = runs / name
            directory.mkdir(parents=True)
            (directory / "status.json").write_text(json.dumps({"state": state}))
        with mock.patch.dict(os.environ, {"OPENSWARM_FLEET_CAPACITY_ONLY": "1"}), \
                mock.patch.object(xs, "RUNS", str(runs)), \
                mock.patch.object(xs, "run_digest", side_effect=lambda rid: {"id": rid, "state": "running"}) as digest:
            snapshot = xs.fleet_snapshot()
        digest.assert_called_once_with("active")
        self.assertEqual([row["id"] for row in snapshot["active_runs"]], ["active"])

    def test_text_status_shows_config_invalid_with_reason(self):
        self.config([{"name": "host", "kind": "local"}])
        output = io.StringIO()
        snapshot = {"lanes": [], "active_runs": [], "lane_config_valid": False,
                    "lane_config_error": "invalid lane config /etc/openswarm/lanes.json: bad JSON"}
        with mock.patch.object(xs, "host_json", return_value=snapshot), contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=False))
        self.assertIn("host  config invalid  lanes=0 active=0", output.getvalue())
        self.assertIn("invalid lane config /etc/openswarm/lanes.json: bad JSON", output.getvalue())

    def test_transport_argv_binary_input_and_timeout(self):
        for host, prefix in [({"kind": "ssh", "ssh": "alias"}, ["ssh", "alias", "--"]),
                             ({"kind": "instabox", "box": "task-box"}, ["instabox", "ssh", "task-box", "--"])]:
            with mock.patch.object(xs.subprocess, "run", return_value=subprocess.CompletedProcess([], 3, b"out", b"err")) as run:
                self.assertEqual(xs.host_run(host, "command", timeout=7, input=b"\x00bundle"), (3, b"out", b"err"))
                self.assertEqual(run.call_args.args[0], prefix + ["command"])
                self.assertEqual(run.call_args.kwargs["input"], b"\x00bundle")
        self.assertEqual(xs.host_run({"kind": "local"}, "exec sleep 5", timeout=0.01)[0], 124)

    def test_exec_passes_arguments_and_exit_code_without_shell_expansion(self):
        binary = self.root / "stub"
        binary.write_text("#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(7)\n")
        binary.chmod(0o755)
        self.config([{"name": "local", "kind": "local", "openswarm": str(binary)}])
        proc = subprocess.run([sys.executable, str(pathlib.Path(_load.ROOT) / "bin" / "openswarm"),
                               "fleet", "exec", "local", "--", "run", "--prompt", "$(false); literal"],
                              env={**xs.os.environ, "OPENSWARM_HOME": str(self.root)}, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(json.loads(proc.stdout), ["run", "--prompt", "$(false); literal"])

    def test_invalid_config_rejected(self):
        self.config([{"name": "x", "kind": "ssh", "ssh": "-unsafe"}])
        with self.assertRaises(ValueError):
            xs.fleet_config()

    @unittest.skipIf(os.name == "nt", 'the test drives an extensionless `ssh` shim placed on PATH; Windows process creation only resolves PATHEXT executables, so the fake remote transport cannot be discovered (local-only ledger behaviour is covered by other fleet tests)')
    def test_ledger_pull_merges_redacts_deduplicates_and_skips_unreachable_hosts(self):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        ssh = fake_bin / "ssh"
        ssh.write_text(
            f"#!{sys.executable}\n"
            "import os, pathlib, sys\n"
            "host = sys.argv[1]\n"
            "if host == 'down':\n"
            "    print('unreachable', file=sys.stderr)\n"
            "    raise SystemExit(255)\n"
            "assert sys.argv[2] == '--' and sys.argv[3].startswith('cat ')\n"
            "path = pathlib.Path(os.environ['OPENSWARM_TEST_SSH_ROOT']) / (host + '.jsonl')\n"
            "sys.stdout.buffer.write(path.read_bytes())\n"
        )
        ssh.chmod(0o755)
        self.config([{"name": "local", "kind": "local"},
                     {"name": "remote", "kind": "ssh", "ssh": "good"},
                     {"name": "down", "kind": "ssh", "ssh": "down"}])
        local_ledger = self.root / "local.jsonl"
        ledger_patch = mock.patch.object(xs, "LEDGER", str(local_ledger))
        ledger_patch.start()
        self.addCleanup(ledger_patch.stop)
        local_ledger.write_text(json.dumps({"run_id": "shared", "host": "same-host", "title": "local"}) + "\n")
        (self.root / "good.jsonl").write_text("\n".join([
            json.dumps({"run_id": "shared", "host": "same-host", "title": "duplicate"}),
            json.dumps({"run_id": "remote-run", "title": "token sk-12345678901234567890"}),
            "",
        ]))
        destination = self.root / "ledger-fleet.jsonl"
        original_argv = sys.argv
        output, errors = io.StringIO(), io.StringIO()
        ledger_output = io.StringIO()
        try:
            sys.argv = ["openswarm", "fleet", "ledger", "pull"]
            with mock.patch.dict(xs.os.environ, {
                "PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", ""),
                "OPENSWARM_TEST_SSH_ROOT": str(self.root),
            }), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                xs.main()
            rows = [json.loads(line) for line in destination.read_text().splitlines()]
            first_pull = destination.read_bytes()
            sys.argv = ["openswarm", "fleet", "ledger", "pull"]
            with mock.patch.dict(xs.os.environ, {
                "PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", ""),
                "OPENSWARM_TEST_SSH_ROOT": str(self.root),
            }), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                xs.main()
            self.assertEqual(destination.read_bytes(), first_pull)

            custom_destination = self.root / "custom.jsonl"
            sys.argv = ["openswarm", "fleet", "ledger", "pull", "--hosts", "remote",
                        "--into", str(custom_destination)]
            with mock.patch.dict(xs.os.environ, {
                "PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", ""),
                "OPENSWARM_TEST_SSH_ROOT": str(self.root),
            }), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                xs.main()
            self.assertEqual([json.loads(line)["run_id"] for line in custom_destination.read_text().splitlines()],
                             ["shared", "remote-run"])

            sys.argv = ["openswarm", "ledger", "--fleet", "--json"]
            with contextlib.redirect_stdout(ledger_output):
                xs.main()
        finally:
            sys.argv = original_argv

        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["run_id"] == "shared" for row in rows), 1)
        remote_row = next(row for row in rows if row["run_id"] == "remote-run")
        self.assertEqual(remote_row["host"], "remote")
        self.assertEqual(remote_row["title"], "token [REDACTED]")
        self.assertIn("down: unreachable", errors.getvalue())
        # The --fleet reader uses the merged file, not the local source ledger.
        self.assertEqual(json.loads(ledger_output.getvalue()), rows)

    def test_ledger_pull_fails_when_every_host_is_unreachable(self):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        ssh = fake_bin / "ssh"
        ssh.write_text(f"#!{sys.executable}\nraise SystemExit(255)\n")
        ssh.chmod(0o755)
        self.config([{"name": "down", "kind": "ssh", "ssh": "down"}])
        original_argv = sys.argv
        try:
            sys.argv = ["openswarm", "fleet", "ledger", "pull", "--into", str(self.root / "out.jsonl")]
            with mock.patch.dict(xs.os.environ, {"PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", "")}):
                with self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
                    xs.main()
        finally:
            sys.argv = original_argv
        self.assertEqual(error.exception.code, 1)
        self.assertFalse((self.root / "out.jsonl").exists())

    def test_ledger_pull_enforces_line_and_record_size_limits(self):
        oversized = b"{" + (b" " * xs.LEDGER_MAX_LINE_BYTES) + b"}\n"
        valid = json.dumps({"run_id": "kept"}).encode() + b"\n"
        self.assertEqual([row["run_id"] for row in xs.ledger_records(oversized + valid, "h")], ["kept"])
        self.assertEqual(xs.ledger_records(b"\n" * xs.LEDGER_MAX_HOST_LINES + valid, "h"), [])


STUB = pathlib.Path(_load.HERE) / "fixtures" / "fake_fleet_openswarm.py"
OCGO_MODEL = "mid-coder"  # balanced ocgo model in the fixture state


class _StdoutWithBuffer:
    """sys.stdout stand-in for in-process cmd_fleet exec calls, which write
    the forwarded bytes via sys.stdout.buffer."""

    def __init__(self):
        self.buffer = io.BytesIO()
        self.text = io.StringIO()

    def write(self, value):
        return self.text.write(value)

    def flush(self):
        pass


class FleetWeightedRoutingTests(unittest.TestCase):
    """M7 slice 2 (#124): `fleet exec -- run ...` dispatches through
    weighted routing at the controller, reusing the single-run / batch
    decision path; the decision is recorded in the controller ledger."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-fleet-weighted-")
        self.root = pathlib.Path(self.tmp.name)
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
        self.config([{"name": "local", "kind": "local", "openswarm": str(STUB)}])
        self.patch = mock.patch.object(xs, "DATA", str(self.root))
        self.patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.patch.stop)

    def config(self, hosts):
        (self.root / "fleet.json").write_text(json.dumps({"hosts": hosts}))

    def write_policy(self, **routing):
        """Routing overrides via policy.local.json; codex require_default_model
        off so the fixture state routes (as in test_weighted_routing)."""
        local = {"codex": {"require_default_model": False}}
        if routing:
            local["routing"] = routing
        (self.root / "policy.local.json").write_text(json.dumps(local))

    def rows_for(self, backend, model, passes, n=8):
        return [{"id": f"{backend[:2]}{i}", "backend": backend, "model": model, "card_type": "fix",
                 "status": "done", "exec_pass": i < passes, "attempts": [{}], "seconds": 10,
                 "tokens": {"total": 100}} for i in range(n)]

    def seed(self, codex_pass=2, ocgo_pass=7, codex_n=8, ocgo_n=8):
        """Seed the controller ledger around the live default pick (codex)."""
        policy = xs.load_policy()
        decision = xs.route({"type": "impl", "difficulty": 3}, self.state, policy)
        self.assertTrue(decision["ok"], f"unroutable: {decision}")
        self.assertEqual(decision["backend"], "codex")
        rows = self.rows_for("codex", decision["model"], codex_pass, codex_n)
        rows += self.rows_for("ocgo", OCGO_MODEL, ocgo_pass, ocgo_n)
        (self.root / "ledger.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in rows))
        return policy, decision

    def env(self):
        return {**os.environ, "OPENSWARM_HOME": str(self.root),
                "OPENSWARM_STATE_FILE": str(self.root / "state.json")}

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, str(pathlib.Path(_load.ROOT) / "bin" / "openswarm"), *argv],
                              env=self.env(), capture_output=True, text=True, timeout=120)

    def exec_run(self, *extra):
        """`fleet exec local -- run ...` through the real CLI; returns the
        CompletedProcess. The fake fleet stub needs a task file, cwd and
        budget; --backend/--model are the caller's to add (or omit)."""
        command = ["run", "--task-file", str(self.task), "--card-type", "fix",
                   "--cwd", str(self.root), "--budget-minutes", "5", *extra]
        return self.run_cli("fleet", "exec", "local", "--", *command)

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

    def decision_entries(self):
        return [entry for entry in self.ledger_entries() if entry.get("fleet_routing_decision")]

    def test_weighted_on_reroutes_forwarded_run(self):
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        _, decision = self.seed()
        proc = self.exec_run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reply = json.loads(proc.stdout)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "ocgo")
        self.assertEqual(starts[0]["args"]["model"], OCGO_MODEL)
        decisions = self.decision_entries()
        self.assertEqual(len(decisions), 1)
        entry = decisions[0]
        self.assertEqual(entry["run_id"], reply["id"])
        self.assertEqual(entry["backend"], "ocgo")
        self.assertEqual(entry["model"], OCGO_MODEL)
        self.assertEqual(entry["status"], "dispatched")
        self.assertEqual(entry["host"], "local")
        self.assertTrue(entry["routed_by_advice"])
        self.assertEqual(entry["routed_from"], {"backend": "codex", "model": decision["model"]})
        self.assertEqual(entry["advice_pass_rate"], 0.875)
        self.assertEqual(entry["advice_samples"], 8)
        self.assertEqual(entry["actual_pass_rate"], 0.25)
        self.assertEqual(entry["actual_samples"], 8)
        self.assertTrue(entry["weighted_routing"]["applied"])
        # The decision record must not move the weights it was computed from.
        weights = xs.compute_routing_weights(self.ledger_entries(), 8)["entries"]
        self.assertEqual({(row["backend"], row["runs"]) for row in weights},
                         {("codex", 8), ("ocgo", 8)})

    def test_weighted_off_forwards_verbatim_without_ledger_entry(self):
        self.write_policy()
        _, decision = self.seed()
        before = self.ledger_entries()
        proc = self.exec_run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertIsNone(starts[0]["args"]["backend"])
        self.assertIsNone(starts[0]["args"]["model"])
        self.assertEqual(self.decision_entries(), [])
        self.assertEqual(self.ledger_entries(), before)

    def test_explicit_backend_wins(self):
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        self.seed()
        proc = self.exec_run("--backend", "codex")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "codex")
        self.assertIsNone(starts[0]["args"]["model"])
        decisions = self.decision_entries()
        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0]["weighted_routing"]["applied"])
        self.assertIn("explicit --backend", decisions[0]["weighted_routing"]["reason"])
        self.assertNotIn("routed_by_advice", decisions[0])

    def test_margin_blocks_small_advantage(self):
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8,
                          advice_margin=0.8)
        self.seed()
        proc = self.exec_run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertIsNone(starts[0]["args"]["backend"])
        decisions = self.decision_entries()
        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0]["weighted_routing"]["applied"])
        self.assertIn("below margin", decisions[0]["weighted_routing"]["reason"])
        self.assertNotIn("routed_by_advice", decisions[0])

    def test_no_history_for_default_pick_means_no_switch(self):
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        self.seed(codex_n=0)
        proc = self.exec_run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertIsNone(starts[0]["args"]["backend"])
        decisions = self.decision_entries()
        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0]["weighted_routing"]["applied"])
        self.assertIn("comparison unavailable", decisions[0]["weighted_routing"]["reason"])
        self.assertNotIn("routed_by_advice", decisions[0])

    def test_non_run_command_is_untouched(self):
        """`fleet exec` forwards non-run commands verbatim even with
        weighted routing on: no routing decision, no ledger entry."""
        echo = self.root / "echo-stub"
        echo.write_text("#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        echo.chmod(0o755)
        self.config([{"name": "local", "kind": "local", "openswarm": str(echo)}])
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        self.seed()
        proc = self.run_cli("fleet", "exec", "local", "--", "lane", "status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), ["lane", "status"])
        self.assertEqual(self.decision_entries(), [])

    def test_dry_run_is_rerouted_but_not_recorded(self):
        """A dry run dispatches nothing, so the reroute is still forwarded
        (the remote dry-run decision shows the advised pick) but no
        `dispatched` ledger record is written."""
        echo = self.root / "echo-stub"
        echo.write_text("#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        echo.chmod(0o755)
        self.config([{"name": "local", "kind": "local", "openswarm": str(echo)}])
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        self.seed()
        proc = self.run_cli("fleet", "exec", "local", "--", "run", "--dry-run",
                            "--task-file", str(self.task), "--card-type", "fix",
                            "--cwd", str(self.root), "--budget-minutes", "5")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        forwarded = json.loads(proc.stdout)
        self.assertEqual(forwarded[:5], ["run", "--backend", "ocgo", "--model", OCGO_MODEL])
        self.assertEqual(self.decision_entries(), [])

    def test_transport_failure_records_no_entry(self):
        """rc 124/127 means the command never ran on the host, so there is
        no dispatch to record — even though a decision was computed."""
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        policy, _ = self.seed()
        namespace = argparse.Namespace(fleet_cmd="exec", host="local", timeout=300,
                                       args=["--", "run", "--task-file", str(self.task),
                                             "--card-type", "fix", "--cwd", str(self.root),
                                             "--budget-minutes", "5"])
        stdout = _StdoutWithBuffer()
        with mock.patch.object(xs, "get_state", return_value=self.state), \
                mock.patch.object(xs, "host_run", return_value=(124, b"", b"")) as transport, \
                mock.patch.object(sys, "stdout", stdout):
            with self.assertRaises(SystemExit) as exited:
                xs.cmd_fleet(namespace, policy)
        self.assertEqual(exited.exception.code, 124)
        transport.assert_called_once()
        self.assertEqual(self.decision_entries(), [])


if __name__ == "__main__":
    unittest.main()
