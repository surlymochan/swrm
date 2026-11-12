"""Quota-flip evaluation report (M19 slice 1, #177).

`report --quota-eval` replays recorded quota decisions through the same
live rule `apply_quota_routing` applies (M17, #169): a backend whose
remaining quota is at or below the threshold no longer receives new runs.
Each decision is replayed from the dispatch-time snapshot the ledger
itself recorded (`quota_routing.quota_before` with the threshold recorded
alongside it) — live quota is never re-read — and the replay verdict
(rerouted / refused / stayed / unknown) is cross-checked against the
recorded live verdict, target included. Expected pass rates are over
scored runs only: a stayed run keeps its observed outcome, a rerouted run
takes the default backend's prefix observed rate under quota-off and the
target's prefix rate under quota-on, and a refused run takes the
default's prefix rate under quota-off and 0 under quota-on (it never
dispatches). Batch/fleet decision records carry no outcome, so they enter
the reroute/refuse shares but not the pass rates. The command is
read-only: it never changes routing, the shipped default, or the ledger.

Run: python3 -m unittest tests.test_m19_slice1
     (or: python3 tests/test_m19_slice1.py)
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


def quota_record(threshold=0, applied=False, rerouted=False, from_backend=None,
                 to_backend=None, quota_before=None, all_exhausted=False,
                 reason=None):
    record = {"enabled": True, "threshold": threshold, "applied": applied,
              "quota_before": dict(quota_before or {}),
              "reason": reason or "synthetic"}
    if rerouted:
        record["rerouted"] = True
        record["from"] = from_backend
        record["to"] = to_backend
    if all_exhausted:
        record["all_exhausted"] = True
    return record


def entry(run_id, backend, outcome, card_type="fix", quota=None, extra=None):
    row = {"id": run_id, "backend": backend, "model": backend,
           "card_type": card_type, "status": "done", "exec_pass": outcome}
    if quota is not None:
        row["quota_routing"] = quota
        if quota.get("rerouted"):
            row["quota_rerouted_from"] = quota.get("from")
    if extra:
        row.update(extra)
    return row


def known_ledger():
    """Hand-computable fixture; the group's expected row is derived below.

    Ledger order is part of the fixture: seed runs come first so the
    prefix table sees them, then the runs whose quota verdict is tested.
    Threshold 0 throughout.

    low/fix seeds: 4 runs on low, 1 pass -> prefix rate 0.25.
    high/fix seeds: 4 runs on high, 4 pass -> prefix rate 1.00.
    R1 reroutes low -> high (0 -> 5), passes: prefix low 0.25, high 1.00,
        so off 0.25, on 1.00.
    R2 stays on low (5), fails: off 0, on 0.
    R3 is refused (low 0, high 0), exec_pass false is the refusal
        artefact: prefix low is 1/5 = 0.20 (4 seeds + R2), so off 0.20,
        on 0.00.
    R4 reroutes low -> high (0 -> 3), passes: prefix low 1/5 = 0.20
        (R3 ran nowhere, backend null), high 5/5 = 1.00, so off 0.20,
        on 1.00.
    Group (low, fix): decisions 4, rerouted 2, refused 1, stayed 1,
        shares 0.50 / 0.25 / 0.25; n = 4, actual 0.50;
        expected off (0.25 + 0 + 0.20 + 0.20) / 4 = 0.1625,
        expected on (1.00 + 0 + 0 + 1.00) / 4 = 0.50, delta +0.3375.
        The 8 seed runs carry no quota record and are unreplayable.
    """
    rows = []
    for i in range(4):
        rows.append(entry(f"low-s{i}", "low", i == 3, quota=None))
    for i in range(4):
        rows.append(entry(f"high-s{i}", "high", True, quota=None))
    rows.append(entry("R1", "high", True, quota=quota_record(
        applied=True, rerouted=True, from_backend="low", to_backend="high",
        quota_before={"low": 0, "high": 5},
        reason="low remaining 0 is at or below threshold 0; rerouted to high (remaining 5)")))
    rows.append(entry("R2", "low", False, quota=quota_record(
        quota_before={"low": 5},
        reason="low remaining 5 is above threshold 0")))
    rows.append(entry("R3", None, False, quota=quota_record(
        all_exhausted=True, quota_before={"low": 0, "high": 0},
        reason="all routable backends at or below threshold 0: low remaining 0, high remaining 0; refusing to dispatch")))
    rows.append(entry("R4", "high", True, quota=quota_record(
        applied=True, rerouted=True, from_backend="low", to_backend="high",
        quota_before={"low": 0, "high": 3},
        reason="low remaining 0 is at or below threshold 0; rerouted to high (remaining 3)")))
    return rows


def by_key(result):
    return {(row["backend"], row["card_type"]): row for row in result["groups"]}


def load_fixture(name):
    path = Path(__file__).resolve().parent / "fixtures" / name
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def recorded_ids(entries, predicate):
    return [entry["id"] for entry in entries
            if isinstance(entry.get("quota_routing"), dict)
            and predicate(entry["quota_routing"])]


class QuotaEvalTests(unittest.TestCase):
    def test_known_numbers(self):
        result = xs.compute_quota_eval(known_ledger())
        groups = by_key(result)
        self.assertEqual(sorted(groups), [("low", "fix")])
        row = groups[("low", "fix")]
        self.assertEqual(row["decisions"], 4)
        self.assertEqual(row["rerouted"], 2)
        self.assertEqual(row["refused"], 1)
        self.assertEqual(row["stayed"], 1)
        self.assertEqual(row["unknown"], 0)
        self.assertEqual(row["decided"], 4)
        self.assertAlmostEqual(row["rerouted_share"], 0.5)
        self.assertAlmostEqual(row["refused_share"], 0.25)
        self.assertAlmostEqual(row["stayed_share"], 0.25)
        self.assertEqual(row["rerouted_ids"], ["R1", "R4"])
        self.assertEqual(row["refused_ids"], ["R3"])
        self.assertEqual(row["n"], 4)
        self.assertAlmostEqual(row["actual_pass_rate"], 0.5)
        self.assertAlmostEqual(row["expected_pass_rate_quota_off"], 0.1625)
        self.assertAlmostEqual(row["expected_pass_rate_quota_on"], 0.5)
        self.assertAlmostEqual(row["expected_delta"], 0.3375)
        self.assertEqual(row["replay_matches"], 4)
        self.assertEqual(row["mismatched_ids"], [])
        self.assertTrue(row["small_n"])
        self.assertEqual(result["total_replayable"], 4)
        self.assertEqual(result["total_decided"], 4)
        self.assertEqual(result["total_rerouted"], 2)
        self.assertEqual(result["total_refused"], 1)
        self.assertEqual(result["total_stayed"], 1)
        self.assertEqual(result["total_unknown"], 0)
        self.assertAlmostEqual(result["rerouted_share"], 0.5)
        self.assertAlmostEqual(result["refused_share"], 0.25)
        self.assertAlmostEqual(result["stayed_share"], 0.25)
        self.assertEqual(result["rerouted_ids"], ["R1", "R4"])
        self.assertEqual(result["refused_ids"], ["R3"])
        self.assertEqual(result["total_scored"], 4)
        self.assertAlmostEqual(result["actual_pass_rate"], 0.5)
        self.assertAlmostEqual(result["expected_pass_rate_quota_off"], 0.1625)
        self.assertAlmostEqual(result["expected_pass_rate_quota_on"], 0.5)
        self.assertAlmostEqual(result["expected_delta"], 0.3375)
        self.assertEqual(result["replay_matches"], 4)
        self.assertEqual(result["mismatched_ids"], [])
        self.assertEqual(result["unreplayable"], 8)
        self.assertEqual(result["unreplayable_ids"],
                         [f"low-s{i}" for i in range(4)]
                         + [f"high-s{i}" for i in range(4)])
        self.assertEqual(result["default_threshold"], 0)

    def test_explicit_backend_stays(self):
        rows = [entry("E1", "low", True, quota=quota_record(
            quota_before={},
            reason="explicit --backend wins over quota routing"))]
        result = xs.compute_quota_eval(rows)
        row = by_key(result)[("low", "fix")]
        self.assertEqual(row["decisions"], 1)
        self.assertEqual(row["stayed"], 1)
        self.assertEqual(row["replay_matches"], 1)
        self.assertEqual(result["unreplayable"], 0)

    def test_unknown_when_snapshot_misses_default(self):
        # The read for the default pick failed, so the record kept the old
        # behaviour: the replay cannot say what quota-on would have done.
        rows = [entry("U1", "low", True, quota=quota_record(
            quota_before={"high": 5},
            reason="quota read failed for 'low': quota env var MISSING is not set; kept the default pick (old behaviour)"))]
        # A failed read for the default pick leaves it out of
        # quota_before; that missing coverage is what makes this unknown.
        result = xs.compute_quota_eval(rows)
        row = by_key(result)[("low", "fix")]
        self.assertEqual(row["decisions"], 1)
        self.assertEqual(row["unknown"], 1)
        self.assertEqual(row["decided"], 0)
        self.assertIsNone(row["rerouted_share"])
        self.assertEqual(row["n"], 1)
        self.assertAlmostEqual(row["expected_pass_rate_quota_off"], 1.0)
        self.assertAlmostEqual(row["expected_delta"], 0.0)
        self.assertEqual(row["replay_matches"], 0)
        self.assertEqual(row["mismatched_ids"], ["U1"])

    def test_unknown_remaining_target_is_taken(self):
        # No known-good fallback, but one backend reports unknown
        # remaining: the live code reroutes to it, and so does the replay.
        rows = [entry("K1", "high", True, quota=quota_record(
            applied=True, rerouted=True, from_backend="low", to_backend="high",
            quota_before={"low": 0, "high": None},
            reason="low remaining 0 is at or below threshold 0; rerouted to high (remaining unknown)"))]
        result = xs.compute_quota_eval(rows)
        row = by_key(result)[("low", "fix")]
        self.assertEqual(row["rerouted"], 1)
        self.assertEqual(row["replay_matches"], 1)

    def test_unknown_remaining_default_stays(self):
        rows = [entry("K2", "low", True, quota=quota_record(
            quota_before={"low": None},
            reason="low quota unknown; kept the default pick"))]
        result = xs.compute_quota_eval(rows)
        row = by_key(result)[("low", "fix")]
        self.assertEqual(row["stayed"], 1)
        self.assertEqual(row["replay_matches"], 1)

    def test_recorded_threshold_wins_over_default(self):
        # Recorded threshold 5: low at 2 is exhausted. Under the default
        # threshold 0 the same snapshot would stay.
        rows = [entry("T1", "high", True, quota=quota_record(
            threshold=5, applied=True, rerouted=True, from_backend="low",
            to_backend="high", quota_before={"low": 2, "high": 9},
            reason="low remaining 2 is at or below threshold 5; rerouted to high (remaining 9)"))]
        result = xs.compute_quota_eval(rows, default_threshold=0)
        self.assertEqual(by_key(result)[("low", "fix")]["rerouted"], 1)
        rows[0]["quota_routing"] = dict(rows[0]["quota_routing"])
        del rows[0]["quota_routing"]["threshold"]
        result = xs.compute_quota_eval(rows, default_threshold=0)
        self.assertEqual(by_key(result)[("low", "fix")]["stayed"], 1)
        result = xs.compute_quota_eval(rows, default_threshold=5)
        self.assertEqual(by_key(result)[("low", "fix")]["rerouted"], 1)

    def test_invalid_recorded_threshold_is_unreplayable(self):
        rows = [entry("B1", "low", True, quota=quota_record(
            threshold=-1, quota_before={"low": 5}))]
        result = xs.compute_quota_eval(rows)
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["total_replayable"], 0)
        self.assertEqual(result["unreplayable"], 1)
        self.assertEqual(result["unreplayable_ids"], ["B1"])

    def test_invalid_default_threshold_rejected(self):
        for bad in (True, -1, "0", None, float("nan")):
            with self.assertRaises(ValueError, msg=f"default_threshold={bad!r}"):
                xs.compute_quota_eval(known_ledger(), default_threshold=bad)

    def test_same_history_reproduces_same_eval(self):
        """The replay is a pure function of ledger order: the same input
        twice yields the same result."""
        first = xs.compute_quota_eval(known_ledger())
        second = xs.compute_quota_eval(known_ledger())
        self.assertEqual(first, second)

    def test_empty_ledger(self):
        result = xs.compute_quota_eval([])
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["total_replayable"], 0)
        self.assertEqual(result["total_scored"], 0)
        self.assertIsNone(result["rerouted_share"])
        self.assertIsNone(result["expected_pass_rate_quota_off"])
        self.assertIsNone(result["expected_delta"])
        self.assertEqual(result["unreplayable"], 0)
        text = xs.quota_eval_text(result, "<missing>")
        self.assertIn("no quota history", text)


class QuotaEvalRealLedgerTests(unittest.TestCase):
    def test_m17_single_runs_replay(self):
        """M17 slice 2 (#171) evidence: 4 real single runs on the merged
        main (echo backends, env quota sources). The numbers below are a
        hand recount of the ledger, not the report's own output:
        S1 reroutes low -> high (0 -> 5) and passes; S2 stays on low (5)
        and passes; S3 is refused (low 0, high 0) with exec_pass false as
        the refusal artefact; S4 reroutes low -> high (0 -> 5) and passes.
        Under quota-off the refused run would have run on low, whose
        prefix observed rate was 1.00 (S2 passed), so expected-off is
        (1.00 + 1.00 + 1.00 + 1.00) / 4 = 1.00 and expected-on is
        (1.00 + 1.00 + 0 + 1.00) / 4 = 0.75: the whole delta (-0.25) is
        the refusal penalty, because both echo backends always pass and
        rerouting changes nothing.
        """
        entries = load_fixture("m19-quota-eval-single.jsonl")
        result = xs.compute_quota_eval(entries)
        groups = by_key(result)
        self.assertEqual(sorted(groups), [("echo-low", "-")])
        row = groups[("echo-low", "-")]
        self.assertEqual(row["decisions"], 4)
        self.assertEqual(row["rerouted"], 2)
        self.assertEqual(row["refused"], 1)
        self.assertEqual(row["stayed"], 1)
        self.assertAlmostEqual(row["rerouted_share"], 0.5)
        self.assertAlmostEqual(row["refused_share"], 0.25)
        self.assertAlmostEqual(row["stayed_share"], 0.25)
        self.assertEqual(row["rerouted_ids"],
                         ["20261112-021442-6c0ba2", "20261112-021553-222b11"])
        self.assertEqual(row["refused_ids"], ["20261112-021526-129b79"])
        self.assertEqual(row["n"], 4)
        self.assertAlmostEqual(row["actual_pass_rate"], 0.75)
        self.assertAlmostEqual(row["expected_pass_rate_quota_off"], 1.0)
        self.assertAlmostEqual(row["expected_pass_rate_quota_on"], 0.75)
        self.assertAlmostEqual(row["expected_delta"], -0.25)
        self.assertEqual(row["replay_matches"], 4)
        self.assertEqual(row["mismatched_ids"], [])
        self.assertEqual(result["total_replayable"], 4)
        self.assertEqual(result["total_rerouted"], 2)
        self.assertEqual(result["total_refused"], 1)
        self.assertEqual(result["total_stayed"], 1)
        self.assertEqual(result["replay_matches"], 4)
        self.assertEqual(result["unreplayable"], 0)
        # The replay reproduces the recorded live verdicts exactly.
        self.assertEqual(result["rerouted_ids"], recorded_ids(
            entries, lambda qr: bool(qr.get("rerouted"))))
        self.assertEqual(result["refused_ids"], recorded_ids(
            entries, lambda qr: bool(qr.get("all_exhausted"))))

    def test_m18_batch_fleet_replay(self):
        """M18 slice 2 (#175) evidence: 5 real batch/fleet decisions on
        the merged main (echo backends, env quota sources). B1 batch
        reroutes low -> high, B2 batch stays on low, B3-a batch is refused
        (a.md) while B3-b is blocked by its dependency and carries no
        quota record, F1 fleet reroutes low -> high, F2 fleet is refused.
        Decision records carry no outcome, so the groups have n = 0 and
        no invented rates; the blocked card is unreplayable, not a quota
        decision.
        """
        entries = load_fixture("m19-quota-eval-batch-fleet.jsonl")
        result = xs.compute_quota_eval(entries)
        groups = by_key(result)
        self.assertEqual(sorted(groups), [("echo-low", "-"), ("echo-low", "fix")])
        batch = groups[("echo-low", "-")]
        self.assertEqual(batch["decisions"], 3)
        self.assertEqual(batch["rerouted"], 1)
        self.assertEqual(batch["refused"], 1)
        self.assertEqual(batch["stayed"], 1)
        self.assertAlmostEqual(batch["rerouted_share"], round(1 / 3, 6))
        self.assertAlmostEqual(batch["refused_share"], round(1 / 3, 6))
        self.assertEqual(batch["n"], 0)
        self.assertIsNone(batch["actual_pass_rate"])
        self.assertIsNone(batch["expected_pass_rate_quota_off"])
        self.assertIsNone(batch["expected_delta"])
        self.assertEqual(batch["replay_matches"], 3)
        fleet = groups[("echo-low", "fix")]
        self.assertEqual(fleet["decisions"], 2)
        self.assertEqual(fleet["rerouted"], 1)
        self.assertEqual(fleet["refused"], 1)
        self.assertEqual(fleet["stayed"], 0)
        self.assertAlmostEqual(fleet["rerouted_share"], 0.5)
        self.assertAlmostEqual(fleet["refused_share"], 0.5)
        self.assertEqual(fleet["n"], 0)
        self.assertEqual(fleet["replay_matches"], 2)
        self.assertEqual(result["total_replayable"], 5)
        self.assertEqual(result["total_rerouted"], 2)
        self.assertEqual(result["total_refused"], 2)
        self.assertEqual(result["total_stayed"], 1)
        self.assertAlmostEqual(result["rerouted_share"], 0.4)
        self.assertAlmostEqual(result["refused_share"], 0.4)
        self.assertAlmostEqual(result["stayed_share"], 0.2)
        self.assertEqual(result["total_scored"], 0)
        self.assertIsNone(result["expected_delta"])
        self.assertEqual(result["replay_matches"], 5)
        self.assertEqual(result["mismatched_ids"], [])
        self.assertEqual(result["unreplayable"], 1)
        self.assertEqual(result["unreplayable_ids"],
                         ["batch-20261112-040935-d0ff4c-b.md"])
        self.assertEqual(result["rerouted_ids"], recorded_ids(
            entries, lambda qr: bool(qr.get("rerouted"))))
        self.assertEqual(result["refused_ids"], recorded_ids(
            entries, lambda qr: bool(qr.get("all_exhausted"))))

    def test_m19_seed_ledger_replay(self):
        """Fresh seed ledger run for this slice (driver
        bench-env/m19s1-seed/run_seed.py, 9 scenarios / 11 decisions on
        this clone's binary): S0 stays on echo-fail and fails, S1 reroutes
        low -> high, S2 stays on low, S3 is refused, S4 reroutes
        echo-fail -> echo-low, B1 reroutes 2 cards, B2 stays 2 cards, F1
        reroutes, F2 is refused. Hand recount, not the report's output:
        the failing-backend group gains from rerouting (off 0.00, on
        0.50, delta +0.50: S0 fails on echo-fail, S4 passes after
        rerouting to echo-low, whose prefix rate was 1.00), while the
        low/fix group loses exactly the refused runs (off 1.00, on 0.67,
        delta -0.33: S3 would have run on low at prefix 1.00); net delta
        over all scored runs is 0.00 (off 0.60, on 0.60) — the group split
        is the finding, and a single total would hide it.
        """
        entries = load_fixture("m19-quota-eval-seed.jsonl")
        result = xs.compute_quota_eval(entries)
        groups = by_key(result)
        self.assertEqual(sorted(groups), [("echo-fail", "fix"), ("echo-low", "-"),
                                         ("echo-low", "fix")])
        fail = groups[("echo-fail", "fix")]
        self.assertEqual(fail["decisions"], 2)
        self.assertEqual(fail["rerouted"], 1)
        self.assertEqual(fail["refused"], 0)
        self.assertEqual(fail["stayed"], 1)
        self.assertEqual(fail["n"], 2)
        self.assertAlmostEqual(fail["actual_pass_rate"], 0.5)
        self.assertAlmostEqual(fail["expected_pass_rate_quota_off"], 0.0)
        self.assertAlmostEqual(fail["expected_pass_rate_quota_on"], 0.5)
        self.assertAlmostEqual(fail["expected_delta"], 0.5)
        self.assertEqual(fail["replay_matches"], 2)
        batch = groups[("echo-low", "-")]
        self.assertEqual(batch["decisions"], 4)
        self.assertEqual(batch["rerouted"], 2)
        self.assertEqual(batch["refused"], 0)
        self.assertEqual(batch["stayed"], 2)
        self.assertEqual(batch["n"], 0)
        self.assertIsNone(batch["expected_delta"])
        self.assertEqual(batch["replay_matches"], 4)
        low = groups[("echo-low", "fix")]
        self.assertEqual(low["decisions"], 5)
        self.assertEqual(low["rerouted"], 2)
        self.assertEqual(low["refused"], 2)
        self.assertEqual(low["stayed"], 1)
        self.assertEqual(low["n"], 3)
        self.assertAlmostEqual(low["actual_pass_rate"], round(2 / 3, 6))
        self.assertAlmostEqual(low["expected_pass_rate_quota_off"], 1.0)
        self.assertAlmostEqual(low["expected_pass_rate_quota_on"], round(2 / 3, 6))
        self.assertAlmostEqual(low["expected_delta"], round(-1 / 3, 6))
        self.assertEqual(low["replay_matches"], 5)
        self.assertEqual(result["total_replayable"], 11)
        self.assertEqual(result["total_rerouted"], 5)
        self.assertEqual(result["total_refused"], 2)
        self.assertEqual(result["total_stayed"], 4)
        self.assertAlmostEqual(result["rerouted_share"], round(5 / 11, 6))
        self.assertAlmostEqual(result["refused_share"], round(2 / 11, 6))
        self.assertAlmostEqual(result["stayed_share"], round(4 / 11, 6))
        self.assertEqual(result["total_scored"], 5)
        self.assertAlmostEqual(result["actual_pass_rate"], 0.6)
        self.assertAlmostEqual(result["expected_pass_rate_quota_off"], 0.6)
        self.assertAlmostEqual(result["expected_pass_rate_quota_on"], 0.6)
        self.assertAlmostEqual(result["expected_delta"], 0.0)
        self.assertEqual(result["replay_matches"], 11)
        self.assertEqual(result["mismatched_ids"], [])
        self.assertEqual(result["unreplayable"], 0)
        self.assertEqual(result["rerouted_ids"], recorded_ids(
            entries, lambda qr: bool(qr.get("rerouted"))))
        self.assertEqual(result["refused_ids"], recorded_ids(
            entries, lambda qr: bool(qr.get("all_exhausted"))))


GOLDEN = Path(__file__).resolve().parent / "golden" / "report-quota-eval.txt"


class QuotaEvalGoldenTests(unittest.TestCase):
    def test_report_text_matches_golden(self):
        result = xs.compute_quota_eval(known_ledger())
        self.assertEqual(xs.quota_eval_text(result, "LEDGER"),
                         GOLDEN.read_text(encoding="utf-8"))


class QuotaEvalCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-quota-eval-")
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
        args = SimpleNamespace(quota_eval=True, fleet=False, json=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_quota_eval(args, {})
        text = out.getvalue()
        self.assertIn("low", text)
        self.assertIn("small-n", text)
        self.assertIn("rerouted: 2 (0.50 of decided)", text)
        self.assertIn("expected pass rate: quota off 0.16 vs quota on 0.50 (delta +0.34)", text)
        self.assertIn("replay vs recorded: 4/4 match", text)
        self.assertIn("unreplayable entries (no usable quota snapshot): 8", text)
        args = SimpleNamespace(quota_eval=True, fleet=False, json=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_quota_eval(args, {})
        parsed = json.loads(out.getvalue())
        row = next(item for item in parsed["groups"]
                   if (item["backend"], item["card_type"]) == ("low", "fix"))
        self.assertEqual(row["rerouted"], 2)
        self.assertAlmostEqual(row["expected_delta"], 0.3375)
        self.assertAlmostEqual(parsed["expected_delta"], 0.3375)
        self.assertEqual(parsed["default_threshold"], 0)
        self.assertEqual(parsed["unreplayable"], 8)

    def test_report_option_missing_ledger_does_not_crash(self):
        args = SimpleNamespace(quota_eval=True, fleet=False, json=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_quota_eval(args, {})
        self.assertIn("no quota history", out.getvalue())
        args = SimpleNamespace(quota_eval=True, fleet=False, json=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_quota_eval(args, {})
        self.assertEqual(json.loads(out.getvalue())["groups"], [])

    def test_report_dispatches_through_cmd_report(self):
        self.write_ledger(known_ledger())
        args = SimpleNamespace(quota_eval=True, advice_accuracy=False, weighted_eval=False,
                               gate_eval=False, fleet=False, json=False, daily=False,
                               date=None, html=None, since_minutes=60)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_report(args, {})
        self.assertIn("small-n", out.getvalue())

    def test_cli_accepts_report_quota_eval(self):
        # The real binary reads $OPENSWARM_HOME/ledger.jsonl (not the
        # in-process xs.LEDGER override), so write the ledger there; and
        # assert on content that only appears when history was actually
        # replayed — "small-n" alone would pass vacuously, since the
        # method paragraph mentions it even with no history.
        (self.root / "ledger.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in known_ledger()))
        env = dict(os.environ, OPENSWARM_HOME=str(self.root))
        proc = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "..", "bin", "openswarm"),
             "report", "--quota-eval"],
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rerouted: 2 (0.50 of decided)", proc.stdout)
        self.assertIn("expected pass rate: quota off 0.16 vs quota on 0.50 (delta +0.34)",
                      proc.stdout)
        self.assertIn("replay vs recorded: 4/4 match", proc.stdout)

    def test_policy_threshold_is_the_default(self):
        """A record without its own threshold replays under the policy
        threshold: low at 2 reroutes under 5 and stays under 0."""
        rows = [entry("T1", "high", True, quota=quota_record(
            applied=True, rerouted=True, from_backend="low", to_backend="high",
            quota_before={"low": 2, "high": 9},
            reason="synthetic"))]
        # Drop the recorded threshold to exercise the policy fallback.
        del rows[0]["quota_routing"]["threshold"]
        self.write_ledger(rows)
        policy = {"routing": {"quota": {"enabled": False, "threshold": 5, "sources": {}}}}
        args = SimpleNamespace(quota_eval=True, fleet=False, json=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_quota_eval(args, policy)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed["default_threshold"], 5)
        self.assertEqual(parsed["total_rerouted"], 1)
        self.assertEqual(parsed["replay_matches"], 1)
        policy = {"routing": {"quota": {"enabled": False, "threshold": 0, "sources": {}}}}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            xs.cmd_quota_eval(args, policy)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed["default_threshold"], 0)
        self.assertEqual(parsed["total_stayed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
