"""M33 slice 1 (#263): health gate on the auto-rework escalation pick.

End-to-end cmd_run tests with the fake pi executor and a fake agy for the
escalation path, plus verify scripts that fail until the escalated backend
fixes the task. The fake agy answers `agy models` according to
FAKE_AGY_MODE so the gate sees a real probe verdict (no mocking of the
gate itself): unset/healthy -> logged in, "logged-out" -> auth failure
(unhealthy), "inconclusive" -> non-auth failure (unknown). No network and
no real CLI.
"""
import argparse, contextlib, copy, io, json, os, shutil, stat, sys, tempfile, textwrap, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
POLICY = xs.load_policy()
POLICY["pi"].update({"provider": None, "default_model": None, "require_isolation": False})
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
    # The gate's login probe: mode is fixed per test via the environment.
    mode = os.environ.get("FAKE_AGY_MODE", "healthy")
    if mode == "logged-out":
        print("Error: not authenticated", file=sys.stderr)
        sys.exit(1)
    if mode == "inconclusive":
        print("boom", file=sys.stderr)
        sys.exit(1)
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
                prompt="add a function add(a, b)", task_file=None, title="escalation gate test", cwd=".",
                no_worktree=True, verify=None, verify_baseline=False, auto_rework=False,
                keep_worktree=False, no_fallback=False,
                budget_minutes=5, detach=False, run_id=None, dry_run=False,
                health_gate=False, no_health_gate=False, skip_health_gate=False)
    base.update(kw)
    return argparse.Namespace(**base)


def gate_policy(enabled=True, on_unknown="allow", pi_quota=None):
    policy = copy.deepcopy(POLICY_PI_AGY)
    policy["routing"]["health_gate"] = {"enabled": enabled, "timeout_seconds": 10,
                                        "on_unknown": on_unknown}
    if pi_quota is not None:
        # A readable pi quota makes the *initial* pi probe healthy (the
        # fixture's live MiniMax quota has no usable reading, so pi would
        # otherwise probe "unknown" and on_unknown=block would refuse the
        # initial pick before the run ever reaches escalation). Quota
        # routing stays disabled; the source is consulted by the health
        # probe only.
        policy["routing"]["quota"] = {"enabled": False, "threshold": 0,
                                      "sources": {"pi": {"source": "env", "var": "M33_PI_QUOTA"}}}
        os.environ["M33_PI_QUOTA"] = str(pi_quota)
    return policy


class EscalationGateRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-m33s1-")
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
                      "FAKE_AGY_LOG", "FAKE_AGY_MODE", "VERIFY_COUNT_FILE", "VERIFY_MODE", "M33_PI_QUOTA")}
        os.environ["OPENSWARM_PI_BIN"] = self.script
        os.environ["OPENSWARM_AGY_BIN"] = self.agy_script
        os.environ["OPENSWARM_STATE_FILE"] = _load.FIXTURE_STATE
        os.environ["FAKE_PI_LOG"] = self.log
        os.environ["FAKE_AGY_LOG"] = self.agy_log
        os.environ["FAKE_PI_MODE"] = "ok"
        os.environ["VERIFY_COUNT_FILE"] = self.count_file
        os.environ["VERIFY_MODE"] = "fail-until-fixed"
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
                           policy or POLICY_PI_AGY)
            except SystemExit as e:
                return e.code
        self.fail("cmd_run returned without exiting")

    def last_result(self):
        # The two files are written in different encodings by design:
        # Both files are UTF-8 (M34 slice 1, #269): result.json via jsave
        # and the ledger via append_ledger, so both are read as UTF-8.
        # (Before M34 the ledger was locale-encoded and this helper read
        # it with the default encoding; that split is gone.)
        with open(xs.LEDGER, encoding="utf-8") as f:
            entry = json.loads(f.readlines()[-1])
        with open(os.path.join(xs.RUNS, entry["id"], "result.json"), encoding="utf-8") as f:
            return entry, json.load(f)

    def pi_calls(self):
        if not os.path.exists(self.log):
            return 0
        with open(self.log) as fh:
            return len(fh.readlines())

    def agy_calls(self):
        # Only real dispatches (argv contains "-p"); the gate's `models`
        # probe is not one.
        if not os.path.exists(self.agy_log):
            return 0
        with open(self.agy_log) as fh:
            return len([line for line in fh
                        if line.strip() and "-p" in json.loads(line)])

    def test_healthy_escalation_dispatches_with_gate_record(self):
        self.assertEqual(self.dispatch(auto_rework=True, policy=gate_policy()), 0)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "done")
        esc = result["auto_rework"]["escalation"]
        self.assertEqual(esc["to_backend"], "agy")
        self.assertTrue(esc["recovered"])
        gate = esc["health_gate"]
        self.assertEqual(gate["stage"], "escalation")
        self.assertEqual(gate["backend"], "agy")
        self.assertEqual(gate["verdict"], "healthy")
        self.assertEqual(gate["action"], "allowed")
        self.assertEqual(entry["auto_rework"]["escalation"]["health_gate"]["verdict"], "healthy")
        self.assertEqual(self.agy_calls(), 1)
        self.assertEqual(self.pi_calls(), 3)

    def test_unhealthy_escalation_blocked_with_zero_exec(self):
        os.environ["FAKE_AGY_MODE"] = "logged-out"
        self.assertEqual(self.dispatch(auto_rework=True, policy=gate_policy()), 1)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["verify"]["rc"], 1)
        self.assertEqual(result["backend"], "pi")          # no silent reroute anywhere
        rework = result["auto_rework"]
        self.assertFalse(rework["recovered"])
        esc = rework["escalation"]
        self.assertEqual(esc["from_backend"], "pi")
        self.assertEqual(esc["to_backend"], "agy")
        self.assertIn("health gate", esc["blocked"])
        gate = esc["health_gate"]
        self.assertEqual(gate["stage"], "escalation")
        self.assertEqual(gate["verdict"], "unhealthy")
        self.assertEqual(gate["action"], "blocked")
        self.assertIn("not logged in", gate["reason"])
        self.assertFalse(esc["recovered"])
        self.assertEqual(entry["auto_rework"]["escalation"]["health_gate"]["verdict"], "unhealthy")
        self.assertEqual(entry["auto_rework"]["escalation"]["blocked"], esc["blocked"])
        self.assertEqual(self.agy_calls(), 0)              # zero exec attempts on agy
        self.assertEqual(self.pi_calls(), 3)               # initial + 2 retries, no silent fallback
        self.assertFalse(os.path.exists(os.path.join(result["run_dir"], "rework-prompt-escalation.md")))

    def test_unknown_allow_dispatches_with_record(self):
        os.environ["FAKE_AGY_MODE"] = "inconclusive"
        self.assertEqual(self.dispatch(auto_rework=True, policy=gate_policy(on_unknown="allow")), 0)
        entry, result = self.last_result()
        esc = result["auto_rework"]["escalation"]
        self.assertEqual(esc["to_backend"], "agy")
        self.assertTrue(esc["recovered"])
        gate = esc["health_gate"]
        self.assertEqual(gate["verdict"], "unknown")
        self.assertEqual(gate["action"], "allowed")
        self.assertIn("on_unknown=allow", gate["reason"])
        self.assertEqual(self.agy_calls(), 1)

    def test_unknown_block_refuses(self):
        os.environ["FAKE_AGY_MODE"] = "inconclusive"
        # pi_quota=0.5 keeps the initial pi pick healthy so the run reaches
        # escalation; the block must come from the escalation gate, not the
        # initial one.
        self.assertEqual(self.dispatch(auto_rework=True, policy=gate_policy(on_unknown="block", pi_quota=0.5)), 1)
        entry, result = self.last_result()
        self.assertEqual(result["health_gate"]["verdict"], "healthy")
        esc = result["auto_rework"]["escalation"]
        self.assertEqual(esc["to_backend"], "agy")
        self.assertFalse(esc["recovered"])
        gate = esc["health_gate"]
        self.assertEqual(gate["verdict"], "unknown")
        self.assertEqual(gate["action"], "blocked")
        self.assertIn("on_unknown=block", gate["reason"])
        self.assertEqual(self.agy_calls(), 0)
        self.assertEqual(self.pi_calls(), 3)

    def test_gate_off_escalation_unchanged(self):
        # Gate disabled: even an unhealthy target dispatches exactly as
        # before this slice — no health_gate key anywhere on the record.
        os.environ["FAKE_AGY_MODE"] = "logged-out"
        self.assertEqual(self.dispatch(auto_rework=True, policy=gate_policy(enabled=False)), 0)
        entry, result = self.last_result()
        esc = result["auto_rework"]["escalation"]
        self.assertEqual(esc["to_backend"], "agy")
        self.assertTrue(esc["recovered"])
        self.assertNotIn("health_gate", esc)
        self.assertNotIn("health_gate", result)
        self.assertEqual(self.agy_calls(), 1)

    def test_skip_flag_bypasses_with_record(self):
        os.environ["FAKE_AGY_MODE"] = "logged-out"
        self.assertEqual(self.dispatch(auto_rework=True, policy=gate_policy(),
                                      skip_health_gate=True), 0)
        entry, result = self.last_result()
        esc = result["auto_rework"]["escalation"]
        self.assertEqual(esc["to_backend"], "agy")
        self.assertTrue(esc["recovered"])
        gate = esc["health_gate"]
        self.assertEqual(gate["stage"], "escalation")
        self.assertEqual(gate["verdict"], "bypassed")
        self.assertEqual(gate["action"], "bypassed")
        self.assertEqual(self.agy_calls(), 1)

    def test_no_health_gate_flag_disables(self):
        os.environ["FAKE_AGY_MODE"] = "logged-out"
        self.assertEqual(self.dispatch(auto_rework=True, policy=gate_policy(),
                                      no_health_gate=True), 0)
        entry, result = self.last_result()
        esc = result["auto_rework"]["escalation"]
        self.assertEqual(esc["to_backend"], "agy")
        self.assertTrue(esc["recovered"])
        self.assertNotIn("health_gate", esc)
        self.assertEqual(self.agy_calls(), 1)

    def _route_canned_for_escalation(self, canned):
        """Patch route() only for the escalation call (exclude=["pi"]);
        the initial pick delegates to the real router."""
        real_route = xs.route

        def canned_route(prof, st, policy, exclude=(), custom_backends=()):
            if list(exclude) == ["pi"]:
                return dict(canned)
            return real_route(prof, st, policy, exclude=exclude, custom_backends=custom_backends)

        return mock.patch.object(xs, "route", side_effect=canned_route)

    def test_same_backend_skip_unchanged(self):
        canned = {"ok": True, "backend": "pi", "tier": "deep", "model": None}
        with self._route_canned_for_escalation(canned):
            self.assertEqual(self.dispatch(auto_rework=True, policy=gate_policy()), 1)
        entry, result = self.last_result()
        esc = result["auto_rework"]["escalation"]
        self.assertEqual(esc["skipped"], "router returned the same backend")
        self.assertEqual(esc["tier"], "deep")
        self.assertNotIn("health_gate", esc)
        self.assertEqual(self.agy_calls(), 0)
        self.assertEqual(self.pi_calls(), 3)

    def test_no_backend_skip_unchanged(self):
        canned = {"ok": False, "reasons": ["no working agy binary"],
                  "skipped": {}, "skipped_details": {}, "profile": {}}
        with self._route_canned_for_escalation(canned):
            self.assertEqual(self.dispatch(auto_rework=True, policy=gate_policy()), 1)
        entry, result = self.last_result()
        esc = result["auto_rework"]["escalation"]
        self.assertEqual(esc["skipped"], "no stronger backend available")
        self.assertEqual(esc["tier"], "deep")
        self.assertNotIn("health_gate", esc)
        self.assertEqual(self.agy_calls(), 0)
        self.assertEqual(self.pi_calls(), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
