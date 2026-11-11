"""Weighted routing enforcement (M6, #114 / #115).

When routing.weighted is true, ledger advice moves the live route: a run
follows the advised backend when its observed pass rate beats the default
pick by at least routing.advice_margin. With weighted false (explicit
opt-out) advice stays record-only and the route is unchanged.
Run: python3 -m unittest discover -s tests   (or: python3 tests/test_weighted_routing.py)
"""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _load

xs = _load.load()
xs.breakers = lambda: {}  # isolate from any breakers on disk
xs.load_policy = lambda: xs.jload(f"{xs.SKILL_DIR}/policy.json", {})  # ignore policy.local.json overrides

POLICY = xs.load_policy()
POLICY["codex"]["require_default_model"] = False
with open(_load.FIXTURE_STATE) as f:
    BASE = json.load(f)
NOW = time.time()
OCGO_MODEL = "mid-coder"  # balanced ocgo model in the fixture state


def win(used, hours_left, minutes):
    return xs.window_stats(used * 100, minutes, NOW + hours_left * 3600)


def scenario():
    st = copy.deepcopy(BASE)
    st["codex"]["limits"] = {"codex": {"windows": {"seven_day": win(0.3, 100, 10080)}}}
    st["claude"].update({"provider": "official", "metered_by_anthropic": True, "stale": False,
                         "five_hour": win(0.2, 3, 300), "seven_day": win(0.3, 100, 10080)})
    st["deepseek"]["balance"] = 55.0
    for m in st["ocgo"]["models"]:
        m["usage"] = {"5h": 0.0, "7d": 0.0, "30d": 0.0}
    return st


def weighted_policy(**overrides):
    policy = copy.deepcopy(POLICY)
    policy["routing"]["default_order"] = ["codex", "ocgo"]
    policy["routing"]["min_samples"] = 8
    policy["routing"].update(overrides)
    return policy


def rows_for(backend, model, passes, n=8):
    return [{"id": f"{backend[:2]}{i}", "backend": backend, "model": model, "card_type": "fix",
             "status": "done", "exec_pass": i < passes, "attempts": [{}], "seconds": 10,
             "tokens": {"total": 100}} for i in range(n)]


def prof(**kw):
    return {"type": "impl", "difficulty": 3, **kw}


class WeightedRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-weighted-")
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.ledger = self.data / "ledger.jsonl"
        self.runs = self.data / "runs"
        self.constants = {key: getattr(xs, key) for key in ("DATA", "LEDGER", "RUNS", "HOME")}
        xs.DATA, xs.LEDGER, xs.RUNS, xs.HOME = str(self.data), str(self.ledger), str(self.runs), str(self.root)

    def tearDown(self):
        for key, value in self.constants.items():
            setattr(xs, key, value)
        self.tmp.cleanup()

    def write_ledger(self, entries):
        self.data.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    def seed(self, policy, st, codex_pass=2, ocgo_pass=7, codex_n=8, ocgo_n=8):
        """Seed ledger rows around the live default pick; return its model."""
        decision = xs.route(prof(), st, policy)
        self.assertTrue(decision["ok"], f"unroutable: {decision}")
        self.assertEqual(decision["backend"], "codex")
        rows = rows_for("codex", decision["model"], codex_pass, codex_n)
        rows += rows_for("ocgo", OCGO_MODEL, ocgo_pass, ocgo_n)
        self.write_ledger(rows)
        return decision

    def test_weighted_off_keeps_record_only(self):
        policy = weighted_policy(weighted=False)
        st = scenario()
        decision = self.seed(policy, st)
        advice = xs.routing_advice(prof(), "fix", st, policy, decision)
        self.assertEqual(advice["backend"], "ocgo")
        self.assertEqual(advice["pass_rate"], 0.875)
        self.assertEqual(advice["actual_pass_rate"], 0.25)
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertIn("weighted is false", info["reason"])

    def test_weighted_on_switches_to_advised_backend(self):
        policy = weighted_policy(weighted=True)
        st = scenario()
        decision = self.seed(policy, st)
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "ocgo")
        self.assertEqual(new["model"], OCGO_MODEL)
        self.assertTrue(new["routed_by_advice"])
        self.assertEqual(new["routed_from"], {"backend": "codex", "model": decision["model"]})
        self.assertEqual(new["advice_pass_rate"], 0.875)
        self.assertEqual(new["actual_pass_rate"], 0.25)
        self.assertEqual(new["advice_samples"], 8)
        self.assertEqual(new["actual_samples"], 8)
        self.assertIn("codex", new["fallbacks"])
        self.assertTrue(info["applied"])
        self.assertAlmostEqual(info["advantage"], 0.625)
        self.assertEqual(info["margin"], 0.10)
        self.assertTrue(any("weighted routing" in r for r in new["reasons"]))

    def test_weighted_on_switches_to_advised_custom_backend(self):
        """M6 slice 2 blocker (#117): advice must see custom backends.

        Regression test for the slice-1 bug where routing_advice() only
        checked the built-in PLANNERS table, so a custom backend with a
        1.00 pass rate against a 0.00 default pick never got advice and
        weighted routing never rerouted to it (two real runs stayed on
        echo-bad and failed; evidence in m6-slice2-evidence-2026-11-11.md).
        """
        policy = weighted_policy(weighted=True)
        policy["routing"]["default_order"] = ["echo-bad", "echo-good"]
        st = scenario()
        custom = [
            {"id": "echo-bad", "name": "Echo Bad", "command": ["python3", "--version"],
             "env": {}, "quota_source": "none", "model": "echo-bad-1"},
            {"id": "echo-good", "name": "Echo Good", "command": ["python3", "--version"],
             "env": {}, "quota_source": "none", "model": "echo-good-1"},
        ]
        decision = xs.route(prof(), st, policy, custom_backends=custom)
        self.assertTrue(decision["ok"], f"unroutable: {decision}")
        self.assertEqual(decision["backend"], "echo-bad")
        rows = rows_for("echo-bad", "echo-bad-1", 0) + rows_for("echo-good", "echo-good-1", 8)
        self.write_ledger(rows)
        advice = xs.routing_advice(prof(), "fix", st, policy, decision, custom_backends=custom)
        self.assertEqual(advice["backend"], "echo-good")
        self.assertEqual(advice["pass_rate"], 1.0)
        self.assertEqual(advice["actual_pass_rate"], 0.0)
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision,
                                              custom_backends=custom)
        self.assertEqual(new["backend"], "echo-good")
        self.assertEqual(new["model"], "echo-good-1")
        self.assertTrue(new["routed_by_advice"])
        self.assertEqual(new["routed_from"], {"backend": "echo-bad", "model": "echo-bad-1"})
        self.assertEqual(new["advice_pass_rate"], 1.0)
        self.assertEqual(new["actual_pass_rate"], 0.0)
        self.assertTrue(info["applied"])
        self.assertAlmostEqual(info["advantage"], 1.0)

    def test_margin_blocks_small_advantage(self):
        policy = weighted_policy(weighted=True, advice_margin=0.8)
        st = scenario()
        decision = self.seed(policy, st)
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertIn("below margin", info["reason"])

    def test_explicit_backend_wins(self):
        policy = weighted_policy(weighted=True)
        st = scenario()
        self.seed(policy, st)
        decision = xs.route(prof(backend="codex"), st, policy)
        self.assertEqual(decision["backend"], "codex")
        new, info = xs.apply_weighted_routing(prof(backend="codex"), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertIn("explicit --backend", info["reason"])

    def test_no_history_for_default_pick_means_no_switch(self):
        policy = weighted_policy(weighted=True)
        st = scenario()
        decision = xs.route(prof(), st, policy)
        self.write_ledger(rows_for("ocgo", OCGO_MODEL, 7))
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertIn("comparison unavailable", info["reason"])

    def test_below_min_samples_means_no_switch(self):
        policy = weighted_policy(weighted=True)
        st = scenario()
        decision = self.seed(policy, st, codex_pass=2, ocgo_pass=4, ocgo_n=4)
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertIn("agrees with the default pick", info["reason"])

    def test_advice_margin_validation(self):
        self.assertEqual(xs.routing_advice_margin({}), 0.10)
        self.assertEqual(xs.routing_advice_margin({"routing": {"advice_margin": 0.25}}), 0.25)
        for bad in (-0.1, 1.0, 2, "0.1", True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    xs.routing_advice_margin({"routing": {"advice_margin": bad}})

    def test_batch_weighted_route_off_makes_no_routing_calls(self):
        """M7 slice 1 (#122): with weighted false the batch path must not
        even ask route()/get_state(), so dispatch stays byte-identical."""
        policy = weighted_policy(weighted=False)
        st = scenario()
        spec = {"backend": "codex", "backend_explicit": False, "model": None, "card_type": "fix"}
        with mock.patch.object(xs, "route", side_effect=AssertionError("route must not be called")), \
                mock.patch.object(xs, "get_state", side_effect=AssertionError("get_state must not be called")):
            backend, model, info, decision = xs.batch_weighted_route(spec, policy)
        self.assertEqual((backend, model, info, decision), ("codex", None, None, None))

    def test_batch_weighted_route_falls_back_when_advised_backend_fails_to_plan(self):
        policy = weighted_policy(weighted=True)
        st = scenario()
        self.seed(policy, st)
        real_route = xs.route

        def flaky_route(prof, state, pol, exclude=(), custom_backends=()):
            if prof.get("backend") == "ocgo":
                return {"ok": False, "reasons": ["ocgo planner unavailable"]}
            return real_route(prof, state, pol, exclude, custom_backends)

        spec = {"backend": "codex", "backend_explicit": False, "model": None, "card_type": "fix"}
        with mock.patch.object(xs, "get_state", return_value=st), \
                mock.patch.object(xs, "route", side_effect=flaky_route):
            backend, model, info, new_decision = xs.batch_weighted_route(spec, policy)
        self.assertEqual(backend, "codex")
        self.assertFalse(info["applied"])
        self.assertIn("failed to plan", info["reason"])
        # The fallback returns the original (default) decision, unmodified.
        self.assertEqual(new_decision["backend"], "codex")
        self.assertNotIn("routed_by_advice", new_decision)

    def test_batch_weighted_route_falls_back_when_state_is_unavailable(self):
        policy = weighted_policy(weighted=True)
        st = scenario()
        self.seed(policy, st)
        spec = {"backend": "codex", "backend_explicit": False, "model": None, "card_type": "fix"}
        with mock.patch.object(xs, "get_state", side_effect=RuntimeError("probe failed")):
            backend, model, info, new_decision = xs.batch_weighted_route(spec, policy)
        self.assertEqual(backend, "codex")
        self.assertFalse(info["applied"])
        self.assertIn("unavailable", info["reason"])
        self.assertIsNone(new_decision)

    def test_batch_weighted_route_treats_unflagged_spec_backend_as_explicit(self):
        """Specs written before backend_explicit existed carry a backend but
        no flag; treat that backend as explicit so an old queued batch never
        reroutes."""
        policy = weighted_policy(weighted=True)
        st = scenario()
        self.seed(policy, st)
        spec = {"backend": "codex", "model": None, "card_type": "fix"}
        with mock.patch.object(xs, "get_state", return_value=st):
            backend, model, info, new_decision = xs.batch_weighted_route(spec, policy)
        self.assertEqual(backend, "codex")
        self.assertFalse(info["applied"])
        self.assertIn("explicit --backend", info["reason"])
        self.assertIsNone(new_decision)

    def test_cmd_run_executes_on_advised_backend(self):
        policy = weighted_policy(weighted=True)
        state = scenario()
        decision = self.seed(policy, state)
        expected_model = decision["model"]
        ocgo = xs.route({**prof(), "backend": "ocgo", "model": OCGO_MODEL}, state, policy)
        self.assertTrue(ocgo["ok"])
        args = type("Args", (), {
            "card_type": "fix", "rework_of": None, "run_id": None, "prompt": "implement fix",
            "task_file": None, "title": "fix", "type": "impl", "difficulty": 3, "context_tokens": 0,
            "backend": None, "model": None, "effort": None, "budget_minutes": 5, "dry_run": False,
            "detach": False, "cwd": str(self.root), "no_worktree": True, "base": None,
            "keep_worktree": True, "verify": None, "paths": [], "no_fallback": False,
            "verify_baseline": False,
        })()
        executor_attempt = {"backend": "ocgo", "seconds": 1, "error_class": None,
                            "meta": {"usage": {"input": 3, "output": 4}}}
        output = io.StringIO()
        with mock.patch.object(xs, "get_state", return_value=state), \
                mock.patch.object(xs, "admit_budget"), \
                mock.patch.object(xs, "prepare_workspace", return_value={"mode": "direct", "path": str(self.root),
                                                                           "root": str(self.root), "repo": None,
                                                                           "base": None}), \
                mock.patch.object(xs, "run_workspace_prepare", return_value=None), \
                mock.patch.object(xs, "add_lessons_to_prompt", return_value=("prompt", [])), \
                mock.patch.object(xs, "run_executor_with_rate_limit",
                                  return_value=([executor_attempt], {"status": "partial", "summary": "ok",
                                                                    "files_changed": []}, None)), \
                mock.patch.object(xs, "probe", return_value=state), \
                mock.patch.object(xs, "finalize_workspace", return_value={"branch": None, "commit": "abc123",
                                                                           "diffstat": "", "patch": None,
                                                                           "worktree": None, "merge_hint": None}), \
                mock.patch.object(xs, "cleanup_worktree"), \
                mock.patch.object(xs, "changed_paths", return_value=[]), \
                contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as result:
                xs.cmd_run(args, policy)
        self.assertEqual(result.exception.code, 1)
        run = json.loads(output.getvalue())
        self.assertEqual(run["backend"], "ocgo")
        self.assertTrue(run["weighted_routing"]["applied"])
        self.assertEqual(run["routing_advice"]["backend"], "ocgo")
        # M6 slice 3 (#119): the reroute decision itself must land in the
        # result.json output, mirroring the route/dry-run decision object.
        self.assertTrue(run["routed_by_advice"])
        self.assertEqual(run["routed_from"], {"backend": "codex", "model": expected_model})
        self.assertEqual(run["advice_pass_rate"], 7 / 8)
        self.assertEqual(run["advice_samples"], 8)
        self.assertEqual(run["actual_pass_rate"], 2 / 8)
        self.assertEqual(run["actual_samples"], 8)
        recorded = json.loads(self.ledger.read_text().splitlines()[-1])
        self.assertEqual(recorded["backend"], "ocgo")
        self.assertEqual(recorded["routing_advice"]["backend"], "ocgo")
        self.assertTrue(recorded["weighted_routing"]["applied"])
        self.assertEqual(recorded["weighted_routing"]["from"]["model"], expected_model)
        self.assertTrue(recorded["exec_pass"])  # partial with a commit is an execution pass
        # ... and in the ledger row too.
        self.assertTrue(recorded["routed_by_advice"])
        self.assertEqual(recorded["routed_from"], {"backend": "codex", "model": expected_model})
        self.assertEqual(recorded["advice_pass_rate"], 7 / 8)
        self.assertEqual(recorded["advice_samples"], 8)
        self.assertEqual(recorded["actual_pass_rate"], 2 / 8)
        self.assertEqual(recorded["actual_samples"], 8)


class FleetWeightedRoutingTests(unittest.TestCase):
    """M7 slice 2 (#124): `fleet exec -- run ...` is a fleet dispatch, so
    the controller applies weighted routing before forwarding, reusing the
    same apply_weighted_routing() decision path as single runs and batch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-fleet-weighted-")
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.ledger = self.data / "ledger.jsonl"
        self.runs = self.data / "runs"
        self.constants = {key: getattr(xs, key) for key in ("DATA", "LEDGER", "RUNS", "HOME")}
        xs.DATA, xs.LEDGER, xs.RUNS, xs.HOME = str(self.data), str(self.ledger), str(self.runs), str(self.root)

    def tearDown(self):
        for key, value in self.constants.items():
            setattr(xs, key, value)
        self.tmp.cleanup()

    def write_ledger(self, entries):
        self.data.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    def seed(self, policy, st, codex_pass=2, ocgo_pass=7, codex_n=8, ocgo_n=8):
        """Seed ledger rows around the live default pick; return its model."""
        decision = xs.route(prof(), st, policy)
        self.assertTrue(decision["ok"], f"unroutable: {decision}")
        self.assertEqual(decision["backend"], "codex")
        rows = rows_for("codex", decision["model"], codex_pass, codex_n)
        rows += rows_for("ocgo", OCGO_MODEL, ocgo_pass, ocgo_n)
        self.write_ledger(rows)
        return decision

    def command(self, **flags):
        cmd = ["run", "--task-file", "task.md", "--card-type", "fix",
               "--cwd", str(self.root), "--budget-minutes", "5"]
        for key, value in flags.items():
            cmd += ["--" + key.replace("_", "-"), str(value)]
        return cmd

    def test_weighted_off_makes_no_routing_calls(self):
        """With weighted false the fleet path must not even ask route() /
        get_state(), so forwarding stays byte-identical."""
        policy = weighted_policy(weighted=False)
        cmd = self.command()
        with mock.patch.object(xs, "route", side_effect=AssertionError("route must not be called")), \
                mock.patch.object(xs, "get_state", side_effect=AssertionError("get_state must not be called")):
            eff, info, decision = xs.fleet_weighted_route(cmd, policy)
        self.assertEqual((eff, info, decision), (cmd, None, None))

    def test_non_run_command_passes_through(self):
        policy = weighted_policy(weighted=True)
        cmd = ["lane", "status"]
        with mock.patch.object(xs, "route", side_effect=AssertionError("route must not be called")), \
                mock.patch.object(xs, "get_state", side_effect=AssertionError("get_state must not be called")):
            self.assertEqual(xs.fleet_weighted_route(cmd, policy), (cmd, None, None))

    def test_weighted_on_injects_advised_backend_after_run(self):
        policy = weighted_policy(weighted=True)
        st = scenario()
        decision = self.seed(policy, st)
        with mock.patch.object(xs, "get_state", return_value=st):
            eff, info, new_decision = xs.fleet_weighted_route(self.command(), policy)
        self.assertTrue(info["applied"])
        self.assertTrue(new_decision["routed_by_advice"])
        self.assertEqual(new_decision["routed_from"], {"backend": "codex", "model": decision["model"]})
        self.assertEqual(eff[:5], ["run", "--backend", "ocgo", "--model", OCGO_MODEL])
        self.assertEqual(eff[5:], self.command()[1:])
        # The injected pick is explicit, so the remote run will not re-route it.
        self.assertEqual(eff[eff.index("--backend") + 1], "ocgo")

    def test_explicit_backend_wins(self):
        policy = weighted_policy(weighted=True)
        st = scenario()
        self.seed(policy, st)
        cmd = self.command(backend="codex")
        with mock.patch.object(xs, "get_state", return_value=st):
            eff, info, decision = xs.fleet_weighted_route(cmd, policy)
        self.assertEqual(eff, cmd)
        self.assertFalse(info["applied"])
        self.assertIn("explicit --backend", info["reason"])
        self.assertIsNone(decision)

    def test_margin_blocks_small_advantage(self):
        policy = weighted_policy(weighted=True, advice_margin=0.8)
        st = scenario()
        self.seed(policy, st)
        cmd = self.command()
        with mock.patch.object(xs, "get_state", return_value=st):
            eff, info, decision = xs.fleet_weighted_route(cmd, policy)
        self.assertEqual(eff, cmd)
        self.assertFalse(info["applied"])
        self.assertIn("below margin", info["reason"])
        self.assertEqual(decision["backend"], "codex")

    def test_no_history_for_default_pick_means_no_switch(self):
        policy = weighted_policy(weighted=True)
        st = scenario()
        decision = xs.route(prof(), st, policy)
        self.write_ledger(rows_for("ocgo", OCGO_MODEL, 7))
        cmd = self.command()
        with mock.patch.object(xs, "get_state", return_value=st):
            eff, info, new_decision = xs.fleet_weighted_route(cmd, policy)
        self.assertEqual(eff, cmd)
        self.assertFalse(info["applied"])
        self.assertIn("comparison unavailable", info["reason"])
        self.assertEqual(new_decision["backend"], decision["backend"])

    def test_falls_back_when_advised_backend_fails_to_plan(self):
        policy = weighted_policy(weighted=True)
        st = scenario()
        self.seed(policy, st)
        real_route = xs.route

        def flaky_route(profile, state, pol, exclude=(), custom_backends=()):
            if profile.get("backend") == "ocgo":
                return {"ok": False, "reasons": ["ocgo planner unavailable"]}
            return real_route(profile, state, pol, exclude, custom_backends)

        cmd = self.command()
        with mock.patch.object(xs, "get_state", return_value=st), \
                mock.patch.object(xs, "route", side_effect=flaky_route):
            eff, info, new_decision = xs.fleet_weighted_route(cmd, policy)
        self.assertEqual(eff, cmd)
        self.assertFalse(info["applied"])
        self.assertIn("failed to plan", info["reason"])
        self.assertEqual(new_decision["backend"], "codex")
        self.assertNotIn("routed_by_advice", new_decision)

    def test_falls_back_when_state_is_unavailable(self):
        policy = weighted_policy(weighted=True)
        st = scenario()
        self.seed(policy, st)
        cmd = self.command()
        with mock.patch.object(xs, "get_state", side_effect=RuntimeError("probe failed")):
            eff, info, new_decision = xs.fleet_weighted_route(cmd, policy)
        self.assertEqual(eff, cmd)
        self.assertFalse(info["applied"])
        self.assertIn("unavailable", info["reason"])
        self.assertIsNone(new_decision)

    def test_run_id_parsed_from_reply(self):
        self.assertEqual(xs.fleet_exec_run_id(b'{"id": "20261111-090000-abcdef", "state": "started"}'),
                         "20261111-090000-abcdef")
        self.assertEqual(xs.fleet_exec_run_id(b'{"id": "r1", "status": "done", "result": {"id": "r1"}}'), "r1")
        self.assertIsNone(xs.fleet_exec_run_id(b"no json here"))
        self.assertIsNone(xs.fleet_exec_run_id(b""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
