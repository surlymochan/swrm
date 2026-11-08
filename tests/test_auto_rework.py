"""Auto rework on host verify failure (#93, slice 1 of #92).

Covers the pure builders plus an end-to-end cmd_run test with the fake pi
executor and a fail-once verify script: no network and no real CLI.
"""
import argparse, contextlib, io, json, os, shutil, stat, sys, tempfile, textwrap, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
POLICY = xs.load_policy()
POLICY["pi"].update({"provider": None, "default_model": None, "require_isolation": False})
FAKE = r"""#!/usr/bin/env python3
import json, os, sys
log = os.environ.get("FAKE_PI_LOG")
if log:
    with open(log, "a") as fh:
        json.dump(sys.argv, fh)
        fh.write("\n")
print(json.dumps({"type": "session", "id": "s1"}))
print(json.dumps({"type": "message_end", "message": {"role": "user", "content": [], "usage": {"input": 999}}}))
def end(text, usage, **extra):
    msg = {"role": "assistant", "content": [{"type": "text", "text": text}], "usage": usage}
    msg.update(extra)
    print(json.dumps({"type": "message_end", "message": msg}))
end("looking", {"input": 100, "output": 20, "cacheRead": 5, "cacheWrite": 1, "totalTokens": 126})
body = {"status": "done", "summary": "added add", "files_changed": ["add.py"],
        "tests": "not run: host verify", "followups": []}
end(json.dumps(body), {"input": 20, "output": 25, "cacheRead": 3, "cacheWrite": 1, "totalTokens": 49})
"""
VERIFY_SCRIPT = r"""#!/usr/bin/env python3
import os, sys
path = os.environ["VERIFY_COUNT_FILE"]
n = int(open(path).read()) if os.path.exists(path) else 0
open(path, "w").write(str(n + 1))
mode = os.environ.get("VERIFY_MODE", "pass")
if mode == "always-fail" or (mode == "fail-once" and n == 0):
    print("FAILED test_add")
    sys.exit(1)
sys.exit(0)
"""


def ns(**kw):
    base = dict(type="impl", difficulty=3, context_tokens=0, backend="pi", model=None, effort=None,
                prompt="add a function add(a, b)", task_file=None, title="auto rework test", cwd=".",
                no_worktree=True, verify=None, verify_baseline=False, auto_rework=False,
                keep_worktree=False, no_fallback=False,
                budget_minutes=5, detach=False, run_id=None, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


class AutoReworkBuilders(unittest.TestCase):
    def test_build_rework_task_carries_failure_evidence(self):
        task = xs.build_rework_task(
            "add a function add(a, b)",
            {"cmd": "pytest -q", "rc": 1, "failing": ["test_add"], "tail": "FAILED test_add\nassert 1 == 2"},
            attempt=1)
        self.assertIn("add a function add(a, b)", task)
        self.assertIn("pytest -q", task)
        self.assertIn("exit code: 1", task)
        self.assertIn("test_add", task)
        self.assertIn("assert 1 == 2", task)

    def test_eligible_only_on_real_failure_below_cap(self):
        self.assertTrue(xs.auto_rework_eligible({"rc": 1, "failing": ["test_add"]}, True, 0))
        self.assertFalse(xs.auto_rework_eligible({"rc": 1}, False, 0))       # flag off
        self.assertFalse(xs.auto_rework_eligible({"rc": 0}, True, 0))        # verify passed
        self.assertFalse(xs.auto_rework_eligible({"rc": 124}, True, 0))      # timeout is not a code failure
        self.assertFalse(xs.auto_rework_eligible(None, True, 0))             # verify never ran
        self.assertFalse(xs.auto_rework_eligible({"rc": 1}, True, 1))        # slice 1 caps at one rework


class AutoReworkRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-rework-")
        self.script = os.path.join(self.tmp, "pi")
        with open(self.script, "w") as f:
            f.write(textwrap.dedent(FAKE))
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self.verify_script = os.path.join(self.tmp, "verify.py")
        with open(self.verify_script, "w") as f:
            f.write(textwrap.dedent(VERIFY_SCRIPT))
        os.chmod(self.verify_script, os.stat(self.verify_script).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.tmp, "argv.json")
        self.count_file = os.path.join(self.tmp, "verify-count")
        self.cwd = os.path.join(self.tmp, "work")
        os.makedirs(self.cwd)
        self._env = {k: os.environ.get(k) for k in
                     ("OPENSWARM_PI_BIN", "OPENSWARM_STATE_FILE", "FAKE_PI_MODE", "FAKE_PI_LOG",
                      "VERIFY_COUNT_FILE", "VERIFY_MODE")}
        os.environ["OPENSWARM_PI_BIN"] = self.script
        os.environ["OPENSWARM_STATE_FILE"] = _load.FIXTURE_STATE
        os.environ["FAKE_PI_LOG"] = self.log
        os.environ["FAKE_PI_MODE"] = "ok"
        os.environ["VERIFY_COUNT_FILE"] = self.count_file
        os.environ["VERIFY_MODE"] = "pass"
        if os.path.exists(xs.BREAKERS):
            os.remove(xs.BREAKERS)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dispatch(self, policy=None, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                xs.cmd_run(ns(cwd=self.cwd, verify=sys.executable + " " + self.verify_script, **kw),
                           policy or POLICY)
            except SystemExit as e:
                return e.code
        self.fail("cmd_run returned without exiting")

    def last_result(self):
        with open(xs.LEDGER) as f:
            entry = json.loads(f.readlines()[-1])
        with open(os.path.join(xs.RUNS, entry["id"], "result.json")) as f:
            return entry, json.load(f)

    def pi_calls(self):
        if not os.path.exists(self.log):
            return 0
        with open(self.log) as fh:
            return len(fh.readlines())

    def test_fail_once_recovers_with_one_rework(self):
        os.environ["VERIFY_MODE"] = "fail-once"
        self.assertEqual(self.dispatch(auto_rework=True), 0)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["verify"]["rc"], 0)
        rework = result["auto_rework"]
        self.assertEqual(rework["attempts"], 1)
        self.assertTrue(rework["recovered"])
        self.assertEqual(rework["first_verify"], {"rc": 1, "failing": ["test_add"]})
        self.assertEqual(rework["rework_verify"], {"rc": 0, "failing": []})
        self.assertEqual(entry["auto_rework"]["attempts"], 1)
        self.assertTrue(entry["auto_rework"]["recovered"])
        self.assertEqual(self.pi_calls(), 2)
        with open(os.path.join(result["run_dir"], "rework-prompt.md")) as fh:
            prompt = fh.read()
        self.assertIn("test_add", prompt)
        self.assertIn(self.verify_script, prompt)
        self.assertIn("add a function add(a, b)", prompt)

    def test_flag_off_does_not_rework(self):
        os.environ["VERIFY_MODE"] = "always-fail"
        self.assertEqual(self.dispatch(auto_rework=False), 1)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["verify"]["rc"], 1)
        self.assertNotIn("auto_rework", result)
        self.assertNotIn("auto_rework", entry)
        self.assertEqual(self.pi_calls(), 1)

    def test_verify_pass_first_time_dispatches_no_rework(self):
        self.assertEqual(self.dispatch(auto_rework=True), 0)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["auto_rework"]["attempts"], 0)
        self.assertEqual(result["auto_rework"]["skipped"], "verify passed or timed out")
        self.assertFalse(result["auto_rework"]["recovered"])
        self.assertEqual(self.pi_calls(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
