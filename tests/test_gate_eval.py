"""Gate-flip evaluation report (M16 slice 1, #164).

`report --gate-eval` replays recorded routing advice through the same
diversion rule `report --weighted-eval` replays (M9, #132), then asks what
the M15 calibration gate (#160) would have done about each diversion: a
would-divert run is intercepted when its advised (backend, card type)
group is miscalibrated (|advised - actual| > 0.20 with n >= 8, at the
shipped defaults) against the history recorded *before* that run — the
live gate's view at dispatch time, so a group that was miscalibrated
early and recovered later gates only the runs dispatched while it was
miscalibrated. Two expected pass rates are reported: the quoted basis
(M9-consistent, advice taken at face value) and the calibrated basis
(the advised group's observed rate for diverted runs — the decision
metric for flipping the shipped default). No per-run counterfactual
outcome is invented, and the command is read-only: it never changes
routing, the shipped default, or the ledger.

Run: python3 -m unittest tests.test_gate_eval
     (or: python3 tests/test_gate_eval.py)
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

    Ledger order is part of the fixture: seed runs come first so the
    prefix table sees them, then the runs whose gate verdict is tested.
    Margin 0.10, gate diff 0.20, min_samples 8 throughout.

    bad/fix: 8 seed runs on bad, quoted 1.00, 2 pass -> advised 1.00,
        actual 0.25, diff 0.75, n=8 -> gated. 4 would-divert runs (other,
        quoted 0.90 vs actual-pick 0.50, 2 pass) are all intercepted: the
        diverted runs did not run on bad, so the prefix table stays
        (1.00, 0.25, n=8) for each of them. n=12, advised 0.966667,
        actual 0.333333; quoted off (2 + 4x0.90)/12 = 0.466667, on
        (2 + 4x0.50)/12 = 0.333333, delta -0.133333; calibrated off
        (2 + 4x0.25)/12 = 0.25, on 0.333333, delta +0.083333.
        Intercepted actual 0.50 vs quoted 0.90 (over-promise +0.40).
    ok/fix: 8 seed runs on ok, quoted 0.90, 7 pass -> diff 0.025, not
        gated. 4 would-divert runs (quoted 0.90 vs 0.50, 3 pass) divert
        and are not intercepted. n=12, advised 0.90, actual 0.833333;
        quoted off/on (7 + 4x0.90)/12 = 0.883333, delta 0; calibrated
        off/on (7 + 4x0.875)/12 = 0.875, delta 0.
    thin/fix: 4 seed runs on thin, quoted 1.00, 0 pass -> diff 1.00 but
        n=4 < 8, never gated. 2 would-divert runs (quoted 1.00 vs 0.50,
        1 pass) divert. n=6, advised 1.00, actual 0.166667; quoted
        off/on (0 + 2x1.00)/6 = 0.333333, delta 0; calibrated off/on
        0.00, delta 0; small-n.
    edge/fix: 10 seed runs on edge, quoted 0.90, 7 pass -> diff exactly
        0.20, and the flag is strictly greater, so not gated. 2
        would-divert runs (quoted 0.90 vs 0.60, 1 pass) divert.
        n=12, advised 0.90, actual 0.666667; quoted off/on
        (7 + 2x0.90)/12 = 0.733333, delta 0; calibrated off/on
        (7 + 2x0.70)/12 = 0.70, delta 0.
    rec/fix: 8 seed runs on rec, quoted 1.00, 2 pass -> diff 0.75, n=8,
        gated; 2 early would-divert runs (quoted 0.90 vs 0.50, 1 pass)
        are intercepted; 24 recovery runs on rec, quoted 0.90, all pass
        -> prefix table (0.925, 0.8125, n=32), diff 0.1125, recovered;
        2 late would-divert runs (quoted 0.90 vs 0.50, 1 pass) divert
        and are not intercepted. n=36, diverted 4, intercepted 2,
        advised 0.922222, actual 0.777778; quoted off 29.6/36 =
        0.822222, on 28.8/36 = 0.80, delta -0.022222; calibrated off
        (2 + 2x0.25 + 24 + 2x0.8125)/36 = 0.78125, on
        (2 + 2x0.50 + 24 + 2x0.8125)/36 = 0.795139, delta +0.013889.
        Intercepted actual 0.50 vs quoted 0.90 (over-promise +0.40).
    cursor/review: quoted pass rate missing -> comparison unavailable ->
        none divert; 4 runs, 2 pass -> advised None, actual 0.50,
        expected off/on 0.50, calibrated off/on 0.50, small-n.
    pi/fix: 3 advice records, no scored run -> n=0, no rates, no divert.
    """
    rows = []
    for i in range(8):
        rows.append(entry(f"bad-s{i}", "bad", "fix", i < 2, 1.00, actual_rate=0.50))
    for i in range(4):
        rows.append(entry(f"bad-d{i}", "other", "fix", i < 2, 0.90,
                          advised_backend="bad", actual_rate=0.50))
    for i in range(8):
        rows.append(entry(f"ok-s{i}", "ok", "fix", i < 7, 0.90, actual_rate=0.50))
    for i in range(4):
        rows.append(entry(f"ok-d{i}", "other", "fix", i < 3, 0.90,
                          advised_backend="ok", actual_rate=0.50))
    for i in range(4):
        rows.append(entry(f"thin-s{i}", "thin", "fix", False, 1.00, actual_rate=0.50))
    for i in range(2):
        rows.append(entry(f"thin-d{i}", "other", "fix", i < 1, 1.00,
                          advised_backend="thin", actual_rate=0.50))
    for i in range(10):
        rows.append(entry(f"edge-s{i}", "edge", "fix", i < 7, 0.90, actual_rate=0.60))
    for i in range(2):
        rows.append(entry(f"edge-d{i}", "other", "fix", i < 1, 0.90,
                          advised_backend="edge", actual_rate=0.60))
    for i in range(8):
        rows.append(entry(f"rec-s{i}", "rec", "fix", i < 2, 1.00, actual_rate=0.50))
    for i in range(2):
        rows.append(entry(f"rec-early{i}", "other", "fix", i < 1, 0.90,
                          advised_backend="rec", actual_rate=0.50))
    for i in range(24):
        rows.append(entry(f"rec-good{i}", "rec", "fix", True, 0.90, actual_rate=0.50))
    for i in range(2):
        rows.append(entry(f"rec-late{i}", "other", "fix", i < 1, 0.90,
                          advised_backend="rec", actual_rate=0.50))
    for i in range(4):
        rows.append(entry(f"cu{i}", "cursor", "review", i < 2, None, actual_rate=0.50))
    for i in range(3):
        # Advice recorded but the run has no scored outcome.
        rows.append(entry(f"pi-u{i}", "pi", "fix", None, 0.75))
    # A batch routing-decision record must not inflate any group.
    rows.append(entry("batch1", "bad", "fix", True, 0.95,
                      extra={"batch_routing_decision": {"cards": ["a"]}}))
    # Advice with no backend and an entry with no advice at all: ignored.
    rows.append({"id": "noadv-backend", "backend": "bad", "card_type": "fix",
                 "status": "done", "exec_pass": True,
                 "routing_advice": {"backend": None, "pass_rate": None, "runs": 0}})
    rows.append({"id": "noadv", "backend": "bad", "card_type": "fix",
                 "status": "done", "exec_pass": True})
    return rows


def by_key(result):
    return {(row["backend"], row["card_type"]): row for row in result["groups"]}


class GateEvalTests(unittest.TestCase):
    def test_known_numbers(self):
        result = xs.compute_gate_eval(known_ledger())
        groups = by_key(result)
        self.assertEqual(sorted(groups), [("bad", "fix"), ("cursor", "review"),
                                         ("edge", "fix"), ("ok", "fix"),
                                         ("pi", "fix"), ("rec", "fix"),
                                         ("thin", "fix")])
        bad = groups[("bad", "fix")]
        self.assertEqual(bad["n"], 12)
        self.assertEqual(bad["would_divert"], 4)
        self.assertEqual(bad["intercepted"], 4)
        self.assertAlmostEqual(bad["intercepted_share"], 1.0)
        self.assertAlmostEqual(bad["intercepted_of_scored"], round(4 / 12, 6))
        self.assertEqual(bad["intercepted_ids"],
                         ["bad-d0", "bad-d1", "bad-d2", "bad-d3"])
        self.assertAlmostEqual(bad["intercepted_actual_pass_rate"], 0.50)
        self.assertAlmostEqual(bad["intercepted_expected_if_followed"], 0.90)
        self.assertAlmostEqual(bad["intercepted_overpromise"], 0.40)
        self.assertAlmostEqual(bad["advised_pass_rate"], 0.966667)
        self.assertAlmostEqual(bad["actual_pass_rate"], 0.333333)
        self.assertAlmostEqual(bad["expected_pass_rate_gate_off"], 0.466667)
        self.assertAlmostEqual(bad["expected_pass_rate_gate_on"], 0.333333)
        # The delta is the difference of the rounded rates (M9 convention),
        # not the rounded raw difference (-0.133333).
        self.assertAlmostEqual(bad["expected_delta"], -0.133334)
        self.assertAlmostEqual(bad["calibrated_expected_pass_rate_gate_off"], 0.25)
        self.assertAlmostEqual(bad["calibrated_expected_pass_rate_gate_on"], 0.333333)
        self.assertAlmostEqual(bad["calibrated_delta"], 0.083333)
        self.assertFalse(bad["small_n"])
        self.assertEqual(result["total_scored"], 82)
        self.assertEqual(result["total_would_divert"], 16)
        self.assertEqual(result["total_intercepted"], 6)
        self.assertAlmostEqual(result["intercepted_share"], 0.375)
        self.assertAlmostEqual(result["intercepted_of_scored"], round(6 / 82, 6))
        self.assertEqual(result["intercepted_ids"],
                         ["bad-d0", "bad-d1", "bad-d2", "bad-d3",
                          "rec-early0", "rec-early1"])
        self.assertAlmostEqual(result["intercepted_actual_pass_rate"], 0.50)
        self.assertAlmostEqual(result["intercepted_expected_if_followed"], 0.90)
        self.assertAlmostEqual(result["intercepted_overpromise"], 0.40)
        self.assertAlmostEqual(result["expected_pass_rate_gate_off"], 0.714634)
        self.assertAlmostEqual(result["expected_pass_rate_gate_on"], 0.685366)
        self.assertAlmostEqual(result["expected_delta"], -0.029268)
        self.assertAlmostEqual(result["calibrated_expected_pass_rate_gate_off"], 0.634451)
        self.assertAlmostEqual(result["calibrated_expected_pass_rate_gate_on"], 0.652744)
        self.assertAlmostEqual(result["calibrated_delta"], 0.018293)
        self.assertEqual(result["total_advice_records"], 85)
        self.assertAlmostEqual(result["margin"], 0.10)
        self.assertAlmostEqual(result["gate_diff"], 0.20)
        self.assertEqual(result["gate_min_samples"], 8)

    def test_calibrated_group_still_diverts(self):
        groups = by_key(xs.compute_gate_eval(known_ledger()))
        ok = groups[("ok", "fix")]
        self.assertEqual(ok["n"], 12)
        self.assertEqual(ok["would_divert"], 4)
        self.assertEqual(ok["intercepted"], 0)
        self.assertAlmostEqual(ok["intercepted_share"], 0.0)
        self.assertEqual(ok["intercepted_ids"], [])
        self.assertIsNone(ok["intercepted_actual_pass_rate"])
        self.assertIsNone(ok["intercepted_expected_if_followed"])
        self.assertIsNone(ok["intercepted_overpromise"])
        self.assertAlmostEqual(ok["expected_pass_rate_gate_off"], 0.883333)
        self.assertAlmostEqual(ok["expected_pass_rate_gate_on"], 0.883333)
        self.assertAlmostEqual(ok["expected_delta"], 0.0)
        self.assertAlmostEqual(ok["calibrated_expected_pass_rate_gate_off"], 0.875)
        self.assertAlmostEqual(ok["calibrated_expected_pass_rate_gate_on"], 0.875)
        self.assertAlmostEqual(ok["calibrated_delta"], 0.0)

    def test_small_n_never_gated(self):
        groups = by_key(xs.compute_gate_eval(known_ledger()))
        thin = groups[("thin", "fix")]
        self.assertEqual(thin["n"], 6)
        self.assertEqual(thin["would_divert"], 2)
        self.assertEqual(thin["intercepted"], 0)
        self.assertAlmostEqual(thin["advised_pass_rate"], 1.0)
        self.assertAlmostEqual(thin["actual_pass_rate"], round(1 / 6, 6))
        self.assertAlmostEqual(thin["expected_pass_rate_gate_off"], 0.333333)
        self.assertAlmostEqual(thin["calibrated_expected_pass_rate_gate_off"], 0.0)
        self.assertTrue(thin["small_n"])

    def test_boundary_diff_exactly_threshold_not_gated(self):
        groups = by_key(xs.compute_gate_eval(known_ledger()))
        edge = groups[("edge", "fix")]
        self.assertEqual(edge["n"], 12)
        self.assertEqual(edge["would_divert"], 2)
        self.assertEqual(edge["intercepted"], 0)
        self.assertAlmostEqual(edge["advised_pass_rate"], 0.90)
        self.assertAlmostEqual(edge["actual_pass_rate"], round(8 / 12, 6))
        self.assertAlmostEqual(edge["calibrated_expected_pass_rate_gate_off"], 0.70)
        self.assertFalse(edge["small_n"])

    def test_recovery_restores_following(self):
        """The gate is re-evaluated against the history before each run:
        the early diversions are intercepted while the group is
        miscalibrated, the late ones divert once it has recovered."""
        groups = by_key(xs.compute_gate_eval(known_ledger()))
        rec = groups[("rec", "fix")]
        self.assertEqual(rec["n"], 36)
        self.assertEqual(rec["would_divert"], 4)
        self.assertEqual(rec["intercepted"], 2)
        self.assertAlmostEqual(rec["intercepted_share"], 0.5)
        self.assertEqual(rec["intercepted_ids"], ["rec-early0", "rec-early1"])
        self.assertAlmostEqual(rec["intercepted_actual_pass_rate"], 0.50)
        self.assertAlmostEqual(rec["intercepted_expected_if_followed"], 0.90)
        self.assertAlmostEqual(rec["expected_pass_rate_gate_off"], 0.822222)
        self.assertAlmostEqual(rec["expected_pass_rate_gate_on"], 0.80)
        self.assertAlmostEqual(rec["expected_delta"], -0.022222)
        self.assertAlmostEqual(rec["calibrated_expected_pass_rate_gate_off"], 0.78125)
        self.assertAlmostEqual(rec["calibrated_expected_pass_rate_gate_on"], 0.795139)
        self.assertAlmostEqual(rec["calibrated_delta"], 0.013889)

    def test_missing_quoted_rate_never_diverts(self):
        groups = by_key(xs.compute_gate_eval(known_ledger()))
        cursor = groups[("cursor", "review")]
        self.assertEqual(cursor["n"], 4)
        self.assertEqual(cursor["would_divert"], 0)
        self.assertEqual(cursor["intercepted"], 0)
        self.assertIsNone(cursor["intercepted_share"])
        self.assertIsNone(cursor["advised_pass_rate"])
        self.assertAlmostEqual(cursor["actual_pass_rate"], 0.50)
        self.assertAlmostEqual(cursor["expected_pass_rate_gate_off"], 0.50)
        self.assertAlmostEqual(cursor["expected_delta"], 0.0)
        self.assertTrue(cursor["small_n"])

    def test_no_history_group_shows_n_zero_without_rates(self):
        groups = by_key(xs.compute_gate_eval(known_ledger()))
        pi = groups[("pi", "fix")]
        self.assertEqual(pi["n"], 0)
        self.assertEqual(pi["would_divert"], 0)
        self.assertEqual(pi["intercepted"], 0)
        self.assertIsNone(pi["intercepted_share"])
        self.assertIsNone(pi["intercepted_of_scored"])
        self.assertIsNone(pi["intercepted_actual_pass_rate"])
        self.assertIsNone(pi["intercepted_expected_if_followed"])
        self.assertIsNone(pi["intercepted_overpromise"])
        self.assertIsNone(pi["advised_pass_rate"])
        self.assertIsNone(pi["actual_pass_rate"])
        self.assertIsNone(pi["expected_pass_rate_gate_off"])
        self.assertIsNone(pi["expected_delta"])
        self.assertIsNone(pi["calibrated_expected_pass_rate_gate_off"])
        self.assertIsNone(pi["calibrated_delta"])
        self.assertEqual(pi["advice_records"], 3)
        self.assertFalse(pi["small_n"])

    def test_custom_gate_bar(self):
        # A looser diff bar (0.80) leaves the bad group (diff 0.75)
        # ungated: nothing is intercepted.
        groups = by_key(xs.compute_gate_eval(known_ledger(), gate_diff=0.80))
        self.assertEqual(groups[("bad", "fix")]["intercepted"], 0)
        self.assertEqual(groups[("rec", "fix")]["intercepted"], 0)
        # A lower sample bar (4) gates the thin group (n=4, diff 1.00).
        groups = by_key(xs.compute_gate_eval(known_ledger(), gate_min_samples=4))
        thin = groups[("thin", "fix")]
        self.assertEqual(thin["intercepted"], 2)
        self.assertAlmostEqual(thin["intercepted_share"], 1.0)
        self.assertEqual(thin["intercepted_ids"], ["thin-d0", "thin-d1"])
        # The bad group is unaffected by the sample-bar change.
        self.assertEqual(groups[("bad", "fix")]["intercepted"], 4)

    def test_higher_margin_diverts_nothing(self):
        # 0.60 clears every advantage in the fixture (largest is thin's
        # 1.00 - 0.50 = 0.50); with no diversion the two expected rates
        # both collapse to the observed pass rate, 53/82.
        result = xs.compute_gate_eval(known_ledger(), margin=0.60)
        self.assertEqual(result["total_would_divert"], 0)
        self.assertEqual(result["total_intercepted"], 0)
        self.assertIsNone(result["intercepted_share"])
        self.assertAlmostEqual(result["expected_pass_rate_gate_off"], round(53 / 82, 6))
        self.assertAlmostEqual(result["expected_pass_rate_gate_off"],
                               result["calibrated_expected_pass_rate_gate_off"])
        self.assertAlmostEqual(result["calibrated_delta"], 0.0)

    def test_invalid_args_rejected(self):
        for bad in (1, -0.1, 1.0, "0.1", True, None):
            with self.assertRaises(ValueError, msg=f"margin={bad!r}"):
                xs.compute_gate_eval(known_ledger(), margin=bad)
        for bad in (0, 1.0, -0.1, "0.2", True, None):
            with self.assertRaises(ValueError, msg=f"gate_diff={bad!r}"):
                xs.compute_gate_eval(known_ledger(), gate_diff=bad)
        for bad in (0, 2.5, True, "8", None):
            with self.assertRaises(ValueError, msg=f"gate_min_samples={bad!r}"):
                xs.compute_gate_eval(known_ledger(), gate_min_samples=bad)

    def test_same_history_reproduces_same_eval(self):
        """The replay is a pure function of ledger order: the same input
        twice yields the same result."""
        first = xs.compute_gate_eval(known_ledger())
        second = xs.compute_gate_eval(known_ledger())
        self.assertEqual(first, second)

    def test_empty_ledger(self):
        result = xs.compute_gate_eval([])
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["total_scored"], 0)
        self.assertEqual(result["total_would_divert"], 0)
        self.assertEqual(result["total_intercepted"], 0)
        self.assertIsNone(result["intercepted_share"])
        self.assertIsNone(result["expected_pass_rate_gate_off"])
        self.assertIsNone(result["calibrated_delta"])
        text = xs.gate_eval_text(result, "<missing>")
        self.assertIn("no advice history", text)


def load_fixture(name):
    path = Path(__file__).resolve().parent / "fixtures" / name
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def recorded_gated_ids(entries):
    return [entry["id"] for entry in entries
            if isinstance(entry.get("weighted_routing"), dict)
            and entry["weighted_routing"].get("gated_by_calibration")]


class GateEvalRealLedgerTests(unittest.TestCase):
    def test_m15_slice2_home_a_replay(self):
        """M15 slice 2 (#162) evidence home-a: the prefix replay must
        reproduce the one gate decision the live gate actually made —
        run 20261111-231536-ca5ae9, gated when echo-flaky/fix stood at
        diff 0.725372 over 8 samples — even though the group looks
        calibrated in the final whole-ledger table (diff 0.057, n=33).
        The numbers below are the hand recount in
        docs/m16-gate-flip-evidence.md, not the report's own output.
        """
        entries = load_fixture("m16-gate-eval-home-a.jsonl")
        result = xs.compute_gate_eval(entries)
        groups = by_key(result)
        self.assertEqual(sorted(groups), [("echo-flaky", "fix"), ("echo-flaky", "test")])
        fix = groups[("echo-flaky", "fix")]
        self.assertEqual(fix["n"], 34)
        self.assertEqual(fix["would_divert"], 1)
        self.assertEqual(fix["intercepted"], 1)
        self.assertAlmostEqual(fix["intercepted_share"], 1.0)
        self.assertEqual(fix["intercepted_ids"], ["20261111-231536-ca5ae9"])
        self.assertEqual(fix["intercepted_ids"], recorded_gated_ids(entries))
        self.assertAlmostEqual(fix["intercepted_actual_pass_rate"], 0.0)
        self.assertAlmostEqual(fix["intercepted_expected_if_followed"], 0.50)
        self.assertAlmostEqual(fix["intercepted_overpromise"], 0.50)
        self.assertAlmostEqual(fix["advised_pass_rate"], 0.694719)
        self.assertAlmostEqual(fix["actual_pass_rate"], 0.735294)
        self.assertAlmostEqual(fix["expected_pass_rate_gate_off"], 0.75)
        self.assertAlmostEqual(fix["expected_pass_rate_gate_on"], 0.735294)
        self.assertAlmostEqual(fix["expected_delta"], -0.014706)
        self.assertAlmostEqual(fix["calibrated_expected_pass_rate_gate_off"], 0.735294)
        self.assertAlmostEqual(fix["calibrated_expected_pass_rate_gate_on"], 0.735294)
        self.assertAlmostEqual(fix["calibrated_delta"], 0.0)
        self.assertFalse(fix["small_n"])
        test = groups[("echo-flaky", "test")]
        self.assertEqual(test["n"], 5)
        self.assertEqual(test["would_divert"], 0)
        self.assertEqual(test["intercepted"], 0)
        self.assertAlmostEqual(test["advised_pass_rate"], 0.816566)
        self.assertAlmostEqual(test["actual_pass_rate"], 0.0)
        self.assertAlmostEqual(test["expected_pass_rate_gate_off"], 0.0)
        self.assertAlmostEqual(test["calibrated_delta"], 0.0)
        self.assertTrue(test["small_n"])
        self.assertEqual(result["total_scored"], 39)
        self.assertEqual(result["total_would_divert"], 1)
        self.assertEqual(result["total_intercepted"], 1)
        self.assertAlmostEqual(result["intercepted_share"], 1.0)
        self.assertAlmostEqual(result["intercepted_of_scored"], round(1 / 39, 6))
        self.assertAlmostEqual(result["expected_pass_rate_gate_off"], 0.653846)
        self.assertAlmostEqual(result["expected_pass_rate_gate_on"], 0.641026)
        self.assertAlmostEqual(result["expected_delta"], -0.01282)
        self.assertAlmostEqual(result["calibrated_expected_pass_rate_gate_off"], 0.641026)
        self.assertAlmostEqual(result["calibrated_expected_pass_rate_gate_on"], 0.641026)
        self.assertAlmostEqual(result["calibrated_delta"], 0.0)
        self.assertEqual(result["total_advice_records"], 39)

    def test_m15_slice2_home_b_replay(self):
        """M15 slice 2 evidence home-b: calibrated advice throughout, so
        nothing would divert and nothing is intercepted."""
        entries = load_fixture("m16-gate-eval-home-b.jsonl")
        result = xs.compute_gate_eval(entries)
        groups = by_key(result)
        self.assertEqual(sorted(groups), [("echo-good", "test")])
        row = groups[("echo-good", "test")]
        self.assertEqual(row["n"], 9)
        self.assertEqual(row["would_divert"], 0)
        self.assertEqual(row["intercepted"], 0)
        self.assertIsNone(row["intercepted_share"])
        self.assertIsNone(row["intercepted_actual_pass_rate"])
        self.assertAlmostEqual(row["advised_pass_rate"], 1.0)
        self.assertAlmostEqual(row["actual_pass_rate"], 1.0)
        self.assertAlmostEqual(row["expected_pass_rate_gate_off"], 1.0)
        self.assertAlmostEqual(row["expected_pass_rate_gate_on"], 1.0)
        self.assertAlmostEqual(row["expected_delta"], 0.0)
        self.assertAlmostEqual(row["calibrated_delta"], 0.0)
        self.assertEqual(result["total_scored"], 9)
        self.assertEqual(result["total_intercepted"], 0)
        self.assertEqual(result["intercepted_ids"], [])
        self.assertEqual(recorded_gated_ids(entries), [])
        self.assertEqual(result["total_advice_records"], 9)


GOLDEN = Path(__file__).resolve().parent / "golden" / "report-gate-eval.txt"


class GateEvalGoldenTests(unittest.TestCase):
    def test_report_text_matches_golden(self):
        result = xs.compute_gate_eval(known_ledger())
        self.assertEqual(xs.gate_eval_text(result, "LEDGER"),
                         GOLDEN.read_text(encoding="utf-8"))


class GateEvalCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-gate-eval-")
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
        args = SimpleNamespace(gate_eval=True, fleet=False, json=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_gate_eval(args, {})
        text = out.getvalue()
        self.assertIn("bad", text)
        self.assertIn("small-n", text)
        self.assertIn("n/a", text)  # the pi/fix n=0 row shows no invented rates
        self.assertIn("intercepted runs: actual 0.50 vs expected-if-followed 0.90", text)
        self.assertIn("calibrated", text)
        args = SimpleNamespace(gate_eval=True, fleet=False, json=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_gate_eval(args, {})
        parsed = json.loads(out.getvalue())
        bad = next(row for row in parsed["groups"]
                   if (row["backend"], row["card_type"]) == ("bad", "fix"))
        self.assertEqual(bad["intercepted"], 4)
        self.assertAlmostEqual(bad["calibrated_delta"], 0.083333)
        self.assertAlmostEqual(parsed["calibrated_delta"], 0.018293)
        self.assertAlmostEqual(parsed["gate_diff"], 0.20)
        self.assertEqual(parsed["gate_min_samples"], 8)

    def test_report_option_missing_ledger_does_not_crash(self):
        args = SimpleNamespace(gate_eval=True, fleet=False, json=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_gate_eval(args, {})
        self.assertIn("no advice history", out.getvalue())
        args = SimpleNamespace(gate_eval=True, fleet=False, json=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_gate_eval(args, {})
        self.assertEqual(json.loads(out.getvalue())["groups"], [])

    def test_report_dispatches_through_cmd_report(self):
        self.write_ledger(known_ledger())
        args = SimpleNamespace(gate_eval=True, advice_accuracy=False, weighted_eval=False,
                               fleet=False, json=False, daily=False, date=None, html=None,
                               since_minutes=60)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_report(args, {})
        self.assertIn("small-n", out.getvalue())

    def test_cli_accepts_report_gate_eval(self):
        self.write_ledger(known_ledger())
        env = dict(os.environ, OPENSWARM_HOME=str(self.root))
        proc = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "..", "bin", "openswarm"),
             "report", "--gate-eval"],
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("small-n", proc.stdout)

    def test_gate_bar_comes_from_policy(self):
        """A custom gate bar in policy replays under that bar: with
        gate_diff 0.80 the bad group is no longer gated."""
        self.write_ledger(known_ledger())
        policy = {"routing": {"calibration_gate": True, "calibration_gate_diff": 0.80}}
        args = SimpleNamespace(gate_eval=True, fleet=False, json=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_gate_eval(args, policy)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed["total_intercepted"], 0)
        self.assertAlmostEqual(parsed["gate_diff"], 0.80)


if __name__ == "__main__":
    unittest.main(verbosity=2)
