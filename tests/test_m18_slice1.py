"""M18 slice 1 (#173): quota-aware rerouting for batch + fleet.

M17 slice 1 (#169) wired quota-aware rerouting into single `openswarm run`
only; batch cards and fleet-forwarded runs still routed without looking at
quota. This slice extends the same apply_quota_routing() decision path to
both dispatch paths: a batch card (batch_weighted_route) and a forwarded
`fleet exec -- run` (fleet_weighted_route) reroute off a default backend at
or below routing.quota.threshold, record quota_before + reason + both
remainings in the controller ledger, and refuse to dispatch when every
routable backend is exhausted. Explicit --backend always wins; a failed
quota read keeps the old behaviour on the record; weighted/calibration
semantics are unchanged. Default stays enabled:false.
Run: python3 -m unittest discover -s tests   (or: python3 tests/test_m18_slice1.py)
"""
import argparse, contextlib, copy, io, json, os, shutil, stat, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402
import test_batch  # noqa: E402  (FakeFleetFixture for the batch end-to-end tests)

xs = _load.load()
xs.breakers = lambda: {}  # isolate from any breakers on disk
xs.load_policy = lambda: xs.jload(f"{xs.SKILL_DIR}/policy.json", {})  # ignore policy.local.json overrides

POLICY = xs.load_policy()
POLICY["codex"]["require_default_model"] = False
with open(_load.FIXTURE_STATE) as f:
    BASE = json.load(f)

LOW_VAR = "OPENSWARM_M18_TEST_LOW"
HIGH_VAR = "OPENSWARM_M18_TEST_HIGH"

ECHO_CFG = {"id": "echo-low", "name": "Echo low (test backend)",
            "command": ["python3", "fake-echo-cli.py", "--prompt", "{prompt}",
                        "--workdir", "{worktree}", "--model", "{model}"],
            "env": {}, "quota_source": "none", "model": "echo-low"}
HIGH_CFG = dict(ECHO_CFG, id="echo-high", name="Echo high (test backend)", model="echo-high")

FAKE_ECHO_CLI = r"""#!/usr/bin/env python3
import argparse, json, os, sys
p = argparse.ArgumentParser()
p.add_argument("--prompt", required=True)
p.add_argument("--workdir", default=".")
p.add_argument("--model", default="")
a = p.parse_args()
with open(os.path.join(a.workdir, "echo-output.txt"), "w") as fh:
    fh.write("echo: %s\nmodel: %s\n" % (a.prompt, a.model))
print(json.dumps({"status": "done", "summary": "echoed the prompt",
                  "files_changed": ["echo-output.txt"], "tests": "no tests claimed"}))
"""


def doc(*backends):
    return {"backends": list(backends)}


def echo_backends(script):
    low = dict(ECHO_CFG, command=[sys.executable, script, "--prompt", "{prompt}",
                                  "--workdir", "{worktree}", "--model", "{model}"])
    high = dict(HIGH_CFG, command=[sys.executable, script, "--prompt", "{prompt}",
                                   "--workdir", "{worktree}", "--model", "{model}"])
    return [low, high]


def quota_policy(low_source, high_source, enabled=True, threshold=0, weighted=False):
    policy = copy.deepcopy(POLICY)
    policy["routing"]["default_order"] = ["echo-low", "echo-high"]
    policy["routing"]["weighted"] = weighted
    policy["routing"]["min_samples"] = 8
    policy["routing"]["quota"] = {
        "enabled": enabled, "threshold": threshold,
        "sources": {"echo-low": low_source, "echo-high": high_source}}
    return policy


def env_sources():
    return ({"source": "env", "var": LOW_VAR}, {"source": "env", "var": HIGH_VAR})


def rows_for(backend, model, passes, n=8):
    return [{"id": f"{backend[:2]}{i}", "backend": backend, "model": model, "card_type": "fix",
             "status": "done", "exec_pass": i < passes, "attempts": [{}], "seconds": 10,
             "tokens": {"total": 100}} for i in range(n)]


def read_ledger():
    try:
        with open(xs.LEDGER) as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def reset_ledger(entries=()):
    os.makedirs(os.path.dirname(xs.LEDGER), exist_ok=True)
    with open(xs.LEDGER, "w") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


class QuotaRouteHarness:
    """Shared echo-backend + env setup for the in-process route tests."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (LOW_VAR, HIGH_VAR, "OPENSWARM_STATE_FILE")}
        os.environ["OPENSWARM_STATE_FILE"] = _load.FIXTURE_STATE
        self.tmp = tempfile.mkdtemp(prefix="openswarm-m18-")
        self.script = os.path.join(self.tmp, "fake-echo-cli.py")
        with open(self.script, "w") as fh:
            fh.write(FAKE_ECHO_CLI)
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self.config = os.path.join(self.tmp, "backends.json")
        with open(self.config, "w") as fh:
            json.dump(doc(*echo_backends(self.script)), fh)
        self.backends = xs.load_custom_backends(xs.load_policy(), explicit=self.config)
        reset_ledger()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


class BatchQuotaRouteTests(QuotaRouteHarness, unittest.TestCase):
    def route_card(self, policy, **spec_extra):
        spec = {"backend": None, "backend_explicit": False, "model": None,
                "card_type": "fix", "backends_file": self.config}
        spec.update(spec_extra)
        return xs.batch_weighted_route(spec, policy)

    def test_reroutes_off_low_default_and_records_both_remainings(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources())
        backend, model, weighted_info, decision, quota = self.route_card(policy)
        self.assertEqual(backend, "echo-high")
        self.assertEqual(model, "echo-high")
        self.assertIsNone(weighted_info)
        self.assertTrue(quota["applied"])
        self.assertEqual(quota["from"], "echo-low")
        self.assertEqual(quota["to"], "echo-high")
        self.assertEqual(quota["quota_before"], {"echo-low": 0, "echo-high": 5})
        self.assertIn("threshold 0", quota["reason"])
        self.assertEqual(decision["backend"], "echo-high")
        self.assertEqual(decision.get("model"), "echo-high")

    def test_stays_when_sufficient(self):
        os.environ[LOW_VAR] = "5"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources())
        backend, model, weighted_info, decision, quota = self.route_card(policy)
        self.assertEqual(backend, "echo-low")
        self.assertFalse(quota["applied"])
        self.assertEqual(quota["quota_before"], {"echo-low": 5})

    def test_explicit_backend_wins(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources())
        backend, model, weighted_info, decision, quota = self.route_card(
            policy, backend="echo-low", backend_explicit=True)
        self.assertEqual(backend, "echo-low")
        self.assertIsNone(decision)
        self.assertFalse(quota["applied"])
        self.assertIn("explicit --backend wins", quota["reason"])

    def test_disabled_is_a_no_op(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources(), enabled=False)
        spec = {"backend": "codex", "backend_explicit": False, "model": None, "card_type": "fix"}
        with mock.patch.object(xs, "route", side_effect=AssertionError("route must not be called")), \
                mock.patch.object(xs, "get_state", side_effect=AssertionError("get_state must not be called")):
            self.assertEqual(xs.batch_weighted_route(spec, policy),
                             ("codex", None, None, None, None))

    def test_all_exhausted_is_flagged_not_dispatched(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "0"
        policy = quota_policy(*env_sources())
        backend, model, weighted_info, decision, quota = self.route_card(policy)
        self.assertEqual(backend, "echo-low")
        self.assertTrue(quota["all_exhausted"])
        self.assertEqual(quota["quota_before"], {"echo-low": 0, "echo-high": 0})

    def test_read_failure_keeps_old_behaviour_on_the_record(self):
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy({"source": "command", "command": ["false"]},
                              {"source": "env", "var": HIGH_VAR})
        backend, model, weighted_info, decision, quota = self.route_card(policy)
        self.assertEqual(backend, "echo-low")
        self.assertFalse(quota["applied"])
        self.assertIn("echo-low", quota["quota_error"])
        self.assertIn("exited 1", quota["quota_error"]["echo-low"])

    def test_invalid_quota_config_fails_loudly(self):
        policy = quota_policy(*env_sources(), threshold=-1)
        with self.assertRaises(ValueError):
            self.route_card(policy)

    def test_weighted_and_quota_compose(self):
        # Weighted advice prefers echo-high (7/8 vs 2/8); quota is sufficient
        # there, so the weighted pick stands and quota only records the check.
        os.environ[LOW_VAR] = "5"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources(), weighted=True)
        reset_ledger(rows_for("echo-low", "echo-low", 2) + rows_for("echo-high", "echo-high", 7))
        backend, model, weighted_info, decision, quota = self.route_card(policy)
        self.assertEqual(backend, "echo-high")
        self.assertTrue(weighted_info["applied"])
        self.assertTrue(decision["routed_by_advice"])
        self.assertFalse(quota["applied"])
        self.assertEqual(quota["quota_before"], {"echo-high": 5})

    def test_weighted_no_history_then_quota_reroutes(self):
        # Only the advised backend has history, so weighted cannot compare
        # against the default pick — but quota still reroutes.
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources(), weighted=True)
        reset_ledger(rows_for("echo-high", "echo-high", 7))
        backend, model, weighted_info, decision, quota = self.route_card(policy)
        self.assertEqual(backend, "echo-high")
        self.assertFalse(weighted_info["applied"])
        self.assertIn("comparison unavailable", weighted_info["reason"])
        self.assertTrue(quota["applied"])
        self.assertEqual(quota["quota_before"], {"echo-low": 0, "echo-high": 5})

    def test_record_carries_quota_fields(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources())
        backend, model, weighted_info, decision, quota = self.route_card(policy)
        summary = {"id": "b1", "scope": "repo"}
        row = {"card": "one.md", "run_id": "r1", "backend": backend, "model": model}
        xs.batch_record_routing_decision(summary, row, "fix", weighted_info, decision, quota)
        entries = read_ledger()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertTrue(entry["batch_routing_decision"])
        self.assertEqual(entry["backend"], "echo-high")
        self.assertEqual(entry["quota_routing"], quota)
        self.assertTrue(entry["quota_rerouted"])
        self.assertEqual(entry["quota_rerouted_from"], "echo-low")
        self.assertEqual(entry["quota_before"], {"echo-low": 0, "echo-high": 5})
        # A decision record must not move the weights it was computed from.
        weights = xs.compute_routing_weights(entries, 8)["entries"]
        self.assertEqual(weights, [])

    def test_probe_readings_match_batch_decision(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources())
        args = argparse.Namespace(backends_file=self.config, json=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            xs.cmd_quota(args, policy, custom_backends=self.backends)
        rows = {row["backend"]: row for row in json.loads(buf.getvalue())["backends"]}
        self.assertEqual(rows["echo-low"]["status"], "low")
        self.assertEqual(rows["echo-high"]["status"], "ok")
        _, _, _, _, quota = self.route_card(policy)
        self.assertTrue(quota["applied"])
        self.assertEqual(quota["from"], "echo-low")
        os.environ[LOW_VAR] = "5"
        with contextlib.redirect_stdout(io.StringIO()):
            xs.cmd_quota(args, policy, custom_backends=self.backends)
        _, _, _, _, quota = self.route_card(policy)
        self.assertFalse(quota["applied"])


class FleetQuotaRouteTests(QuotaRouteHarness, unittest.TestCase):
    def command(self, **flags):
        cmd = ["run", "--task-file", "task.md", "--card-type", "fix",
               "--cwd", ".", "--budget-minutes", "5", "--backends-file", self.config]
        for key, value in flags.items():
            cmd += ["--" + key.replace("_", "-"), str(value)]
        return cmd

    def test_reroutes_and_injects_backend(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources())
        cmd = self.command()
        eff, weighted_info, decision, quota = xs.fleet_weighted_route(cmd, policy)
        self.assertEqual(eff[:5], ["run", "--backend", "echo-high", "--model", "echo-high"])
        self.assertEqual(eff[5:], cmd[1:])
        self.assertIsNone(weighted_info)
        self.assertTrue(quota["applied"])
        self.assertEqual(quota["from"], "echo-low")
        self.assertEqual(quota["to"], "echo-high")
        self.assertEqual(quota["quota_before"], {"echo-low": 0, "echo-high": 5})
        self.assertEqual(decision["backend"], "echo-high")

    def test_stays_when_sufficient(self):
        os.environ[LOW_VAR] = "5"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources())
        cmd = self.command()
        eff, weighted_info, decision, quota = xs.fleet_weighted_route(cmd, policy)
        self.assertEqual(eff, cmd)
        self.assertFalse(quota["applied"])
        self.assertEqual(quota["quota_before"], {"echo-low": 5})
        self.assertEqual(decision["backend"], "echo-low")

    def test_explicit_backend_wins(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources())
        cmd = self.command(backend="echo-low")
        eff, weighted_info, decision, quota = xs.fleet_weighted_route(cmd, policy)
        self.assertEqual(eff, cmd)
        self.assertIsNone(decision)
        self.assertFalse(quota["applied"])
        self.assertIn("explicit --backend wins", quota["reason"])

    def test_disabled_is_a_no_op(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources(), enabled=False)
        cmd = self.command()
        with mock.patch.object(xs, "route", side_effect=AssertionError("route must not be called")), \
                mock.patch.object(xs, "get_state", side_effect=AssertionError("get_state must not be called")):
            self.assertEqual(xs.fleet_weighted_route(cmd, policy), (cmd, None, None, None))

    def test_all_exhausted_is_flagged_not_forwarded(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "0"
        policy = quota_policy(*env_sources())
        cmd = self.command()
        eff, weighted_info, decision, quota = xs.fleet_weighted_route(cmd, policy)
        self.assertEqual(eff, cmd)
        self.assertTrue(quota["all_exhausted"])
        self.assertEqual(quota["quota_before"], {"echo-low": 0, "echo-high": 0})
        self.assertEqual(decision["backend"], "echo-low")

    def test_read_failure_keeps_command(self):
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy({"source": "command", "command": ["false"]},
                              {"source": "env", "var": HIGH_VAR})
        cmd = self.command()
        eff, weighted_info, decision, quota = xs.fleet_weighted_route(cmd, policy)
        self.assertEqual(eff, cmd)
        self.assertFalse(quota["applied"])
        self.assertIn("echo-low", quota["quota_error"])

    def test_non_run_command_is_untouched(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources())
        cmd = ["lane", "status"]
        with mock.patch.object(xs, "route", side_effect=AssertionError("route must not be called")), \
                mock.patch.object(xs, "get_state", side_effect=AssertionError("get_state must not be called")):
            self.assertEqual(xs.fleet_weighted_route(cmd, policy), (cmd, None, None, None))

    def test_invalid_quota_config_fails_loudly(self):
        policy = quota_policy(*env_sources(), threshold=-1)
        with self.assertRaises(ValueError):
            xs.fleet_weighted_route(self.command(), policy)

    def test_weighted_and_quota_compose(self):
        os.environ[LOW_VAR] = "5"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources(), weighted=True)
        reset_ledger(rows_for("echo-low", "echo-low", 2) + rows_for("echo-high", "echo-high", 7))
        cmd = self.command()
        eff, weighted_info, decision, quota = xs.fleet_weighted_route(cmd, policy)
        self.assertEqual(eff[:5], ["run", "--backend", "echo-high", "--model", "echo-high"])
        self.assertTrue(weighted_info["applied"])
        self.assertTrue(decision["routed_by_advice"])
        self.assertFalse(quota["applied"])
        self.assertEqual(quota["quota_before"], {"echo-high": 5})

    def test_record_carries_quota_fields(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy(*env_sources())
        cmd = self.command()
        eff, weighted_info, decision, quota = xs.fleet_weighted_route(cmd, policy)
        xs.fleet_record_routing_decision("local", "fleet-local-abc", "echo-high", "echo-high",
                                         "fix", "repo", weighted_info, decision,
                                         run_id="r1", quota_info=quota)
        entries = read_ledger()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertTrue(entry["fleet_routing_decision"])
        self.assertEqual(entry["backend"], "echo-high")
        self.assertEqual(entry["run_id"], "r1")
        self.assertEqual(entry["quota_routing"], quota)
        self.assertTrue(entry["quota_rerouted"])
        self.assertEqual(entry["quota_rerouted_from"], "echo-low")
        self.assertEqual(entry["quota_before"], {"echo-low": 0, "echo-high": 5})


if __name__ == "__main__":
    unittest.main()

STUB = Path(_load.HERE) / "fixtures" / "fake_fleet_openswarm.py"


class FleetExecQuotaTests(unittest.TestCase):
    """`fleet exec -- run ...` through the real CLI against the fake fleet
    stub: the controller applies quota routing before forwarding."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m18-fleet-exec-")
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
        (self.root / "state.json").write_text(json.dumps(state))
        STUB.chmod(0o755)
        (self.root / "fleet.json").write_text(json.dumps(
            {"hosts": [{"name": "local", "kind": "local", "openswarm": str(STUB)}]}))
        (self.root / "policy.local.json").write_text(json.dumps({
            "codex": {"require_default_model": False},
            "routing": {"default_order": ["echo-low", "echo-high"], "weighted": False,
                        "min_samples": 8,
                        "quota": {"enabled": True, "threshold": 0,
                                  "sources": {"echo-low": {"source": "env", "var": LOW_VAR},
                                              "echo-high": {"source": "env", "var": HIGH_VAR}}}}}))
        self.script = self.root / "fake-echo-cli.py"
        self.script.write_text(FAKE_ECHO_CLI)
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self.config = self.root / "backends.json"
        self.config.write_text(json.dumps(doc(*echo_backends(str(self.script)))))
        self.addCleanup(self.tmp.cleanup)

    def env(self, low, high):
        return {**os.environ, "OPENSWARM_HOME": str(self.root),
                "OPENSWARM_STATE_FILE": str(self.root / "state.json"),
                LOW_VAR: low, HIGH_VAR: high}

    def run_cli(self, *argv, low="0", high="5"):
        return subprocess.run([sys.executable, str(Path(_load.ROOT) / "bin" / "openswarm"), *argv],
                              env=self.env(low, high), capture_output=True, text=True, timeout=120)

    def exec_run(self, *extra, low="0", high="5"):
        command = ["run", "--task-file", str(self.task), "--card-type", "fix",
                   "--cwd", str(self.root), "--budget-minutes", "5",
                   "--backends-file", str(self.config), *extra]
        return self.run_cli("fleet", "exec", "local", "--", *command, low=low, high=high)

    def start_events(self):
        path = self.root / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()
                and json.loads(line)["event"] == "start"]

    def ledger_entries(self):
        path = self.root / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def decision_entries(self):
        return [entry for entry in self.ledger_entries() if entry.get("fleet_routing_decision")]

    def test_reroutes_forwarded_run(self):
        proc = self.exec_run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reply = json.loads(proc.stdout)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "echo-high")
        self.assertEqual(starts[0]["args"]["model"], "echo-high")
        decisions = self.decision_entries()
        self.assertEqual(len(decisions), 1)
        entry = decisions[0]
        self.assertEqual(entry["run_id"], reply["id"])
        self.assertEqual(entry["backend"], "echo-high")
        self.assertEqual(entry["status"], "dispatched")
        self.assertEqual(entry["host"], "local")
        quota = entry["quota_routing"]
        self.assertTrue(quota["applied"])
        self.assertEqual(quota["from"], "echo-low")
        self.assertEqual(quota["to"], "echo-high")
        self.assertEqual(quota["quota_before"], {"echo-low": 0, "echo-high": 5})

    def test_sufficient_forwards_default(self):
        proc = self.exec_run(low="5", high="5")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertIsNone(starts[0]["args"]["backend"])
        decisions = self.decision_entries()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["backend"], "echo-low")
        self.assertFalse(decisions[0]["quota_routing"]["applied"])
        self.assertEqual(decisions[0]["quota_routing"]["quota_before"], {"echo-low": 5})

    def test_all_exhausted_refuses_without_forwarding(self):
        proc = self.exec_run(low="0", high="0")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn('"ok": false', proc.stderr)
        self.assertIn("all routable backends", proc.stderr)
        self.assertEqual(self.start_events(), [])
        refusals = [entry for entry in self.ledger_entries()
                    if entry.get("error_class") == "quota_exhausted"]
        self.assertEqual(len(refusals), 1)
        refusal = refusals[0]
        self.assertEqual(refusal["status"], "unroutable")
        self.assertTrue(refusal["quota_routing"]["all_exhausted"])
        self.assertEqual(refusal["quota_routing"]["quota_before"], {"echo-low": 0, "echo-high": 0})

    def test_explicit_backend_wins(self):
        proc = self.exec_run("--backend", "echo-low")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "echo-low")
        decisions = self.decision_entries()
        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0]["quota_routing"]["applied"])
        self.assertIn("explicit --backend wins", decisions[0]["quota_routing"]["reason"])

    def test_probe_readings_match_fleet_decision(self):
        proc = self.run_cli("quota", "--json", "--backends-file", str(self.config))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = {row["backend"]: row for row in json.loads(proc.stdout)["backends"]}
        self.assertEqual(rows["echo-low"]["status"], "low")
        self.assertEqual(rows["echo-high"]["status"], "ok")
        forwarded = self.exec_run()
        self.assertEqual(forwarded.returncode, 0, forwarded.stderr)
        self.assertEqual(self.start_events()[0]["args"]["backend"], "echo-high")


class BatchExecQuotaTests(test_batch.FakeFleetFixture, unittest.TestCase):
    """End-to-end batch dispatch through the fake fleet transport: per-card
    quota routing, ledger record, and the all-exhausted refusal."""

    def setUp(self):
        test_batch.FakeFleetFixture.setUp(self)
        self._saved_env = {k: os.environ.get(k) for k in (LOW_VAR, HIGH_VAR, "OPENSWARM_STATE_FILE")}
        os.environ["OPENSWARM_STATE_FILE"] = _load.FIXTURE_STATE
        self.script = self.root / "fake-echo-cli.py"
        self.script.write_text(FAKE_ECHO_CLI)
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self.config = self.root / "backends.json"
        self.config.write_text(json.dumps(doc(*echo_backends(str(self.script)))))
        (self.data / "policy.local.json").write_text(json.dumps({
            "codex": {"require_default_model": False},
            "routing": {"default_order": ["echo-low", "echo-high"], "weighted": False,
                        "min_samples": 8,
                        "quota": {"enabled": True, "threshold": 0,
                                  "sources": {"echo-low": {"source": "env", "var": LOW_VAR},
                                              "echo-high": {"source": "env", "var": HIGH_VAR}}}}}))
        self.addCleanup(self.restore_env)

    def restore_env(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_quota_batch(self, low, high):
        os.environ[LOW_VAR] = low
        os.environ[HIGH_VAR] = high
        card = self.cards / "one.md"
        card.write_text("Add a greeting file")
        # model=None: no caller model override, so the rerouted backend's
        # own model is what the card runs with.
        return self.run_batch(hosts="b1", backend=None, model=None, backends_file=str(self.config))

    def ledger_entries(self):
        path = self.data / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def decision_entries(self):
        return [entry for entry in self.ledger_entries() if entry.get("batch_routing_decision")]

    def start_events(self, host="b1"):
        path = self.root / host / "data" / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()
                and json.loads(line)["event"] == "start"]

    def test_card_reroutes_off_low_default(self):
        code, summary, output = self.run_quota_batch("0", "5")
        self.assertEqual(code, 0, output)
        row = summary["cards"][0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["backend"], "echo-high")
        quota = row["quota_routing"]
        self.assertTrue(quota["applied"])
        self.assertEqual(quota["from"], "echo-low")
        self.assertEqual(quota["quota_before"], {"echo-low": 0, "echo-high": 5})
        decisions = self.decision_entries()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["quota_routing"], quota)
        self.assertEqual(decisions[0]["quota_before"], {"echo-low": 0, "echo-high": 5})
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "echo-high")
        self.assertEqual(starts[0]["args"]["model"], "echo-high")

    def test_card_stays_when_sufficient(self):
        code, summary, output = self.run_quota_batch("5", "5")
        self.assertEqual(code, 0, output)
        row = summary["cards"][0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["backend"], "echo-low")
        self.assertFalse(row["quota_routing"]["applied"])
        self.assertEqual(row["quota_routing"]["quota_before"], {"echo-low": 5})
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "echo-low")

    def test_dependent_blocks_when_quota_fails_dependency(self):
        # M4 semantics unchanged: a quota-failed card blocks its dependents
        # exactly like any other failed card — nothing is dispatched at all.
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "0"
        (self.cards / "a.md").write_text("task a")
        (self.cards / "b.md").write_text("depends_on: a.md\ntask b")
        code, summary, output = self.run_batch(hosts="b1", backend=None, model=None,
                                               backends_file=str(self.config))
        self.assertEqual(code, 1, output)
        rows = {row["card"]: row for row in summary["cards"]}
        self.assertEqual(rows["a.md"]["status"], "failed")
        self.assertIsNone(rows["a.md"].get("run_id"))
        self.assertEqual(rows["b.md"]["status"], "blocked")
        self.assertEqual(rows["b.md"]["blocked_by"], "a.md")
        self.assertIsNone(rows["b.md"].get("run_id"))
        self.assertEqual(self.start_events(), [])
        decisions = self.decision_entries()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["card"], "a.md")
        self.assertTrue(decisions[0]["quota_routing"]["all_exhausted"])

    def test_card_all_exhausted_fails_without_dispatch(self):
        code, summary, output = self.run_quota_batch("0", "0")
        self.assertEqual(code, 1, output)
        row = summary["cards"][0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("refusing to dispatch", row["summary"])
        self.assertIsNone(row.get("run_id"))
        decisions = self.decision_entries()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["status"], "unroutable")
        self.assertTrue(decisions[0]["quota_routing"]["all_exhausted"])
        self.assertEqual(decisions[0]["quota_routing"]["quota_before"], {"echo-low": 0, "echo-high": 0})
        self.assertEqual(self.start_events(), [])
        self.assertFalse(any("--detach" in cmd for _, cmd in self.calls))
