"""M12 slice 2 (#148): explicit fleet reap + real fleet evidence.

`fleet health --reap` reaps runs the health check judged dead, reusing the
M11 reap semantics (mark finished/exited, stop BUDGET/STALL alerts) and
records each reap action as a flagged ledger line so it is reviewable.
Default `fleet health` stays read-only: without --reap nothing is reaped.

Zombie runs are NOT reaped: their runner is alive, and M11's reap guard
refuses a live runner. A zombie is reported as skipped with the reason, so
the operator can act on it deliberately instead of the tool silently
marking a live run finished.
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

IDENTITY_SUPPORTED = xs.proc_start_time(os.getpid()) is not None
needs_identity = unittest.skipUnless(
    IDENTITY_SUPPORTED,
    "process start-time identity is unreadable on this platform; "
    "pid check falls back to the legacy existence test")


class FleetReapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m12s2-")
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
        return str(rdir)

    def ledger_lines(self):
        try:
            return (self.data / "ledger.jsonl").read_text().splitlines()
        except FileNotFoundError:
            return []

    def test_reap_dead_run_marks_exited_and_records_ledger(self):
        self.make_run("run-dead", {"state": "running", "runner_pid": 999999,
                                   "title": "dead run"})
        before = len(self.ledger_lines())
        out = xs.fleet_health_reap()
        self.assertEqual([row["id"] for row in out["reaped"]], ["run-dead"])
        self.assertEqual(out["summary"]["reaped"], 1)
        st = json.loads((self.data / "runs" / "run-dead" / "status.json").read_text())
        self.assertEqual(st["state"], "finished")
        self.assertEqual(st["status"], "exited")
        self.assertEqual(st["reaped"], "dead-runner")
        lines = self.ledger_lines()
        self.assertEqual(len(lines), before + 1)
        record = json.loads(lines[-1])
        self.assertTrue(record["fleet_health_reap"])
        self.assertEqual(record["run_id"], "run-dead")
        self.assertEqual(record["status"], "exited")
        self.assertEqual(record["reaped"], "dead-runner")
        self.assertEqual(record["verdict"], "dead")
        self.assertEqual(record["event"], "reap")
        # The reported ledger line is the 1-based line just appended.
        self.assertEqual(out["reaped"][0]["ledger_line"], len(lines))
        # The run's evidence pointer now resolves to the reap record.
        self.assertEqual(xs.ledger_run_lines().get("run-dead"), len(lines))

    @needs_identity
    def test_reap_recycled_pid_is_reaped(self):
        pid = os.getpid()
        self.make_run("run-recycled", {"state": "running", "runner_pid": pid,
                                       "runner_pid_start": "proc:0"})
        out = xs.fleet_health_reap()
        self.assertEqual([row["id"] for row in out["reaped"]], ["run-recycled"])
        self.assertEqual(out["reaped"][0]["ledger_line"], len(self.ledger_lines()))
        st = json.loads((self.data / "runs" / "run-recycled" / "status.json").read_text())
        self.assertEqual(st["status"], "exited")

    def test_reap_is_idempotent(self):
        self.make_run("run-dead", {"state": "running", "runner_pid": 999999})
        xs.fleet_health_reap()
        lines_after_first = len(self.ledger_lines())
        out = xs.fleet_health_reap()
        self.assertEqual(out["reaped"], [])
        self.assertEqual(len(self.ledger_lines()), lines_after_first)
        # A finished run is no longer a health candidate at all.
        self.assertEqual(out["health"]["finished_runs"], 1)
        self.assertEqual(out["health"]["runs"], [])

    @needs_identity
    def test_reap_skips_zombie_runner_alive(self):
        pid = os.getpid()
        rdir = self.make_run("run-zombie", {"state": "running", "runner_pid": pid,
                                            "runner_pid_start": xs.proc_start_time(pid)},
                             events_age_min=30)
        before_status = Path(rdir, "status.json").read_bytes()
        before_ledger = len(self.ledger_lines())
        out = xs.fleet_health_reap(stall_minutes=10)
        self.assertEqual(out["reaped"], [])
        self.assertEqual([row["id"] for row in out["skipped"]], ["run-zombie"])
        self.assertEqual(out["skipped"][0]["verdict"], "zombie")
        self.assertIn("alive", out["skipped"][0]["reason"])
        # Nothing was mutated: a live runner must never be marked finished.
        self.assertEqual(Path(rdir, "status.json").read_bytes(), before_status)
        self.assertEqual(len(self.ledger_lines()), before_ledger)

    def test_reap_skips_alive_and_unknown(self):
        pid = os.getpid()
        self.make_run("run-alive", {"state": "running", "runner_pid": pid,
                                    "runner_pid_start": xs.proc_start_time(pid)})
        self.make_run("run-nopid", {"state": "running"})
        out = xs.fleet_health_reap()
        self.assertEqual(out["reaped"], [])
        self.assertEqual({row["id"]: row["verdict"] for row in out["skipped"]},
                         {"run-alive": "alive", "run-nopid": "unknown"})
        for rid in ("run-alive", "run-nopid"):
            st = json.loads((self.data / "runs" / rid / "status.json").read_text())
            self.assertEqual(st["state"], "running")

    def test_reap_record_does_not_pollute_routing_weights(self):
        self.make_run("run-dead", {"state": "running", "runner_pid": 999999,
                                   "title": "dead run"})
        xs.fleet_health_reap()
        reap_entry = json.loads(self.ledger_lines()[-1])
        normal = {"id": "run-ok", "backend": "pi", "model": "m",
                  "card_type": "fix", "status": "done", "exec_pass": True}
        self.assertEqual(xs.compute_routing_weights([reap_entry, normal]),
                         xs.compute_routing_weights([normal]))

    def test_health_snapshot_without_reap_stays_read_only(self):
        rdir = self.make_run("run-dead", {"state": "running", "runner_pid": 999999})
        before_status = Path(rdir, "status.json").read_bytes()
        before_ledger = self.ledger_lines()
        xs.fleet_health_snapshot()
        self.assertEqual(Path(rdir, "status.json").read_bytes(), before_status)
        self.assertEqual(self.ledger_lines(), before_ledger)


class FleetHealthReapCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m12s2-cmd-")
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(xs, "DATA", str(self.root))
        self.patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.patch.stop)

    def config(self, hosts):
        (self.root / "fleet.json").write_text(json.dumps({"hosts": hosts}))

    def test_health_without_reap_flag_calls_health_not_reap(self):
        self.config([{"name": "local", "kind": "local"}])
        seen = []

        def snapshot(host, args, **kw):
            seen.append(args)
            return {"runs": [], "lanes": [], "summary": {"alive": 0, "dead": 0, "zombie": 0, "unknown": 0},
                    "finished_runs": 0, "stall_minutes": 10}
        with mock.patch.object(xs, "host_json", side_effect=snapshot), \
                contextlib.redirect_stdout(io.StringIO()):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="health", json=True,
                                            stall_minutes=10, reap=False))
        self.assertEqual(seen, [["fleet", "_health", "--stall-minutes", "10"]])

    def test_json_health_reap_reports_reaped_and_skipped(self):
        self.config([{"name": "local", "kind": "local"},
                     {"name": "box", "kind": "instabox", "box": "task-box"}])
        reap = {"health": {"runs": [{"id": "r1", "verdict": "dead", "evidence": {}}],
                          "lanes": [], "summary": {"alive": 0, "dead": 1, "zombie": 0, "unknown": 0},
                          "finished_runs": 0, "stall_minutes": 10},
                "reaped": [{"id": "r1", "verdict": "dead", "ledger_line": 7}],
                "skipped": [{"id": "r2", "verdict": "zombie", "reason": "runner alive"}],
                "summary": {"reaped": 1, "skipped": 1, "alive": 0, "dead": 1, "zombie": 0, "unknown": 0},
                "stall_minutes": 10}

        def snapshot(host, args, **kw):
            self.assertEqual(args[:2], ["fleet", "_reap"])
            return reap
        output = io.StringIO()
        with mock.patch.object(xs, "sh", return_value=(0, "task-box  Paused\n", "")), \
                mock.patch.object(xs, "host_json", side_effect=snapshot), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="health", json=True,
                                            stall_minutes=10, reap=True))
        rows = json.loads(output.getvalue())["hosts"]
        self.assertTrue(rows[0]["reachable"])
        self.assertEqual(rows[0]["reap"]["reaped"][0]["id"], "r1")
        self.assertEqual(rows[0]["reap"]["skipped"][0]["id"], "r2")
        self.assertTrue(rows[1]["paused"])
        self.assertIsNone(rows[1].get("reap"))

    def test_text_health_reap_lists_reaped_and_skipped(self):
        self.config([{"name": "local", "kind": "local"}])
        reap = {"health": {"runs": [{"id": "r1", "verdict": "dead",
                                     "evidence": {"runner_pid": 999999, "pid_identity": "gone",
                                                  "idle_minutes": None, "last_activity": None,
                                                  "ledger_line": 3}}],
                          "lanes": [], "summary": {"alive": 0, "dead": 1, "zombie": 0, "unknown": 0},
                          "finished_runs": 0, "stall_minutes": 10},
                "reaped": [{"id": "r1", "verdict": "dead", "ledger_line": 7}],
                "skipped": [{"id": "r2", "verdict": "zombie", "reason": "runner alive; not reaped"}],
                "summary": {"reaped": 1, "skipped": 1, "alive": 0, "dead": 1, "zombie": 0, "unknown": 0},
                "stall_minutes": 10}
        output = io.StringIO()
        with mock.patch.object(xs, "host_json", return_value=reap), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="health", json=False,
                                            stall_minutes=10, reap=True))
        text = output.getvalue()
        self.assertIn("reaped=1", text)
        self.assertIn("reaped r1", text)
        self.assertIn("ledger:7", text)
        self.assertIn("skip r2 zombie", text)


STUB = Path(_load.HERE) / "fixtures" / "fake_fleet_openswarm.py"


class FleetReapFakeTransportTests(unittest.TestCase):
    """End to end through the fake fleet transport: the controller asks the
    stub for `fleet _reap`, the stub delegates to the real binary, and the
    dead run on disk is really reaped (status + ledger)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m12s2-e2e-")
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

    def test_reap_over_fake_transport_reaps_dead_run(self):
        rdir = self.root / "runs" / "fake-dead"
        rdir.mkdir(parents=True)
        (rdir / "events.jsonl").write_text("")
        (rdir / "status.json").write_text(json.dumps(
            {"state": "running", "runner_pid": 999999, "title": "fake dead",
             "events": str(rdir / "events.jsonl"), "started": time.time() - 3600}))
        (self.root / "ledger.jsonl").write_text(
            json.dumps({"run_id": "fake-dead", "status": "started"}) + "\n")
        proc = self.run_cli("fleet", "health", "--reap", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(proc.stdout)["hosts"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["reachable"])
        reap = rows[0]["reap"]
        self.assertEqual([row["id"] for row in reap["reaped"]], ["fake-dead"])
        st = json.loads((rdir / "status.json").read_text())
        self.assertEqual((st["state"], st["status"]), ("finished", "exited"))
        lines = (self.root / "ledger.jsonl").read_text().splitlines()
        record = json.loads(lines[-1])
        self.assertTrue(record["fleet_health_reap"])
        self.assertEqual(record["run_id"], "fake-dead")
        self.assertEqual(reap["reaped"][0]["ledger_line"], len(lines))

    def test_health_without_reap_over_fake_transport_reaps_nothing(self):
        rdir = self.root / "runs" / "fake-dead"
        rdir.mkdir(parents=True)
        (rdir / "events.jsonl").write_text("")
        (rdir / "status.json").write_text(json.dumps(
            {"state": "running", "runner_pid": 999999,
             "events": str(rdir / "events.jsonl"), "started": time.time() - 3600}))
        proc = self.run_cli("fleet", "health", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(proc.stdout)["hosts"]
        self.assertEqual(rows[0]["health"]["runs"][0]["verdict"], "dead")
        self.assertIsNone(rows[0].get("reap"))
        st = json.loads((rdir / "status.json").read_text())
        self.assertEqual(st["state"], "running")


if __name__ == "__main__":
    unittest.main(verbosity=2)
