"""Calibration gate (M15 slice 1, #160).

With routing.calibration_gate on, an advised (backend, card-type) group the
M8 calibration table flags as miscalibrated (|advised - actual| > 0.20 with
n >= 8, at the shipped defaults) is not followed at all: apply_weighted_routing
keeps the default pick and records gated_by_calibration with the diff and
sample count in the info dict — the ledger's `weighted_routing` record on
every path (run, batch, fleet, route). Groups below the sample bar are never
gated, a group whose calibration recovers is followed again with no manual
reset, an explicit --backend always wins, and with the switch off every decision
is byte-identical to the pre-M16 behaviour.
Run: python3 -m unittest discover -s tests   (or: python3 tests/test_calibration_gate.py)
"""
import copy
import io
import json
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


def advice_rows(backend, model, quoted, passes, n=8, prefix="h", card_type="fix"):
    """Ledger runs whose recorded routing_advice named `backend` (M8 scoring).

    These rows also feed compute_routing_weights, so callers must keep their
    pass pattern in line with the weights rows for the same backend.
    """
    return [{"id": f"{prefix}-{backend[:2]}{i}", "backend": backend, "model": model,
             "card_type": card_type, "status": "done", "exec_pass": i < passes,
             "routing_advice": {"backend": backend, "model": model, "pass_rate": quoted},
             "attempts": [{}], "seconds": 10, "tokens": {"total": 100}} for i in range(n)]


def prof(**kw):
    return {"type": "impl", "difficulty": 3, **kw}


class CalibrationGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-calibration-gate-")
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
        """Seed ledger weights rows around the live default pick; return it."""
        decision = xs.route(prof(), st, policy)
        self.assertTrue(decision["ok"], f"unroutable: {decision}")
        self.assertEqual(decision["backend"], "codex")
        rows = rows_for("codex", decision["model"], codex_pass, codex_n)
        rows += rows_for("ocgo", OCGO_MODEL, ocgo_pass, ocgo_n)
        self.write_ledger(rows)
        return decision

    def miscalibrated_rows(self, decision):
        """Quoted 1.00 against observed 0.25 on n=8: diff 0.75, gated."""
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        rows += advice_rows("ocgo", OCGO_MODEL, 1.0, 2, n=8, prefix="bad")
        self.write_ledger(rows)
        return rows

    def test_switch_off_is_byte_identical(self):
        """Gate switch off (explicitly disabled): decisions, info dict and
        decision object are exactly the static ones — no ledger read, no
        new field anywhere."""
        policy = weighted_policy(weighted=True, calibration_gate=False)  # switch off: explicit, no longer the shipped default (M16, #163)
        st = scenario()
        decision = self.seed(policy, st)
        self.assertEqual(xs.calibration_gate_settings(policy)["enabled"], False)
        gated, detail = xs.calibration_gate_for(policy, "ocgo", "fix")
        self.assertEqual((gated, detail["reason"]), (False, "disabled"))
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "ocgo")
        self.assertEqual(info, {"applied": True,
                                "from": {"backend": "codex", "model": decision["model"]},
                                "to": {"backend": "ocgo", "model": OCGO_MODEL},
                                "advantage": 0.625, "margin": 0.10,
                                "reason": ("best observed pass, first-try, rework, and P1+ rates "
                                           "among 8 eligible samples; ties use median tokens then "
                                           "seconds; actual routing unchanged")})
        self.assertNotIn("gated_by_calibration", info)
        self.assertNotIn("gated_by_calibration", new)

    def test_miscalibrated_group_is_gated(self):
        """Quoted 1.00 vs observed 0.25 on n=8 (diff 0.75): the diversion a
        0.31 advantage would otherwise take is blocked, the default pick is
        kept, and the gate (diff + samples) is recorded in the info dict."""
        policy = weighted_policy(weighted=True, calibration_gate=True)
        st = scenario()
        decision = self.seed(policy, st)
        self.miscalibrated_rows(decision)
        gated, detail = xs.calibration_gate_for(policy, "ocgo", "fix")
        self.assertTrue(gated)
        self.assertEqual((detail["diff"], detail["n"], detail["advised_pass_rate"],
                          detail["actual_pass_rate"]), (0.75, 8, 1.0, 0.25))
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertTrue(info["gated_by_calibration"])
        self.assertEqual((info["calibration_diff"], info["calibration_samples"],
                          info["calibration_advised"], info["calibration_actual"]),
                         (0.75, 8, 1.0, 0.25))
        self.assertIn("miscalibrated", info["reason"])
        self.assertNotIn("margin", info)
        self.assertNotIn("routed_by_advice", new)

    def test_calibrated_group_still_diverts(self):
        """Quoted 0.90 vs observed 0.875 on n=8 (diff 0.025): not gated, the
        diversion goes through exactly as with the gate off."""
        policy = weighted_policy(weighted=True, calibration_gate=True)
        st = scenario()
        decision = self.seed(policy, st)
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        rows += advice_rows("ocgo", OCGO_MODEL, 0.9, 7, n=8, prefix="ok")
        self.write_ledger(rows)
        gated, detail = xs.calibration_gate_for(policy, "ocgo", "fix")
        self.assertEqual((gated, detail["reason"]), (False, "calibrated"))
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "ocgo")
        self.assertTrue(info["applied"])
        self.assertNotIn("gated_by_calibration", info)

    def test_small_n_never_gated(self):
        """Quoted 1.00 vs observed 0.00, but only n=4: diff 1.00 yet below
        the sample bar, so the group is not gated and the diversion goes
        through."""
        policy = weighted_policy(weighted=True, calibration_gate=True)
        st = scenario()
        decision = self.seed(policy, st)
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        rows += advice_rows("ocgo", OCGO_MODEL, 1.0, 0, n=4, prefix="thin")
        self.write_ledger(rows)
        gated, detail = xs.calibration_gate_for(policy, "ocgo", "fix")
        self.assertEqual((gated, detail["reason"], detail["n"]), (False, "below min_samples", 4))
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "ocgo")
        self.assertTrue(info["applied"])
        self.assertNotIn("gated_by_calibration", info)

    def test_boundary_diff_exactly_threshold_not_gated(self):
        """Quoted 0.90 vs observed 0.70 on n=10: diff is exactly 0.20, and
        the flag is strictly greater — not gated."""
        policy = weighted_policy(weighted=True, calibration_gate=True)
        st = scenario()
        decision = self.seed(policy, st)
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        rows += advice_rows("ocgo", OCGO_MODEL, 0.9, 7, n=10, prefix="edge")
        self.write_ledger(rows)
        gated, detail = xs.calibration_gate_for(policy, "ocgo", "fix")
        self.assertEqual((gated, detail["diff"], detail["n"]), (False, 0.2, 10))
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "ocgo")
        self.assertTrue(info["applied"])

    def test_no_history_not_gated(self):
        """No advice records at all: nothing to condemn, the diversion goes
        through and the gate reports 'no history'."""
        policy = weighted_policy(weighted=True, calibration_gate=True)
        st = scenario()
        decision = self.seed(policy, st)
        gated, detail = xs.calibration_gate_for(policy, "ocgo", "fix")
        self.assertEqual((gated, detail["reason"]), (False, "no history"))
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "ocgo")
        self.assertTrue(info["applied"])

    def test_recovery_restores_following(self):
        """The gate is re-evaluated from ledger history on every decision:
        a gated group that later scores well (24 good runs pull diff 0.75
        down to 0.1125) is followed again with no manual reset."""
        policy = weighted_policy(weighted=True, calibration_gate=True)
        st = scenario()
        decision = self.seed(policy, st)
        rows = self.miscalibrated_rows(decision)
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "codex")
        self.assertTrue(info["gated_by_calibration"])
        rows += advice_rows("ocgo", OCGO_MODEL, 0.9, 24, n=24, prefix="good")
        self.write_ledger(rows)
        gated, detail = xs.calibration_gate_for(policy, "ocgo", "fix")
        self.assertEqual((gated, detail["diff"], detail["n"]), (False, 0.1125, 32))
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "ocgo")
        self.assertTrue(info["applied"])
        self.assertNotIn("gated_by_calibration", info)

    def test_gate_checked_before_margin(self):
        """Gate on and adaptive margin on, miscalibrated group: the gate
        blocks the diversion before any margin is consulted — no margin in
        the info, no adaptive fields on the decision."""
        policy = weighted_policy(weighted=True, calibration_gate=True, adaptive_margin=True)
        st = scenario()
        decision = self.seed(policy, st)
        self.miscalibrated_rows(decision)
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "codex")
        self.assertTrue(info["gated_by_calibration"])
        self.assertNotIn("margin", info)
        self.assertNotIn("margin_source", info)
        self.assertNotIn("advice_margin", new)

    def test_explicit_backend_wins_with_gate_on(self):
        """Explicit --backend wins over the gate exactly as over weighted
        routing: the gate is never consulted for a forced backend."""
        policy = weighted_policy(weighted=True, calibration_gate=True)
        st = scenario()
        decision = self.seed(policy, st)
        self.miscalibrated_rows(decision)
        forced = xs.route(prof(backend="codex"), st, policy)
        new, info = xs.apply_weighted_routing(prof(backend="codex"), "fix", st, policy, forced)
        self.assertEqual(new["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertIn("explicit --backend", info["reason"])
        self.assertNotIn("gated_by_calibration", info)

    def test_settings_validation(self):
        base = weighted_policy(calibration_gate=True)
        for bad in ({"calibration_gate": "yes"}, {"calibration_gate_diff": 0},
                    {"calibration_gate_diff": 1.0}, {"calibration_gate_diff": -0.1},
                    {"calibration_gate_diff": "0.2"},
                    {"calibration_gate_min_samples": 0}, {"calibration_gate_min_samples": 2.5},
                    {"calibration_gate_min_samples": True}):
            policy = weighted_policy(**{**base["routing"], **bad})
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    xs.calibration_gate_settings(policy)
        # Custom thresholds are honoured.
        policy = weighted_policy(calibration_gate=True, calibration_gate_diff=0.5,
                                 calibration_gate_min_samples=4)
        settings = xs.calibration_gate_settings(policy)
        self.assertEqual((settings["diff"], settings["min_samples"]), (0.5, 4))
        # Defaults match the M8 report flag values when the keys are absent.
        policy = weighted_policy(calibration_gate=True)
        del policy["routing"]["calibration_gate_diff"]
        del policy["routing"]["calibration_gate_min_samples"]
        settings = xs.calibration_gate_settings(policy)
        self.assertEqual((settings["diff"], settings["min_samples"]), (0.2, 8))

    def test_same_history_reproduces_same_gate(self):
        """Same ledger history gates the same way twice, and in a different
        entry order — the method is a pure function of history."""
        policy = weighted_policy(calibration_gate=True)
        st = scenario()
        decision = self.seed(policy, st)
        rows = self.miscalibrated_rows(decision)
        # Not-followed advice and routing-decision records must not score.
        rows.append({"id": "nf1", "backend": "codex", "model": decision["model"],
                     "card_type": "fix", "status": "done", "exec_pass": True,
                     "routing_advice": {"backend": "ocgo", "model": OCGO_MODEL, "pass_rate": 0.0}})
        rows.append({"id": "bd1", "backend": "ocgo", "model": OCGO_MODEL, "card_type": "fix",
                     "batch_routing_decision": True, "exec_pass": False,
                     "routing_advice": {"backend": "ocgo", "model": OCGO_MODEL, "pass_rate": 0.0}})
        first = xs.calibration_gate_for(policy, "ocgo", "fix", entries=list(rows))
        second = xs.calibration_gate_for(policy, "ocgo", "fix", entries=list(rows))
        shuffled = xs.calibration_gate_for(policy, "ocgo", "fix", entries=list(reversed(rows)))
        self.assertEqual(first, second)
        self.assertEqual(first, shuffled)
        self.assertTrue(first[0])
        self.assertEqual(first[1]["n"], 8)

    def test_cmd_run_dry_run_records_gate(self):
        """route / run --dry-run share the one apply_weighted_routing path:
        a dry run under the gate keeps the default pick, still records the
        (miscalibrated) advice, and carries the gate in `weighted_routing`
        — the same dict cmd_run puts into the ledger on the real path."""
        policy = weighted_policy(weighted=True, calibration_gate=True)
        st = scenario()
        decision = xs.route(prof(), st, policy)
        rows = rows_for("codex", decision["model"], 2, n=8)
        rows += rows_for("ocgo", OCGO_MODEL, 7, n=8)
        rows += advice_rows("ocgo", OCGO_MODEL, 1.0, 2, n=8, prefix="bad")
        self.write_ledger(rows)
        args = type("Args", (), {
            "card_type": "fix", "rework_of": None, "run_id": None, "prompt": "implement fix",
            "task_file": None, "title": "fix", "type": "impl", "difficulty": 3, "context_tokens": 0,
            "backend": None, "model": None, "effort": None, "budget_minutes": 5, "dry_run": True,
            "detach": False, "cwd": str(self.root), "no_worktree": True, "base": None,
            "keep_worktree": True, "verify": None, "paths": [], "no_fallback": False,
            "verify_baseline": False, "verify_evalid": False, "backends_file": None,
        })()
        output = io.StringIO()
        with mock.patch.object(xs, "get_state", return_value=st), \
                mock.patch.object(xs, "load_custom_backends", return_value=[]):
            import contextlib
            with contextlib.redirect_stdout(output):
                xs.cmd_run(args, policy)
        run = json.loads(output.getvalue())
        self.assertTrue(run["dry_run"])
        self.assertEqual(run["decision"]["backend"], "codex")
        self.assertNotIn("routed_by_advice", run["decision"])
        self.assertEqual(run["routing_advice"]["backend"], "ocgo")
        self.assertFalse(run["weighted_routing"]["applied"])
        self.assertTrue(run["weighted_routing"]["gated_by_calibration"])
        self.assertEqual(run["weighted_routing"]["calibration_diff"], 0.75)
        self.assertEqual(run["weighted_routing"]["calibration_samples"], 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
