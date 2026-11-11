"""Weighted-eval replay report (M9 slice 1, #132).

`report --weighted-eval` replays recorded routing advice through the same
diversion rule `apply_weighted_routing` enforces (M6, #114): a run would
divert when the advised (backend, model) differs from the run's own
(backend, model) and the quoted advice pass rate beats the recorded
actual-pick pass rate by at least `routing.advice_margin` (equal diverts).
For each (advised backend, card type) group the report shows the diverted
share and the expected pass rate under weighted routing — the quoted advice
rate for diverted runs, the observed outcome otherwise — against the
actually observed pass rate. No per-run counterfactual outcome is invented:
a diverted run's expected outcome is exactly the rate advice quoted at
dispatch time, and groups with too few scored runs are flagged small-n.

Run: python3 -m unittest tests.test_weighted_eval
     (or: python3 tests/test_weighted_eval.py)
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _load

xs = _load.load()


def advice(backend, rate, model="m1", actual_rate=None, actual_runs=0):
    return {"backend": backend, "model": model, "pass_rate": rate, "runs": 10,
            "actual_pass_rate": actual_rate, "actual_runs": actual_runs,
            "reason": "synthetic"}


def entry(run_id, backend, card_type, outcome, advised_rate, advised_backend=None,
          model="m1", advised_model="m1", actual_rate=None, accepted=None, extra=None):
    row = {"id": run_id, "backend": backend, "model": model, "card_type": card_type,
           "status": "done", "exec_pass": outcome,
           "routing_advice": advice(advised_backend or backend, advised_rate,
                                    advised_model, actual_rate)}
    if accepted is not None:
        row["accepted"] = accepted
    if extra:
        row.update(extra)
    return row


def known_ledger():
    """Hand-computable fixture; every group's expected row is derived below.

    codex/fix: advised codex, actual ocgo, quoted 0.80 vs actual-pick 0.60
        (advantage 0.20 >= margin 0.10) -> all 10 divert; 7 pass ->
        advised 0.80, actual 0.70, expected 0.80, delta +0.10.
    ocgo/fix: advised ocgo, actual codex, quoted 0.70 vs actual-pick 0.65
        (advantage 0.05 < 0.10) -> none divert; 4 pass (one via
        accepted=True over exec_pass=False) -> advised 0.70, actual 0.50,
        expected 0.50, delta 0.00.
    agy/fix: margin boundary, quoted 0.75 vs actual-pick 0.65 (advantage
        exactly 0.10) -> all 8 divert; 4 pass -> advised 0.75, actual 0.50,
        expected 0.75, delta +0.25.
    deepseek/docs: advice agrees with the run (same backend and model) ->
        none divert; 3/5 pass -> advised 0.60, actual 0.60, expected 0.60.
    cursor/review: quoted pass rate missing -> comparison unavailable ->
        none divert; 4/7 pass -> advised None, actual 4/7, expected 4/7.
    codex/test: same backend, different model (advised m2 vs run m1):
        4 runs quoted 0.90 vs actual-pick 0.70 -> divert, 1 run quoted 0.72
        vs 0.70 (advantage 0.02) -> no divert; outcomes pass, pass, fail,
        fail, fail -> advised 0.86, actual 0.40, expected 0.72, delta +0.32.
    pi/fix: 3 advice records, no scored run -> n=0, no rates, no divert.
    """
    rows = []
    for i in range(10):
        rows.append(entry(f"cx{i}", "ocgo", "fix", i < 7, 0.80,
                          advised_backend="codex", actual_rate=0.60))
    for i in range(8):
        # 3 exec_pass passes + 1 accepted-over-exec_pass pass + 4 fails.
        if i < 3:
            rows.append(entry(f"oc{i}", "codex", "fix", True, 0.70,
                              advised_backend="ocgo", actual_rate=0.65))
        elif i == 3:
            rows.append(entry(f"oc{i}", "codex", "fix", False, 0.70,
                              advised_backend="ocgo", actual_rate=0.65,
                              accepted=True))
        else:
            rows.append(entry(f"oc{i}", "codex", "fix", False, 0.70,
                              advised_backend="ocgo", actual_rate=0.65))
    for i in range(8):
        rows.append(entry(f"ag{i}", "codex", "fix", i < 4, 0.75,
                          advised_backend="agy", actual_rate=0.65))
    for i in range(5):
        rows.append(entry(f"ds{i}", "deepseek", "docs", i < 3, 0.60,
                          actual_rate=0.60))
    for i in range(7):
        rows.append(entry(f"cu{i}", "cursor", "review", i < 4, None,
                          actual_rate=0.50))
    for i in range(4):
        rows.append(entry(f"ct{i}", "codex", "test", i < 2, 0.90,
                          advised_model="m2", actual_rate=0.70))
    rows.append(entry("ct4", "codex", "test", False, 0.72,
                      advised_model="m2", actual_rate=0.70))
    for i in range(3):
        # Advice recorded but the run has no scored outcome.
        rows.append(entry(f"pi-u{i}", "pi", "fix", None, 0.75))
    # A batch routing-decision record must not inflate any group.
    rows.append(entry("batch1", "codex", "fix", True, 0.95,
                      extra={"batch_routing_decision": {"cards": ["a"]}}))
    # Advice with no backend and an entry with no advice at all: ignored.
    rows.append({"id": "noadv-backend", "backend": "codex", "card_type": "fix",
                 "status": "done", "exec_pass": True,
                 "routing_advice": {"backend": None, "pass_rate": None, "runs": 0}})
    rows.append({"id": "noadv", "backend": "codex", "card_type": "fix",
                 "status": "done", "exec_pass": True})
    return rows


def by_key(result):
    return {(row["backend"], row["card_type"]): row for row in result["groups"]}


class WeightedEvalTests(unittest.TestCase):
    def test_known_numbers(self):
        result = xs.compute_weighted_eval(known_ledger())
        groups = by_key(result)
        self.assertEqual(sorted(groups), [("agy", "fix"), ("codex", "fix"),
                                         ("codex", "test"), ("cursor", "review"),
                                         ("deepseek", "docs"), ("ocgo", "fix"),
                                         ("pi", "fix")])
        codex = groups[("codex", "fix")]
        self.assertEqual(codex["n"], 10)
        self.assertEqual(codex["diverted"], 10)
        self.assertAlmostEqual(codex["diverted_share"], 1.0)
        self.assertAlmostEqual(codex["advised_pass_rate"], 0.80)
        self.assertAlmostEqual(codex["actual_pass_rate"], 0.70)
        self.assertAlmostEqual(codex["expected_pass_rate"], 0.80)
        self.assertAlmostEqual(codex["expected_delta"], 0.10)
        self.assertFalse(codex["small_n"])
        ocgo = groups[("ocgo", "fix")]
        self.assertEqual(ocgo["n"], 8)
        self.assertEqual(ocgo["diverted"], 0)
        self.assertAlmostEqual(ocgo["diverted_share"], 0.0)
        self.assertAlmostEqual(ocgo["advised_pass_rate"], 0.70)
        self.assertAlmostEqual(ocgo["actual_pass_rate"], 0.50)
        self.assertAlmostEqual(ocgo["expected_pass_rate"], 0.50)
        self.assertAlmostEqual(ocgo["expected_delta"], 0.0)
        deepseek = groups[("deepseek", "docs")]
        self.assertEqual(deepseek["diverted"], 0)
        self.assertAlmostEqual(deepseek["expected_pass_rate"], 0.60)
        self.assertAlmostEqual(deepseek["expected_delta"], 0.0)
        self.assertTrue(deepseek["small_n"])
        self.assertEqual(result["total_scored"], 10 + 8 + 8 + 5 + 7 + 5)
        self.assertEqual(result["total_diverted"], 10 + 8 + 4)
        self.assertEqual(result["total_advice_records"], 10 + 8 + 8 + 5 + 7 + 5 + 3)
        self.assertAlmostEqual(result["margin"], 0.10)

    def test_margin_boundary_diverts(self):
        groups = by_key(xs.compute_weighted_eval(known_ledger()))
        # Advantage exactly equal to the margin diverts (live rule is
        # `advantage < margin` keeps the default pick).
        agy = groups[("agy", "fix")]
        self.assertEqual(agy["diverted"], 8)
        self.assertAlmostEqual(agy["expected_pass_rate"], 0.75)
        self.assertAlmostEqual(agy["expected_delta"], 0.25)

    def test_model_difference_can_divert(self):
        groups = by_key(xs.compute_weighted_eval(known_ledger()))
        codex_test = groups[("codex", "test")]
        self.assertEqual(codex_test["n"], 5)
        self.assertEqual(codex_test["diverted"], 4)
        self.assertAlmostEqual(codex_test["diverted_share"], 0.80)
        self.assertAlmostEqual(codex_test["advised_pass_rate"], 0.864)
        self.assertAlmostEqual(codex_test["actual_pass_rate"], 0.40)
        self.assertAlmostEqual(codex_test["expected_pass_rate"], 0.72)
        self.assertAlmostEqual(codex_test["expected_delta"], 0.32)
        self.assertTrue(codex_test["small_n"])

    def test_missing_quoted_rate_never_diverts(self):
        groups = by_key(xs.compute_weighted_eval(known_ledger()))
        cursor = groups[("cursor", "review")]
        self.assertEqual(cursor["diverted"], 0)
        self.assertIsNone(cursor["advised_pass_rate"])
        self.assertAlmostEqual(cursor["actual_pass_rate"], round(4 / 7, 6))
        self.assertAlmostEqual(cursor["expected_pass_rate"], round(4 / 7, 6))
        self.assertAlmostEqual(cursor["expected_delta"], 0.0)
        self.assertTrue(cursor["small_n"])

    def test_higher_margin_diverts_nothing(self):
        groups = by_key(xs.compute_weighted_eval(known_ledger(), margin=0.30))
        self.assertEqual(groups[("codex", "fix")]["diverted"], 0)
        self.assertEqual(groups[("agy", "fix")]["diverted"], 0)
        self.assertAlmostEqual(groups[("codex", "fix")]["expected_pass_rate"], 0.70)
        self.assertAlmostEqual(groups[("codex", "fix")]["expected_delta"], 0.0)

    def test_advised_backend_plan_failure_keeps_default(self):
        """Live `apply_weighted_routing` keeps the default pick when the
        advised backend fails to plan; the replay models that with plan_ok.
        """
        result = xs.compute_weighted_eval(
            known_ledger(), plan_ok=lambda backend, model: backend != "codex")
        groups = by_key(result)
        codex = groups[("codex", "fix")]
        self.assertEqual(codex["diverted"], 0)
        self.assertEqual(codex["plan_failed"], 10)
        self.assertAlmostEqual(codex["expected_pass_rate"], 0.70)
        self.assertAlmostEqual(codex["expected_delta"], 0.0)
        # Other backends are unaffected by the codex planner failure.
        self.assertEqual(groups[("agy", "fix")]["diverted"], 8)
        self.assertEqual(groups[("agy", "fix")]["plan_failed"], 0)
        # The codex/test group is advised codex too, so its 4 margin-passing
        # runs also keep the default (the 5th never reaches planning: its
        # advantage 0.02 is below the margin, as in the live path).
        self.assertEqual(groups[("codex", "test")]["diverted"], 0)
        self.assertEqual(groups[("codex", "test")]["plan_failed"], 4)
        self.assertEqual(result["total_diverted"], 8)

    def test_invalid_margin_rejected(self):
        for bad in (1, -0.1, 1.0, "0.1", True, None):
            with self.assertRaises(ValueError, msg=f"margin={bad!r}"):
                xs.compute_weighted_eval(known_ledger(), margin=bad)

    def test_no_history_group_shows_n_zero_without_rates(self):
        groups = by_key(xs.compute_weighted_eval(known_ledger()))
        pi = groups[("pi", "fix")]
        self.assertEqual(pi["n"], 0)
        self.assertEqual(pi["diverted"], 0)
        self.assertIsNone(pi["diverted_share"])
        self.assertIsNone(pi["advised_pass_rate"])
        self.assertIsNone(pi["actual_pass_rate"])
        self.assertIsNone(pi["expected_pass_rate"])
        self.assertIsNone(pi["expected_delta"])
        self.assertEqual(pi["advice_records"], 3)
        self.assertFalse(pi["small_n"])

    def test_empty_ledger(self):
        result = xs.compute_weighted_eval([])
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["total_scored"], 0)
        self.assertEqual(result["total_diverted"], 0)
        self.assertEqual(result["total_advice_records"], 0)
        text = xs.weighted_eval_text(result, "<missing>")
        self.assertIn("no advice history", text)

    def test_real_ledger_all_diverted(self):
        """M9 slice 2 (#134): the real-ledger counterfactual, hand-recomputed.

        Both groups quote pass rate 1.0 on every scored run while the runs
        actually ran on echo-bad and failed 0/8, so all 16 divert (share
        1.00), expected 1.00, delta +1.00. The numbers below are the hand
        recount in docs/m9-counterfactual-evidence.md, not the report's
        own output.
        """
        result = xs.compute_weighted_eval(load_real_ledger())
        groups = by_key(result)
        self.assertEqual(sorted(groups), [("echo-good", "fix"), ("echo-good", "test")])
        for key in (("echo-good", "fix"), ("echo-good", "test")):
            row = groups[key]
            self.assertEqual(row["n"], 8)
            self.assertEqual(row["diverted"], 8)
            self.assertAlmostEqual(row["diverted_share"], 1.0)
            self.assertAlmostEqual(row["advised_pass_rate"], 1.0)
            self.assertAlmostEqual(row["actual_pass_rate"], 0.0)
            self.assertAlmostEqual(row["expected_pass_rate"], 1.0)
            self.assertAlmostEqual(row["expected_delta"], 1.0)
            self.assertEqual(row["plan_failed"], 0)
            self.assertFalse(row["small_n"])
        self.assertEqual(result["total_scored"], 16)
        self.assertEqual(result["total_diverted"], 16)
        self.assertEqual(result["total_advice_records"], 16)


REAL_LEDGER_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "weighted-eval-real-ledger.jsonl"


def load_real_ledger():
    """Verbatim copy of the M9 slice 2 (#134) real-run ledger (48 lines).

    Produced by 48 real `openswarm run` calls in scratch
    bench-env/m9-slice2 under the default config (no routing.weighted
    key: 16 seed + 8 record-only runs per card type, fix and test);
    see docs/m9-counterfactual-evidence.md for the hand recount.
    """
    return [json.loads(line) for line in
            REAL_LEDGER_FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


GOLDEN = Path(__file__).resolve().parent / "golden" / "report-weighted-eval.txt"


class WeightedEvalGoldenTests(unittest.TestCase):
    def test_report_text_matches_golden(self):
        result = xs.compute_weighted_eval(known_ledger())
        self.assertEqual(xs.weighted_eval_text(result, "LEDGER"),
                         GOLDEN.read_text(encoding="utf-8"))


class WeightedEvalCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-weighted-eval-")
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.ledger = self.data / "ledger.jsonl"
        self.constants = {key: getattr(xs, key) for key in ("DATA", "LEDGER", "RUNS", "HOME")}
        xs.DATA, xs.LEDGER, xs.RUNS, xs.HOME = (str(self.data), str(self.ledger),
                                                 str(self.root / "runs"), str(self.root))

    def tearDown(self):
        for key, value in self.constants.items():
            setattr(xs, key, value)
        self.tmp.cleanup()

    def write_ledger(self, entries):
        self.data.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("".join(json.dumps(item) + "\n" for item in entries))

    def test_report_option_text_and_json(self):
        self.write_ledger(known_ledger())
        args = SimpleNamespace(weighted_eval=True, fleet=False, json=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_weighted_eval(args, {})
        text = out.getvalue()
        self.assertIn("codex", text)
        self.assertIn("small-n", text)
        self.assertIn("n/a", text)  # the pi/fix n=0 row shows no invented rates
        args = SimpleNamespace(weighted_eval=True, fleet=False, json=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_weighted_eval(args, {})
        parsed = json.loads(out.getvalue())
        codex = next(row for row in parsed["groups"]
                     if (row["backend"], row["card_type"]) == ("codex", "fix"))
        self.assertEqual(codex["diverted"], 10)
        self.assertAlmostEqual(codex["expected_delta"], 0.10)
        self.assertAlmostEqual(parsed["margin"], 0.10)

    def test_report_option_missing_ledger_does_not_crash(self):
        args = SimpleNamespace(weighted_eval=True, fleet=False, json=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_weighted_eval(args, {})
        self.assertIn("no advice history", out.getvalue())
        args = SimpleNamespace(weighted_eval=True, fleet=False, json=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_weighted_eval(args, {})
        self.assertEqual(json.loads(out.getvalue())["groups"], [])

    def test_report_dispatches_through_cmd_report(self):
        self.write_ledger(known_ledger())
        args = SimpleNamespace(weighted_eval=True, advice_accuracy=False, fleet=False,
                               json=False, daily=False, date=None, html=None,
                               since_minutes=60)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_report(args, {})
        self.assertIn("small-n", out.getvalue())

    def test_cli_accepts_report_weighted_eval(self):
        self.write_ledger(known_ledger())
        env = dict(os.environ, OPENSWARM_HOME=str(self.root))
        proc = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "..", "bin", "openswarm"),
             "report", "--weighted-eval"],
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("small-n", proc.stdout)


if __name__ == "__main__":
    unittest.main()
