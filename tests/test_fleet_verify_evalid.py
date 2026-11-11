"""Fleet-path --verify-evalid (M13 slice 2, #153).

`fleet exec -- run ... --verify-evalid` forwards evalid verification to
the remote run; the controller refuses --verify-evalid without --verify
before dispatching, waits for the remote run to finish, and copies its
claim-vs-evidence record into the controller ledger (fleet_verify_evalid).
No network and no real evalid checkout needed: the guard/record/wait
units are exercised with mocked transports, and the end-to-end tests run
the real CLI against the fake fleet stub (the same stub M7 slice 1 and
M13 slice 1 use), which synthesises the remote run's verify_evalid
record in `fleet _run-info`.
"""
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
STUB = pathlib.Path(_load.HERE) / "fixtures" / "fake_fleet_openswarm.py"
OCGO_MODEL = "mid-coder"  # balanced ocgo model in the fixture state


class FleetRunArgParserTests(unittest.TestCase):
    def test_verify_evalid_flags_parse(self):
        parsed, _ = xs.fleet_run_arg_parser().parse_known_args(
            ["--verify", "true", "--verify-evalid", "--evalid-bin", "/tmp/fake-xflow"])
        self.assertEqual(parsed.verify, "true")
        self.assertTrue(parsed.verify_evalid)
        self.assertEqual(parsed.evalid_bin, "/tmp/fake-xflow")

    def test_verify_evalid_defaults_off(self):
        parsed, _ = xs.fleet_run_arg_parser().parse_known_args([])
        self.assertFalse(parsed.verify_evalid)
        self.assertIsNone(parsed.evalid_bin)
        self.assertIsNone(parsed.verify)

    def test_unknown_flags_still_pass_through(self):
        _, rest = xs.fleet_run_arg_parser().parse_known_args(
            ["--verify", "true", "--verify-evalid", "--some-future-flag", "x"])
        self.assertEqual(rest, ["--some-future-flag", "x"])


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


class FleetExecGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m13-fleet-guard-")
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "fleet.json").write_text(
            json.dumps({"hosts": [{"name": "local", "kind": "local", "openswarm": "stub"}]}))
        self.patches = [mock.patch.object(xs, "DATA", str(self.data)),
                        mock.patch.object(xs, "LEDGER", str(self.data / "ledger.jsonl"))]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self.tmp.cleanup)
        for patch in self.patches:
            self.addCleanup(patch.stop)

    def namespace(self, *run_args):
        return argparse.Namespace(fleet_cmd="exec", host="local", timeout=300,
                                  args=["--", "run", *run_args])

    def run_exec(self, namespace, host_run=None, host_json=None):
        stdout = _StdoutWithBuffer()
        patches = [mock.patch.object(sys, "stdout", stdout)]
        if host_run is not None:
            patches.append(mock.patch.object(xs, "host_run", host_run))
        if host_json is not None:
            patches.append(mock.patch.object(xs, "host_json", host_json))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with self.assertRaises(SystemExit) as exited:
                xs.cmd_fleet(namespace)
        return exited.exception.code, stdout

    def test_verify_evalid_without_verify_is_refused_before_dispatch(self):
        transport = mock.Mock(return_value=(0, b"{}", b""))
        with self.assertRaises(ValueError) as ctx:
            with mock.patch.object(xs, "host_run", transport):
                xs.cmd_fleet(self.namespace("--verify-evalid"))
        self.assertIn("--verify-evalid requires --verify", str(ctx.exception))
        transport.assert_not_called()

    def test_verify_evalid_with_verify_dispatches(self):
        transport = mock.Mock(return_value=(0, b'{"id": "run1", "state": "started"}', b""))
        info = {"state": "finished",
                "result": {"id": "run1", "status": "done", "seconds": 1.5,
                           "verify_evalid": {"mode": "verify-evalid", "verdict": "pass",
                                             "claim": "Ran `true`",
                                             "replay": {"verdict": "verified"}, "error": None}}}
        with mock.patch.object(xs, "host_json", return_value=info) as host_json:
            code, _ = self.run_exec(self.namespace("--verify", "true", "--verify-evalid"),
                                    host_run=transport, host_json=host_json)
        self.assertEqual(code, 0)
        transport.assert_called_once()
        forwarded = transport.call_args.args[1]
        self.assertIn("--verify-evalid", forwarded)
        host_json.assert_called_once()
        self.assertEqual(host_json.call_args.args[1], ["fleet", "_run-info", "run1"])

    def test_evalid_bin_is_forwarded_verbatim(self):
        transport = mock.Mock(return_value=(0, b'{"id": "run1", "state": "started"}', b""))
        info = {"state": "finished", "result": {"id": "run1", "status": "done",
                                                "verify_evalid": {"verdict": "pass"}}}
        with mock.patch.object(xs, "host_json", return_value=info):
            self.run_exec(self.namespace("--verify", "true", "--verify-evalid",
                                         "--evalid-bin", "/tmp/fake-xflow"),
                          host_run=transport)
        forwarded = transport.call_args.args[1]
        self.assertIn("--evalid-bin", forwarded)
        self.assertIn("/tmp/fake-xflow", forwarded)

    def test_dry_run_records_no_verify_entry(self):
        transport = mock.Mock(return_value=(0, b'{"id": "run1", "state": "started"}', b""))
        host_json = mock.Mock(side_effect=AssertionError("must not poll on a dry run"))
        code, _ = self.run_exec(self.namespace("--dry-run", "--verify", "true",
                                                "--verify-evalid"),
                                host_run=transport, host_json=host_json)
        self.assertEqual(code, 0)
        host_json.assert_not_called()
        self.assertEqual(self.entries(), [])

    def test_transport_failure_records_no_verify_entry(self):
        transport = mock.Mock(return_value=(124, b"", b"transport timed out"))
        host_json = mock.Mock(side_effect=AssertionError("must not poll after a transport failure"))
        with mock.patch.object(xs, "host_json", host_json):
            code, _ = self.run_exec(self.namespace("--verify", "true", "--verify-evalid"),
                                    host_run=transport)
        self.assertEqual(code, 124)
        host_json.assert_not_called()
        self.assertEqual(self.entries(), [])

    def test_non_run_command_is_untouched_by_the_guard(self):
        transport = mock.Mock(return_value=(0, b"ok", b""))
        code, _ = self.run_exec(
            argparse.Namespace(fleet_cmd="exec", host="local", timeout=300,
                               args=["--", "lane", "status"]),
            host_run=transport)
        self.assertEqual(code, 0)
        transport.assert_called_once()
        self.assertEqual(self.entries(), [])

    def entries(self):
        path = self.data / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class FleetWaitRunResultTests(unittest.TestCase):
    def test_running_then_finished(self):
        calls = {"n": 0}

        def host_json(host, args, **kw):
            calls["n"] += 1
            if calls["n"] < 2:
                return {"state": "running", "result": None}
            return {"state": "finished",
                    "result": {"id": "run1", "status": "done",
                               "verify_evalid": {"verdict": "pass"}}}

        with mock.patch.object(xs, "host_json", side_effect=host_json):
            result, error = xs.fleet_wait_run_result({"name": "local"}, "run1", timeout=30)
        self.assertIsNone(error)
        self.assertEqual(result["verify_evalid"]["verdict"], "pass")
        self.assertEqual(calls["n"], 2)

    def test_timeout_is_an_explicit_error(self):
        with mock.patch.object(xs, "host_json", return_value={"state": "running", "result": None}):
            result, error = xs.fleet_wait_run_result({"name": "local"}, "run1",
                                                     timeout=0.2, poll_seconds=0.05)
        self.assertIsNone(result)
        self.assertIn("did not finish", error)
        self.assertIn("run1", error)

    def test_transport_error_is_an_explicit_error(self):
        with mock.patch.object(xs, "host_json", side_effect=RuntimeError("host down")):
            result, error = xs.fleet_wait_run_result({"name": "local"}, "run1", timeout=30)
        self.assertIsNone(result)
        self.assertIn("could not read remote run run1", error)


class FleetRecordVerifyEvalidTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m13-fleet-ledger-")
        self.data = pathlib.Path(self.tmp.name)
        self.patches = [mock.patch.object(xs, "DATA", str(self.data)),
                        mock.patch.object(xs, "LEDGER", str(self.data / "ledger.jsonl"))]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self.tmp.cleanup)
        for patch in self.patches:
            self.addCleanup(patch.stop)

    def entries(self):
        path = self.data / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def record(self, result, fetch_error=None, run_id="run1"):
        xs.fleet_record_verify_evalid("local", "fleet-local-run1", result, run_id=run_id,
                                      card_type="fix", scope="repo",
                                      backend="pi", model=None, seconds=1.5,
                                      fetch_error=fetch_error)
        return self.entries()

    def test_pass_record_carries_claim_and_replay(self):
        ve = {"mode": "verify-evalid", "verdict": "pass", "claim": "Ran `true` — all pass",
              "claims_text": "Ran `true` — all pass",
              "replay": {"verdict": "verified", "claims": [{"command": "true", "match": True}]},
              "error": None}
        entries = self.record({"verify_evalid": ve})
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertTrue(entry["fleet_verify_evalid"])
        self.assertEqual(entry["verify_evalid_verdict"], "pass")
        self.assertEqual(entry["verify_evalid"], ve)
        self.assertEqual(entry["verify_evalid_replay_verdict"], "verified")
        self.assertEqual(entry["run_id"], "run1")
        self.assertEqual(entry["host"], "local")
        self.assertEqual(entry["status"], "dispatched")
        self.assertEqual(entry["card_type"], "fix")

    def test_mismatch_record_carries_the_verdict(self):
        ve = {"mode": "verify-evalid", "verdict": "mismatch", "claim": "Ran `true`",
              "replay": {"verdict": "mismatch"}, "error": "evalid audit verdict mismatch"}
        entries = self.record({"verify_evalid": ve})
        self.assertEqual(entries[0]["verify_evalid_verdict"], "mismatch")
        self.assertEqual(entries[0]["verify_evalid_replay_verdict"], "mismatch")
        self.assertIn("mismatch", entries[0]["error"])

    def test_error_record_keeps_the_evalid_reason(self):
        # evalid unavailable on the executor host: the remote run fails
        # loudly (M2 fail-fast); the fleet record must carry that error,
        # never a silent plain-verify pass.
        ve = {"mode": "verify-evalid", "verdict": "error", "claim": "Ran `true`",
              "replay": None, "error": "evalid-unavailable: evalid not found"}
        entries = self.record({"verify_evalid": ve})
        self.assertEqual(entries[0]["verify_evalid_verdict"], "error")
        self.assertIn("evalid-unavailable", entries[0]["error"])

    def test_missing_record_is_an_explicit_error_not_a_pass(self):
        entries = self.record({})
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["verify_evalid_verdict"], "error")
        self.assertIn("no verify_evalid record", entry["error"])
        self.assertEqual(entry["verify_evalid"]["verdict"], "error")

    def test_fetch_error_is_an_explicit_error_not_a_pass(self):
        entries = self.record({}, fetch_error="remote run run1 did not finish within 300s")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["verify_evalid_verdict"], "error")
        self.assertIn("did not finish", entry["error"])
        self.assertEqual(entry["run_id"], "run1")

    def test_unknown_run_id_is_an_explicit_error(self):
        entries = self.record(
            {}, fetch_error="could not determine remote run id from the fleet reply",
            run_id=None)
        self.assertEqual(entries[0]["verify_evalid_verdict"], "error")
        self.assertIn("could not determine remote run id", entries[0]["error"])
        # append_ledger normalises a missing run_id to the entry id.
        self.assertEqual(entries[0]["run_id"], "fleet-local-run1")

    def test_record_does_not_inflate_routing_weights(self):
        ve = {"mode": "verify-evalid", "verdict": "pass", "claim": "Ran `true`",
              "replay": {"verdict": "verified"}, "error": None}
        entries = self.record({"verify_evalid": ve})
        run = {"backend": "pi", "model": None, "card_type": "fix", "status": "done",
               "exec_pass": True}
        weights = xs.compute_routing_weights(entries + [run], 8)["entries"]
        self.assertEqual([(row["backend"], row["runs"]) for row in weights], [("pi", 1)])

    def test_record_does_not_inflate_advice_accuracy(self):
        ve = {"mode": "verify-evalid", "verdict": "pass", "claim": "Ran `true`",
              "replay": {"verdict": "verified"}, "error": None}
        entries = self.record({"verify_evalid": ve})
        advised = {"backend": "pi", "model": None, "card_type": "fix", "status": "done",
                   "exec_pass": True, "routing_advice": {"backend": "pi"}}
        accuracy = xs.compute_advice_accuracy(entries + [advised])
        self.assertEqual(accuracy["total_advice_records"], 1)
        self.assertEqual(accuracy["total_scored"], 1)

    def test_ledger_write_failure_does_not_fail_the_exec(self):
        with mock.patch.object(xs, "append_ledger", side_effect=OSError("disk full")):
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                xs.fleet_record_verify_evalid(
                    "local", "fleet-local-run1", {"verify_evalid": {"verdict": "pass"}},
                    run_id="run1")


class FleetVerifyEvalidEndToEndTests(unittest.TestCase):
    """Real CLI `fleet exec local -- run ... --verify-evalid` against the
    fake fleet stub: dispatch, _run-info polling and the controller
    ledger record, with routing off and on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-fleet-m13e2e-")
        self.root = pathlib.Path(self.tmp.name)
        self.task = self.root / "task.md"
        self.task.write_text("task one")
        # The fake stub's `_run-info` finish path checks out the run's
        # base in the run's cwd, so the e2e cwd must be a git repo with a
        # real base commit (as in test_batch.py's FakeFleetFixture).
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.check_output(["git", "-C", str(self.repo), "init", "-q"])
        subprocess.check_output(["git", "-C", str(self.repo), "config", "maintenance.auto", "false"])
        subprocess.check_output(["git", "-C", str(self.repo), "config", "gc.auto", "0"])
        (self.repo / "base").write_text("base")
        subprocess.check_output(["git", "-C", str(self.repo), "add", "base"])
        subprocess.check_output(["git", "-C", str(self.repo), "-c", "user.name=t",
                                 "-c", "user.email=t@example.invalid", "commit", "-qm", "base"])
        self.base = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
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
        command = ["run", "--task-file", str(self.task), "--card-type", "fix",
                   "--cwd", str(self.repo), "--base", self.base,
                   "--budget-minutes", "5", *extra]
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

    def verify_entries(self):
        return [entry for entry in self.ledger_entries() if entry.get("fleet_verify_evalid")]

    def test_pass_run_records_claim_vs_evidence(self):
        proc = self.exec_run("--verify", "true", "--verify-evalid")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reply = json.loads(proc.stdout)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertTrue(starts[0]["args"]["verify_evalid"])
        entries = self.verify_entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["run_id"], reply["id"])
        self.assertEqual(entry["host"], "local")
        self.assertEqual(entry["verify_evalid_verdict"], "pass")
        self.assertEqual(entry["verify_evalid"]["mode"], "verify-evalid")
        self.assertEqual(entry["verify_evalid_replay_verdict"], "verified")
        self.assertIn("Ran `true`", entry["verify_evalid"]["claim"])
        self.assertEqual(entry["status"], "dispatched")

    def test_mismatch_run_records_mismatch(self):
        self.task.write_text("task one EVALID-MISMATCH")
        proc = self.exec_run("--verify", "true", "--verify-evalid")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entries = self.verify_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["verify_evalid_verdict"], "mismatch")
        self.assertEqual(entries[0]["verify_evalid_replay_verdict"], "mismatch")
        self.assertIn("mismatch", entries[0]["error"])

    def test_guard_refuses_before_dispatch_end_to_end(self):
        proc = self.exec_run("--verify-evalid")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--verify-evalid requires --verify", proc.stderr)
        self.assertEqual(self.start_events(), [])
        self.assertEqual(self.verify_entries(), [])

    def test_verify_entry_recorded_alongside_routing_decision(self):
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        _, decision = self.seed()
        proc = self.exec_run("--verify", "true", "--verify-evalid")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "ocgo")
        decisions = [entry for entry in self.ledger_entries()
                     if entry.get("fleet_routing_decision")]
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0]["routed_by_advice"])
        entries = self.verify_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["verify_evalid_verdict"], "pass")
        self.assertEqual(entries[0]["backend"], "ocgo")
        self.assertEqual(entries[0]["model"], OCGO_MODEL)
        # Neither record moves the weights it was computed from.
        weights = xs.compute_routing_weights(self.ledger_entries(), 8)["entries"]
        self.assertEqual({(row["backend"], row["runs"]) for row in weights},
                         {("codex", 8), ("ocgo", 8)})

    def test_plain_verify_run_records_no_verify_entry(self):
        proc = self.exec_run("--verify", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.verify_entries(), [])


if __name__ == "__main__":
    unittest.main()
