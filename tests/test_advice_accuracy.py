"""Advice calibration report (M8 slice 1, #127).

`report --advice-accuracy` turns recorded routing advice into a calibration
table by (advised backend, card type): the pass rate advice quoted at
dispatch time vs the pass rate actually observed on runs that ran on the
advised backend. Groups whose advice was never followed by a scored run
show n=0 with no invented rates; a group is flagged MISCALIBRATED only
when |advised - actual| > 0.20 with n >= 8.

Run: python3 -m unittest tests.test_advice_accuracy
     (or: python3 tests/test_advice_accuracy.py)
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


def advice(backend, rate, model="m1"):
    return {"backend": backend, "model": model, "pass_rate": rate, "runs": 10,
            "actual_pass_rate": None, "actual_runs": 0, "reason": "synthetic"}


def entry(run_id, backend, card_type, outcome, advised_rate, advised_backend=None,
          model="m1", accepted=None, extra=None):
    row = {"id": run_id, "backend": backend, "model": model, "card_type": card_type,
           "status": "done", "exec_pass": outcome,
           "routing_advice": advice(advised_backend or backend, advised_rate, model)}
    if accepted is not None:
        row["accepted"] = accepted
    if extra:
        row.update(extra)
    return row


def known_ledger():
    """Hand-computable fixture; every group's expected row is derived below.

    codex/fix: 10 scored, advised rates 0.80 x5 + 1.00 x5 (mean 0.90),
        7 pass -> actual 0.70, diff 0.20, n=10 -> NOT flagged (boundary).
    ocgo/fix: 8 scored, advised 0.80, 4 pass (one via accepted=True over
        exec_pass=False) -> actual 0.50, diff 0.30, n=8 -> MISCALIBRATED.
    agy/fix: 8 scored, advised 0.70, 4 pass -> actual 0.50, diff 0.20,
        n=8 -> NOT flagged (boundary, diff must be strictly > 0.20).
    deepseek/docs: 5 scored, advised 0.60, 3 pass -> actual 0.60,
        diff 0.00, n=5 -> NOT flagged.
    cursor/review: 7 scored, advised 0.90, 4 pass -> actual 4/7, diff > 0.20,
        n=7 -> NOT flagged (boundary, n must be >= 8).
    pi/fix: advice recorded but never followed by a scored run -> n=0,
        rates None, not_followed=3.
    """
    rows = []
    for i in range(10):
        rows.append(entry(f"cx{i}", "codex", "fix", i < 7,
                          0.80 if i < 5 else 1.00))
    for i in range(8):
        # 3 exec_pass passes + 1 accepted-over-exec_pass pass + 4 fails.
        if i < 3:
            rows.append(entry(f"oc{i}", "ocgo", "fix", True, 0.80))
        elif i == 3:
            rows.append(entry(f"oc{i}", "ocgo", "fix", False, 0.80, accepted=True))
        else:
            rows.append(entry(f"oc{i}", "ocgo", "fix", False, 0.80))
    for i in range(8):
        rows.append(entry(f"ag{i}", "agy", "fix", i < 4, 0.70))
    for i in range(5):
        rows.append(entry(f"ds{i}", "deepseek", "docs", i < 3, 0.60))
    for i in range(7):
        rows.append(entry(f"cu{i}", "cursor", "review", i < 4, 0.90))
    for i in range(3):
        # Advice named pi but the run actually ran on codex: not followed.
        rows.append(entry(f"pi-nf{i}", "codex", "fix", True, 0.75,
                          advised_backend="pi"))
    for i in range(2):
        # Followed pi, but no scored outcome: contributes no sample.
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


REAL_LEDGER_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "advice-accuracy-real-ledger.jsonl"


def load_real_ledger():
    """Verbatim copy of the M8 slice 2 (#129) real-run ledger (48 lines).

    Produced by 48 real `openswarm run` calls in scratch
    bench-env/m8-slice2 (16 seed + 8 weighted runs per card type, fix and
    test); see docs/m8-advice-accuracy-evidence.md for the hand recount.
    """
    return [json.loads(line) for line in
            REAL_LEDGER_FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


class AdviceAccuracyTests(unittest.TestCase):
    def test_known_numbers(self):
        result = xs.compute_advice_accuracy(known_ledger())
        groups = by_key(result)
        self.assertEqual(sorted(groups), [("agy", "fix"), ("codex", "fix"),
                                         ("cursor", "review"), ("deepseek", "docs"),
                                         ("ocgo", "fix"), ("pi", "fix")])
        codex = groups[("codex", "fix")]
        self.assertAlmostEqual(codex["advised_pass_rate"], 0.90)
        self.assertAlmostEqual(codex["actual_pass_rate"], 0.70)
        self.assertEqual(codex["n"], 10)
        self.assertAlmostEqual(codex["diff"], 0.20)
        self.assertFalse(codex["miscalibrated"])
        ocgo = groups[("ocgo", "fix")]
        self.assertAlmostEqual(ocgo["advised_pass_rate"], 0.80)
        self.assertAlmostEqual(ocgo["actual_pass_rate"], 0.50)
        self.assertEqual(ocgo["n"], 8)
        self.assertAlmostEqual(ocgo["diff"], 0.30)
        self.assertTrue(ocgo["miscalibrated"])
        deepseek = groups[("deepseek", "docs")]
        self.assertAlmostEqual(deepseek["advised_pass_rate"], 0.60)
        self.assertAlmostEqual(deepseek["actual_pass_rate"], 0.60)
        self.assertEqual(deepseek["n"], 5)
        self.assertAlmostEqual(deepseek["diff"], 0.0)
        self.assertFalse(deepseek["miscalibrated"])
        self.assertEqual(result["total_scored"], 10 + 8 + 8 + 5 + 7)
        self.assertEqual(result["total_advice_records"], 10 + 8 + 8 + 5 + 7 + 3 + 2)

    def test_real_ledger_two_groups_n_eight(self):
        """M8 slice 2 (#129): the real-ledger calibration table, hand-recomputed.

        Both groups quote pass rate 1.0 on every scored run and observe 8/8
        passes, so advised == actual == 1.0, diff 0.0, no MISCALIBRATED
        group. The numbers below are the hand recount in
        docs/m8-advice-accuracy-evidence.md, not the report's own output.
        """
        result = xs.compute_advice_accuracy(load_real_ledger())
        groups = by_key(result)
        self.assertEqual(sorted(groups), [("echo-good", "fix"), ("echo-good", "test")])
        for key in (("echo-good", "fix"), ("echo-good", "test")):
            row = groups[key]
            self.assertAlmostEqual(row["advised_pass_rate"], 1.0)
            self.assertAlmostEqual(row["actual_pass_rate"], 1.0)
            self.assertEqual(row["n"], 8)
            self.assertAlmostEqual(row["diff"], 0.0)
            self.assertFalse(row["miscalibrated"])
            self.assertEqual(row["not_followed"], 0)
        self.assertEqual(result["total_scored"], 16)
        self.assertEqual(result["total_advice_records"], 16)
        self.assertFalse(any(row["miscalibrated"] for row in result["groups"]))

    def test_flag_boundaries(self):
        groups = by_key(xs.compute_advice_accuracy(known_ledger()))
        # diff exactly 0.20 with n >= 8 is not flagged.
        self.assertFalse(groups[("agy", "fix")]["miscalibrated"])
        self.assertAlmostEqual(groups[("agy", "fix")]["diff"], 0.20)
        self.assertEqual(groups[("agy", "fix")]["n"], 8)
        # diff > 0.20 with n < 8 is not flagged.
        cursor = groups[("cursor", "review")]
        self.assertGreater(cursor["diff"], 0.20)
        self.assertEqual(cursor["n"], 7)
        self.assertFalse(cursor["miscalibrated"])

    def test_no_history_group_shows_n_zero_without_rates(self):
        groups = by_key(xs.compute_advice_accuracy(known_ledger()))
        pi = groups[("pi", "fix")]
        self.assertEqual(pi["n"], 0)
        self.assertIsNone(pi["advised_pass_rate"])
        self.assertIsNone(pi["actual_pass_rate"])
        self.assertIsNone(pi["diff"])
        self.assertFalse(pi["miscalibrated"])
        self.assertEqual(pi["not_followed"], 3)

    def test_advice_without_pass_rate_excluded_from_advised_mean(self):
        rows = [entry("a0", "codex", "fix", True, 0.80),
                entry("a1", "codex", "fix", True, None)]
        row = xs.compute_advice_accuracy(rows)["groups"][0]
        self.assertAlmostEqual(row["advised_pass_rate"], 0.80)
        self.assertAlmostEqual(row["actual_pass_rate"], 1.0)
        self.assertEqual(row["n"], 2)

    def test_empty_ledger(self):
        result = xs.compute_advice_accuracy([])
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["total_scored"], 0)
        self.assertEqual(result["total_advice_records"], 0)
        text = xs.advice_accuracy_text(result, "<missing>")
        self.assertIn("no advice history", text)


class AdviceAccuracyCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-advice-accuracy-")
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
        args = SimpleNamespace(advice_accuracy=True, fleet=False, json=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_advice_accuracy(args, {})
        text = out.getvalue()
        self.assertIn("ocgo", text)
        self.assertIn("MISCALIBRATED", text)
        self.assertIn("n/a", text)  # the pi/fix n=0 row shows no invented rates
        args = SimpleNamespace(advice_accuracy=True, fleet=False, json=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_advice_accuracy(args, {})
        parsed = json.loads(out.getvalue())
        ocgo = next(row for row in parsed["groups"]
                    if (row["backend"], row["card_type"]) == ("ocgo", "fix"))
        self.assertTrue(ocgo["miscalibrated"])
        self.assertAlmostEqual(ocgo["diff"], 0.30)

    def test_report_option_missing_ledger_does_not_crash(self):
        args = SimpleNamespace(advice_accuracy=True, fleet=False, json=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_advice_accuracy(args, {})
        self.assertIn("no advice history", out.getvalue())
        args = SimpleNamespace(advice_accuracy=True, fleet=False, json=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_advice_accuracy(args, {})
        self.assertEqual(json.loads(out.getvalue())["groups"], [])

    def test_report_dispatches_through_cmd_report(self):
        self.write_ledger(known_ledger())
        args = SimpleNamespace(advice_accuracy=True, fleet=False, json=False,
                               daily=False, date=None, html=None,
                               since_minutes=60)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_report(args, {})
        self.assertIn("MISCALIBRATED", out.getvalue())

    def test_cli_accepts_report_advice_accuracy(self):
        self.write_ledger(known_ledger())
        env = dict(os.environ, OPENSWARM_HOME=str(self.root))
        proc = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "..", "bin", "openswarm"),
             "report", "--advice-accuracy"],
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MISCALIBRATED", proc.stdout)


if __name__ == "__main__":
    unittest.main()
