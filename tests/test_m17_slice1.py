"""M17 slice 1 (#169): quota probe + reroute core + guardrails.

`openswarm quota` lists each backend's remaining quota (read-only);
`openswarm run` reroutes off a default backend whose configured remaining
quota is at or below routing.quota.threshold, records the decision
(quota_before, reason, both remainings) in ledger/result.json, and refuses
to dispatch when every routable backend is exhausted. Explicit --backend
always wins; a failed quota read keeps the old behaviour on the record.
Run: python3 -m unittest discover -s tests   (or: python3 tests/test_m17_slice1.py)
"""
import argparse, contextlib, copy, io, json, os, shutil, stat, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
xs.breakers = lambda: {}  # isolate from any breakers on disk
xs.load_policy = lambda: xs.jload(f"{xs.SKILL_DIR}/policy.json", {})  # ignore policy.local.json overrides

POLICY = xs.load_policy()
POLICY["codex"]["require_default_model"] = False
with open(_load.FIXTURE_STATE) as f:
    BASE = json.load(f)

LOW_VAR = "OPENSWARM_M17_TEST_LOW"
HIGH_VAR = "OPENSWARM_M17_TEST_HIGH"

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


def cfg(command, **kw):
    entry = {"id": "echo-low", "command": command, "env": {}, "quota_source": "none",
             "model": "echo-low"}
    entry.update(kw)
    return entry


def quota_policy(low_source, high_source, enabled=True, threshold=0):
    policy = copy.deepcopy(POLICY)
    policy["routing"]["default_order"] = ["echo-low", "echo-high"]
    policy["routing"]["weighted"] = False
    policy["routing"]["quota"] = {
        "enabled": enabled, "threshold": threshold,
        "sources": {"echo-low": low_source, "echo-high": high_source}}
    return policy


def echo_backends(script):
    low = dict(ECHO_CFG, command=[sys.executable, script, "--prompt", "{prompt}",
                                  "--workdir", "{worktree}", "--model", "{model}"])
    high = dict(HIGH_CFG, command=[sys.executable, script, "--prompt", "{prompt}",
                                   "--workdir", "{worktree}", "--model", "{model}"])
    return [low, high]


class QuotaSourceReading(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (LOW_VAR, HIGH_VAR)}
        self.tmp = tempfile.mkdtemp(prefix="openswarm-m17-src-")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_source_reads_remaining(self):
        os.environ[LOW_VAR] = "5"
        remaining, error = xs.quota_remaining_from_source({"source": "env", "var": LOW_VAR})
        self.assertEqual((remaining, error), (5, None))
        os.environ[LOW_VAR] = "0"
        remaining, error = xs.quota_remaining_from_source({"source": "env", "var": LOW_VAR})
        self.assertEqual((remaining, error), (0, None))

    def test_env_source_failures_are_errors_not_zero(self):
        os.environ.pop(LOW_VAR, None)
        remaining, error = xs.quota_remaining_from_source({"source": "env", "var": LOW_VAR})
        self.assertIsNone(remaining)
        self.assertIn("is not set", error)
        os.environ[LOW_VAR] = "plenty"
        remaining, error = xs.quota_remaining_from_source({"source": "env", "var": LOW_VAR})
        self.assertIsNone(remaining)
        self.assertIn("is not a number", error)

    def test_file_source_json_and_plain(self):
        path = os.path.join(self.tmp, "quota.json")
        with open(path, "w") as fh:
            fh.write('{"remaining": 3}')
        self.assertEqual(xs.quota_remaining_from_source({"source": "file", "file": path}), (3, None))
        with open(path, "w") as fh:
            fh.write("7\n")
        self.assertEqual(xs.quota_remaining_from_source({"source": "file", "file": path}), (7, None))
        remaining, error = xs.quota_remaining_from_source(
            {"source": "file", "file": os.path.join(self.tmp, "missing.json")})
        self.assertIsNone(remaining)
        self.assertIn("unreadable", error)

    def test_command_source_json_and_plain(self):
        self.assertEqual(xs.quota_remaining_from_source(
            {"source": "command", "command": [sys.executable, "-c", "print(4)"]}), (4, None))
        self.assertEqual(xs.quota_remaining_from_source(
            {"source": "command", "command": [sys.executable, "-c", "print('{\"remaining\": 2}')"]}),
            (2, None))
        remaining, error = xs.quota_remaining_from_source({"source": "command", "command": ["false"]})
        self.assertIsNone(remaining)
        self.assertIn("exited 1", error)

    def test_none_source_is_unknown_not_zero(self):
        self.assertEqual(xs.quota_remaining_from_source({"source": "none"}), (None, None))

    def test_quota_config_validation(self):
        base = {"routing": {}}
        cfg = xs.quota_config(base)
        self.assertEqual(cfg, {"enabled": False, "threshold": 0, "sources": {},
                               "snapshot_max_age_minutes": 60})  # M23 #193 default
        with self.assertRaises(ValueError):
            xs.quota_config({"routing": {"quota": {"enabled": True, "threshold": -1}}})
        with self.assertRaises(ValueError):
            xs.quota_config({"routing": {"quota": {"enabled": True, "threshold": 0,
                                                   "sources": {"codex": {"source": "smoke"}}}}})
        with self.assertRaises(ValueError):
            xs.quota_config({"routing": {"quota": {"enabled": True, "threshold": 0,
                                                   "sources": {"codex": {"source": "env"}}}}})
        with self.assertRaises(ValueError):
            xs.quota_config({"routing": {"quota": {"enabled": False, "typo": 1}}})
        with self.assertRaises(ValueError):
            xs.quota_config({"routing": {"quota": {"snapshot_max_age_minutes": 0}}})
        self.assertEqual(xs.quota_config(
            {"routing": {"quota": {"snapshot_max_age_minutes": 45}}})["snapshot_max_age_minutes"], 45)

    def test_custom_backend_quota_keeps_old_semantics(self):
        # The refactor must not change (available, why) for custom backends.
        os.environ[LOW_VAR] = "0"
        self.assertEqual(xs.custom_backend_quota(
            {"id": "x", "quota_source": "env", "quota_env_var": LOW_VAR}),
            (False, f"quota exhausted ({LOW_VAR} remaining is 0.0)"))
        os.environ.pop(LOW_VAR, None)
        available, why = xs.custom_backend_quota(
            {"id": "x", "quota_source": "env", "quota_env_var": LOW_VAR})
        self.assertFalse(available)
        self.assertIn("is not set", why)
        self.assertEqual(xs.custom_backend_quota({"id": "x", "quota_source": "none"}), (True, None))


class QuotaProbe(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (LOW_VAR, HIGH_VAR)}
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        self.tmp = tempfile.mkdtemp(prefix="openswarm-m17-probe-")
        self.script = os.path.join(self.tmp, "fake-echo-cli.py")
        with open(self.script, "w") as fh:
            fh.write(FAKE_ECHO_CLI)
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self.config = os.path.join(self.tmp, "backends.json")
        with open(self.config, "w") as fh:
            json.dump(doc(*echo_backends(self.script)), fh)
        self.backends = xs.load_custom_backends(
            xs.load_policy(), explicit=self.config)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def probe(self, policy, **kw):
        args = argparse.Namespace(backends_file=self.config, json=kw.get("json", False))
        buf = io.StringIO()
        code = None
        with contextlib.redirect_stdout(buf):
            try:
                xs.cmd_quota(args, policy, custom_backends=self.backends)
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue()

    def test_probe_lists_remaining_and_unknown(self):
        policy = quota_policy({"source": "env", "var": LOW_VAR},
                              {"source": "env", "var": HIGH_VAR})
        code, out = self.probe(policy)
        self.assertIsNone(code)
        self.assertIn("echo-low", out)
        self.assertIn("low", out)
        self.assertIn("echo-high", out)
        self.assertIn("ok", out)
        self.assertIn("codex", out)
        self.assertIn("unknown", out)

    def test_probe_json_shape(self):
        policy = quota_policy({"source": "env", "var": LOW_VAR},
                              {"source": "env", "var": HIGH_VAR})
        code, out = self.probe(policy, json=True)
        self.assertIsNone(code)
        data = json.loads(out)
        self.assertTrue(data["quota_routing_enabled"])
        self.assertEqual(data["threshold"], 0)
        rows = {row["backend"]: row for row in data["backends"]}
        self.assertEqual(rows["echo-low"]["remaining"], 0)
        self.assertEqual(rows["echo-low"]["status"], "low")
        self.assertEqual(rows["echo-high"]["remaining"], 5)
        self.assertEqual(rows["echo-high"]["status"], "ok")
        self.assertEqual(rows["codex"]["status"], "unknown")

    def test_probe_exits_1_on_read_error(self):
        policy = quota_policy({"source": "command", "command": ["false"]},
                              {"source": "env", "var": HIGH_VAR})
        code, out = self.probe(policy)
        self.assertEqual(code, 1)
        self.assertIn("error", out)
        self.assertIn("exited 1", out)


class QuotaRoutingApply(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (LOW_VAR, HIGH_VAR)}
        self.tmp = tempfile.mkdtemp(prefix="openswarm-m17-apply-")
        self.script = os.path.join(self.tmp, "fake-echo-cli.py")
        with open(self.script, "w") as fh:
            fh.write(FAKE_ECHO_CLI)
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self.config = os.path.join(self.tmp, "backends.json")
        with open(self.config, "w") as fh:
            json.dump(doc(*echo_backends(self.script)), fh)
        self.backends = xs.load_custom_backends(xs.load_policy(), explicit=self.config)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def decide(self, policy):
        prof = {"type": "impl", "difficulty": 3}
        decision = xs.route(prof, copy.deepcopy(BASE), policy, custom_backends=self.backends)
        self.assertTrue(decision["ok"], decision)
        return xs.apply_quota_routing(prof, copy.deepcopy(BASE), policy, decision,
                                      custom_backends=self.backends)

    def test_reroute_records_both_remainings(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy({"source": "env", "var": LOW_VAR},
                              {"source": "env", "var": HIGH_VAR})
        decision, info = self.decide(policy)
        self.assertEqual(decision["backend"], "echo-high")
        self.assertTrue(info["applied"])
        self.assertEqual(info["from"], "echo-low")
        self.assertEqual(info["to"], "echo-high")
        self.assertEqual(info["quota_before"], {"echo-low": 0, "echo-high": 5})
        self.assertIn("threshold 0", info["reason"])
        # The rerouted decision carries its own model, not the old backend's.
        self.assertEqual(decision.get("model"), "echo-high")

    def test_no_reroute_when_sufficient(self):
        os.environ[LOW_VAR] = "5"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy({"source": "env", "var": LOW_VAR},
                              {"source": "env", "var": HIGH_VAR})
        decision, info = self.decide(policy)
        self.assertEqual(decision["backend"], "echo-low")
        self.assertFalse(info["applied"])
        self.assertEqual(info["quota_before"], {"echo-low": 5})

    def test_explicit_backend_always_wins(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy({"source": "env", "var": LOW_VAR},
                              {"source": "env", "var": HIGH_VAR})
        prof = {"type": "impl", "difficulty": 3, "backend": "echo-low"}
        decision = xs.route(prof, copy.deepcopy(BASE), policy, custom_backends=self.backends)
        decision, info = xs.apply_quota_routing(prof, copy.deepcopy(BASE), policy, decision,
                                                custom_backends=self.backends)
        self.assertEqual(decision["backend"], "echo-low")
        self.assertFalse(info["applied"])
        self.assertIn("explicit --backend wins", info["reason"])

    def test_disabled_is_a_no_op(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy({"source": "env", "var": LOW_VAR},
                              {"source": "env", "var": HIGH_VAR}, enabled=False)
        decision, info = self.decide(policy)
        self.assertEqual(decision["backend"], "echo-low")
        self.assertIsNone(info)

    def test_all_exhausted_is_flagged_not_dispatched(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "0"
        policy = quota_policy({"source": "env", "var": LOW_VAR},
                              {"source": "env", "var": HIGH_VAR})
        decision, info = self.decide(policy)
        self.assertEqual(decision["backend"], "echo-low")
        self.assertTrue(info["all_exhausted"])
        self.assertEqual(info["quota_before"], {"echo-low": 0, "echo-high": 0})

    def test_read_failure_keeps_old_behaviour_on_the_record(self):
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy({"source": "command", "command": ["false"]},
                              {"source": "env", "var": HIGH_VAR})
        decision, info = self.decide(policy)
        self.assertEqual(decision["backend"], "echo-low")
        self.assertFalse(info["applied"])
        self.assertIn("echo-low", info["quota_error"])
        self.assertIn("exited 1", info["quota_error"]["echo-low"])


def ns(**kw):
    base = dict(type="impl", difficulty=3, context_tokens=0, backend=None, model=None, effort=None,
                prompt="add a file echo-output.txt with a greeting", task_file=None,
                title="quota e2e", cwd=".", scope="repo", base=None, no_worktree=False,
                verify="test -f echo-output.txt", verify_baseline=False, verify_evalid=False,
                evalid_bin=None, auto_rework=False, keep_worktree=False, no_fallback=False,
                budget_minutes=5, detach=False, run_id=None, dry_run=False, read_only=False,
                card_type=None, rework_of=None, paths=[], no_auto_review=True, backends_file=None,
                executor=None)
    base.update(kw)
    return argparse.Namespace(**base)


class QuotaRunE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-m17-e2e-")
        self.script = os.path.join(self.tmp, "fake-echo-cli.py")
        with open(self.script, "w") as fh:
            fh.write(FAKE_ECHO_CLI)
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self.config = os.path.join(self.tmp, "backends.json")
        with open(self.config, "w") as fh:
            json.dump(doc(*echo_backends(self.script)), fh)
        self.cwd = os.path.join(self.tmp, "work")
        os.makedirs(self.cwd)
        subprocess.run(["git", "init", "-q"], cwd=self.cwd, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.cwd, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=self.cwd, check=True)
        with open(os.path.join(self.cwd, "README.md"), "w") as fh:
            fh.write("# demo\n")
        subprocess.run(["git", "add", "."], cwd=self.cwd, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.cwd, check=True)
        self._env = {k: os.environ.get(k) for k in ("OPENSWARM_STATE_FILE", LOW_VAR, HIGH_VAR)}
        os.environ["OPENSWARM_STATE_FILE"] = _load.FIXTURE_STATE
        if os.path.exists(xs.BREAKERS):
            os.remove(xs.BREAKERS)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dispatch(self, policy, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                xs.cmd_run(ns(cwd=self.cwd, backends_file=self.config, **kw), policy)
            except SystemExit as e:
                return e.code
        self.fail("cmd_run returned without exiting")

    def last_result(self):
        with open(xs.LEDGER) as f:
            entry = json.loads(f.readlines()[-1])
        with open(os.path.join(xs.RUNS, entry["id"], "result.json")) as f:
            return entry, json.load(f)

    @unittest.skipIf(os.name == "nt", "worktree dispatch relies on POSIX symlinks and shell prepare steps; "
                                       "Windows git/symlink semantics differ (same reason as test_workspace)")
    def test_reroute_dispatches_to_quota_backend_and_ledgers(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy({"source": "env", "var": LOW_VAR},
                              {"source": "env", "var": HIGH_VAR})
        self.assertEqual(self.dispatch(policy), 0)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result.get("backend"), "echo-high")
        self.assertEqual(entry.get("backend"), "echo-high")
        quota = result.get("quota_routing") or {}
        self.assertTrue(quota.get("applied"))
        self.assertEqual(quota.get("quota_before"), {"echo-low": 0, "echo-high": 5})
        self.assertEqual(entry.get("quota_routing"), quota)
        branch = result.get("branch")
        self.assertTrue(branch)
        shown = subprocess.run(["git", "show", f"{branch}:echo-output.txt"], cwd=self.cwd,
                               capture_output=True, text=True, check=True).stdout
        self.assertIn("add a file echo-output.txt", shown)

    @unittest.skipIf(os.name == "nt", "worktree dispatch relies on POSIX symlinks and shell prepare steps; "
                                       "Windows git/symlink semantics differ (same reason as test_workspace)")
    def test_sufficient_quota_stays_on_default(self):
        os.environ[LOW_VAR] = "5"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy({"source": "env", "var": LOW_VAR},
                              {"source": "env", "var": HIGH_VAR})
        self.assertEqual(self.dispatch(policy), 0)
        _, result = self.last_result()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result.get("backend"), "echo-low")
        self.assertFalse((result.get("quota_routing") or {}).get("applied", False))

    @unittest.skipIf(os.name == "nt", "worktree dispatch relies on POSIX symlinks and shell prepare steps; "
                                       "Windows git/symlink semantics differ (same reason as test_workspace)")
    def test_explicit_backend_wins_end_to_end(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy({"source": "env", "var": LOW_VAR},
                              {"source": "env", "var": HIGH_VAR})
        self.assertEqual(self.dispatch(policy, backend="echo-low"), 0)
        _, result = self.last_result()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result.get("backend"), "echo-low")
        self.assertIn("explicit --backend wins", (result.get("quota_routing") or {}).get("reason", ""))

    def test_all_exhausted_fails_loudly_without_dispatch(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "0"
        policy = quota_policy({"source": "env", "var": LOW_VAR},
                              {"source": "env", "var": HIGH_VAR})
        self.assertEqual(self.dispatch(policy), 2)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "unroutable")
        self.assertEqual(result.get("error_class"), "quota_exhausted")
        self.assertEqual(entry.get("error_class"), "quota_exhausted")
        self.assertTrue((result.get("quota_routing") or {}).get("all_exhausted"))
        self.assertIsNone(result.get("branch"))

    @unittest.skipIf(os.name == "nt", "worktree dispatch relies on POSIX symlinks and shell prepare steps; "
                                       "Windows git/symlink semantics differ (same reason as test_workspace)")
    def test_read_failure_falls_back_to_old_behaviour(self):
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy({"source": "command", "command": ["false"]},
                              {"source": "env", "var": HIGH_VAR})
        self.assertEqual(self.dispatch(policy), 0)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result.get("backend"), "echo-low")
        quota = result.get("quota_routing") or {}
        self.assertFalse(quota.get("applied", False))
        self.assertIn("echo-low", quota.get("quota_error", {}))

    @unittest.skipIf(os.name == "nt", "worktree dispatch relies on POSIX symlinks and shell prepare steps; "
                                       "Windows git/symlink semantics differ (same reason as test_workspace)")
    def test_disabled_quota_leaves_no_trace(self):
        os.environ[LOW_VAR] = "0"
        os.environ[HIGH_VAR] = "5"
        policy = quota_policy({"source": "env", "var": LOW_VAR},
                              {"source": "env", "var": HIGH_VAR}, enabled=False)
        self.assertEqual(self.dispatch(policy), 0)
        _, result = self.last_result()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result.get("backend"), "echo-low")
        self.assertNotIn("quota_routing", result)


if __name__ == "__main__":
    unittest.main()
