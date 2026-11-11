import argparse
import contextlib
import copy
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
STUB = Path(_load.HERE) / "fixtures" / "fake_fleet_openswarm.py"


class FakeFleetFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-batch-")
        self.root = Path(self.tmp.name)
        self.data = self.root / "planner"
        self.cards = self.root / "cards"
        self.cards.mkdir()
        self.repo = self.root / "local"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q")
        # Git may detach automatic maintenance after a commit or fetch. These short-lived
        # fixture repos are deleted immediately after the test and need no background work.
        self.git(self.repo, "config", "maintenance.auto", "false")
        self.git(self.repo, "config", "gc.auto", "0")
        (self.repo / "base").write_text("base")
        self.git(self.repo, "add", "base")
        self.git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
        self.initial = self.git(self.repo, "rev-parse", "HEAD")
        self.hosts = {"local": {"name": "local", "kind": "local", "repos": {"project": str(self.repo)}}}
        for name in ("b1", "b2"):
            repo = self.root / name / "repo"
            repo.mkdir(parents=True)
            self.git(repo, "init", "-q")
            self.git(repo, "config", "maintenance.auto", "false")
            self.git(repo, "config", "gc.auto", "0")
            bundle = self.root / "initial.bundle"
            bundle.write_bytes(subprocess.check_output(["git", "-C", str(self.repo), "bundle", "create", "-", "HEAD"]))
            self.git(repo, "fetch", str(bundle), "HEAD")
            self.git(repo, "checkout", "-q", "--detach", "FETCH_HEAD")
            self.hosts[name] = {"name": name, "kind": "instabox", "box": name + "-box",
                                "openswarm": str(STUB), "repos": {"project": str(repo)}}
        (self.repo / "new-base").write_text("local only")
        self.git(self.repo, "add", "new-base")
        self.git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "new base")
        self.base = self.git(self.repo, "rev-parse", "HEAD")
        STUB.chmod(0o755)
        self.data.mkdir()
        (self.data / "fleet.json").write_text(json.dumps({"hosts": list(self.hosts.values())}))
        self.calls = []
        self.patches = [mock.patch.object(xs, "DATA", str(self.data)),
                        mock.patch.object(xs, "LEDGER", str(self.data / "ledger.jsonl")),
                        mock.patch.object(xs, "host_run", side_effect=self.transport),
                        mock.patch.object(xs, "sh", side_effect=self.shell)]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self.tmp.cleanup)
        for patch in self.patches:
            self.addCleanup(patch.stop)

    def git(self, repo, *args):
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE).decode().strip()

    def shell(self, command, **kwargs):
        if command[:2] == ["instabox", "ls"]:
            states = [{"name": host["box"], "status": "Running"} for host in self.hosts.values()
                      if host["kind"] == "instabox"]
            return 0, json.dumps(states), ""
        return 0, "", ""

    def transport(self, host, command, timeout=60, input=None):
        self.calls.append((host["name"], command))
        home = self.data if host["name"] == "local" else self.root / host["name"] / "data"
        proc = subprocess.run(["sh", "-c", command], capture_output=True, input=input, timeout=timeout,
                              env={**os.environ, "OPENSWARM_HOME": str(home)})
        return proc.returncode, proc.stdout, proc.stderr

    def args(self, **fields):
        values = dict(batch_action=None, id=None, batch_id=None, repo="project", cards=str(self.cards), hosts="b1,b2",
                      per_host=1, backend="pi", model="cheap", effort=None, verify="true", budget_minutes=1,
                      base=None, detach=False, poll_seconds=0.001)
        values.update(fields)
        return argparse.Namespace(**values)

    def run_batch(self, **fields):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exited:
            xs.cmd_batch(self.args(**fields), xs.load_policy())
        paths = list((self.data / "batches").glob("*/summary.json"))
        self.assertEqual(len(paths), 1)
        return exited.exception.code, json.loads(paths[0].read_text()), output.getvalue()


class BatchTests(FakeFleetFixture, unittest.TestCase):
    def test_batch_launch_passes_path_hints_to_remote_run(self):
        card = self.cards / "one.md"
        card.write_text("Update src/widget.py")
        spec = {"hosts": {"b1": self.hosts["b1"]}, "repos": {"b1": str(self.repo)}, "scope": "host",
                "base": self.base, "paths": ["src/widget.py"], "card_type": "fix"}
        row = {"host": "b1", "path": str(card), "run_id": "run1"}
        with mock.patch.object(xs, "host_python", side_effect=[b"remote-card.md", b""]), \
                mock.patch.object(xs, "host_require", side_effect=lambda host, output, context: output), \
                mock.patch.object(xs, "host_json", return_value={"id": "run1"}) as host_json:
            self.assertEqual(xs.batch_launch(spec, row), "run1")
        remote_args = host_json.call_args.args[1]
        self.assertEqual(remote_args[remote_args.index("--paths") + 1:], ["src/widget.py"])
        self.assertEqual(remote_args[remote_args.index("--scope") + 1], "host")

    def test_batch_launch_passes_backends_file_to_remote_run(self):
        card = self.cards / "one.md"
        card.write_text("Update src/widget.py")
        spec = {"hosts": {"b1": self.hosts["b1"]}, "repos": {"b1": str(self.repo)}, "scope": "repo",
                "base": self.base, "paths": [], "card_type": None,
                "backends_file": "/tmp/m4-backends.json"}
        row = {"host": "b1", "path": str(card), "run_id": "run1"}
        with mock.patch.object(xs, "host_python", side_effect=[b"remote-card.md", b""]), \
                mock.patch.object(xs, "host_require", side_effect=lambda host, output, context: output), \
                mock.patch.object(xs, "host_json", return_value={"id": "run1"}) as host_json:
            self.assertEqual(xs.batch_launch(spec, row), "run1")
        remote_args = host_json.call_args.args[1]
        self.assertEqual(remote_args[remote_args.index("--backends-file") + 1], "/tmp/m4-backends.json")

    def test_batch_launch_without_backends_file_passes_no_flag(self):
        card = self.cards / "one.md"
        card.write_text("Update src/widget.py")
        spec = {"hosts": {"b1": self.hosts["b1"]}, "repos": {"b1": str(self.repo)}, "scope": "repo",
                "base": self.base, "paths": [], "card_type": None}
        row = {"host": "b1", "path": str(card), "run_id": "run1"}
        with mock.patch.object(xs, "host_python", side_effect=[b"remote-card.md", b""]), \
                mock.patch.object(xs, "host_require", side_effect=lambda host, output, context: output), \
                mock.patch.object(xs, "host_json", return_value={"id": "run1"}) as host_json:
            self.assertEqual(xs.batch_launch(spec, row), "run1")
        self.assertNotIn("--backends-file", host_json.call_args.args[1])

    def test_batch_host_scope_reaches_remote_run(self):
        (self.cards / "one.md").write_text("host maintenance")
        rc, summary, _ = self.run_batch(scope="host")
        self.assertEqual(rc, 0)
        self.assertEqual(summary["scope"], "host")
        calls = [json.loads(line) for line in (self.root / "b1" / "data" / "calls.jsonl").read_text().splitlines()]
        start = next(call for call in calls if call["event"] == "start")
        self.assertEqual(start["args"]["scope"], "host")

    def test_budget_refuses_batch_before_launch(self):
        (self.cards / "one.md").write_text("one")
        policy = xs.deep_merge(xs.load_policy(), {"budget": {"daily_executor_minutes": 0}})
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(xs, "execute_batch") as execute, \
                self.assertRaises(SystemExit) as e:
            xs.cmd_batch(self.args(), policy)
        self.assertEqual(e.exception.code, 3)
        self.assertEqual(json.loads(output.getvalue())["error"], "budget_exceeded_daily")
        execute.assert_not_called()

    def test_markdown_summary_preserves_zero_numeric_cells(self):
        summary = {"id": "zeros", "state": "finished", "cards": [{
            "card": "zero.md", "host": "b1", "status": "done", "verify_rc": 0,
            "branch": "branch", "commit": "commit", "seconds": 0, "summary": "ok",
        }]}
        directory = self.data / "batches" / "zeros"
        directory.mkdir(parents=True)
        xs.batch_save(summary)
        markdown = (directory / "summary.md").read_text()
        self.assertIn("| zero.md | b1 | done | 0 | branch | commit | 0 | ok |", markdown)

    def test_distribution_queueing_bundle_fetch_failure_and_summaries(self):
        for i, text in enumerate(("card zero", "FAIL card one", "card two", "card three", "card four")):
            (self.cards / f"{i}.md").write_text(text)
        rc, summary, output = self.run_batch()
        self.assertEqual(rc, 1)
        self.assertEqual(summary["state"], "finished")
        self.assertEqual([r["host"] for r in summary["cards"]], ["b1", "b2", "b1", "b2", "b1"])
        self.assertEqual([r["status"] for r in summary["cards"]], ["done", "failed", "done", "done", "done"])
        self.assertEqual(summary["cards"][1]["verify_rc"], 1)
        for host in ("b1", "b2"):
            calls = [json.loads(line) for line in (self.root / host / "data" / "calls.jsonl").read_text().splitlines()]
            for call in calls:
                if call["event"] == "start":
                    self.assertEqual(call["active_before"], 0)
                    self.assertEqual(call["args"]["base"], self.base)
                    self.assertEqual(call["args"]["backend"], "pi")
            self.assertEqual(self.git(self.root / host / "repo", "cat-file", "-t", self.base), "commit")
        for row in summary["cards"]:
            self.assertEqual(self.git(self.repo, "rev-parse", row["branch"]), row["commit"])
            self.assertEqual(self.git(self.repo, "show", row["branch"] + ":new-base"), "local only")
            remote_branches = self.git(self.root / row["host"] / "repo", "branch", "--list", row["branch"])
            self.assertEqual(remote_branches, "")
            self.assertEqual(output.count(f"{row['card']} @{row['host']}: running (running)"), 1)
        markdown = (self.data / "batches" / summary["id"] / "summary.md").read_text()
        self.assertIn("FAIL card one", markdown)
        self.assertIn("Verify rc", markdown)
        wakes = [call for call in xs.sh.call_args_list if call.args[0][:2] == ["instabox", "wake"]]
        self.assertEqual(len(wakes), 2)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.repo, "remote"), "")

    def test_multiple_slots_and_explicit_base(self):
        for i in range(6):
            (self.cards / f"{i}.md").write_text(f"card {i}")
        rc, summary, _ = self.run_batch(per_host=2, base=self.initial)
        self.assertEqual(rc, 0)
        for name in ("b1", "b2"):
            calls = [json.loads(line) for line in (self.root / name / "data" / "calls.jsonl").read_text().splitlines()]
            starts = [call for call in calls if call["event"] == "start"]
            self.assertEqual([call["active_before"] for call in starts], [0, 1, 0])
            self.assertTrue(all(call["args"]["base"] == self.initial for call in starts))
        self.assertEqual(summary["base"], self.initial)

    def test_batch_provider_cap_queues_across_fleet_hosts(self):
        for name in ("b1", "b2"):
            self.hosts[name]["kind"] = "local"
        (self.data / "fleet.json").write_text(json.dumps({"hosts": list(self.hosts.values())}))
        (self.data / "policy.local.json").write_text(json.dumps({"concurrency": {"pi": 1}}))
        (self.cards / "one.md").write_text("first card")
        (self.cards / "two.md").write_text("second card")
        rc, summary, _ = self.run_batch()
        self.assertEqual(rc, 0)
        self.assertEqual([row["host"] for row in summary["cards"]], ["b1", "b2"])
        starts, finishes = [], []
        for name in ("b1", "b2"):
            rows = [json.loads(line) for line in (self.root / name / "data" / "calls.jsonl").read_text().splitlines()]
            starts.extend(row["ts"] for row in rows if row["event"] == "start")
            finishes.extend(row["ts"] for row in rows if row["event"] == "finish")
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(finishes), 2)
        self.assertLess(finishes[0], starts[1])

    def test_batch_total_cap_queues_across_fleet_hosts(self):
        for name in ("b1", "b2"):
            self.hosts[name]["kind"] = "local"
        (self.data / "fleet.json").write_text(json.dumps({"hosts": list(self.hosts.values())}))
        (self.data / "policy.local.json").write_text(json.dumps({"concurrency": {"total": 1}}))
        (self.cards / "one.md").write_text("first card")
        (self.cards / "two.md").write_text("second card")
        rc, summary, _ = self.run_batch()
        self.assertEqual(rc, 0)
        starts, finishes = [], []
        for name in ("b1", "b2"):
            rows = [json.loads(line) for line in (self.root / name / "data" / "calls.jsonl").read_text().splitlines()]
            starts.extend(row["ts"] for row in rows if row["event"] == "start")
            finishes.extend(row["ts"] for row in rows if row["event"] == "finish")
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(finishes), 2)
        self.assertLess(finishes[0], starts[1])

    def test_unreachable_active_run_does_not_release_slot_for_queued_card(self):
        for i in range(3):
            (self.cards / f"{i}.md").write_text(f"card {i}")
        def offline(host, command, **kwargs):
            if host["name"] == "b1" and " _run-info " in command:
                return 255, b"", b"offline"
            return self.transport(host, command, **kwargs)
        with mock.patch.object(xs, "host_run", side_effect=offline):
            rc, summary, _ = self.run_batch()
        self.assertEqual(rc, 1)
        starts = [json.loads(line) for line in (self.root / "b1" / "data" / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(sum(row["event"] == "start" for row in starts), 1)
        self.assertEqual(summary["cards"][1]["status"], "done")
        self.assertEqual(summary["cards"][2]["status"], "failed")
        self.assertNotIn("run_id", summary["cards"][2])

    def test_detach_spawns_batch_runner_and_status_reads_summary(self):
        (self.cards / "card.md").write_text("card")
        output = io.StringIO()
        real_popen = subprocess.Popen
        spawned = []
        def spawn_batch(argv, **kwargs):
            if "--batch-id" in argv:
                spawned.append((argv, kwargs))
                return mock.Mock(pid=os.getpid())
            return real_popen(argv, **kwargs)
        with mock.patch.object(xs.subprocess, "Popen", side_effect=spawn_batch), \
                contextlib.redirect_stdout(output):
            xs.cmd_batch(self.args(detach=True), xs.load_policy())
        bid = json.loads(output.getvalue())["id"]
        self.assertIn("--batch-id", spawned[0][0])
        self.assertTrue(spawned[0][1]["start_new_session"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            xs.cmd_batch(self.args(batch_action="status", id=bid), xs.load_policy())
        self.assertEqual(json.loads(output.getvalue())["state"], "queued")
        # Exercise the exact worker path used by the detached child with the fake transport.
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exited:
            xs.cmd_batch(self.args(batch_id=bid), xs.load_policy())
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(json.loads((self.data / "batches" / bid / "summary.json").read_text())["state"], "finished")

    def test_detached_cli_runs_entire_batch_in_background(self):
        (self.cards / "card.md").write_text("background card")
        for name in ("b1", "b2"):
            self.hosts[name]["kind"] = "local"
        (self.data / "fleet.json").write_text(json.dumps({"hosts": list(self.hosts.values())}))
        proc = subprocess.run([sys.executable, str(Path(_load.ROOT) / "bin" / "openswarm"), "batch",
                               "--repo", "project", "--cards", str(self.cards), "--hosts", "b1,b2", "--detach",
                               "--poll-seconds", "0.01"], capture_output=True, text=True, timeout=10,
                              env={**os.environ, "OPENSWARM_HOME": str(self.data)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reply = json.loads(proc.stdout)
        path = self.data / "batches" / reply["id"] / "summary.json"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                summary = json.loads(path.read_text())
            except (OSError, ValueError):
                # The daemon publishes summary.json by atomic replacement; on
                # Windows a poll can observe the file mid-replacement
                # (PermissionError) or partially written. Keep polling.
                time.sleep(0.05)
                continue
            if summary["state"] in ("finished", "failed"):
                break
            time.sleep(0.05)
        else:
            self.fail("detached batch did not finish")
        self.assertEqual(summary["state"], "finished", summary)
        self.assertEqual(summary["cards"][0]["status"], "done", summary)
        self.assertEqual(self.git(self.repo, "rev-parse", summary["cards"][0]["branch"]), summary["cards"][0]["commit"])


class BatchDependencyTests(unittest.TestCase):
    """M4 slice 1 (#106): depends_on parsing, validation, topological order."""

    def test_parse_card_dependencies(self):
        self.assertEqual(xs.parse_card_dependencies("depends_on: a.md, b.md\nDo the thing"),
                         ["a.md", "b.md"])
        self.assertEqual(xs.parse_card_dependencies("Do the thing\ndepends_on: [a.md]\n"), ["a.md"])
        self.assertEqual(xs.parse_card_dependencies("Do the thing"), [])
        self.assertEqual(xs.parse_card_dependencies("depends_on: a.md, a.md\n"), ["a.md"])
        self.assertEqual(xs.parse_card_dependencies("depends_on:\nDo the thing"), [])

    def test_strip_card_dependencies_removes_declaration_line(self):
        self.assertEqual(xs.strip_card_dependencies("depends_on: a.md\nDo the thing\n"), "Do the thing\n")
        self.assertEqual(xs.strip_card_dependencies("Do the thing\n"), "Do the thing\n")

    def test_topo_sort_orders_dependencies_first(self):
        order = xs.topo_sort_cards({"a.md": [], "b.md": ["a.md"], "c.md": ["a.md", "b.md"]})
        self.assertEqual(order, ["a.md", "b.md", "c.md"])

    def test_topo_sort_stable_without_dependencies(self):
        self.assertEqual(xs.topo_sort_cards({"b.md": [], "a.md": [], "c.md": []}),
                         ["a.md", "b.md", "c.md"])

    def test_topo_sort_rejects_unknown_dependency(self):
        with self.assertRaises(ValueError) as ctx:
            xs.topo_sort_cards({"a.md": ["ghost.md"]})
        message = str(ctx.exception)
        self.assertIn("a.md", message)
        self.assertIn("ghost.md", message)

    def test_topo_sort_rejects_self_dependency(self):
        with self.assertRaises(ValueError) as ctx:
            xs.topo_sort_cards({"a.md": ["a.md"]})
        self.assertIn("a.md", str(ctx.exception))

    def test_topo_sort_rejects_cycle(self):
        with self.assertRaises(ValueError) as ctx:
            xs.topo_sort_cards({"a.md": ["b.md"], "b.md": ["a.md"]})
        message = str(ctx.exception)
        self.assertIn("a.md", message)
        self.assertIn("b.md", message)
        self.assertIn("cycle", message)


class BatchPlanTests(FakeFleetFixture, unittest.TestCase):
    """M4 slice 1 (#106): `batch plan` output and dispatch in topo order."""

    def plan(self, **fields):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            xs.cmd_batch(self.args(batch_action="plan", **fields), xs.load_policy())
        return json.loads(output.getvalue())

    def test_plan_prints_topological_order(self):
        (self.cards / "a.md").write_text("task a")
        (self.cards / "b.md").write_text("depends_on: a.md\ntask b")
        (self.cards / "c.md").write_text("depends_on: a.md, b.md\ntask c")
        plan = self.plan()
        self.assertEqual(plan["order"], ["a.md", "b.md", "c.md"])
        self.assertEqual(plan["cards"], [
            {"card": "a.md", "depends_on": [], "order": 0},
            {"card": "b.md", "depends_on": ["a.md"], "order": 1},
            {"card": "c.md", "depends_on": ["a.md", "b.md"], "order": 2},
        ])

    def test_plan_without_dependencies_keeps_filename_order(self):
        (self.cards / "b.md").write_text("task b")
        (self.cards / "a.md").write_text("task a")
        self.assertEqual(self.plan()["order"], ["a.md", "b.md"])

    def test_plan_rejects_unknown_dependency(self):
        (self.cards / "a.md").write_text("depends_on: ghost.md\ntask a")
        with self.assertRaises(ValueError) as ctx:
            self.plan()
        message = str(ctx.exception)
        self.assertIn("a.md", message)
        self.assertIn("ghost.md", message)

    def test_plan_rejects_self_dependency(self):
        (self.cards / "a.md").write_text("depends_on: a.md\ntask a")
        with self.assertRaises(ValueError) as ctx:
            self.plan()
        self.assertIn("a.md", str(ctx.exception))

    def test_plan_rejects_cycle(self):
        (self.cards / "a.md").write_text("depends_on: b.md\ntask a")
        (self.cards / "b.md").write_text("depends_on: a.md\ntask b")
        with self.assertRaises(ValueError) as ctx:
            self.plan()
        message = str(ctx.exception)
        self.assertIn("a.md", message)
        self.assertIn("b.md", message)
        self.assertIn("cycle", message)

    def test_batch_creation_records_plan_and_orders_rows(self):
        (self.cards / "a.md").write_text("task a")
        (self.cards / "b.md").write_text("depends_on: a.md\ntask b")
        rc, summary, _ = self.run_batch()
        self.assertEqual(rc, 0)
        self.assertEqual([row["card"] for row in summary["cards"]], ["a.md", "b.md"])
        self.assertEqual(summary["plan"], [
            {"card": "a.md", "depends_on": [], "order": 0},
            {"card": "b.md", "depends_on": ["a.md"], "order": 1},
        ])
        self.assertEqual(summary["cards"][1]["depends_on"], ["a.md"])
        self.assertEqual(summary["cards"][1]["order"], 1)

    def test_batch_creation_rejects_unknown_dependency(self):
        (self.cards / "a.md").write_text("depends_on: ghost.md\ntask a")
        with self.assertRaises(ValueError):
            self.run_batch()
        self.assertFalse(list((self.data / "batches").glob("*/summary.json")))

    def test_batch_dispatch_waits_for_dependency(self):
        (self.cards / "a.md").write_text("task a")
        (self.cards / "c.md").write_text("depends_on: a.md\ntask c")
        rc, summary, _ = self.run_batch()
        self.assertEqual(rc, 0)
        events = []
        for host in ("b1", "b2"):
            events.extend(json.loads(line)
                          for line in (self.root / host / "data" / "calls.jsonl").read_text().splitlines())
        starts = {event["card"].strip().splitlines()[-1]: event
                  for event in events if event["event"] == "start"}
        finishes = {event["id"]: event["ts"] for event in events if event["event"] == "finish"}
        self.assertLess(finishes[starts["task a"]["id"]], starts["task c"]["ts"])

    def test_batch_launch_strips_depends_on_from_dispatched_card(self):
        (self.cards / "a.md").write_text("depends_on: b.md\ntask a")
        (self.cards / "b.md").write_text("task b")
        rc, summary, _ = self.run_batch()
        self.assertEqual(rc, 0)
        calls = []
        for host in ("b1", "b2"):
            calls.extend(json.loads(line)
                         for line in (self.root / host / "data" / "calls.jsonl").read_text().splitlines())
        start = next(call for call in calls
                     if call["event"] == "start" and "task a" in call["card"])
        self.assertNotIn("depends_on", start["card"])
        self.assertIn("task a", start["card"])


class BatchSlice2Tests(FakeFleetFixture, unittest.TestCase):
    """M4 slice 2 (#108): branch stacking + failure blocking."""

    def summary_with(self, rows):
        return {"id": "batch-test", "cards": rows}

    def row(self, card, state="finished", status="done", commit=None, order=0, depends_on=None):
        return {"card": card, "state": state, "status": status, "commit": commit,
                "order": order, "depends_on": depends_on or []}

    def test_stack_base_without_dependencies_is_batch_base(self):
        base, stacked_on = xs.batch_stack_base(self.summary_with([]), self.row("a.md"))
        self.assertIsNone(base)
        self.assertEqual(stacked_on, {})

    def test_stack_base_uses_latest_finished_dependency_commit(self):
        summary = self.summary_with([
            self.row("a.md", commit="aaa", order=0),
            self.row("b.md", commit="bbb", order=1, depends_on=["a.md"]),
        ])
        base, stacked_on = xs.batch_stack_base(summary, self.row("c.md", depends_on=["a.md", "b.md"]))
        self.assertEqual(base, "bbb")
        self.assertEqual(stacked_on, {"a.md": "aaa", "b.md": "bbb"})

    def test_stack_base_no_change_dependency_falls_back_to_batch_base(self):
        summary = self.summary_with([self.row("a.md", commit=None, order=0)])
        base, stacked_on = xs.batch_stack_base(
            summary, self.row("b.md", depends_on=["a.md"]), batch_base=self.base)
        self.assertEqual(base, self.base)
        self.assertEqual(stacked_on, {"a.md": self.base})

    def test_dependency_failure_none_when_dependencies_pass(self):
        summary = self.summary_with([self.row("a.md", commit="aaa")])
        self.assertIsNone(xs.batch_dependency_failure(summary, self.row("b.md", depends_on=["a.md"])))

    def test_dependency_failure_none_while_dependency_unfinished(self):
        summary = self.summary_with([self.row("a.md", state="started", status="running")])
        self.assertIsNone(xs.batch_dependency_failure(summary, self.row("b.md", depends_on=["a.md"])))

    def test_dependency_failure_names_failed_dependency_and_verify_rc(self):
        summary = self.summary_with([
            self.row("a.md", status="failed", commit="aaa", order=0)])
        summary["cards"][0]["verify_rc"] = 1
        summary["cards"][0]["summary"] = "verify failed"
        dep, reason = xs.batch_dependency_failure(summary, self.row("b.md", depends_on=["a.md"]))
        self.assertEqual(dep, "a.md")
        self.assertIn("failed", reason)
        self.assertIn("1", reason)

    def test_dependency_failure_is_transitive_through_blocked(self):
        summary = self.summary_with([
            self.row("a.md", status="failed", order=0),
            self.row("b.md", status="blocked", order=1, depends_on=["a.md"]),
        ])
        dep, _reason = xs.batch_dependency_failure(summary, self.row("c.md", depends_on=["b.md"]))
        self.assertEqual(dep, "b.md")

    def real_git(self, args, cwd, timeout=60):
        # The fixture mocks xs.sh (which xs.git delegates to) to always
        # succeed, so ancestry checks must run the real git binary here.
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout.decode(), proc.stderr.decode()

    def test_verify_stacked_accepts_ancestor_and_rejects_unrelated(self):
        spec = {"repo_path": str(self.repo)}
        with mock.patch.object(xs, "git", side_effect=self.real_git):
            xs.batch_verify_stacked(spec, {"stacked_on": {"a.md": self.initial}}, self.base)
            with self.assertRaises(RuntimeError):
                xs.batch_verify_stacked(spec, {"stacked_on": {"a.md": self.base}}, self.initial)

    def test_batch_stacks_dependency_branch_into_successor(self):
        (self.cards / "a.md").write_text("task a")
        (self.cards / "b.md").write_text("depends_on: a.md\ntask b")
        rc, summary, _ = self.run_batch()
        self.assertEqual(rc, 0)
        a_row, b_row = summary["cards"]
        self.assertEqual(b_row["base"], a_row["commit"])
        self.assertEqual(b_row["stacked_on"], {"a.md": a_row["commit"]})
        starts = []
        for host in ("b1", "b2"):
            starts.extend(json.loads(line) for line in
                          (self.root / host / "data" / "calls.jsonl").read_text().splitlines()
                          if json.loads(line)["event"] == "start")
        b_start = next(call for call in starts if "task b" in call["card"])
        self.assertEqual(b_start["args"]["base"], a_row["commit"])
        self.assertEqual(self.git(self.repo, "merge-base", "--is-ancestor", a_row["commit"], b_row["commit"]), "")
        self.assertEqual(self.git(self.repo, "show", f"{b_row['branch']}:{a_row['run_id']}.txt"), "task a")

    def test_batch_blocks_dependent_when_dependency_fails(self):
        (self.cards / "a.md").write_text("FAIL task a")
        (self.cards / "b.md").write_text("depends_on: a.md\ntask b")
        (self.cards / "c.md").write_text("depends_on: b.md\ntask c")
        rc, summary, _ = self.run_batch()
        self.assertEqual(rc, 1)
        a_row, b_row, c_row = summary["cards"]
        self.assertEqual(a_row["status"], "failed")
        self.assertEqual(a_row["verify_rc"], 1)
        self.assertEqual(b_row["status"], "blocked")
        self.assertEqual(b_row["blocked_by"], "a.md")
        self.assertIn("a.md", b_row["summary"])
        self.assertEqual(c_row["status"], "blocked")
        self.assertEqual(c_row["blocked_by"], "b.md")
        self.assertNotIn("run_id", b_row)
        self.assertNotIn("run_id", c_row)
        starts = []
        for host in ("b1", "b2"):
            calls_path = self.root / host / "data" / "calls.jsonl"
            if not calls_path.exists():
                continue
            starts.extend(json.loads(line) for line in calls_path.read_text().splitlines())
        started_cards = [call["card"] for call in starts if call["event"] == "start"]
        self.assertEqual(len(started_cards), 1)
        self.assertNotIn("task b", " ".join(started_cards))
        self.assertNotIn("task c", " ".join(started_cards))
        ledger = [json.loads(line) for line in
                  (self.data / "ledger.jsonl").read_text().splitlines()]
        blocked = [entry for entry in ledger if entry.get("status") == "blocked"]
        self.assertEqual({entry["card"] for entry in blocked}, {"b.md", "c.md"})
        self.assertEqual({entry["blocked_by"] for entry in blocked}, {"a.md", "b.md"})
        self.assertTrue(all(entry.get("batch_id") == summary["id"] for entry in blocked))
        markdown = (self.data / "batches" / summary["id"] / "summary.md").read_text()
        self.assertIn("blocked", markdown)



class BatchWeightedRoutingTests(FakeFleetFixture, unittest.TestCase):
    """M7 slice 1 (#122): batch cards follow ledger advice per card, reusing
    the single-run weighted-routing decision path; M4 semantics preserved."""

    def setUp(self):
        super().setUp()
        with open(_load.FIXTURE_STATE) as f:
            self.base_state = json.load(f)
        self.now = time.time()

    def win(self, used, hours_left, minutes):
        return xs.window_stats(used * 100, minutes, self.now + hours_left * 3600)

    def scenario(self):
        st = copy.deepcopy(self.base_state)
        st["codex"]["limits"] = {"codex": {"windows": {"seven_day": self.win(0.3, 100, 10080)}}}
        st["claude"].update({"provider": "official", "metered_by_anthropic": True, "stale": False,
                             "five_hour": self.win(0.2, 3, 300), "seven_day": self.win(0.3, 100, 10080)})
        st["deepseek"]["balance"] = 55.0
        for m in st["ocgo"]["models"]:
            m["usage"] = {"5h": 0.0, "7d": 0.0, "30d": 0.0}
        return st

    def write_policy(self, **routing):
        """Routing overrides via policy.local.json; codex require_default_model
        off so the fixture state routes (as in test_weighted_routing)."""
        local = {"codex": {"require_default_model": False}}
        if routing:
            local["routing"] = routing
        (self.data / "policy.local.json").write_text(json.dumps(local))

    def rows_for(self, backend, model, passes, n=8):
        return [{"id": f"{backend[:2]}{i}", "backend": backend, "model": model, "card_type": "fix",
                 "status": "done", "exec_pass": i < passes, "attempts": [{}], "seconds": 10,
                 "tokens": {"total": 100}} for i in range(n)]

    def seed(self, codex_pass=2, ocgo_pass=7, codex_n=8, ocgo_n=8):
        """Seed the controller ledger around the live default pick (codex);
        return (state, default decision)."""
        policy = xs.load_policy()
        state = self.scenario()
        decision = xs.route({"type": "impl", "difficulty": 3}, state, policy)
        self.assertTrue(decision["ok"], f"unroutable: {decision}")
        self.assertEqual(decision["backend"], "codex")
        rows = self.rows_for("codex", decision["model"], codex_pass, codex_n)
        rows += self.rows_for("ocgo", "mid-coder", ocgo_pass, ocgo_n)
        (self.data / "ledger.jsonl").write_text("".join(json.dumps(e) + "\n" for e in rows))
        return state, decision

    def run_weighted_batch(self, state, **fields):
        with mock.patch.object(xs, "get_state", return_value=state):
            return self.run_batch(**fields)

    def start_events(self):
        events = []
        for host in ("b1", "b2"):
            path = self.root / host / "data" / "calls.jsonl"
            if path.exists():
                events.extend(json.loads(line) for line in path.read_text().splitlines())
        return [e for e in events if e["event"] == "start"]

    def ledger_entries(self):
        path = self.data / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def decision_entries(self):
        return [e for e in self.ledger_entries() if e.get("batch_routing_decision")]

    def spec_json(self, summary):
        return json.loads((self.data / "batches" / summary["id"] / "spec.json").read_text())

    def test_weighted_on_reroutes_card_to_advised_backend(self):
        (self.cards / "one.md").write_text("task one")
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        state, decision = self.seed()
        rc, summary, _ = self.run_weighted_batch(state, backend=None, model=None, card_type="fix")
        self.assertEqual(rc, 0)
        self.assertFalse(self.spec_json(summary)["backend_explicit"])
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "ocgo")
        self.assertEqual(starts[0]["args"]["model"], "mid-coder")
        row = summary["cards"][0]
        self.assertEqual(row["backend"], "ocgo")
        self.assertEqual(row["model"], "mid-coder")
        self.assertTrue(row["routed_by_advice"])
        self.assertEqual(row["routed_from"], {"backend": "codex", "model": decision["model"]})
        self.assertEqual(row["advice_pass_rate"], 0.875)
        self.assertEqual(row["advice_samples"], 8)
        self.assertEqual(row["actual_pass_rate"], 0.25)
        self.assertEqual(row["actual_samples"], 8)
        self.assertTrue(row["weighted_routing"]["applied"])
        decisions = self.decision_entries()
        self.assertEqual(len(decisions), 1)
        entry = decisions[0]
        self.assertEqual(entry["backend"], "ocgo")
        self.assertEqual(entry["model"], "mid-coder")
        self.assertEqual(entry["run_id"], row["run_id"])
        self.assertEqual(entry["status"], "dispatched")
        self.assertTrue(entry["routed_by_advice"])
        self.assertEqual(entry["routed_from"], {"backend": "codex", "model": decision["model"]})
        self.assertEqual(entry["advice_pass_rate"], 0.875)
        self.assertEqual(entry["advice_samples"], 8)
        self.assertEqual(entry["actual_pass_rate"], 0.25)
        self.assertEqual(entry["actual_samples"], 8)
        self.assertTrue(entry["weighted_routing"]["applied"])
        # The decision record must not move the weights it was computed from.
        weights = xs.compute_routing_weights(self.ledger_entries(), 8)["entries"]
        self.assertEqual({(r["backend"], r["runs"]) for r in weights},
                         {("codex", 8), ("ocgo", 8)})

    def test_weighted_off_dispatches_without_new_fields(self):
        """Explicit opt-out (weighted false): no weighted fields anywhere, remote run gets the
        creation-time default pick exactly as before this change."""
        (self.cards / "one.md").write_text("task one")
        self.write_policy(weighted=False, default_order=["codex", "ocgo"], min_samples=8)
        state, decision = self.seed()
        rc, summary, _ = self.run_weighted_batch(state, backend=None, model=None, card_type="fix")
        self.assertEqual(rc, 0)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "codex")
        self.assertIsNone(starts[0]["args"]["model"])
        row = summary["cards"][0]
        for key in ("weighted_routing", "routed_by_advice", "routed_from",
                    "advice_pass_rate", "advice_samples",
                    "actual_pass_rate", "actual_samples", "backend", "model"):
            self.assertNotIn(key, row)
        self.assertEqual(self.decision_entries(), [])

    def test_explicit_backend_wins(self):
        (self.cards / "one.md").write_text("task one")
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        state, decision = self.seed()
        rc, summary, _ = self.run_weighted_batch(state, backend="codex", model=None, card_type="fix")
        self.assertEqual(rc, 0)
        self.assertTrue(self.spec_json(summary)["backend_explicit"])
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "codex")
        self.assertIsNone(starts[0]["args"]["model"])
        row = summary["cards"][0]
        self.assertEqual(row["backend"], "codex")
        self.assertFalse(row["weighted_routing"]["applied"])
        self.assertIn("explicit --backend", row["weighted_routing"]["reason"])
        self.assertNotIn("routed_by_advice", row)
        decisions = self.decision_entries()
        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0]["weighted_routing"]["applied"])
        self.assertNotIn("routed_by_advice", decisions[0])

    def test_margin_blocks_small_advantage(self):
        (self.cards / "one.md").write_text("task one")
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8,
                          advice_margin=0.8)
        state, decision = self.seed()
        rc, summary, _ = self.run_weighted_batch(state, backend=None, model=None, card_type="fix")
        self.assertEqual(rc, 0)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "codex")
        row = summary["cards"][0]
        self.assertFalse(row["weighted_routing"]["applied"])
        self.assertIn("below margin", row["weighted_routing"]["reason"])
        self.assertNotIn("routed_by_advice", row)

    def test_no_history_for_default_pick_means_no_switch(self):
        (self.cards / "one.md").write_text("task one")
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        state, decision = self.seed(codex_n=0)
        rc, summary, _ = self.run_weighted_batch(state, backend=None, model=None, card_type="fix")
        self.assertEqual(rc, 0)
        starts = self.start_events()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["args"]["backend"], "codex")
        row = summary["cards"][0]
        self.assertFalse(row["weighted_routing"]["applied"])
        self.assertIn("comparison unavailable", row["weighted_routing"]["reason"])
        self.assertNotIn("routed_by_advice", row)

    def test_weighted_reroute_keeps_stacked_branches(self):
        (self.cards / "a.md").write_text("task a")
        (self.cards / "b.md").write_text("depends_on: a.md\ntask b")
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        state, decision = self.seed()
        rc, summary, _ = self.run_weighted_batch(state, backend=None, model=None, card_type="fix")
        self.assertEqual(rc, 0)
        a_row, b_row = summary["cards"]
        self.assertTrue(a_row["routed_by_advice"])
        self.assertTrue(b_row["routed_by_advice"])
        self.assertEqual(b_row["base"], a_row["commit"])
        self.assertEqual(b_row["stacked_on"], {"a.md": a_row["commit"]})
        starts = self.start_events()
        self.assertEqual({s["args"]["backend"] for s in starts}, {"ocgo"})
        self.assertEqual(len(self.decision_entries()), 2)

    def test_weighted_reroute_keeps_failed_dependency_blocked(self):
        (self.cards / "a.md").write_text("FAIL task a")
        (self.cards / "b.md").write_text("depends_on: a.md\ntask b")
        (self.cards / "c.md").write_text("depends_on: b.md\ntask c")
        self.write_policy(weighted=True, default_order=["codex", "ocgo"], min_samples=8)
        state, decision = self.seed()
        rc, summary, _ = self.run_weighted_batch(state, backend=None, model=None, card_type="fix")
        self.assertEqual(rc, 1)
        a_row, b_row, c_row = summary["cards"]
        self.assertEqual(a_row["status"], "failed")
        self.assertTrue(a_row["routed_by_advice"])
        self.assertEqual(b_row["status"], "blocked")
        self.assertEqual(b_row["blocked_by"], "a.md")
        self.assertEqual(c_row["status"], "blocked")
        self.assertEqual(c_row["blocked_by"], "b.md")
        self.assertNotIn("run_id", b_row)
        self.assertNotIn("run_id", c_row)
        # Only the dispatched card gets a routing-decision record.
        self.assertEqual(len(self.decision_entries()), 1)
        self.assertEqual(self.decision_entries()[0]["card"], "a.md")


if __name__ == "__main__":
    unittest.main()
