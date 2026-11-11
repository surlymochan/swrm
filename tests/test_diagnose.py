"""openswarm diagnose (M10 slice 1, #137): failure classification + evidence pointers.

Read-only: diagnose never dispatches, retries, or mutates a run. Every
category must be backed by explicit evidence; when the evidence cannot pin
a cause the category is "unknown", never a guess. A run whose only failure
signal is a dead runner pid is "unknown" with a suspected pid-reuse note
(#98): pid_alive can false-positive when the pid has been recycled.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

import _load

xs = _load.load()
ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "openswarm"


class DiagnoseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-diagnose-")
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        (self.data / "runs").mkdir(parents=True)
        self.constants = {key: getattr(xs, key) for key in ("DATA", "LEDGER", "RUNS", "HOME")}
        xs.DATA = str(self.data)
        xs.LEDGER = str(self.data / "ledger.jsonl")
        xs.RUNS = str(self.data / "runs")
        xs.HOME = str(self.root / "home")

    def tearDown(self):
        for key, value in self.constants.items():
            setattr(xs, key, value)
        self.tmp.cleanup()

    def make_run(self, rid, status=None, result=None, ledger=None, events=None):
        rdir = self.data / "runs" / rid
        rdir.mkdir(parents=True, exist_ok=True)
        if status is not None:
            (rdir / "status.json").write_text(json.dumps(status))
        if result is not None:
            (rdir / "result.json").write_text(json.dumps(result))
        if ledger is not None:
            with open(xs.LEDGER, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": rid, **ledger}) + "\n")
        if events is not None:
            (rdir / "events.1.codex.jsonl").write_text(
                "".join(json.dumps({"msg": line}) + "\n" for line in events))
        return str(rdir)

    def test_timeout_from_budget_exceeded(self):
        self.make_run("run-timeout", status={"state": "finished", "status": "budget_exceeded"},
                      result={"id": "run-timeout", "status": "budget_exceeded",
                              "error_class": "budget_exceeded",
                              "attempts": [{"backend": "codex", "rc": 124, "seconds": 1800,
                                            "error_class": "budget_exceeded"}]})
        out = xs.diagnose_failure("run-timeout")
        self.assertEqual(out["category"], "timeout")
        self.assertTrue(any(e.get("rc") == 124 for e in out["evidence"]))

    def test_quota_from_rate_limit_attempt(self):
        self.make_run("run-quota", status={"state": "finished", "status": "failed"},
                      result={"id": "run-quota", "status": "failed", "error_class": "rate_limit",
                              "attempts": [{"backend": "codex", "rc": 75, "seconds": 12,
                                            "error_class": "rate_limit",
                                            "error_tail": "429 resource exhausted"}]})
        out = xs.diagnose_failure("run-quota")
        self.assertEqual(out["category"], "quota")

    def test_quota_from_log_when_result_says_exec_error(self):
        # The executor record alone says exec_error, but the event log carries
        # the explicit 429 signal: quota wins because it is the named cause.
        self.make_run("run-quota-log", status={"state": "finished", "status": "failed"},
                      result={"id": "run-quota-log", "status": "failed", "error_class": "exec_error",
                              "attempts": [{"backend": "codex", "rc": 1, "seconds": 30,
                                            "error_class": "exec_error", "error_tail": "exit 1"}]},
                      events=["starting executor", "429 Too Many Requests: quota exceeded"])
        out = xs.diagnose_failure("run-quota-log")
        self.assertEqual(out["category"], "quota")
        self.assertTrue(any(e["source"].startswith("events.") for e in out["evidence"]))

    def test_verify_fail_when_executor_succeeded(self):
        self.make_run("run-verify", status={"state": "finished", "status": "failed"},
                      result={"id": "run-verify", "status": "failed",
                              "verify": {"rc": 1, "cmd": "pytest -q"},
                              "verify_note": "host verify failed",
                              "attempts": [{"backend": "codex", "rc": 0, "seconds": 300,
                                            "error_class": None}],
                              "result": {"status": "done", "summary": "did the work"}})
        out = xs.diagnose_failure("run-verify")
        self.assertEqual(out["category"], "verify-fail")
        self.assertTrue(any("verify" in e["pointer"] for e in out["evidence"]))

    def test_exec_error_from_nonzero_rc(self):
        self.make_run("run-exec", status={"state": "finished", "status": "failed"},
                      result={"id": "run-exec", "status": "failed", "error_class": "exec_error",
                              "attempts": [{"backend": "codex", "rc": 1, "seconds": 45,
                                            "error_class": "exec_error",
                                            "error_tail": "Traceback: boom"}]})
        out = xs.diagnose_failure("run-exec")
        self.assertEqual(out["category"], "exec-error")
        self.assertTrue(any(e.get("rc") == 1 for e in out["evidence"]))

    def test_runner_error_from_worktree_unavailable(self):
        self.make_run("run-runner", status={"state": "finished", "status": "failed",
                                            "error_class": "worktree_unavailable"},
                      result={"id": "run-runner", "status": "failed",
                              "error_class": "worktree_unavailable", "attempts": []})
        out = xs.diagnose_failure("run-runner")
        self.assertEqual(out["category"], "runner-error")

    def test_unknown_when_only_signal_is_dead_pid(self):
        # #98: pid_alive false-positives on a recycled pid, so a dead runner
        # pid with no result, no ledger entry and no log signal must not be
        # misread as an executor or runner failure.
        self.make_run("run-deadpid", status={"state": "running", "status": None,
                                             "runner_pid": 999999})
        out = xs.diagnose_failure("run-deadpid")
        self.assertEqual(out["category"], "unknown")
        self.assertIn("pid", (out.get("suspected") or "").lower())

    def test_unknown_when_failed_but_no_evidence(self):
        self.make_run("run-noevidence", status={"state": "finished", "status": "failed"},
                      result={"id": "run-noevidence", "status": "failed", "attempts": []})
        out = xs.diagnose_failure("run-noevidence")
        self.assertEqual(out["category"], "unknown")
        self.assertEqual(out["evidence"], [])

    def test_none_for_successful_run(self):
        self.make_run("run-ok", status={"state": "finished", "status": "done"},
                      result={"id": "run-ok", "status": "done", "verify": {"rc": 0},
                              "attempts": [{"backend": "codex", "rc": 0, "seconds": 60,
                                            "error_class": None}]})
        out = xs.diagnose_failure("run-ok")
        self.assertEqual(out["category"], "none")

    def test_ledger_entry_used_when_result_missing(self):
        self.make_run("run-ledger", status={"state": "finished", "status": "failed"},
                      ledger={"status": "failed", "error_class": "rate_limit",
                              "attempts": [{"backend": "codex", "rc": 75,
                                            "error_class": "rate_limit"}]})
        out = xs.diagnose_failure("run-ledger")
        self.assertEqual(out["category"], "quota")
        self.assertTrue(any(e["source"] == "ledger.jsonl" for e in out["evidence"]))

    def test_missing_run_raises(self):
        with self.assertRaises(ValueError):
            xs.diagnose_failure("run-does-not-exist")

    def test_cli_diagnose_prints_structured_json(self):
        self.make_run("run-cli", status={"state": "finished", "status": "budget_exceeded"},
                      result={"id": "run-cli", "status": "budget_exceeded",
                              "error_class": "budget_exceeded",
                              "attempts": [{"backend": "codex", "rc": 124, "seconds": 900,
                                            "error_class": "budget_exceeded"}]})
        env = dict(os.environ, OPENSWARM_HOME=str(self.data))
        proc = subprocess.run([sys.executable, str(CLI), "diagnose", "run-cli"],
                              capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
        out = json.loads(proc.stdout.decode())
        self.assertEqual(out["run_id"], "run-cli")
        self.assertEqual(out["category"], "timeout")
        self.assertIn("evidence", out)

    def test_cli_missing_run_exits_2(self):
        env = dict(os.environ, OPENSWARM_HOME=str(self.data))
        proc = subprocess.run([sys.executable, str(CLI), "diagnose", "run-nope"],
                              capture_output=True, env=env)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
