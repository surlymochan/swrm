"""Auto rework on host verify failure (#93, slices 1-2 of #92).

Covers the pure builders plus end-to-end cmd_run tests with the fake pi
executor (and a fake agy for the escalation path) and verify scripts that
fail once, fail twice, or fail until the escalated backend fixes the task:
no network and no real CLI.
"""
import argparse, contextlib, copy, io, json, os, shutil, stat, sys, tempfile, textwrap, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
POLICY = xs.load_policy()
POLICY["pi"].update({"provider": None, "default_model": None, "require_isolation": False})
POLICY_PI_ONLY = copy.deepcopy(POLICY)
POLICY_PI_ONLY["routing"]["default_order"] = ["pi"]
POLICY_PI_AGY = copy.deepcopy(POLICY)
POLICY_PI_AGY["routing"]["default_order"] = ["pi", "agy"]
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
FAKE_AGY = r"""#!/usr/bin/env python3
import json, os, sys
log = os.environ.get("FAKE_AGY_LOG")
if log:
    with open(log, "a") as fh:
        json.dump(sys.argv[1:], fh)
        fh.write("\n")
if sys.argv[1:2] == ["models"]:
    print("gemini-3.8-flash-high")
    print("gemini-3.8-flash-medium")
    sys.exit(0)
# The escalated backend actually fixes the task: leave the marker the verify
# script waits for, in the worktree cwd.
with open("fixed.txt", "w") as fh:
    fh.write("fixed by the escalated backend\n")
body = {"status": "done", "summary": "fixed add after escalation", "files_changed": ["fixed.txt"],
        "tests": "not run: host verify", "followups": []}
print(json.dumps({"type": "init", "conversation_id": "conv-esc"}))
print(json.dumps({"type": "result", "conversation_id": "conv-esc", "status": "SUCCESS",
                  "response": json.dumps(body), "duration_seconds": 1.0, "num_turns": 1,
                  "usage": {"input_tokens": 50, "output_tokens": 20, "thinking_tokens": 0,
                            "cache_read_tokens": 0, "total_tokens": 70}}))
"""
VERIFY_SCRIPT = r"""#!/usr/bin/env python3
import os, sys
path = os.environ["VERIFY_COUNT_FILE"]
n = int(open(path).read()) if os.path.exists(path) else 0
open(path, "w").write(str(n + 1))
mode = os.environ.get("VERIFY_MODE", "pass")
if mode == "always-fail" or (mode == "fail-once" and n == 0) or (mode == "fail-twice" and n < 2):
    print("FAILED test_add")
    sys.exit(1)
if mode == "fail-until-fixed" and not os.path.exists("fixed.txt"):
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
        self.assertIn("attempt 1/2", task)

    def test_build_rework_task_marks_escalation(self):
        task = xs.build_rework_task(
            "add a function add(a, b)",
            {"cmd": "pytest -q", "rc": 1, "failing": ["test_add"], "tail": "FAILED test_add"},
            attempt=3, escalated_from="pi")
        self.assertIn("escalated rework after 2/2 failed retries", task)
        self.assertIn("escalated", task.lower())
        self.assertIn("pi", task)

    def test_max_attempts_flag_forms(self):
        self.assertEqual(xs.auto_rework_max_attempts(True), 2)    # bare flag: default 2
        self.assertEqual(xs.auto_rework_max_attempts(1), 1)
        self.assertEqual(xs.auto_rework_max_attempts(3), 3)
        self.assertEqual(xs.auto_rework_max_attempts(False), 0)   # flag off
        self.assertEqual(xs.auto_rework_max_attempts(0), 0)
        self.assertEqual(xs.auto_rework_max_attempts(None), 0)

    def test_eligible_only_on_real_failure_below_cap(self):
        self.assertTrue(xs.auto_rework_eligible({"rc": 1, "failing": ["test_add"]}, True, 0))
        self.assertTrue(xs.auto_rework_eligible({"rc": 1}, True, 1))         # default cap is 2
        self.assertFalse(xs.auto_rework_eligible({"rc": 1}, True, 2))        # cap reached
        self.assertTrue(xs.auto_rework_eligible({"rc": 1}, 3, 2))
        self.assertFalse(xs.auto_rework_eligible({"rc": 1}, 3, 3))
        self.assertFalse(xs.auto_rework_eligible({"rc": 1}, 1, 1))           # explicit --auto-rework 1
        self.assertFalse(xs.auto_rework_eligible({"rc": 1}, False, 0))       # flag off
        self.assertFalse(xs.auto_rework_eligible({"rc": 0}, True, 0))        # verify passed
        self.assertFalse(xs.auto_rework_eligible({"rc": 124}, True, 0))      # timeout is not a code failure
        self.assertFalse(xs.auto_rework_eligible(None, True, 0))             # verify never ran

    def test_escalation_profile_goes_one_tier_stronger(self):
        prof = {"type": "impl", "difficulty": 3, "backend": "pi", "model": "m", "context_tokens": 0}
        esc, tier = xs.escalation_profile(prof, "balanced")
        self.assertEqual(tier, "deep")
        self.assertEqual(esc["difficulty"], 5)
        self.assertIsNone(esc["backend"])
        self.assertIsNone(esc["model"])
        esc, tier = xs.escalation_profile(prof, "fast")
        self.assertEqual((tier, esc["difficulty"]), ("balanced", 4))
        esc, tier = xs.escalation_profile(prof, "deep")
        self.assertEqual((tier, esc["difficulty"]), ("deep", 5))             # already deep: switch backend
        esc, tier = xs.escalation_profile(prof, None)
        self.assertEqual((tier, esc["difficulty"]), ("deep", 5))


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
        self.agy_script = os.path.join(self.tmp, "agy")
        with open(self.agy_script, "w") as f:
            f.write(textwrap.dedent(FAKE_AGY))
        os.chmod(self.agy_script, os.stat(self.agy_script).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.tmp, "argv.json")
        self.agy_log = os.path.join(self.tmp, "agy-argv.json")
        self.count_file = os.path.join(self.tmp, "verify-count")
        self.cwd = os.path.join(self.tmp, "work")
        os.makedirs(self.cwd)
        self._env = {k: os.environ.get(k) for k in
                     ("OPENSWARM_PI_BIN", "OPENSWARM_AGY_BIN", "OPENSWARM_STATE_FILE", "FAKE_PI_MODE", "FAKE_PI_LOG",
                      "FAKE_AGY_LOG", "VERIFY_COUNT_FILE", "VERIFY_MODE")}
        os.environ["OPENSWARM_PI_BIN"] = self.script
        os.environ["OPENSWARM_AGY_BIN"] = self.agy_script
        os.environ["OPENSWARM_STATE_FILE"] = _load.FIXTURE_STATE
        os.environ["FAKE_PI_LOG"] = self.log
        os.environ["FAKE_AGY_LOG"] = self.agy_log
        os.environ["FAKE_PI_MODE"] = "ok"
        os.environ["VERIFY_COUNT_FILE"] = self.count_file
        os.environ["VERIFY_MODE"] = "pass"
        xs._AGY_LOGIN_CACHE.clear()
        if os.path.exists(xs.BREAKERS):
            os.remove(xs.BREAKERS)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        xs._AGY_LOGIN_CACHE.clear()
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

    def agy_calls(self):
        # Only real dispatches (argv contains "-p"); the login probe ("models") is not one.
        if not os.path.exists(self.agy_log):
            return 0
        with open(self.agy_log) as fh:
            return len([line for line in fh
                        if line.strip() and "-p" in json.loads(line)])

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

    def test_fail_twice_recovers_with_two_reworks(self):
        os.environ["VERIFY_MODE"] = "fail-twice"
        self.assertEqual(self.dispatch(auto_rework=True), 0)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["verify"]["rc"], 0)
        rework = result["auto_rework"]
        self.assertEqual(rework["attempts"], 2)
        self.assertEqual(rework["max_attempts"], 2)
        self.assertTrue(rework["recovered"])
        self.assertEqual(rework["first_verify"], {"rc": 1, "failing": ["test_add"]})
        self.assertEqual(rework["verifies"],
                         [{"attempt": 1, "rc": 1, "failing": ["test_add"]},
                          {"attempt": 2, "rc": 0, "failing": []}])
        self.assertEqual(rework["rework_verify"], {"rc": 0, "failing": []})
        self.assertNotIn("escalation", rework)
        self.assertEqual(entry["auto_rework"]["attempts"], 2)
        self.assertEqual(self.pi_calls(), 3)
        for n in (1, 2):
            with open(os.path.join(result["run_dir"], f"rework-prompt-{n}.md")) as fh:
                prompt = fh.read()
            self.assertIn("test_add", prompt)
            self.assertIn(f"attempt {n}/2", prompt)
        with open(os.path.join(result["run_dir"], "rework-prompt.md")) as fh:
            self.assertIn("attempt 2/2", fh.read())

    def test_explicit_one_keeps_single_retry_and_skips_escalation(self):
        os.environ["VERIFY_MODE"] = "fail-twice"
        self.assertEqual(self.dispatch(auto_rework=1, policy=POLICY_PI_ONLY), 1)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["verify"]["rc"], 1)
        rework = result["auto_rework"]
        self.assertEqual(rework["attempts"], 1)
        self.assertEqual(rework["max_attempts"], 1)
        self.assertFalse(rework["recovered"])
        self.assertEqual(rework["escalation"]["skipped"], "no stronger backend available")
        self.assertEqual(rework["escalation"]["tier"], "deep")
        self.assertEqual(self.pi_calls(), 2)

    def test_always_fail_escalates_to_stronger_backend(self):
        os.environ["VERIFY_MODE"] = "fail-until-fixed"
        self.assertEqual(self.dispatch(auto_rework=True, policy=POLICY_PI_AGY), 0)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["verify"]["rc"], 0)
        rework = result["auto_rework"]
        self.assertEqual(rework["attempts"], 2)
        self.assertFalse(rework["verifies"][1]["rc"] == 0)
        esc = rework["escalation"]
        self.assertEqual(esc["from_backend"], "pi")
        self.assertEqual(esc["to_backend"], "agy")
        self.assertEqual(esc["tier"], "deep")
        self.assertEqual(esc["verify"], {"rc": 0, "failing": []})
        self.assertTrue(esc["recovered"])
        self.assertTrue(rework["recovered"])
        self.assertEqual(result["backend"], "agy")
        self.assertEqual(entry["auto_rework"]["escalation"]["to_backend"], "agy")
        self.assertEqual(self.pi_calls(), 3)
        self.assertEqual(self.agy_calls(), 1)
        self.assertTrue(os.path.exists(os.path.join(result["run_dir"], "rework-prompt-escalation.md")))
        with open(os.path.join(result["run_dir"], "rework-prompt-escalation.md")) as fh:
            prompt = fh.read()
        self.assertIn("escalated rework after 2/2 failed retries", prompt)
        self.assertIn("test_add", prompt)

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
