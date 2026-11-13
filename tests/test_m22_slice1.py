"""M22 slice 1 (#189): quota_before records planner-vetoed backends as
skipped + reason, and the Delta replay attributes planner-veto rescues.

Background (#187 / #185 comment 6833284828): with codex genuinely
exhausted, plan_codex() vetoes codex at the worst window (used 1.0 >=
window_hard 0.95) before apply_quota_routing() ever sees it, so the run
lands on pi and the ledger records a stay with quota_before {pi: 0.86}
— the rescue is real but invisible to the replay, which counts it as a
stay and gives quota-on no positive Delta.

This slice wires the handoff through: route() keeps the verbatim veto
notes per backend (skipped_details); apply_quota_routing() records them
in quota_routing.planner_vetoed as {backend: {status "skipped", reason,
used, window_hard, quota_veto}} with used / window_hard read off the
planner's own note and never invented; compute_quota_eval() re-attributes
a stayed run with a hard-window planner veto as a reroute from the
vetoed backend to the backend that ran. quota_before itself stays
numeric-only (only backends quota routing actually checked), and quota
off / no veto / non-quota veto all behave exactly as today.

Run: python3 -m unittest tests.test_m22_slice1
     (or: python3 tests/test_m22_slice1.py)
"""
import copy
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
xs.breakers = lambda: {}  # isolate from any breakers on disk
xs.load_policy = lambda: xs.jload(f"{xs.SKILL_DIR}/policy.json", {})  # ignore policy.local.json overrides

POLICY = xs.load_policy()
POLICY["codex"]["require_default_model"] = False
with open(_load.FIXTURE_STATE) as f:
    BASE = json.load(f)

PI_ISOLATED = os.path.join(_load.ROOT, "bin", "pi-isolated")


def pi_state(quota, binary=None):
    return {"binary": binary or PI_ISOLATED, "provider": "minimax-cn",
            "default_model": "MiniMax-M3.1-Flash", "quota": quota}


def state_with_pi(quota, codex_used=None):
    st = copy.deepcopy(BASE)
    st["pi"] = pi_state(quota)
    if codex_used is not None:
        for window in st["codex"]["limits"]["codex"]["windows"].values():
            window["used"] = codex_used
    return st


def quota_policy(order, threshold=0, sources=None, enabled=True):
    policy = copy.deepcopy(POLICY)
    policy["routing"]["default_order"] = list(order)
    policy["routing"]["weighted"] = False
    policy["routing"]["quota"] = {"enabled": enabled, "threshold": threshold,
                                  "sources": sources or {}}
    return policy


def quota_record(threshold=0, applied=False, rerouted=False, from_backend=None,
                 to_backend=None, quota_before=None, all_exhausted=False,
                 reason=None, planner_vetoed=None):
    record = {"enabled": True, "threshold": threshold, "applied": applied,
              "quota_before": dict(quota_before or {}),
              "reason": reason or "synthetic"}
    if rerouted:
        record["rerouted"] = True
        record["from"] = from_backend
        record["to"] = to_backend
    if all_exhausted:
        record["all_exhausted"] = True
    if planner_vetoed is not None:
        record["planner_vetoed"] = planner_vetoed
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


def hard_veto(codex_reason="codex limit codex at 100% >= hard 95%"):
    return {"codex": {"status": "skipped", "reason": codex_reason,
                      "used": 1.0, "window_hard": 0.95, "quota_veto": True}}


class PlannerVetoHandoff(unittest.TestCase):
    def setUp(self):
        # route() -> plan_pi() requires a pi binary; these tests exercise
        # quota routing, not pi discovery, so point pi discovery at the
        # in-repo isolated launcher (same as test_m20_slice1).
        self._pi_bin_patch = mock.patch.object(xs, "pi_bin", return_value=PI_ISOLATED)
        self._pi_bin_patch.start()

    def tearDown(self):
        self._pi_bin_patch.stop()

    def decide(self, policy, st, prof=None):
        prof = prof or {"type": "impl", "difficulty": 3}
        decision = xs.route(prof, st, policy)
        self.assertTrue(decision["ok"], decision)
        return xs.apply_quota_routing(prof, st, policy, decision)

    def test_hard_window_veto_recorded_skipped_with_numbers(self):
        # M21 R1 shape: codex genuinely exhausted (worst window used 1.0),
        # pi at 0.86 above the 0.5 threshold.
        st = state_with_pi({"remaining": 0.86, "unit": "fraction"}, codex_used=1.0)
        decision, info = self.decide(quota_policy(["codex", "pi"], threshold=0.5), st)
        self.assertEqual(decision["backend"], "pi")
        self.assertEqual(decision["skipped"], {"codex": "codex unavailable (limits/breakers)"})
        self.assertIn("codex limit codex at 100% >= hard 95%",
                      decision["skipped_details"]["codex"][0])
        # The run stays on pi (no quota reroute fired) — but the veto is
        # on the record, and quota_before is exactly what it was today.
        self.assertFalse(info["applied"])
        self.assertEqual(info["quota_before"], {"pi": 0.86})
        self.assertEqual(info["planner_vetoed"], hard_veto())
        self.assertIn("planner vetoed codex", info["reason"])
        self.assertIn("100% >= hard 95%", info["reason"])

    def test_no_veto_leaves_record_unchanged(self):
        st = state_with_pi({"remaining": 0.86, "unit": "fraction"}, codex_used=0.1)
        decision, info = self.decide(quota_policy(["codex", "pi"], threshold=0.5), st)
        self.assertEqual(decision["backend"], "codex")
        self.assertEqual(decision["skipped"], {})
        self.assertFalse(info["applied"])
        self.assertEqual(info["quota_before"], {"codex": 0.9})
        self.assertNotIn("planner_vetoed", info)

    def test_non_quota_veto_recorded_without_rescue_flag(self):
        # No codex binary: a planner veto, but not a quota-exhaustion one.
        # The reason is recorded verbatim; used / window_hard stay None
        # (never invented) and quota_veto is false.
        st = state_with_pi({"remaining": 0.86, "unit": "fraction"})
        st["codex"]["binary"] = None
        decision, info = self.decide(quota_policy(["codex", "pi"], threshold=0.5), st)
        self.assertEqual(decision["backend"], "pi")
        self.assertEqual(info["quota_before"], {"pi": 0.86})
        self.assertEqual(info["planner_vetoed"], {
            "codex": {"status": "skipped", "reason": "no working codex binary",
                      "used": None, "window_hard": None, "quota_veto": False}})

    def test_quota_disabled_is_still_a_no_op(self):
        st = state_with_pi({"remaining": 0.86, "unit": "fraction"}, codex_used=1.0)
        decision, info = self.decide(
            quota_policy(["codex", "pi"], threshold=0.5, enabled=False), st)
        self.assertEqual(decision["backend"], "pi")  # planner veto still applies
        self.assertIsNone(info)  # quota off: no record at all, as today

    def test_planner_vetoes_unit_without_route(self):
        # Synthetic / old decisions without skipped_details degrade to the
        # generic reason, never to an invented quota veto.
        self.assertEqual(xs.planner_vetoes({"skipped": {}}), {})
        self.assertEqual(xs.planner_vetoes(
            {"skipped": {"codex": "codex unavailable (limits/breakers)"}}), {
                "codex": {"status": "skipped",
                          "reason": "codex unavailable (limits/breakers)",
                          "used": None, "window_hard": None, "quota_veto": False}})
        self.assertEqual(xs.planner_vetoes(
            {"skipped": {"codex": "codex unavailable (limits/breakers)"},
             "skipped_details": {"codex": ["codex limit codex at 100% >= hard 95%"]}}),
            hard_veto())


def by_key(result):
    return {(row["backend"], row["card_type"]): row for row in result["groups"]}


class PlannerVetoReplay(unittest.TestCase):
    def test_veto_rescue_counts_as_reroute_with_positive_delta(self):
        # Seeds first so the prefix table sees them: codex 1/4, pi 4/4.
        rows = []
        for i in range(4):
            rows.append(entry(f"codex-s{i}", "codex", i == 3, quota=None))
        for i in range(4):
            rows.append(entry(f"pi-s{i}", "pi", True, quota=None))
        # M21 R1 shape: live record stayed on pi (0.86 above 0.5), but the
        # planner had already vetoed codex at 100% >= hard 95%.
        rows.append(entry("R1", "pi", True, quota=quota_record(
            threshold=0.5, quota_before={"pi": 0.86},
            reason="pi remaining 0.86 is above threshold 0.5; planner vetoed codex "
                   "(codex limit codex at 100% >= hard 95%)",
            planner_vetoed=hard_veto())))
        result = xs.compute_quota_eval(rows)
        groups = by_key(result)
        self.assertEqual(groups[("codex", "fix")]["decisions"], 1)
        self.assertEqual(groups[("codex", "fix")]["rerouted"], 1)
        self.assertEqual(groups[("codex", "fix")]["stayed"], 0)
        self.assertEqual(groups[("codex", "fix")]["replay_matches"], 1)
        self.assertEqual(groups[("codex", "fix")]["mismatched_ids"], [])
        self.assertEqual(groups[("codex", "fix")]["expected_pass_rate_quota_off"], 0.25)
        self.assertEqual(groups[("codex", "fix")]["expected_pass_rate_quota_on"], 1.0)
        self.assertEqual(groups[("codex", "fix")]["expected_delta"], 0.75)
        self.assertEqual(result["total_rerouted"], 1)
        self.assertEqual(result["total_stayed"], 0)
        self.assertEqual(result["mismatched_ids"], [])

    def test_non_quota_veto_stays_a_stay(self):
        rows = [entry("R2", "pi", False, quota=quota_record(
            threshold=0.5, quota_before={"pi": 0.86},
            reason="pi remaining 0.86 is above threshold 0.5",
            planner_vetoed={"codex": {"status": "skipped",
                                      "reason": "no working codex binary",
                                      "used": None, "window_hard": None,
                                      "quota_veto": False}}))]
        result = xs.compute_quota_eval(rows)
        groups = by_key(result)
        self.assertEqual(groups[("pi", "fix")]["stayed"], 1)
        self.assertEqual(groups[("pi", "fix")]["rerouted"], 0)
        self.assertEqual(result["mismatched_ids"], [])

    def test_plain_record_without_veto_field_is_unchanged(self):
        rows = [entry("R3", "pi", True, quota=quota_record(
            threshold=0.5, quota_before={"pi": 0.86},
            reason="pi remaining 0.86 is above threshold 0.5"))]
        result = xs.compute_quota_eval(rows)
        groups = by_key(result)
        self.assertEqual(groups[("pi", "fix")]["stayed"], 1)
        self.assertEqual(result["total_rerouted"], 0)

    def test_live_quota_reroute_wins_over_veto_rule(self):
        # One run is never two reroutes: a live quota reroute low -> high
        # keeps low as the default even when a hard planner veto is also
        # on the record.
        rows = [entry("R4", "high", True, quota=quota_record(
            applied=True, rerouted=True, from_backend="low", to_backend="high",
            quota_before={"low": 0, "high": 5},
            reason="low remaining 0 is at or below threshold 0; rerouted to high (remaining 5)",
            planner_vetoed=hard_veto()))]
        result = xs.compute_quota_eval(rows)
        groups = by_key(result)
        self.assertEqual(groups[("low", "fix")]["rerouted"], 1)
        self.assertNotIn(("codex", "fix"), groups)
        self.assertEqual(result["mismatched_ids"], [])

    def test_veto_rescue_without_prefix_history_counts_but_delta_zero(self):
        # No seed runs: the reroute is counted, but with no prefix rate
        # either side falls back to the observed outcome, so the Delta is
        # honestly 0 rather than invented.
        rows = [entry("R5", "pi", True, quota=quota_record(
            threshold=0.5, quota_before={"pi": 0.86},
            reason="pi remaining 0.86 is above threshold 0.5",
            planner_vetoed=hard_veto()))]
        result = xs.compute_quota_eval(rows)
        groups = by_key(result)
        self.assertEqual(groups[("codex", "fix")]["rerouted"], 1)
        self.assertEqual(groups[("codex", "fix")]["expected_delta"], 0.0)
        self.assertEqual(result["mismatched_ids"], [])


if __name__ == "__main__":
    unittest.main()
