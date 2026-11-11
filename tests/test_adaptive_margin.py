"""Adaptive routing margin (M14 slice 1, #156).

With routing.adaptive_margin on, the margin apply_weighted_routing enforces
is computed per (backend, card type) from ledger history: margin =
clamp(|advised - actual| + uncertainty, floor, cap), where uncertainty is
the actual pass rate minus its 95% Wilson lower bound. Groups below
min_samples or with no history fall back to the static
routing.advice_margin. With the switch off (the shipped default) every
decision is byte-identical to the static-margin behaviour.
Run: python3 -m unittest discover -s tests   (or: python3 tests/test_adaptive_margin.py)
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


class AdaptiveMarginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-adaptive-margin-")
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

    def test_switch_off_is_byte_identical(self):
        """Adaptive switch off (shipped default): decisions, info dict and
        decision object are exactly the static-margin ones."""
        policy = weighted_policy(weighted=True)  # adaptive_margin absent -> off
        st = scenario()
        decision = self.seed(policy, st)
        self.assertEqual(xs.adaptive_margin_settings(policy)["enabled"], False)
        margin, detail = xs.adaptive_margin_for(policy, "ocgo", "fix")
        self.assertEqual((margin, detail["margin_source"], detail["adapted"]),
                         (0.10, "static", False))
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "ocgo")
        self.assertEqual(info, {"applied": True,
                                "from": {"backend": "codex", "model": decision["model"]},
                                "to": {"backend": "ocgo", "model": OCGO_MODEL},
                                "advantage": 0.625, "margin": 0.10,
                                "reason": ("best observed pass, first-try, rework, and P1+ rates "
                                           "among 8 eligible samples; ties use median tokens then "
                                           "seconds; actual routing unchanged")})
        self.assertNotIn("advice_margin", new)
        self.assertNotIn("adaptive_margin", new)
        self.assertTrue(any(r.endswith("(margin 0.10, 8 samples)") for r in new["reasons"]))
        self.assertFalse(xs.compute_adaptive_margins(xs.ledger_entries(), policy)["enabled"])

    def test_below_min_samples_falls_back_to_static(self):
        """4 scored runs (< min_samples 8): no adaptation, static margin decides."""
        policy = weighted_policy(weighted=True, adaptive_margin=True)
        st = scenario()
        decision = self.seed(policy, st)
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        rows += advice_rows("ocgo", OCGO_MODEL, 1.0, 4, n=4)
        self.write_ledger(rows)
        result = xs.compute_adaptive_margins(xs.ledger_entries(), policy)
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual((group["backend"], group["card_type"], group["n"], group["adapted"]),
                         ("ocgo", "fix", 4, False))
        self.assertEqual(group["margin"], 0.10)
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "ocgo")
        self.assertEqual(info["margin"], 0.10)
        self.assertNotIn("margin_source", info)
        self.assertNotIn("advice_margin", new)

    def test_no_history_falls_back_to_static(self):
        """No advice records at all: the static margin decides, nothing extra recorded."""
        policy = weighted_policy(weighted=True, adaptive_margin=True)
        st = scenario()
        decision = self.seed(policy, st)
        result = xs.compute_adaptive_margins(xs.ledger_entries(), policy)
        self.assertEqual(result["groups"], [])
        margin, detail = xs.adaptive_margin_for(policy, "ocgo", "fix")
        self.assertEqual((margin, detail["margin_source"], detail["reason"]),
                         (0.10, "static", "no history"))
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "ocgo")
        self.assertEqual(info["margin"], 0.10)
        self.assertNotIn("margin_source", info)

    def test_margin_floor_well_calibrated_large_n(self):
        """100/100 at quoted 1.00: diff 0, Wilson uncertainty ~0.037, so the
        margin lands on the floor 0.05 — and a 0.0625 advantage that the
        static 0.10 margin would block now diverts."""
        policy = weighted_policy(weighted=True, adaptive_margin=True)
        st = scenario()
        decision = xs.route(prof(), st, policy)
        rows = rows_for("codex", decision["model"], 15, n=16)
        rows += rows_for("ocgo", OCGO_MODEL, 16, n=16)
        rows += advice_rows("ocgo", OCGO_MODEL, 1.0, 100, n=100)
        self.write_ledger(rows)
        result = xs.compute_adaptive_margins(xs.ledger_entries(), policy)
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual((group["n"], group["advised_pass_rate"], group["actual_pass_rate"],
                          group["diff"], group["adapted"]),
                         (100, 1.0, 1.0, 0.0, True))
        self.assertAlmostEqual(group["wilson_lower"], 0.963, places=3)
        self.assertEqual(group["margin"], 0.05)
        # Static margin blocks this advantage; the adaptive floor lets it through.
        off = weighted_policy(weighted=True)
        _, off_info = xs.apply_weighted_routing(prof(), "fix", st, off, decision)
        self.assertFalse(off_info["applied"])
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "ocgo")
        self.assertTrue(info["applied"])
        self.assertEqual(info["margin"], 0.05)
        self.assertEqual(info["margin_source"], "adaptive")
        self.assertEqual(info["margin_group"], group)
        self.assertEqual(new["advice_margin"], 0.05)
        self.assertEqual(new["adaptive_margin"], group)
        self.assertTrue(any("adaptive margin 0.05" in r for r in new["reasons"]))

    def test_margin_cap_miscalibrated_group(self):
        """Quoted 1.00 against observed 0.875 on n=8: diff 0.125 plus Wilson
        uncertainty ~0.346 pushes the raw margin to ~0.47, clamped to the
        0.30 cap — and a 0.25 advantage that the static margin would let
        through is now blocked."""
        policy = weighted_policy(weighted=True, adaptive_margin=True)
        st = scenario()
        decision = self.seed(policy, st, codex_pass=5, ocgo_pass=7)
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        rows += advice_rows("ocgo", OCGO_MODEL, 1.0, 7, n=8)
        self.write_ledger(rows)
        result = xs.compute_adaptive_margins(xs.ledger_entries(), policy)
        group = result["groups"][0]
        self.assertEqual((group["n"], group["advised_pass_rate"], group["actual_pass_rate"],
                          group["diff"], group["adapted"]),
                         (8, 1.0, 0.875, 0.125, True))
        self.assertAlmostEqual(group["wilson_lower"], 0.529, places=3)
        self.assertEqual(group["margin"], 0.30)
        # Static margin diverts on this advantage; the adaptive cap blocks it.
        off = weighted_policy(weighted=True)
        _, off_info = xs.apply_weighted_routing(prof(), "fix", st, off, decision)
        self.assertTrue(off_info["applied"])
        new, info = xs.apply_weighted_routing(prof(), "fix", st, policy, decision)
        self.assertEqual(new["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertIn("below margin 0.30", info["reason"])

    def test_margin_never_negative_or_above_cap(self):
        """Adversarial sweep: quoted/observed rates at the extremes, n from
        the sample bar to 500 — every margin stays inside [floor, cap]."""
        policy = weighted_policy(adaptive_margin=True)
        entries = []
        for n in (8, 9, 16, 50, 100, 500):
            for quoted, passes in ((0.0, n), (1.0, 0), (0.0, 0), (1.0, n), (0.5, n // 2)):
                # One group per combination (card type disambiguates them).
                entries += advice_rows("ocgo", OCGO_MODEL, quoted, passes, n=n,
                                       prefix=f"s{n}-{quoted}-{passes}",
                                       card_type=f"sweep-{n}-{quoted}-{passes}")
        result = xs.compute_adaptive_margins(entries, policy)
        self.assertEqual(len(result["groups"]), 30)
        for group in result["groups"]:
            with self.subTest(group=group):
                self.assertTrue(group["adapted"])
                self.assertGreaterEqual(group["margin"], 0.05)
                self.assertLessEqual(group["margin"], 0.30)
                self.assertGreaterEqual(group["uncertainty"], 0.0)

    def test_wilson_lower_bound_values(self):
        self.assertAlmostEqual(xs.wilson_lower_bound(1.0, 100), 0.963, places=3)
        self.assertAlmostEqual(xs.wilson_lower_bound(0.875, 8), 0.529, places=3)
        self.assertAlmostEqual(xs.wilson_lower_bound(0.5, 8), 0.215, places=3)
        self.assertAlmostEqual(xs.wilson_lower_bound(0.0, 8), 0.0, places=3)
        self.assertIsNone(xs.wilson_lower_bound(0.5, 0))

    def test_explicit_backend_wins_with_adaptive_on(self):
        """Explicit --backend wins over adaptive routing exactly as over static."""
        policy = weighted_policy(weighted=True, adaptive_margin=True)
        st = scenario()
        decision = xs.route(prof(), st, policy)
        rows = rows_for("codex", decision["model"], 15, n=16)
        rows += rows_for("ocgo", OCGO_MODEL, 16, n=16)
        rows += advice_rows("ocgo", OCGO_MODEL, 1.0, 100, n=100)
        self.write_ledger(rows)
        forced = xs.route(prof(backend="codex"), st, policy)
        new, info = xs.apply_weighted_routing(prof(backend="codex"), "fix", st, policy, forced)
        self.assertEqual(new["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertIn("explicit --backend", info["reason"])

    def test_settings_validation(self):
        base = weighted_policy(adaptive_margin=True)
        for bad in ({"adaptive_margin": "yes"}, {"adaptive_margin_floor": -0.1},
                    {"adaptive_margin_floor": 1.0}, {"adaptive_margin_cap": 1.0},
                    {"adaptive_margin_floor": 0.4, "adaptive_margin_cap": 0.3},
                    {"adaptive_margin_min_samples": 0}, {"adaptive_margin_min_samples": 2.5},
                    {"adaptive_margin_min_samples": True}):
            policy = weighted_policy(**{**base["routing"], **bad})
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    xs.adaptive_margin_settings(policy)
        # Custom floor/cap are honoured: the cap test's raw ~0.47 margin clamps to 0.20.
        policy = weighted_policy(adaptive_margin=True, adaptive_margin_floor=0.02,
                                 adaptive_margin_cap=0.20)
        settings = xs.adaptive_margin_settings(policy)
        self.assertEqual((settings["floor"], settings["cap"], settings["min_samples"]),
                         (0.02, 0.20, 8))
        # min_samples defaults to routing.min_samples when unset.
        policy = weighted_policy(adaptive_margin=True)
        del policy["routing"]["adaptive_margin_min_samples"]
        self.assertEqual(xs.adaptive_margin_settings(policy)["min_samples"], 8)

    def test_same_history_reproduces_same_margin(self):
        """Same ledger history computes the same margin twice, and in a
        different entry order — the method is a pure function of history."""
        policy = weighted_policy(adaptive_margin=True)
        st = scenario()
        decision = self.seed(policy, st, codex_pass=5, ocgo_pass=7)
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        rows += advice_rows("ocgo", OCGO_MODEL, 1.0, 7, n=8)
        # Not-followed advice and routing-decision records must not score.
        rows.append({"id": "nf1", "backend": "codex", "model": decision["model"],
                     "card_type": "fix", "status": "done", "exec_pass": True,
                     "routing_advice": {"backend": "ocgo", "model": OCGO_MODEL, "pass_rate": 0.0}})
        rows.append({"id": "bd1", "backend": "ocgo", "model": OCGO_MODEL, "card_type": "fix",
                     "batch_routing_decision": True, "exec_pass": False,
                     "routing_advice": {"backend": "ocgo", "model": OCGO_MODEL, "pass_rate": 0.0}})
        first = xs.compute_adaptive_margins(rows, policy)
        second = xs.compute_adaptive_margins(list(rows), policy)
        shuffled = xs.compute_adaptive_margins(list(reversed(rows)), policy)
        self.assertEqual(first, second)
        self.assertEqual(first, shuffled)
        self.assertEqual(first["groups"][0]["n"], 8)

    def test_cmd_run_dry_run_uses_adaptive_margin(self):
        """route / run --dry-run share the one apply_weighted_routing path:
        a dry run under the adaptive switch diverts on the adaptive margin."""
        policy = weighted_policy(weighted=True, adaptive_margin=True)
        st = scenario()
        decision = xs.route(prof(), st, policy)
        rows = rows_for("codex", decision["model"], 15, n=16)
        rows += rows_for("ocgo", OCGO_MODEL, 16, n=16)
        rows += advice_rows("ocgo", OCGO_MODEL, 1.0, 100, n=100)
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
        self.assertEqual(run["decision"]["backend"], "ocgo")
        self.assertTrue(run["decision"]["routed_by_advice"])
        self.assertEqual(run["decision"]["advice_margin"], 0.05)
        self.assertTrue(run["weighted_routing"]["applied"])
        self.assertEqual(run["weighted_routing"]["margin"], 0.05)
        self.assertEqual(run["weighted_routing"]["margin_source"], "adaptive")


if __name__ == "__main__":
    unittest.main(verbosity=2)
