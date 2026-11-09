"""--verify-evalid mode (M2 slice 1, #99).

Claim -> evalid clean-clone replay -> ledger verdict, wired onto the
existing --verify path. No network and no real evalid checkout needed:
the evalid side is a fake `xflow audit` script; the real-evalid proof is
the end-to-end demo run recorded in the slice PR.
"""
import argparse, contextlib, copy, io, json, os, shutil, stat, subprocess, sys, tempfile, textwrap, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
POLICY = xs.load_policy()
POLICY_AGY_ONLY = copy.deepcopy(POLICY)
POLICY_AGY_ONLY["routing"]["default_order"] = ["agy"]

FAKE_AUDIT = r"""#!/usr/bin/env python3
import json, os, sys
verdict = os.environ.get("FAKE_EVALID_VERDICT", "verified")
report = {"ok": verdict == "verified", "verdict": verdict,
          "exitCode": {"verified": 0, "mismatch": 1, "unverifiable": 1, "no_claims": 3}.get(verdict, 1),
          "claims": [{"command": "node --test tests/test_calc.mjs", "claimedExit": 0,
                      "replay": {"exit": 0 if verdict == "verified" else 1}, "match": verdict == "verified"}],
          "seals": [], "setup": {"deps": "none"}}
print(json.dumps(report))
sys.exit(report["exitCode"])
"""

FAKE_AGY = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
if sys.argv[1:2] == ["models"]:
    print("gemini-3.8-flash-medium")
    sys.exit(0)
os.makedirs("tests", exist_ok=True)
with open("calc.mjs", "w") as fh:
    fh.write("export function add(a, b) { return a + b; }\n")
with open("tests/test_calc.mjs", "w") as fh:
    fh.write("import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
             "import { add } from '../calc.mjs';\n"
             "test('add sums', () => { assert.equal(add(1, 2), 3); });\n")
proc = subprocess.run([sys.executable, "-c",
                       "import subprocess,sys; sys.exit(subprocess.run(['node', '--test', 'tests/test_calc.mjs'], capture_output=True).returncode)"])
body = {"status": "done", "summary": "added add with a node test",
        "files_changed": ["calc.mjs", "tests/test_calc.mjs"],
        "tests": "Ran `node --test tests/test_calc.mjs` — all pass (exit 0)",
        "followups": []}
print(json.dumps({"type": "result", "conversation_id": "conv-demo", "status": "SUCCESS",
                  "response": json.dumps(body), "usage": {}}))
"""


def ns(**kw):
    base = dict(type="impl", difficulty=3, context_tokens=0, backend="agy", model=None, effort=None,
                prompt="add a function add(a, b) with a test", task_file=None, title="verify-evalid test",
                cwd=".", scope="repo", base=None, no_worktree=False, verify=None, verify_baseline=False,
                verify_evalid=False, evalid_bin=None, auto_rework=False, keep_worktree=False,
                no_fallback=False, budget_minutes=5, detach=False, run_id=None, dry_run=False,
                read_only=False, card_type=None, rework_of=None, paths=[], no_auto_review=True)
    base.update(kw)
    return argparse.Namespace(**base)


class ResolveEvalidBin(unittest.TestCase):
    def test_missing_everywhere_is_an_explicit_error(self):
        argv, source = xs.resolve_evalid_bin(sibling_root="/nonexistent-dir-for-test")
        self.assertIsNone(argv)
        self.assertIn("evalid", source.lower())

    def test_path_xflow_is_found(self):
        tmp = tempfile.mkdtemp(prefix="openswarm-evalid-bin-")
        try:
            # Windows only executes PATHEXT names (a bare extensionless
            # script is invisible to which()); the fixture carries .cmd there.
            name = "xflow.cmd" if os.name == "nt" else "xflow"
            fake = os.path.join(tmp, name)
            with open(fake, "w") as fh:
                fh.write("@echo off\r\necho fake\r\n" if os.name == "nt" else "#!/bin/sh\necho fake\n")
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = tmp + os.pathsep + old_path
            try:
                argv, source = xs.resolve_evalid_bin(sibling_root="/nonexistent-dir-for-test")
            finally:
                os.environ["PATH"] = old_path
            self.assertEqual(argv, [fake])
            self.assertEqual(source, "PATH")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explicit_js_checkout_runs_via_node(self):
        argv, source = xs.resolve_evalid_bin(
            explicit="/nonexistent/evalid/bin/xflow.js", sibling_root="/nonexistent-dir-for-test")
        self.assertIsNone(argv)
        tmp = tempfile.mkdtemp(prefix="openswarm-evalid-js-")
        try:
            os.makedirs(os.path.join(tmp, "bin"))
            js = os.path.join(tmp, "bin", "xflow.js")
            with open(js, "w") as fh:
                fh.write("console.log('fake');\n")
            argv, source = xs.resolve_evalid_bin(explicit=js, sibling_root="/nonexistent-dir-for-test")
            self.assertEqual(argv, [shutil.which("node"), js])
            self.assertEqual(source, "--evalid-bin")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BuildEvalidClaims(unittest.TestCase):
    def test_done_claims_the_verify_command_with_exit_zero(self):
        text = xs.build_evalid_claims("node --test tests/test_calc.mjs", "done",
                                      "Ran `node --test tests/test_calc.mjs` — all pass (exit 0)")
        self.assertIn("`node --test tests/test_calc.mjs`", text)
        self.assertIn("exit 0", text)
        self.assertIn("all pass", text)

    def test_non_done_status_claims_no_pass(self):
        # No pass is fabricated for a non-done executor: the claims text is
        # just the executor's own report, so audit can only check what the
        # executor itself claimed (nothing here -> no_claims -> error).
        text = xs.build_evalid_claims("node --test tests/test_calc.mjs", "partial", "not run: executor stopped")
        self.assertNotIn("claimed exit 0", text)
        self.assertNotIn("`node --test tests/test_calc.mjs`", text)
        self.assertIn("not run: executor stopped", text)


class VerdictMapping(unittest.TestCase):
    def test_audit_verdicts_map_to_pass_mismatch_error(self):
        self.assertEqual(xs.evalid_verdict_from_audit({"verdict": "verified"}), "pass")
        self.assertEqual(xs.evalid_verdict_from_audit({"verdict": "mismatch"}), "mismatch")
        for bad in ("unverifiable", "no_claims", "misuse", "weird", None):
            self.assertEqual(xs.evalid_verdict_from_audit({"verdict": bad}), "error")


class RunEvalidVerify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-evalid-run-")
        self.audit = os.path.join(self.tmp, "fake-xflow")
        with open(self.audit, "w") as fh:
            fh.write(textwrap.dedent(FAKE_AUDIT))
        os.chmod(self.audit, os.stat(self.audit).st_mode | stat.S_IEXEC)
        self._env = {k: os.environ.get(k) for k in ("FAKE_EVALID_VERDICT",)}
        os.environ["FAKE_EVALID_VERDICT"] = "verified"

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, verdict):
        os.environ["FAKE_EVALID_VERDICT"] = verdict
        return xs.run_evalid_verify("node --test tests/test_calc.mjs", "/tmp", "base0", "head0",
                                    "done", "Ran `node --test tests/test_calc.mjs` — all pass (exit 0)",
                                    timeout=60, evalid_bin=self.audit)

    def test_verified_maps_to_pass_with_claim_and_replay(self):
        out = self.call("verified")
        self.assertEqual(out["verdict"], "pass")
        self.assertIn("node --test tests/test_calc.mjs", out["claim"])
        self.assertIn("replay", out)
        self.assertEqual(out["replay"]["claims"][0]["match"], True)

    def test_mismatch_maps_to_mismatch(self):
        out = self.call("mismatch")
        self.assertEqual(out["verdict"], "mismatch")
        self.assertEqual(out["replay"]["claims"][0]["match"], False)

    def test_unverifiable_maps_to_error(self):
        out = self.call("no_claims")
        self.assertEqual(out["verdict"], "error")
        self.assertIn("no_claims", out.get("error", ""))

    def test_missing_bin_is_an_explicit_error_not_a_pass(self):
        out = xs.run_evalid_verify("node --test tests/test_calc.mjs", "/tmp", "base0", "head0",
                                   "done", "all pass", timeout=60,
                                   evalid_bin="/nonexistent-dir-for-test/xflow")
        self.assertEqual(out["verdict"], "error")
        self.assertIn("evalid", out.get("error", "").lower())


class VerifyEvalidRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-evalid-e2e-")
        self.agy_script = os.path.join(self.tmp, "agy")
        with open(self.agy_script, "w") as fh:
            fh.write(textwrap.dedent(FAKE_AGY))
        os.chmod(self.agy_script, os.stat(self.agy_script).st_mode | stat.S_IEXEC)
        self.audit = os.path.join(self.tmp, "fake-xflow")
        with open(self.audit, "w") as fh:
            fh.write(textwrap.dedent(FAKE_AUDIT))
        os.chmod(self.audit, os.stat(self.audit).st_mode | stat.S_IEXEC)
        self.cwd = os.path.join(self.tmp, "work")
        os.makedirs(self.cwd)
        subprocess.run(["git", "init", "-q"], cwd=self.cwd, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.cwd, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=self.cwd, check=True)
        with open(os.path.join(self.cwd, "package.json"), "w") as fh:
            fh.write('{"name": "demo", "type": "module"}\n')
        subprocess.run(["git", "add", "."], cwd=self.cwd, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.cwd, check=True)
        self._env = {k: os.environ.get(k) for k in
                     ("OPENSWARM_AGY_BIN", "OPENSWARM_STATE_FILE", "FAKE_EVALID_VERDICT")}
        os.environ["OPENSWARM_AGY_BIN"] = self.agy_script
        os.environ["OPENSWARM_STATE_FILE"] = _load.FIXTURE_STATE
        os.environ["FAKE_EVALID_VERDICT"] = "verified"
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

    def dispatch(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                xs.cmd_run(ns(cwd=self.cwd, verify="node --test tests/test_calc.mjs",
                              verify_evalid=True, evalid_bin=self.audit, **kw),
                           POLICY_AGY_ONLY)
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
    def test_pass_verdict_lands_in_ledger_and_keeps_done(self):
        self.assertEqual(self.dispatch(), 0)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "done")
        ve = entry.get("verify_evalid") or result.get("verify_evalid")
        self.assertIsNotNone(ve)
        self.assertEqual(ve["verdict"], "pass")
        self.assertIn("node --test tests/test_calc.mjs", ve["claim"])
        self.assertIn("replay", ve)

    @unittest.skipIf(os.name == "nt", "worktree dispatch relies on POSIX symlinks and shell prepare steps; "
                                       "Windows git/symlink semantics differ (same reason as test_workspace)")
    def test_mismatch_verdict_downgrades_done_to_partial(self):
        os.environ["FAKE_EVALID_VERDICT"] = "mismatch"
        self.assertEqual(self.dispatch(), 1)
        entry, result = self.last_result()
        self.assertEqual(result["status"], "partial")
        ve = entry.get("verify_evalid") or result.get("verify_evalid")
        self.assertEqual(ve["verdict"], "mismatch")

    def test_verify_evalid_without_verify_is_a_usage_error(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                xs.cmd_run(ns(cwd=self.cwd, verify=None, verify_evalid=True,
                              evalid_bin=self.audit), POLICY_AGY_ONLY)
        self.assertNotEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
