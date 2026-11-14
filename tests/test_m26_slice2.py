"""M26 slice 2 (#210): multi-runner concurrent fan-out + disconnect recovery.

Covers: `fleet dispatch-many` (least-loaded capacity-aware assignment,
waves when spare capacity runs out, per-card `verify:` lines, ineligible
runners skipped, no-spare-capacity refusal with no local fallback),
`fleet recover` (lost / recovered / finished / still-running verdicts,
final vs non-final recovery entries, idempotent re-probe, explicit mode,
--redispatch to another runner, redispatch failure when the card is
gone), and the routing-weights skip for both new ledger flags.
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


def remote_result(run_id, status="done", verify_rc=0):
    return {"id": run_id, "status": status, "backend": "pi",
            "verify": {"rc": verify_rc, "cmd": "test -f proof.md"},
            "branch": f"swrm/m26-slice2-{run_id}", "commit": "abc1234",
            "seconds": 42}


class M26Slice2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m26s2-")
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.cards_dir = self.root / "cards"
        self.cards_dir.mkdir()
        self.patches = [mock.patch.object(xs, "DATA", str(self.data)),
                        mock.patch.object(xs, "LEDGER", str(self.data / "ledger.jsonl")),
                        mock.patch.object(xs, "RUNS", str(self.data / "runs"))]
        for patch in self.patches:
            patch.start()
        for patch in self.patches:
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def config(self, hosts):
        (self.data / "fleet.json").write_text(json.dumps({"hosts": hosts}))

    def runner(self, name, capacity=2, backends=("pi",), repos=("swrm",)):
        return {"name": name, "kind": "ssh", "ssh": name,
                "backends": list(backends), "capacity": capacity,
                "repos": {"swrm": f"/srv/{name}/swrm"} if "swrm" in repos else {},
                "openswarm": "~/work/swrm-m26/bin/openswarm"}

    def card(self, name, text):
        path = self.cards_dir / name
        path.write_bytes(text.encode())
        return path

    def ledger_entries(self):
        path = self.data / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def seed_dispatch(self, run_id, host="gocc", status=None, card_file=None,
                      verify_cmd="test -f proof.md", base="deadbeef1234"):
        entry = {"id": f"fleet-{host}-seed", "run_id": run_id, "ts": 1.0,
                 "title": f"fleet {host} remote dispatch: seed task",
                 "status": "dispatched", "host": host, "backend": "pi",
                 "fleet_remote_dispatch": True, "remote_run_id": run_id,
                 "remote_repo": "swrm", "remote_base": base,
                 "remote_rc": 0, "verify_rc": 0,
                 "remote_run_dir": f"/home/test/.claude/openswarm/runs/{run_id}",
                 "card_file": card_file, "verify_cmd": verify_cmd}
        if status is not None:
            entry["remote_result"] = {"status": status, "verify_rc": 0}
        with open(self.data / "ledger.jsonl", "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    def fake_transport(self, snapshots=None, run_infos=None, unreachable=()):
        """(host_run, host_json) fakes with per-host run ids and snapshots.

        snapshots: host name -> active_runs list (default empty).
        run_infos: run id -> _run-info dict (default finished + result).
        unreachable: host names whose _snapshot raises.
        """
        snapshots = snapshots or {}
        run_infos = run_infos or {}
        counters = {}

        def fake_host_run(host, command, timeout=60, input=None):
            if "rev-parse" in command:
                return 0, b"deadbeef1234\n", b""
            if command.startswith("printf "):
                return 0, b"/home/test", b""
            if command.startswith("cat > "):
                return 0, b"", b""
            if " run " in command:
                n = counters.get(host["name"], 0) + 1
                counters[host["name"]] = n
                run_id = f"20261114-180000-{host['name']}-{n}"
                return 0, json.dumps(remote_result(run_id)).encode(), b""
            return 0, b"", b""

        def fake_host_json(host, args, timeout=60, capacity_only=False):
            if host["name"] in unreachable:
                raise RuntimeError(f"{host['name']}: openswarm command failed (rc=255)")
            if args[:1] == ["fleet"] and args[1] == "_snapshot":
                return {"active_runs": snapshots.get(host["name"], [])}
            if args[:2] == ["fleet", "_run-info"]:
                run_id = args[2]
                if run_id in run_infos:
                    return run_infos[run_id]
                return {"state": "finished", "result": remote_result(run_id)}
            raise AssertionError(f"unexpected host_json args: {args}")

        return fake_host_run, fake_host_json

    def many_args(self, **overrides):
        kwargs = {"cards_dir": str(self.cards_dir), "repo": "swrm",
                  "backend": "pi", "verify": None, "base": None,
                  "backends_file": None, "timeout": 900, "json": True}
        kwargs.update(overrides)
        return argparse.Namespace(**kwargs)

    def recover_args(self, **overrides):
        kwargs = {"run_id": None, "runner": None, "redispatch": False,
                  "card_file": None, "repo": None, "backend": None,
                  "verify": None, "base": None, "timeout": 900, "json": True}
        kwargs.update(overrides)
        return argparse.Namespace(**kwargs)

    # -- per-card verify lines ---------------------------------------------

    def test_parse_and_strip_card_verify(self):
        self.assertEqual(xs.parse_card_verify("Do the thing.\nverify: test -f a.md\n"), "test -f a.md")
        self.assertIsNone(xs.parse_card_verify("Do the thing.\n"))
        self.assertIsNone(xs.parse_card_verify("Do the thing.\nverify:   \n"))
        stripped = xs.strip_card_verify(b"Do the thing.\nverify: test -f a.md\nDone.\n")
        self.assertEqual(stripped, b"Do the thing.\nDone.\n")
        self.assertEqual(xs.strip_card_verify("No line here.\n"), "No line here.\n")

    # -- dispatch-many ------------------------------------------------------

    def test_dispatch_many_spreads_across_runners(self):
        self.config([self.runner("gocc"), self.runner("mac")])
        self.card("a.md", "Write proof A.\nverify: test -f A.md\n")
        self.card("b.md", "Write proof B.\n")
        self.card("c.md", "Write proof C.\n")
        run, info = self.fake_transport()
        output = io.StringIO()
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet_dispatch_many(self.many_args())
        summary = json.loads(output.getvalue())
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["waves"], 1)
        # Least-loaded first, name tie-break: gocc, mac, gocc.
        self.assertEqual(summary["distribution"], {"gocc": 2, "mac": 1})
        self.assertEqual({row["card"] for row in summary["cards"]}, {"a.md", "b.md", "c.md"})
        for row in summary["cards"]:
            self.assertTrue(row["ok"])
            self.assertEqual(row["remote_status"], "done")
            self.assertEqual(row["verify_rc"], 0)
        # Three dispatch records + one flagged summary record.
        entries = self.ledger_entries()
        dispatches = [e for e in entries if e.get("fleet_remote_dispatch")]
        self.assertEqual(len(dispatches), 3)
        by_card = {os.path.basename(e["card_file"]): e for e in dispatches}
        self.assertEqual(by_card["a.md"]["verify_cmd"], "test -f A.md")
        self.assertIsNone(by_card["b.md"]["verify_cmd"])
        summaries = [e for e in entries if e.get("fleet_dispatch_many")]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["distribution"], {"gocc": 2, "mac": 1})

    def test_dispatch_many_waves_when_capacity_runs_out(self):
        self.config([self.runner("gocc", capacity=1), self.runner("mac", capacity=1)])
        for name in ("a.md", "b.md", "c.md"):
            self.card(name, f"Write proof {name}.\n")
        run, info = self.fake_transport()
        output = io.StringIO()
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet_dispatch_many(self.many_args())
        summary = json.loads(output.getvalue())
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["waves"], 2)
        self.assertEqual(summary["distribution"], {"gocc": 2, "mac": 1})
        self.assertEqual(len(self.ledger_entries()), 4)

    def test_dispatch_many_no_spare_capacity_refuses_loudly(self):
        self.config([self.runner("gocc"), self.runner("mac")])
        self.card("a.md", "Write proof A.\n")
        busy = [{"id": "x1"}, {"id": "x2"}]
        run, info = self.fake_transport(snapshots={"gocc": busy, "mac": busy})
        with mock.patch.object(xs, "host_run", side_effect=run) as transport, \
                mock.patch.object(xs, "host_json", side_effect=info):
            with self.assertRaises(xs.FleetDispatchError) as ctx:
                xs.cmd_fleet_dispatch_many(self.many_args())
        message = str(ctx.exception)
        self.assertIn("no runner can take", message)
        self.assertIn("at capacity (2/2", message)
        transport.assert_not_called()
        self.assertEqual(self.ledger_entries(), [])

    def test_dispatch_many_skips_runner_without_backend(self):
        self.config([self.runner("gocc", backends=("codex",)), self.runner("mac")])
        self.card("a.md", "Write proof A.\n")
        self.card("b.md", "Write proof B.\n")
        run, info = self.fake_transport()
        output = io.StringIO()
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet_dispatch_many(self.many_args())
        summary = json.loads(output.getvalue())
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["distribution"], {"mac": 2})

    def test_dispatch_many_global_verify_applies_without_per_card_line(self):
        self.config([self.runner("gocc")])
        self.card("a.md", "Write proof A.\n")
        seen = []
        transferred = []

        def run(host, command, timeout=60, input=None):
            seen.append(command)
            if "rev-parse" in command:
                return 0, b"deadbeef1234\n", b""
            if command.startswith("printf "):
                return 0, b"/home/test", b""
            if command.startswith("cat > "):
                transferred.append(input)
                return 0, b"", b""
            if " run " in command:
                return 0, json.dumps(remote_result("20261114-180000-gocc-1")).encode(), b""
            return 0, b"", b""

        _, info = self.fake_transport()
        output = io.StringIO()
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet_dispatch_many(self.many_args(verify="test -f global.md"))
        remote_runs = [c for c in seen if " run " in c]
        self.assertEqual(len(remote_runs), 1)
        self.assertIn("--verify", remote_runs[0])
        self.assertIn("test -f global.md", remote_runs[0])
        # The transferred card carries no verify line.
        self.assertEqual(len(transferred), 1)
        self.assertNotIn(b"verify:", transferred[0])

    def test_dispatch_many_records_do_not_inflate_routing_weights(self):
        entries = [
            {"backend": "pi", "model": None, "card_type": "impl",
             "status": "dispatched", "exec_pass": True, "fleet_dispatch_many": True},
            {"backend": "pi", "model": None, "card_type": "impl",
             "status": "dispatched", "exec_pass": True, "fleet_remote_recovery": True},
            {"backend": "pi", "model": None, "card_type": "impl",
             "status": "done", "exec_pass": True, "ts": 1.0},
        ]
        weights = xs.compute_routing_weights(entries, 8)["entries"]
        self.assertEqual({(row["backend"], row["runs"]) for row in weights},
                         {("pi", 1)})

    # -- recover -------------------------------------------------------------

    def test_recover_lost_runner_unreachable_is_non_final(self):
        self.config([self.runner("gocc"), self.runner("mac")])
        self.seed_dispatch("20261114-180000-gocc-1", host="gocc")
        run, info = self.fake_transport(unreachable=("gocc",))
        output = io.StringIO()
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(output):
            summary = xs.cmd_fleet_recover(self.recover_args())
        self.assertEqual(len(summary["recovered"]), 1)
        outcome = summary["recovered"][0]
        self.assertEqual(outcome["disposition"], "lost")
        self.assertIn("unreachable", outcome["reason"])
        self.assertFalse(outcome["recovered_final"])
        # Non-final: a second scan re-probes and leaves a second trace.
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(io.StringIO()):
            summary2 = xs.cmd_fleet_recover(self.recover_args())
        self.assertEqual(len(summary2["recovered"]), 1)
        recoveries = [e for e in self.ledger_entries()
                      if e.get("fleet_remote_recovery")]
        self.assertEqual(len(recoveries), 2)
        self.assertTrue(all(e["disposition"] == "lost" for e in recoveries))

    def test_recover_dead_remote_run_is_final(self):
        self.config([self.runner("gocc"), self.runner("mac")])
        self.seed_dispatch("20261114-180000-gocc-1", host="gocc")
        infos = {"20261114-180000-gocc-1": {"state": "dead", "status": "running",
                                            "result": None}}
        run, info = self.fake_transport(run_infos=infos)
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(io.StringIO()):
            summary = xs.cmd_fleet_recover(self.recover_args())
        outcome = summary["recovered"][0]
        self.assertEqual(outcome["disposition"], "recovered")
        self.assertTrue(outcome["recovered_final"])
        self.assertIsNotNone(outcome["ledger_line"])
        # Final: a second scan finds nothing to do.
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(io.StringIO()):
            summary2 = xs.cmd_fleet_recover(self.recover_args())
        self.assertEqual(summary2["recovered"], [])

    def test_recover_finished_remote_run_records_late_result(self):
        self.config([self.runner("gocc")])
        self.seed_dispatch("20261114-180000-gocc-1", host="gocc")
        infos = {"20261114-180000-gocc-1":
                 {"state": "finished", "result": remote_result("20261114-180000-gocc-1")}}
        run, info = self.fake_transport(run_infos=infos)
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(io.StringIO()):
            summary = xs.cmd_fleet_recover(self.recover_args())
        outcome = summary["recovered"][0]
        self.assertEqual(outcome["disposition"], "finished")
        self.assertTrue(outcome["recovered_final"])
        self.assertEqual(outcome["remote_result"]["status"], "done")

    def test_recover_still_running_writes_no_ledger_line(self):
        self.config([self.runner("gocc")])
        self.seed_dispatch("20261114-180000-gocc-1", host="gocc")
        infos = {"20261114-180000-gocc-1": {"state": "running", "result": None}}
        run, info = self.fake_transport(run_infos=infos)
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(io.StringIO()):
            summary = xs.cmd_fleet_recover(self.recover_args())
        self.assertEqual(summary["recovered"][0]["disposition"], "still-running")
        self.assertEqual(self.ledger_entries(), [e for e in self.ledger_entries()
                                                 if e.get("fleet_remote_dispatch")])

    def test_recover_skips_done_dispatches_and_idless_entries(self):
        self.config([self.runner("gocc")])
        self.seed_dispatch("20261114-180000-gocc-1", host="gocc", status="done")
        self.seed_dispatch(None, host="gocc")
        run, info = self.fake_transport()
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(io.StringIO()):
            summary = xs.cmd_fleet_recover(self.recover_args())
        self.assertEqual(len(summary["recovered"]), 1)
        self.assertEqual(summary["recovered"][0]["disposition"], "skipped")
        self.assertIn("fleet recover <run-id>", summary["recovered"][0]["reason"])

    def test_recover_skips_backfilled_id_when_remote_run_id_missing(self):
        # append_ledger backfills a missing run_id from the entry id, so a
        # dispatch whose remote run id never came back is stored with
        # run_id == entry id and remote_run_id None. Scan mode must read
        # remote_run_id (the authoritative remote id) and skip the entry,
        # never probe the entry id as if it were a run.
        self.config([self.runner("gocc")])
        entry = {"id": "fleet-gocc-abcdef12", "run_id": "fleet-gocc-abcdef12",
                 "ts": 1.0, "title": "fleet gocc remote dispatch: seed task",
                 "status": "dispatched", "host": "gocc", "backend": "pi",
                 "fleet_remote_dispatch": True, "remote_run_id": None,
                 "remote_repo": "swrm", "remote_base": "deadbeef1234",
                 "remote_rc": 255, "verify_rc": None, "remote_run_dir": None,
                 "card_file": None, "verify_cmd": None,
                 "fetch_error": "could not determine remote run id from the runner reply"}
        with open(self.data / "ledger.jsonl", "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        run, info = self.fake_transport()
        with mock.patch.object(xs, "host_run", side_effect=run) as transport, \
                mock.patch.object(xs, "host_json", side_effect=info) as info_mock, \
                contextlib.redirect_stdout(io.StringIO()):
            summary = xs.cmd_fleet_recover(self.recover_args())
        self.assertEqual(len(summary["recovered"]), 1)
        self.assertEqual(summary["recovered"][0]["disposition"], "skipped")
        transport.assert_not_called()
        info_mock.assert_not_called()
        self.assertEqual([e for e in self.ledger_entries()
                          if e.get("fleet_remote_recovery")], [])

    def test_recover_explicit_mode_needs_runner(self):
        self.config([self.runner("gocc")])
        with self.assertRaises(xs.FleetDispatchError) as ctx:
            xs.cmd_fleet_recover(self.recover_args(run_id="20261114-180000-gocc-1"))
        self.assertIn("--runner", str(ctx.exception))

    def test_recover_explicit_mode_rejects_wrong_runner(self):
        self.config([self.runner("gocc"), self.runner("mac")])
        self.seed_dispatch("20261114-180000-gocc-1", host="gocc")
        with self.assertRaises(xs.FleetDispatchError) as ctx:
            xs.cmd_fleet_recover(self.recover_args(run_id="20261114-180000-gocc-1",
                                                   runner="mac"))
        self.assertIn("not 'mac'", str(ctx.exception))

    def test_recover_redispatch_moves_task_to_another_runner(self):
        self.config([self.runner("gocc"), self.runner("mac")])
        card = self.card("lost.md", "Write the lost proof.\nverify: test -f lost.md\n")
        self.seed_dispatch("20261114-180000-gocc-1", host="gocc",
                           card_file=str(card), verify_cmd="test -f lost.md")
        run, info = self.fake_transport(unreachable=("gocc",))
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(io.StringIO()):
            summary = xs.cmd_fleet_recover(self.recover_args(redispatch=True))
        outcome = summary["recovered"][0]
        self.assertEqual(outcome["disposition"], "lost")
        self.assertTrue(outcome["recovered_final"])
        redis = outcome["redispatch"]
        self.assertEqual(redis["runner"], "mac")
        self.assertTrue(redis["ok"])
        self.assertEqual(redis["remote_status"], "done")
        # The new dispatch landed on mac with the original verify command.
        dispatches = [e for e in self.ledger_entries() if e.get("fleet_remote_dispatch")]
        self.assertEqual(len(dispatches), 2)
        self.assertEqual(dispatches[1]["host"], "mac")
        self.assertEqual(dispatches[1]["verify_cmd"], "test -f lost.md")

    def test_recover_redispatch_fails_honestly_when_card_is_gone(self):
        self.config([self.runner("gocc"), self.runner("mac")])
        self.seed_dispatch("20261114-180000-gocc-1", host="gocc",
                           card_file=str(self.root / "gone.md"))
        run, info = self.fake_transport(unreachable=("gocc",))
        with mock.patch.object(xs, "host_run", side_effect=run), \
                mock.patch.object(xs, "host_json", side_effect=info), \
                contextlib.redirect_stdout(io.StringIO()):
            summary = xs.cmd_fleet_recover(self.recover_args(redispatch=True))
        outcome = summary["recovered"][0]
        self.assertEqual(outcome["disposition"], "lost")
        self.assertFalse(outcome["recovered_final"])
        self.assertIsNone(outcome["redispatch"])
        self.assertIn("no longer available", outcome["reason"])


if __name__ == "__main__":
    unittest.main()
