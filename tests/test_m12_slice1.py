"""M12 slice 1 (#146): fleet health check core.

`fleet health` reports one alive/dead/zombie verdict per non-terminal run,
with evidence pointers (ledger line, pid identity, last activity), reusing
M11's identity-checked runner_alive(). Read-only: it never reaps, retries,
dispatches, or mutates a run (reaping is slice 2, #145).
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _load

xs = _load.load()

# Process start-time identity is unreadable on platforms without /proc or
# `ps -o lstart=` (Windows): pid_alive there falls back to the legacy
# existence check by design, so identity-dependent tests skip there.
IDENTITY_SUPPORTED = xs.proc_start_time(os.getpid()) is not None
needs_identity = unittest.skipUnless(
    IDENTITY_SUPPORTED,
    "process start-time identity is unreadable on this platform; "
    "pid check falls back to the legacy existence test")


class RunHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m12s1-")
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

    def make_run(self, rid, status, events_age_min=None, ledger=True):
        rdir = self.data / "runs" / rid
        rdir.mkdir(parents=True, exist_ok=True)
        events = rdir / "events.jsonl"
        events.write_text("")
        if events_age_min is not None:
            old = time.time() - events_age_min * 60
            os.utime(events, (old, old))
        xs.jsave(str(rdir / "status.json"),
                 {"events": str(events), "started": time.time() - 3600, **status})
        if ledger:
            with open(xs.LEDGER, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"run_id": rid, "status": "started"}) + "\n")
                fh.write(json.dumps({"run_id": "other-run", "status": "started"}) + "\n")
                fh.write(json.dumps({"run_id": rid, "status": "progress"}) + "\n")
        return str(rdir)

    @needs_identity
    def test_live_runner_recent_activity_is_alive(self):
        pid = os.getpid()
        self.make_run("run-alive", {"state": "running", "runner_pid": pid,
                                    "runner_pid_start": xs.proc_start_time(pid)})
        out = xs.run_health("run-alive")
        self.assertEqual(out["verdict"], "alive")
        self.assertEqual(out["evidence"]["pid_identity"], "match")
        self.assertEqual(out["evidence"]["runner_pid"], pid)
        # Ledger pointer is the LAST line for this run (1-based).
        self.assertEqual(out["evidence"]["ledger_line"], 3)
        self.assertIsNotNone(out["evidence"]["last_activity"])
        self.assertIsNotNone(out["evidence"]["idle_minutes"])
        self.assertLess(out["evidence"]["idle_minutes"], 1)

    def test_dead_pid_is_dead(self):
        self.make_run("run-dead", {"state": "running", "runner_pid": 999999})
        out = xs.run_health("run-dead")
        self.assertEqual(out["verdict"], "dead")
        self.assertEqual(out["evidence"]["pid_identity"], "gone")

    @needs_identity
    def test_recycled_pid_is_dead(self):
        # The pid is alive (this process) but the recorded identity is
        # someone else's: the recorded runner is dead, never alive (#98).
        pid = os.getpid()
        self.make_run("run-recycled", {"state": "running", "runner_pid": pid,
                                       "runner_pid_start": "proc:0"})
        out = xs.run_health("run-recycled")
        self.assertEqual(out["verdict"], "dead")
        self.assertEqual(out["evidence"]["pid_identity"], "mismatch")

    def test_no_runner_pid_is_unknown(self):
        self.make_run("run-nopid", {"state": "running"})
        out = xs.run_health("run-nopid")
        self.assertEqual(out["verdict"], "unknown")
        self.assertEqual(out["evidence"]["pid_identity"], "no-pid")

    @needs_identity
    def test_live_runner_stale_activity_is_zombie(self):
        pid = os.getpid()
        self.make_run("run-zombie", {"state": "running", "runner_pid": pid,
                                     "runner_pid_start": xs.proc_start_time(pid)},
                      events_age_min=30)
        out = xs.run_health("run-zombie", stall_minutes=10)
        self.assertEqual(out["verdict"], "zombie")
        self.assertEqual(out["evidence"]["pid_identity"], "match")
        self.assertGreaterEqual(out["evidence"]["idle_minutes"], 29)

    def test_legacy_run_without_start_stamp_falls_back_to_pid_check(self):
        # Runs recorded before start times were stamped have no identity:
        # the plain existence check still applies (M11 semantics).
        pid = os.getpid()
        self.make_run("run-legacy", {"state": "running", "runner_pid": pid})
        out = xs.run_health("run-legacy")
        self.assertEqual(out["verdict"], "alive")
        self.assertEqual(out["evidence"]["pid_identity"], "legacy")

    def test_finished_run_is_done(self):
        self.make_run("run-done", {"state": "finished", "status": "done",
                                   "runner_pid": 999999})
        out = xs.run_health("run-done")
        self.assertEqual(out["verdict"], "done")

    def test_missing_ledger_entry_reports_null_line(self):
        self.make_run("run-nolog", {"state": "running", "runner_pid": 999999},
                      ledger=False)
        out = xs.run_health("run-nolog")
        self.assertEqual(out["verdict"], "dead")
        self.assertIsNone(out["evidence"]["ledger_line"])

    def test_health_is_read_only(self):
        rdir = self.make_run("run-ro", {"state": "running", "runner_pid": 999999})
        before = Path(rdir, "status.json").read_bytes()
        xs.run_health("run-ro")
        xs.fleet_health_snapshot()
        self.assertEqual(Path(rdir, "status.json").read_bytes(), before)


class FleetHealthSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m12s1-snap-")
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

    def make_run(self, rid, status):
        rdir = self.data / "runs" / rid
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "events.jsonl").write_text("")
        xs.jsave(str(rdir / "status.json"),
                 {"events": str(rdir / "events.jsonl"), "started": time.time() - 60,
                  **status})
        return str(rdir)

    def test_snapshot_lists_only_non_terminal_runs_with_summary(self):
        pid = os.getpid()
        self.make_run("run-alive", {"state": "running", "runner_pid": pid,
                                    "runner_pid_start": xs.proc_start_time(pid)})
        self.make_run("run-dead", {"state": "running", "runner_pid": 999999})
        self.make_run("run-done", {"state": "finished", "status": "done",
                                   "runner_pid": 999999})
        snap = xs.fleet_health_snapshot()
        self.assertEqual([row["id"] for row in snap["runs"]], ["run-alive", "run-dead"])
        self.assertEqual(snap["summary"], {"alive": 1, "dead": 1, "zombie": 0, "unknown": 0})
        self.assertEqual(snap["finished_runs"], 1)
        self.assertEqual(snap["stall_minutes"], 10)
        self.assertIn("lanes", snap)


class FleetHealthCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m12s1-cmd-")
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(xs, "DATA", str(self.root))
        self.patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.patch.stop)

    def config(self, hosts):
        (self.root / "fleet.json").write_text(json.dumps({"hosts": hosts}))

    def test_json_health_aggregates_hosts_and_marks_unreachable(self):
        self.config([{"name": "local", "kind": "local"}, {"name": "box", "kind": "instabox", "box": "task-box"},
                     {"name": "ssh", "kind": "ssh", "ssh": "host-alias"}])
        health = {"runs": [{"id": "r1", "verdict": "alive", "evidence": {}}],
                  "lanes": [], "summary": {"alive": 1, "dead": 0, "zombie": 0, "unknown": 0},
                  "finished_runs": 0, "stall_minutes": 10}

        def snapshot(host, args, **kw):
            self.assertEqual(args[:2], ["fleet", "_health"])
            if host["name"] == "ssh":
                raise RuntimeError("ssh: openswarm command failed (rc=255)")
            return health
        output = io.StringIO()
        with mock.patch.object(xs, "sh", return_value=(0, "task-box  Paused\n", "")), \
                mock.patch.object(xs, "host_json", side_effect=snapshot), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="health", json=True, stall_minutes=10))
        rows = json.loads(output.getvalue())["hosts"]
        self.assertTrue(rows[0]["reachable"])
        self.assertEqual(rows[0]["health"]["runs"][0]["verdict"], "alive")
        self.assertTrue(rows[1]["paused"])
        self.assertFalse(rows[1]["reachable"])
        self.assertIsNone(rows[1].get("health"))
        self.assertFalse(rows[2]["reachable"])
        self.assertIn("rc=255", rows[2]["error"])

    def test_text_health_lists_verdicts(self):
        self.config([{"name": "local", "kind": "local"}])
        health = {"runs": [{"id": "r1", "verdict": "dead",
                            "evidence": {"runner_pid": 999999, "pid_identity": "gone",
                                         "idle_minutes": None, "last_activity": None,
                                         "ledger_line": 3}}],
                  "lanes": [], "summary": {"alive": 0, "dead": 1, "zombie": 0, "unknown": 0},
                  "finished_runs": 2, "stall_minutes": 10}
        output = io.StringIO()
        with mock.patch.object(xs, "host_json", return_value=health), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="health", json=False, stall_minutes=10))
        text = output.getvalue()
        self.assertIn("local", text)
        self.assertIn("dead=1", text)
        self.assertIn("run r1 dead", text)
        self.assertIn("ledger:3", text)


STUB = Path(_load.HERE) / "fixtures" / "fake_fleet_openswarm.py"


class FleetHealthFakeTransportTests(unittest.TestCase):
    """End to end through the fake fleet transport: the controller asks the
    stub for `fleet _health`, the stub delegates to the real binary, and the
    controller aggregates genuine verdicts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m12s1-e2e-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        STUB.chmod(0o755)
        (self.root / "fleet.json").write_text(json.dumps(
            {"hosts": [{"name": "local", "kind": "local", "openswarm": str(STUB)}]}))

    def env(self):
        return {**os.environ, "OPENSWARM_HOME": str(self.root)}

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, str(Path(_load.ROOT) / "bin" / "openswarm"), *argv],
                              env=self.env(), capture_output=True, text=True, timeout=120)

    @needs_identity
    def test_health_over_fake_transport_reports_live_runner(self):
        pid = os.getpid()
        rdir = self.root / "runs" / "fake-alive"
        rdir.mkdir(parents=True)
        (rdir / "events.jsonl").write_text("")
        (rdir / "status.json").write_text(json.dumps(
            {"state": "running", "runner_pid": pid,
             "runner_pid_start": xs.proc_start_time(pid),
             "events": str(rdir / "events.jsonl"), "started": time.time() - 60}))
        proc = self.run_cli("fleet", "health", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(proc.stdout)["hosts"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["reachable"])
        runs = rows[0]["health"]["runs"]
        self.assertEqual([row["id"] for row in runs], ["fake-alive"])
        self.assertEqual(runs[0]["verdict"], "alive")
        self.assertEqual(runs[0]["evidence"]["pid_identity"], "match")


if __name__ == "__main__":
    unittest.main(verbosity=2)
