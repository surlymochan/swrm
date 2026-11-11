"""M11 slice 1 (#142): pid identity check, dead-run reaping, #140 diagnose fix.

1. pid_alive with a recorded process start time rejects a recycled pid:
   the pid may be alive, but it is not the same process that ran the run.
2. A dead run is reaped to finished/exited so watch stops emitting
   BUDGET/STALL for it (first wake: EXITED, next wake: DONE, never
   BUDGET/STALL).
3. #140: diagnose_failure reports "none" for a run whose final status is
   done with verify passed, even when an earlier attempt still carries a
   residual error_class (run 20261109-201952-a3b247 isomorphism).
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _load

xs = _load.load()


class PidIdentityTests(unittest.TestCase):
    def test_live_pid_with_matching_start_is_alive(self):
        pid = os.getpid()
        start = xs.proc_start_time(pid)
        self.assertIsNotNone(start)
        self.assertTrue(xs.pid_alive(pid, start))
        self.assertTrue(xs.runner_alive({"runner_pid": pid, "runner_pid_start": start}))

    def test_live_pid_with_wrong_start_is_not_the_runner(self):
        # The pid is alive, but it is a different (recycled) process: the
        # recorded runner is dead. This is the #98 false-positive.
        pid = os.getpid()
        self.assertTrue(xs.pid_alive(pid))
        self.assertFalse(xs.pid_alive(pid, "proc:0"))
        self.assertFalse(xs.runner_alive({"runner_pid": pid, "runner_pid_start": "proc:0"}))

    def test_dead_pid_is_not_alive_with_or_without_identity(self):
        self.assertFalse(xs.pid_alive(999999))
        self.assertFalse(xs.pid_alive(999999, "proc:12345"))
        self.assertFalse(xs.runner_alive({"runner_pid": 999999,
                                         "runner_pid_start": "proc:12345"}))

    def test_missing_identity_falls_back_to_legacy_pid_check(self):
        # Runs recorded before start times were stamped have no identity:
        # the plain existence check still applies.
        pid = os.getpid()
        self.assertTrue(xs.runner_alive({"runner_pid": pid}))
        self.assertFalse(xs.runner_alive({"runner_pid": 999999}))
        self.assertFalse(xs.runner_alive({}))

    def test_set_status_stamps_runner_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            rdir = os.path.join(tmp, "run-stamp")
            os.makedirs(rdir)
            xs.set_status(rdir, state="running", runner_pid=os.getpid())
            saved = json.loads(Path(rdir, "status.json").read_text())
            self.assertEqual(saved["runner_pid_start"], xs.proc_start_time(os.getpid()))


class ReapDeadRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.cursor = self.root / "cursor.json"
        self.args = argparse.Namespace(run_id=["r1"], all=False, consumer=None,
                                       cursor_file=str(self.cursor), risk=[], label=None,
                                       from_end=False, stall_minutes=1)
        for name, value in (("RUNS", str(self.runs)), ("DATA", str(self.root))):
            patcher = mock.patch.object(xs, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_fixture(self, status):
        rdir = self.runs / "r1"
        rdir.mkdir(exist_ok=True)
        path = rdir / "events.jsonl"
        path.write_text("")
        xs.jsave(str(rdir / "status.json"), {"events": str(path), **status})
        return rdir

    def wake(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(xs.cmd_watch_wake(self.args), 0)
        return output.getvalue()

    def test_reap_marks_dead_run_finished_exited(self):
        rdir = self.run_fixture({"state": "running", "runner_pid": 999999})
        self.assertTrue(xs.reap_dead_run(str(rdir)))
        saved = json.loads((rdir / "status.json").read_text())
        self.assertEqual(saved["state"], "finished")
        self.assertEqual(saved["status"], "exited")
        self.assertEqual(saved["reaped"], "dead-runner")
        # Second reap is a no-op: the run is already terminal.
        self.assertFalse(xs.reap_dead_run(str(rdir)))

    def test_reap_refuses_recycled_pid_as_alive(self):
        # The pid is alive (this process) but the recorded identity is
        # someone else's: the recorded runner is dead and must be reaped.
        rdir = self.run_fixture({"state": "running", "runner_pid": os.getpid(),
                                 "runner_pid_start": "proc:0"})
        self.assertTrue(xs.reap_dead_run(str(rdir)))
        saved = json.loads((rdir / "status.json").read_text())
        self.assertEqual(saved["status"], "exited")

    def test_reap_leaves_live_runner_alone(self):
        pid = os.getpid()
        rdir = self.run_fixture({"state": "running", "runner_pid": pid,
                                 "runner_pid_start": xs.proc_start_time(pid)})
        self.assertFalse(xs.reap_dead_run(str(rdir)))
        saved = json.loads((rdir / "status.json").read_text())
        self.assertEqual(saved["state"], "running")

    def test_watch_exited_once_then_done_never_budget_or_stall(self):
        # Past deadline + dead runner: the first wake reports EXITED, the
        # run is reaped, and the next wake reports DONE -- BUDGET/STALL
        # are never emitted for the dead run.
        self.run_fixture({"state": "running", "runner_pid": 999999,
                          "deadline": 100, "budget_minutes": 1})
        first = self.wake()
        self.assertIn("EXITED:", first)
        self.assertNotIn("BUDGET:", first)
        self.assertNotIn("STALL:", first)
        saved = json.loads((self.runs / "r1" / "status.json").read_text())
        self.assertEqual((saved["state"], saved["status"]), ("finished", "exited"))
        second = self.wake()
        self.assertIn("DONE:", second)
        self.assertNotIn("BUDGET:", second)
        self.assertNotIn("STALL:", second)


class DiagnoseFinalSuccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m11-")
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

    def make_run(self, rid, status, result):
        rdir = self.data / "runs" / rid
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "status.json").write_text(json.dumps(status))
        if result is not None:
            (rdir / "result.json").write_text(json.dumps(result))
        return str(rdir)

    def test_a3b247_isomorphic_run_is_none_not_exec_error(self):
        # #140: final status done + verify rc 0, but an earlier attempt
        # still carries a residual exec_error. The final outcome wins.
        self.make_run("run-a3b247",
                      status={"state": "finished", "status": "done"},
                      result={"id": "run-a3b247", "status": "done", "verify": {"rc": 0},
                              "attempts": [
                                  {"backend": "codex", "rc": 1, "seconds": 45,
                                   "error_class": "exec_error",
                                   "error_tail": "Traceback: boom"},
                                  {"backend": "codex", "rc": 0, "seconds": 300,
                                   "error_class": None}]})
        out = xs.diagnose_failure("run-a3b247")
        self.assertEqual(out["category"], "none")

    def test_failed_run_with_exec_error_still_exec_error(self):
        # The reorder must not whitewash a genuinely failed run: when the
        # final status is failed, a residual attempt error still counts.
        self.make_run("run-still-failed",
                      status={"state": "finished", "status": "failed"},
                      result={"id": "run-still-failed", "status": "failed",
                              "verify": {"rc": 0},
                              "error_class": "exec_error",
                              "attempts": [{"backend": "codex", "rc": 1, "seconds": 45,
                                            "error_class": "exec_error",
                                            "error_tail": "Traceback: boom"}]})
        out = xs.diagnose_failure("run-still-failed")
        self.assertEqual(out["category"], "exec-error")

    def test_done_without_verify_still_none(self):
        self.make_run("run-done-noverify",
                      status={"state": "finished", "status": "done"},
                      result={"id": "run-done-noverify", "status": "done",
                              "attempts": [{"backend": "codex", "rc": 0, "seconds": 60,
                                            "error_class": None}]})
        out = xs.diagnose_failure("run-done-noverify")
        self.assertEqual(out["category"], "none")


if __name__ == "__main__":
    unittest.main(verbosity=2)
