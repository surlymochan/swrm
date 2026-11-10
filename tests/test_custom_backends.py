"""Declarative custom backends (M3 slice 1, #101).

A CLI agent joins routing from one JSON config file — no code changes:
config format + strict validation + a generic CLI executor wired into
`route` + `run`, proven by a fake echo CLI end-to-end dispatch.
Run: python3 -m unittest discover -s tests   (or: python3 tests/test_custom_backends.py)
"""
import argparse, contextlib, copy, io, json, os, shutil, stat, subprocess, sys, tempfile, textwrap, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
xs.breakers = lambda: {}  # isolate from any breakers on disk
xs.load_policy = lambda: xs.jload(f"{xs.SKILL_DIR}/policy.json", {})  # ignore policy.local.json overrides

POLICY = xs.load_policy()
POLICY["codex"]["require_default_model"] = False
with open(_load.FIXTURE_STATE) as f:
    BASE = json.load(f)

ECHO_CFG = {"id": "echo", "name": "Echo CLI (test backend)",
            "command": ["python3", "fake-echo-cli.py", "--prompt", "{prompt}",
                        "--workdir", "{worktree}", "--model", "{model}"],
            "env": {"ECHO_MODEL": "echo-1"}, "quota_source": "none", "model": "echo-1"}

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


def cfg(**overrides):
    out = copy.deepcopy(ECHO_CFG)
    out.update(overrides)
    return out


def doc(*backends):
    return {"backends": list(backends)}


class ValidateConfig(unittest.TestCase):
    def check_valid(self, data):
        out = xs.validate_custom_backends(data, "test-backends.json")
        self.assertEqual(len(out), 1)
        return out[0]

    def check_error(self, data, *needles):
        with self.assertRaises(xs.CustomBackendConfigError) as cm:
            xs.validate_custom_backends(data, "test-backends.json")
        for needle in needles:
            self.assertIn(needle, str(cm.exception))

    def test_valid_minimal_gets_defaults(self):
        got = self.check_valid(doc({"id": "echo", "command": ["echo", "{prompt}"],
                                    "quota_source": "none"}))
        self.assertEqual(got["id"], "echo")
        self.assertEqual(got["name"], "echo")
        self.assertEqual(got["env"], {})
        self.assertIsNone(got["model"])

    def test_valid_full_config_kept(self):
        got = self.check_valid(doc(ECHO_CFG))
        self.assertEqual(got["model"], "echo-1")
        self.assertEqual(got["env"], {"ECHO_MODEL": "echo-1"})

    def test_missing_backends_key(self):
        self.check_error({}, "backends")

    def test_backends_not_a_list(self):
        self.check_error({"backends": {"id": "echo"}}, "backends", "list")

    def test_entry_not_an_object(self):
        self.check_error(doc("echo"), "object")

    def test_missing_id(self):
        bad = cfg()
        del bad["id"]
        self.check_error(doc(bad), "id")

    def test_bad_id_characters(self):
        self.check_error(doc(cfg(id="Echo CLI")), "id")

    def test_id_collision_with_builtin(self):
        for taken in ("codex", "pi", "hermes", "gemini"):
            self.check_error(doc(cfg(id=taken)), taken, "built-in")

    def test_duplicate_ids(self):
        self.check_error(doc(cfg(), cfg()), "duplicate")

    def test_missing_command(self):
        bad = cfg()
        del bad["command"]
        self.check_error(doc(bad), "command")

    def test_empty_command(self):
        self.check_error(doc(cfg(command=[])), "command")

    def test_command_without_prompt_placeholder(self):
        self.check_error(doc(cfg(command=["echo", "hi"])), "{prompt}")

    def test_command_non_string_token(self):
        self.check_error(doc(cfg(command=["echo", 42, "{prompt}"])), "command")

    def test_unknown_quota_source(self):
        self.check_error(doc(cfg(quota_source="magic")), "quota_source", "none")

    def test_quota_env_needs_var(self):
        self.check_error(doc(cfg(quota_source="env")), "quota_env_var")
        self.check_valid(doc(cfg(quota_source="env", quota_env_var="ECHO_REMAINING")))

    def test_quota_command_needs_command(self):
        self.check_error(doc(cfg(quota_source="command")), "quota_command")
        self.check_valid(doc(cfg(quota_source="command", quota_command=["echo", "5"])))

    def test_quota_file_needs_file(self):
        self.check_error(doc(cfg(quota_source="file")), "quota_file")
        self.check_valid(doc(cfg(quota_source="file", quota_file="/tmp/quota.json")))

    def test_quota_field_for_other_source_rejected(self):
        self.check_error(doc(cfg(quota_source="none", quota_file="/tmp/q.json")),
                         "quota_file", "quota_source")

    def test_unknown_field_rejected(self):
        self.check_error(doc(cfg(model_name="echo-1")), "model_name", "unknown")

    def test_env_must_be_string_map(self):
        self.check_error(doc(cfg(env={"A": 1})), "env")


class LoadConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-backends-load-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_missing_default_file_is_no_custom_backends(self):
        self.assertEqual(xs.load_custom_backends(policy=POLICY), [])

    def test_explicit_missing_file_is_an_error(self):
        with self.assertRaises(xs.CustomBackendConfigError) as cm:
            xs.load_custom_backends(policy=POLICY, explicit=os.path.join(self.tmp, "nope.json"))
        self.assertIn("not found", str(cm.exception))

    def test_malformed_json_is_an_actionable_error(self):
        path = self.write("bad.json", "{not json")
        with self.assertRaises(xs.CustomBackendConfigError) as cm:
            xs.load_custom_backends(policy=POLICY, explicit=path)
        self.assertIn("bad.json", str(cm.exception))

    def test_relative_binary_resolves_against_config_dir(self):
        script = os.path.join(self.tmp, "fake-echo-cli.py")
        with open(script, "w") as fh:
            fh.write(FAKE_ECHO_CLI)
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        path = self.write("backends.json", json.dumps(doc(cfg())))
        loaded = xs.load_custom_backends(policy=POLICY, explicit=path)
        self.assertEqual(loaded[0]["command"][1], script)


class PlanCustomBackend(unittest.TestCase):
    def test_missing_binary_is_unavailable_with_reason(self):
        bad = cfg(command=["definitely-not-a-real-binary-xyz", "{prompt}"])
        d, why = xs.plan_custom_backend(xs.validate_custom_backends(doc(bad), "t.json")[0],
                                        {"type": "impl", "difficulty": 3}, copy.deepcopy(BASE),
                                        POLICY, True, [])
        self.assertIsNone(d)
        self.assertIn("definitely-not-a-real-binary-xyz", why)

    def test_quota_none_with_working_binary_routes(self):
        good = xs.validate_custom_backends(doc(cfg(command=[sys.executable, "{prompt}"])), "t.json")[0]
        d, why = xs.plan_custom_backend(good, {"type": "impl", "difficulty": 3},
                                        copy.deepcopy(BASE), POLICY, True, [])
        self.assertIsNotNone(d, why)
        self.assertEqual(d["backend"], "echo")
        self.assertEqual(d["model"], "echo-1")
        self.assertIs(d["custom_backend"], good)

    def test_quota_env_missing_var_is_unavailable(self):
        os.environ.pop("ECHO_REMAINING_TEST", None)
        good = xs.validate_custom_backends(
            doc(cfg(command=[sys.executable, "{prompt}"], quota_source="env",
                    quota_env_var="ECHO_REMAINING_TEST")), "t.json")[0]
        d, why = xs.plan_custom_backend(good, {"type": "impl", "difficulty": 3},
                                        copy.deepcopy(BASE), POLICY, True, [])
        self.assertIsNone(d)
        self.assertIn("ECHO_REMAINING_TEST", why)

    def test_quota_env_positive_var_is_available(self):
        os.environ["ECHO_REMAINING_TEST"] = "7"
        try:
            good = xs.validate_custom_backends(
                doc(cfg(command=[sys.executable, "{prompt}"], quota_source="env",
                        quota_env_var="ECHO_REMAINING_TEST")), "t.json")[0]
            d, why = xs.plan_custom_backend(good, {"type": "impl", "difficulty": 3},
                                            copy.deepcopy(BASE), POLICY, True, [])
            self.assertIsNotNone(d, why)
        finally:
            os.environ.pop("ECHO_REMAINING_TEST", None)

    def test_quota_file_zero_remaining_is_unavailable(self):
        tmp = tempfile.mkdtemp(prefix="openswarm-quota-file-")
        try:
            qfile = os.path.join(tmp, "quota.json")
            with open(qfile, "w") as fh:
                json.dump({"remaining": 0}, fh)
            good = xs.validate_custom_backends(
                doc(cfg(command=[sys.executable, "{prompt}"], quota_source="file",
                        quota_file=qfile)), "t.json")[0]
            d, why = xs.plan_custom_backend(good, {"type": "impl", "difficulty": 3},
                                            copy.deepcopy(BASE), POLICY, True, [])
            self.assertIsNone(d)
            self.assertIn("remaining", why)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RouteCustomBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-backends-route-")
        self.script = os.path.join(self.tmp, "fake-echo-cli.py")
        with open(self.script, "w") as fh:
            fh.write(FAKE_ECHO_CLI)
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        config = os.path.join(self.tmp, "backends.json")
        with open(config, "w") as fh:
            json.dump(doc(cfg()), fh)
        self.custom = xs.load_custom_backends(policy=POLICY, explicit=config)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_explicit_custom_backend_routes(self):
        st = copy.deepcopy(BASE)
        r = xs.route({"type": "impl", "difficulty": 3, "backend": "echo"}, st, POLICY,
                     custom_backends=self.custom)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["backend"], "echo")
        self.assertEqual(r["model"], "echo-1")

    def test_unknown_backend_is_unroutable_not_a_crash(self):
        st = copy.deepcopy(BASE)
        r = xs.route({"type": "impl", "difficulty": 3, "backend": "nosuch"}, st, POLICY,
                     custom_backends=self.custom)
        self.assertFalse(r["ok"])
        self.assertIn("nosuch", json.dumps(r.get("skipped", {})))

    def test_custom_backend_not_in_automatic_order(self):
        st = copy.deepcopy(BASE)
        r = xs.route({"type": "impl", "difficulty": 3}, st, POLICY, custom_backends=self.custom)
        self.assertTrue(r["ok"], r)
        self.assertNotEqual(r["backend"], "echo")


class ExecCustomCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-backends-exec-")
        self.wd = os.path.join(self.tmp, "work")
        os.makedirs(self.wd)
        self.rdir = os.path.join(self.tmp, "run")
        os.makedirs(self.rdir)
        self.script = os.path.join(self.tmp, "fake-echo-cli.py")
        with open(self.script, "w") as fh:
            fh.write(FAKE_ECHO_CLI)
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self.decision = {"backend": "echo", "model": "echo-1",
                         "custom_backend": xs.validate_custom_backends(
                             doc(cfg(command=[sys.executable, self.script, "--prompt", "{prompt}",
                                            "--workdir", "{worktree}", "--model", "{model}"])),
                             "t.json")[0]}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_echo_cli_runs_and_reports_done(self):
        evp = os.path.join(self.rdir, "events.1.echo.jsonl")
        rc, final, meta, err = xs.exec_custom_cli(
            self.decision, copy.deepcopy(BASE), "add hello.txt", self.wd, self.rdir, 60, evp)
        self.assertEqual(rc, 0, err)
        self.assertEqual((final or {}).get("status"), "done")
        with open(os.path.join(self.wd, "echo-output.txt")) as fh:
            body = fh.read()
        self.assertIn("add hello.txt", body)
        self.assertIn("echo-1", body)
        self.assertEqual(meta.get("custom_backend"), "echo")


def ns(**kw):
    base = dict(type="impl", difficulty=3, context_tokens=0, backend="echo", model=None, effort=None,
                prompt="add a file echo-output.txt with a greeting", task_file=None,
                title="custom backend e2e", cwd=".", scope="repo", base=None, no_worktree=False,
                verify="test -f echo-output.txt", verify_baseline=False, verify_evalid=False,
                evalid_bin=None, auto_rework=False, keep_worktree=False, no_fallback=False,
                budget_minutes=5, detach=False, run_id=None, dry_run=False, read_only=False,
                card_type=None, rework_of=None, paths=[], no_auto_review=True, backends_file=None)
    base.update(kw)
    return argparse.Namespace(**base)


class CustomBackendRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-backends-e2e-")
        self.script = os.path.join(self.tmp, "fake-echo-cli.py")
        with open(self.script, "w") as fh:
            fh.write(FAKE_ECHO_CLI)
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self.config = os.path.join(self.tmp, "backends.json")
        with open(self.config, "w") as fh:
            json.dump(doc(cfg(command=[sys.executable, self.script, "--prompt", "{prompt}",
                                       "--workdir", "{worktree}", "--model", "{model}"])), fh)
        self.cwd = os.path.join(self.tmp, "work")
        os.makedirs(self.cwd)
        subprocess.run(["git", "init", "-q"], cwd=self.cwd, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.cwd, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=self.cwd, check=True)
        with open(os.path.join(self.cwd, "README.md"), "w") as fh:
            fh.write("# demo\n")
        subprocess.run(["git", "add", "."], cwd=self.cwd, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.cwd, check=True)
        self._env = {k: os.environ.get(k) for k in ("OPENSWARM_STATE_FILE",)}
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

    def dispatch(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                xs.cmd_run(ns(cwd=self.cwd, backends_file=self.config, **kw), POLICY)
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
    def test_echo_backend_dispatches_verify_and_ledgers(self):
        self.assertEqual(self.dispatch(), 0)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result.get("backend"), "echo")
        self.assertEqual(entry.get("backend"), "echo")
        branch = result.get("branch")
        self.assertTrue(branch)
        shown = subprocess.run(["git", "show", f"{branch}:echo-output.txt"], cwd=self.cwd,
                               capture_output=True, text=True, check=True).stdout
        self.assertIn("add a file echo-output.txt", shown)

    def test_invalid_config_refuses_to_run(self):
        bad = os.path.join(self.tmp, "bad-backends.json")
        with open(bad, "w") as fh:
            json.dump(doc(cfg(command=["echo", "hi"])), fh)
        with contextlib.redirect_stdout(io.StringIO()):
            # A bad config is a startup failure: the run refuses before
            # routing (ValueError -> exit 2 at the CLI), never silently
            # degrades to the built-ins.
            with self.assertRaises(xs.CustomBackendConfigError) as cm:
                xs.cmd_run(ns(cwd=self.cwd, backends_file=bad), POLICY)
        self.assertIn("{prompt}", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
