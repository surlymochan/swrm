"""M26 slice 1 (#207): remote runner registry + one-to-one remote dispatch.

Covers: registry validation (strict, actionable errors, no silent
downgrade), `fleet runners` listing, `fleet dispatch` happy path
(card transfer, remote run, result fetch, controller ledger record),
and the refusal paths (unreachable runner, backend not offered, at
capacity, unknown runner, missing repo mapping) — none of which may
fall back to a local run.
"""
import argparse
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import _load

xs = _load.load()

RUN_ID = "20261114-170000-abcdef"
REMOTE_RESULT = {"id": RUN_ID, "status": "done", "backend": "codex",
                 "verify": {"rc": 0, "cmd": "test -f proof.md"},
                 "branch": "swrm/m26-slice1-proof", "commit": "abc1234",
                 "seconds": 42}


class M26Slice1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m26s1-")
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.patches = [mock.patch.object(xs, "DATA", str(self.data)),
                        mock.patch.object(xs, "LEDGER", str(self.data / "ledger.jsonl")),
                        mock.patch.object(xs, "RUNS", str(self.data / "runs"))]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self.tmp.cleanup)
        for patch in self.patches:
            self.addCleanup(patch.stop)

    def config(self, hosts):
        (self.data / "fleet.json").write_text(json.dumps({"hosts": hosts}))

    def gocc(self, **overrides):
        host = {"name": "gocc", "kind": "ssh", "ssh": "gocc",
                "backends": ["codex", "pi"], "capacity": 2,
                "repos": {"swrm": "/Users/surlymo/work/swrm-m26"},
                "openswarm": "~/work/swrm-m26/bin/openswarm"}
        host.update(overrides)
        return host

    def task_file(self, text="Write the proof file.\n"):
        path = self.root / "task.md"
        path.write_text(text)
        return path

    def ledger_entries(self):
        path = self.data / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    # -- registry validation -------------------------------------------------

    def test_valid_registry_with_backends_and_capacity(self):
        self.config([{"name": "local", "kind": "local"}, self.gocc()])
        hosts = xs.fleet_config()
        self.assertEqual(hosts["gocc"]["backends"], ["codex", "pi"])
        self.assertEqual(hosts["gocc"]["capacity"], 2)

    def test_default_registry_is_local_only_without_file(self):
        hosts = xs.fleet_config()
        self.assertEqual(hosts, {"local": {"name": "local", "kind": "local"}})

    def test_invalid_registry_cases_raise_actionable_errors(self):
        bad_text = "{not json"
        cases = [
            ("malformed json", bad_text, None),
            ("top level not an object", json.dumps({"hosts": "nope"}), None),
            ("hosts not a list", json.dumps({"hosts": {"a": 1}}), None),
            ("duplicate name", None, [self.gocc(), self.gocc()]),
            ("bad kind", None, [self.gocc(kind="carrier-pigeon")]),
            ("unknown field", None, [self.gocc(surprise=True)]),
            ("empty ssh alias", None, [self.gocc(ssh="")]),
            ("ssh starting with dash", None, [self.gocc(ssh="-oProxy")]),
            ("repos not a dict", None, [self.gocc(repos=["swrm"])]),
            ("repos empty path", None, [self.gocc(repos={"swrm": ""})]),
            ("openswarm empty", None, [self.gocc(openswarm="")]),
            ("backends empty", None, [self.gocc(backends=[])]),
            ("backends unknown id", None, [self.gocc(backends=["codex", "nope"])]),
            ("backends duplicate", None, [self.gocc(backends=["codex", "codex"])]),
            ("backends not a list", None, [self.gocc(backends="codex")]),
            ("capacity zero", None, [self.gocc(capacity=0)]),
            ("capacity negative", None, [self.gocc(capacity=-1)]),
            ("capacity string", None, [self.gocc(capacity="2")]),
            ("capacity bool", None, [self.gocc(capacity=True)]),
            ("bad host name", None, [{"name": "bad name!", "kind": "local"}]),
        ]
        for label, raw, hosts in cases:
            with self.subTest(label=label):
                if raw is not None:
                    (self.data / "fleet.json").write_text(raw)
                else:
                    self.config(hosts)
                with self.assertRaises(xs.FleetConfigError) as ctx:
                    xs.fleet_config()
                self.assertIn("fleet.json", str(ctx.exception))
                self.assertIn("fix", str(ctx.exception))

    # -- fleet runners -------------------------------------------------------

    def test_runners_lists_backends_capacity_and_reachability(self):
        self.config([{"name": "local", "kind": "local"}, self.gocc(),
                     {"name": "down", "kind": "ssh", "ssh": "down-host",
                      "backends": ["pi"], "capacity": 1}])

        def fake_host_json(host, args, timeout=60, capacity_only=False):
            if host["name"] == "down":
                raise RuntimeError("down: openswarm command failed (rc=255)")
            return {"active_runs": [{"id": "r1"}]}

        output = io.StringIO()
        with mock.patch.object(xs, "host_json", side_effect=fake_host_json), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet_runners(argparse.Namespace(json=True))
        rows = {row["name"]: row for row in json.loads(output.getvalue())["runners"]}
        self.assertEqual(rows["gocc"]["backends"], ["codex", "pi"])
        self.assertEqual(rows["gocc"]["capacity"], 2)
        self.assertTrue(rows["gocc"]["reachable"])
        self.assertEqual(rows["gocc"]["active_runs"], 1)
        self.assertTrue(rows["local"]["reachable"])
        self.assertFalse(rows["down"]["reachable"])
        self.assertIn("down", rows["down"]["error"])

    # -- fleet dispatch ------------------------------------------------------

    def dispatch_args(self, **overrides):
        kwargs = {"runner": "gocc", "repo": "swrm",
                  "task_file": str(self.task_file()),
                  "backend": "codex", "title": "m26 slice 1 proof",
                  "verify": "test -f proof.md", "base": None,
                  "backends_file": None, "timeout": 900, "json": False}
        kwargs.update(overrides)
        return argparse.Namespace(**kwargs)

    def fake_transport(self, run_result=None, run_rc=0, snapshot=None):
        """(host_run, host_json) fakes: git base, card transfer, remote run,
        snapshot + _run-info. Records every host_run call."""
        calls = []
        snapshot = {"active_runs": []} if snapshot is None else snapshot
        result = REMOTE_RESULT if run_result is None else run_result

        def fake_host_run(host, command, timeout=60, input=None):
            calls.append({"host": host["name"], "command": command, "input": input})
            if "rev-parse" in command:
                return 0, b"deadbeef1234\n", b""
            if command.startswith("printf "):
                return 0, b"/home/test", b""
            if command.startswith("cat > "):
                return 0, b"", b""
            if " run " in command or command.endswith(" run") or '"run"' in command:
                return run_rc, json.dumps(result).encode(), b""
            return 0, b"", b""

        def fake_host_json(host, args, timeout=60, capacity_only=False):
            if args[:1] == ["fleet"] and args[1] == "_snapshot":
                return snapshot
            if args[:2] == ["fleet", "_run-info"]:
                return {"state": "finished", "result": result}
            raise AssertionError(f"unexpected host_json args: {args}")

        return fake_host_run, fake_host_json, calls

    def test_dispatch_happy_path_records_result_and_ledger(self):
        self.config([{"name": "local", "kind": "local"}, self.gocc()])
        run, info, calls = self.fake_transport()
        output = io.StringIO()
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet_dispatch(self.dispatch_args(), None)
        text = output.getvalue()
        summary = json.loads(text[text.rindex("{"):])
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["remote_run_id"], RUN_ID)
        self.assertEqual(summary["remote_status"], "done")
        self.assertEqual(summary["verify_rc"], 0)
        self.assertEqual(summary["remote_base"], "deadbeef1234")
        self.assertEqual(summary["ledger_line"], 1)
        # The card bytes were transferred verbatim to the runner.
        transfers = [c for c in calls if c["command"].startswith("cat > ")]
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0]["input"], b"Write the proof file.\n")
        # The remote run went over the ssh transport, never locally.
        runs = [c for c in calls if "run" in c["command"] and "rev-parse" not in c["command"]]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["host"], "gocc")
        self.assertIn("work/swrm-m26/bin/openswarm run", runs[0]["command"])
        # The task file path the remote run opens is absolute: the remote
        # Python process never expands `~` (a real dispatch failed on this
        # before the fix).
        self.assertIn("--task-file /home/test/.claude/openswarm/dispatch-",
                      runs[0]["command"])
        self.assertIn("--verify", runs[0]["command"])
        # Controller ledger: one dispatch record, joined to the remote run.
        entries = self.ledger_entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertTrue(entry["fleet_remote_dispatch"])
        self.assertEqual(entry["run_id"], RUN_ID)
        self.assertEqual(entry["status"], "dispatched")
        self.assertEqual(entry["host"], "gocc")
        self.assertEqual(entry["backend"], "codex")
        self.assertEqual(entry["verify_rc"], 0)
        self.assertEqual(entry["remote_result"]["branch"], "swrm/m26-slice1-proof")
        self.assertEqual(entry["remote_run_dir"], f"/home/test/.claude/openswarm/runs/{RUN_ID}")
        self.assertIn(RUN_ID, entry["remote_log"])
        # No local run directory was created: nothing ran locally.
        self.assertFalse((self.data / "runs").exists())

    def test_dispatch_unreachable_refuses_with_no_local_fallback(self):
        self.config([self.gocc()])

        def down_json(host, args, timeout=60, capacity_only=False):
            raise RuntimeError("gocc: openswarm command failed (rc=255)")

        with mock.patch.object(xs, "host_json", side_effect=down_json), \
                mock.patch.object(xs, "host_run") as transport:
            with self.assertRaises(xs.FleetDispatchError) as ctx:
                xs.cmd_fleet_dispatch(self.dispatch_args(), None)
        message = str(ctx.exception)
        self.assertIn("unreachable", message)
        self.assertIn("local", message)
        transport.assert_not_called()
        self.assertEqual(self.ledger_entries(), [])
        self.assertFalse((self.data / "runs").exists())

    def test_dispatch_backend_not_offered_refuses_before_transport(self):
        self.config([self.gocc()])
        with mock.patch.object(xs, "host_json") as info, \
                mock.patch.object(xs, "host_run") as transport:
            with self.assertRaises(xs.FleetDispatchError) as ctx:
                xs.cmd_fleet_dispatch(self.dispatch_args(backend="ocgo"), None)
        self.assertIn("does not offer backend 'ocgo'", str(ctx.exception))
        self.assertIn("codex, pi", str(ctx.exception))
        info.assert_not_called()
        transport.assert_not_called()
        self.assertEqual(self.ledger_entries(), [])

    def test_dispatch_at_capacity_refuses_before_transport(self):
        self.config([self.gocc()])
        snapshot = {"active_runs": [{"id": "a"}, {"id": "b"}]}
        run, info, _ = self.fake_transport(snapshot=snapshot)
        with mock.patch.object(xs, "host_run", side_effect=run) as transport, \
                mock.patch.object(xs, "host_json", side_effect=info):
            with self.assertRaises(xs.FleetDispatchError) as ctx:
                xs.cmd_fleet_dispatch(self.dispatch_args(), None)
        self.assertIn("at capacity (2/2", str(ctx.exception))
        run_calls = [c for c in transport.call_args_list
                     if " run " in str(c)]
        self.assertEqual(run_calls, [])
        self.assertEqual(self.ledger_entries(), [])

    def test_dispatch_unknown_runner_lists_registered(self):
        self.config([self.gocc()])
        with self.assertRaises(xs.FleetDispatchError) as ctx:
            xs.cmd_fleet_dispatch(self.dispatch_args(runner="nope"), None)
        self.assertIn("gocc", str(ctx.exception))

    def test_dispatch_missing_repo_mapping(self):
        self.config([self.gocc(repos={})])
        with mock.patch.object(xs, "host_json",
                               return_value={"active_runs": []}):
            with self.assertRaises(xs.FleetDispatchError) as ctx:
                xs.cmd_fleet_dispatch(self.dispatch_args(), None)
        self.assertIn("no repository mapping", str(ctx.exception))

    def test_dispatch_remote_failure_records_and_exits_with_remote_rc(self):
        self.config([self.gocc()])
        failed = dict(REMOTE_RESULT, status="failed",
                      verify={"rc": 1, "cmd": "test -f proof.md"})
        run, info, _ = self.fake_transport(run_result=failed, run_rc=3)
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                xs.cmd_fleet_dispatch(self.dispatch_args(), None)
        self.assertEqual(ctx.exception.code, 3)
        entries = self.ledger_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["remote_rc"], 3)
        self.assertEqual(entries[0]["verify_rc"], 1)
        self.assertEqual(entries[0]["remote_result"]["status"], "failed")

    def test_dispatch_home_unresolvable_refuses_before_run(self):
        self.config([self.gocc()])

        def run(host, command, timeout=60, input=None):
            if "rev-parse" in command:
                return 0, b"deadbeef1234\n", b""
            if command.startswith("printf "):
                return 1, b"", b"no home"
            raise AssertionError(f"unexpected host_run: {command}")

        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json",
                                  return_value={"active_runs": []}):
            with self.assertRaises(xs.FleetDispatchError) as ctx:
                xs.cmd_fleet_dispatch(self.dispatch_args(), None)
        self.assertIn("home directory", str(ctx.exception))
        self.assertEqual(self.ledger_entries(), [])

    def test_dispatch_record_does_not_inflate_routing_weights(self):
        dispatch = {"backend": "codex", "model": None, "card_type": "impl",
                    "status": "dispatched", "exec_pass": True,
                    "fleet_remote_dispatch": True}
        outcome = {"backend": "pi", "model": None, "card_type": "impl",
                   "status": "done", "exec_pass": True, "ts": 1.0}
        weights = xs.compute_routing_weights([dispatch, outcome], 8)["entries"]
        self.assertEqual({(row["backend"], row["runs"]) for row in weights},
                         {("pi", 1)})

    # -- ssh transport config ------------------------------------------------

    def test_ssh_argv_default_is_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENSWARM_SSH_CONFIG", None)
            self.assertEqual(xs.ssh_argv("gocc"), ["ssh", "gocc", "--"])

    def test_ssh_argv_honors_config_env(self):
        with mock.patch.dict(os.environ,
                             {"OPENSWARM_SSH_CONFIG": "/home/hatch/.ssh/config"}):
            self.assertEqual(xs.ssh_argv("gocc"),
                             ["ssh", "-F", "/home/hatch/.ssh/config", "gocc", "--"])

    def test_host_run_ssh_passes_config_to_subprocess(self):
        host = {"name": "gocc", "kind": "ssh", "ssh": "gocc"}
        with mock.patch.dict(os.environ,
                             {"OPENSWARM_SSH_CONFIG": "/home/hatch/.ssh/config"}), \
                mock.patch.object(xs.subprocess, "run",
                                  return_value=mock.Mock(returncode=0, stdout=b"",
                                                         stderr=b"")) as run:
            xs.host_run(host, "true", timeout=5)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["ssh", "-F", "/home/hatch/.ssh/config", "gocc"])
        self.assertEqual(argv[4], "--")


if __name__ == "__main__":
    unittest.main()
