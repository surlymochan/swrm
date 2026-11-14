"""Codex usage-independent window: opt-in, strictly time-bounded (#222).

`codex.usage_independent_until` (policy.json / policy.local.json) is a
UTC instant. While it is still in the future:

  * the recommended Codex concurrency comes from the configured backend
    cap (`concurrency.codex`), never from the observed Codex usage or
    quota;
  * the observed Codex usage window neither excludes Codex nor changes
    its effort;
  * no Codex quota source is invoked or affects routing.

Strict boundary: before the deadline the window is active, at the
deadline and after it the default behaviour returns. A missing or
malformed setting fails safe to the default behaviour.

Everything else is untouched: hard concurrency caps, breakers (429 and
real provider errors), model availability, the other providers, and the
shipped default policy (the setting is off there).

No real backend, no network, no account data: the state is the
synthetic fixture and the clock is injected (xs.NOW), so the expiry
boundary is exact.

Run: python3 -m unittest discover -s tests   (or: python3 tests/test_codex_usage_independent.py)
"""
import argparse
import copy
import datetime as dt
import json
import os
import sys
import tempfile
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

# Generic mocked dates: the deadline is a fictional UTC instant and the
# clock is pinned around it, so the boundary is exact without waiting.
DEADLINE = "2030-06-01T00:00:00Z"
DEADLINE_EPOCH = dt.datetime(2030, 6, 1, tzinfo=dt.timezone.utc).timestamp()
BEFORE = DEADLINE_EPOCH - 3600
AT = DEADLINE_EPOCH
AFTER = DEADLINE_EPOCH + 3600


def window(used, hours_left=None, pace=None):
    """One synthetic Codex usage window (no account data)."""
    return {"used": used, "window_minutes": 300, "resets_at": None, "resets_at_local": None,
            "hours_to_reset": hours_left, "pace": pace}


def state(codex_used=0.1, codex_unknown=False, ocgo_used=0.0, deepseek_balance=55.0):
    st = copy.deepcopy(BASE)
    if codex_unknown:
        st["codex"]["limits"] = {"codex": {"name": "codex", "windows": {}, "observed_at": None,
                                           "age_minutes": None}}
    else:
        st["codex"]["limits"] = {"codex": {"name": "codex",
                                           "windows": {"five_hour": window(codex_used)},
                                           "observed_at": None, "age_minutes": 1}}
    st["deepseek"]["balance"] = deepseek_balance
    for model in st["ocgo"]["models"]:
        model["usage"] = {"5h": ocgo_used, "7d": ocgo_used, "30d": ocgo_used}
    return st


def split_limit_state(codex_used=1.0):
    """Codex on its own limit id: the planner sees no window for that
    model, while the quota reader sees the exhausted `codex` limit. This
    isolates the quota filter from the planner's usage veto."""
    st = state(codex_used=codex_used)
    for model in st["codex"]["models"]:
        model["limit_id"] = "base_model_inference"
    st["codex"]["limits"]["base_model_inference"] = {"name": "base_model_inference", "windows": {},
                                                     "observed_at": None, "age_minutes": None}
    return st


def policy(deadline=DEADLINE, cap=None, **kw):
    p = copy.deepcopy(POLICY)
    p["routing"]["default_order"] = ["codex", "ocgo"]
    p["routing"]["quota"] = {"enabled": False, "threshold": 0, "sources": {}}
    if deadline is not None:
        p["codex"]["usage_independent_until"] = deadline
    else:
        p["codex"].pop("usage_independent_until", None)
    if cap is not None:
        p["concurrency"] = dict(p["concurrency"], codex=cap)
    p.update(kw)
    return p


def profile(difficulty=3, **kw):
    return {"type": "impl", "difficulty": difficulty, **kw}


class Clock:
    """Inject the clock: xs.NOW is the module's only time source here."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self._saved = xs.NOW
        xs.NOW = lambda: self.value
        return self

    def __exit__(self, *exc):
        xs.NOW = self._saved
        return False


class SettingParsing(unittest.TestCase):
    """A missing or malformed setting fails safe to the default behaviour."""

    def test_absent_setting_is_disabled(self):
        p = policy(deadline=None)
        self.assertIsNone(xs.codex_usage_independent_until(p))
        with Clock(BEFORE):
            self.assertFalse(xs.codex_usage_independent(p))

    def test_shipped_default_policy_is_off(self):
        # The shipped policy must not enable the window (no production
        # policy is overwritten by this feature).
        self.assertIsNone(POLICY["codex"]["usage_independent_until"])
        with Clock(BEFORE):
            self.assertFalse(xs.codex_usage_independent(POLICY))

    def test_malformed_settings_fail_safe(self):
        malformed = ["", "   ", "not-a-date", "2030-06-01", "2030-06-01 00:00:00",
                     "2030-06-01T00:00:00", "2030-06-01T00:00:00+02:00",
                     "2030-06-01T00:00:00.500+02:00", "2030-13-01T00:00:00Z",
                     "2030-02-30T00:00:00Z", "2030-06-01T25:00:00Z",
                     "2030-06-01T00:00:00.1234567Z", "2030-06-01T00:00:00.123456789Z",
                     2030, True, [], {}]
        for value in malformed:
            with self.subTest(value=value):
                p = policy()
                p["codex"]["usage_independent_until"] = value
                self.assertIsNone(xs.codex_usage_independent_until(p))
                with Clock(BEFORE):
                    self.assertFalse(xs.codex_usage_independent(p))

    def test_non_dict_codex_section_fails_safe(self):
        for value in (None, [], "invalid", 1):
            with self.subTest(value=value), Clock(BEFORE):
                self.assertFalse(xs.codex_usage_independent({"codex": value}))

    def test_valid_utc_deadline_is_parsed(self):
        p = policy()
        self.assertEqual(xs.codex_usage_independent_until(p), DEADLINE_EPOCH)
        self.assertEqual(xs.codex_usage_independent_until(policy(deadline="2030-06-01T00:00:00.500Z")),
                         DEADLINE_EPOCH + 0.5)

    def test_expiry_boundary_is_strict(self):
        p = policy()
        with Clock(BEFORE):
            self.assertTrue(xs.codex_usage_independent(p))
        with Clock(AT):
            self.assertFalse(xs.codex_usage_independent(p))
        with Clock(AFTER):
            self.assertFalse(xs.codex_usage_independent(p))

    def test_fractional_expiry_boundary_is_strict(self):
        p = policy(deadline="2030-06-01T00:00:00.123456Z")
        base = dt.datetime(2030, 6, 1, tzinfo=dt.timezone.utc)
        for micros, active in ((123455, True), (123456, False), (123457, False)):
            with self.subTest(micros=micros), Clock((base + dt.timedelta(microseconds=micros)).timestamp()):
                self.assertEqual(xs.codex_usage_independent(p), active)


class Recommendation(unittest.TestCase):
    """The recommended Codex concurrency comes from the configured cap."""

    def test_high_usage_recommendation_uses_the_configured_cap(self):
        st = state(codex_used=1.0)
        with Clock(BEFORE):
            default = xs.concurrency(st, policy(deadline=None))
            active = xs.concurrency(st, policy())
        self.assertEqual(default["codex"], 1)  # usage 100% -> lowest row
        self.assertEqual(active["codex"], policy()["concurrency"]["codex"])

    def test_unknown_usage_recommendation_uses_the_configured_cap(self):
        st = state(codex_unknown=True)
        with Clock(BEFORE):
            default = xs.concurrency(st, policy(deadline=None))
            active = xs.concurrency(st, policy())
        self.assertNotEqual(default["codex"], policy()["concurrency"]["codex"])
        self.assertEqual(active["codex"], policy()["concurrency"]["codex"])

    def test_default_behaviour_returns_at_and_after_the_deadline(self):
        st = state(codex_used=1.0)
        for now in (AT, AFTER):
            with Clock(now):
                self.assertEqual(xs.concurrency(st, policy())["codex"], 1)

    def test_other_provider_recommendation_is_unchanged(self):
        st = state(codex_used=1.0, ocgo_used=0.9)
        with Clock(BEFORE):
            default = xs.concurrency(st, policy(deadline=None))
            active = xs.concurrency(st, policy())
        self.assertEqual(active["ocgo"], default["ocgo"])

    def test_missing_cap_uses_usage_independent_fallback(self):
        p = policy()
        p["concurrency"] = dict(p["concurrency"])
        p["concurrency"]["codex"] = 0  # no configured cap
        with Clock(BEFORE):
            for used in (0.0, 1.0):
                self.assertEqual(xs.concurrency(state(codex_used=used), p)["codex"], 1)

    def test_route_decision_carries_the_recommendation(self):
        st = state(codex_used=1.0)
        with Clock(BEFORE):
            decision = xs.route(profile(difficulty=3), st, policy())
        self.assertTrue(decision["ok"], decision)
        self.assertEqual(decision["concurrency"]["codex"], policy()["concurrency"]["codex"])


class PlannerBehaviour(unittest.TestCase):
    """The usage window neither excludes Codex nor degrades its effort."""

    def test_exhausted_window_excludes_codex_by_default(self):
        st = state(codex_used=1.0)
        with Clock(BEFORE):
            decision = xs.route(profile(difficulty=5), st, policy(deadline=None))
        self.assertNotEqual(decision.get("backend"), "codex")
        self.assertIn("codex", decision.get("skipped", {}))

    def test_exhausted_window_keeps_codex_while_the_window_is_active(self):
        st = state(codex_used=1.0)
        with Clock(BEFORE):
            decision = xs.route(profile(difficulty=5), st, policy())
        self.assertEqual(decision.get("backend"), "codex")
        self.assertTrue(any("usage-independent window" in note for note in decision["reasons"]))

    def test_near_soft_limit_effort_is_not_degraded_while_active(self):
        st = state(codex_used=0.9)
        with Clock(BEFORE):
            default = xs.route(profile(difficulty=5), st, policy(deadline=None))
            active = xs.route(profile(difficulty=5), st, policy())
        self.assertEqual(default["backend"], "codex")
        self.assertEqual(default["effort"], "medium")  # effort -1 near the soft limit
        self.assertEqual(active["effort"], "high")  # untouched while active

    def test_default_behaviour_returns_at_the_deadline(self):
        st = state(codex_used=0.9)
        with Clock(AT):
            decision = xs.route(profile(difficulty=5), st, policy())
        self.assertEqual(decision["effort"], "medium")

    def test_usage_cannot_upgrade_effort_while_active(self):
        st = state(codex_used=0.0)
        st["codex"]["limits"]["codex"]["windows"]["five_hour"] = window(0.0, hours_left=0.1)
        with Clock(BEFORE):
            default = xs.route(profile(difficulty=3), st, policy(deadline=None))
            active = xs.route(profile(difficulty=3), st, policy())
        self.assertEqual(default["backend"], "codex")
        self.assertEqual(active["backend"], "codex")
        self.assertEqual(active["effort"], policy()["difficulty_map"]["3"]["effort"])
        self.assertNotEqual(default["effort"], active["effort"])

    def test_breaker_still_excludes_codex_while_active(self):
        saved = xs.breakers
        xs.breakers = lambda: {"codex/limit/codex": DEADLINE_EPOCH + 86400}
        try:
            st = state(codex_used=0.1)
            with Clock(BEFORE):
                decision = xs.route(profile(difficulty=5), st, policy())
        finally:
            xs.breakers = saved
        self.assertNotEqual(decision.get("backend"), "codex")
        self.assertIn("codex", decision.get("skipped", {}))

    def test_missing_codex_binary_still_excludes_codex_while_active(self):
        st = state(codex_used=0.1)
        st["codex"]["binary"] = None
        with Clock(BEFORE):
            decision = xs.route(profile(difficulty=5), st, policy())
        self.assertNotEqual(decision.get("backend"), "codex")

    def test_other_providers_are_unchanged(self):
        # DeepSeek below its balance floor is still skipped while the
        # Codex window is active.
        st = state(codex_used=0.1, deepseek_balance=1.0)
        p = policy()
        p["routing"]["default_order"] = ["codex", "ocgo", "deepseek"]
        with Clock(BEFORE):
            decision = xs.route(profile(difficulty=5), st, p)
        self.assertNotIn("deepseek", [decision.get("backend")])

    def test_ocgo_usage_limits_still_apply_while_active(self):
        # Every OpenCode Go model is over its window budget: it stays
        # unavailable, exactly as without the window.
        st = state(codex_used=0.1, ocgo_used=0.99)
        st["codex"]["binary"] = None
        with Clock(BEFORE):
            decision = xs.route(profile(difficulty=5), st, policy())
        self.assertFalse(decision.get("ok"))
        self.assertIn("ocgo", decision.get("skipped", {}))


class QuotaFilter(unittest.TestCase):
    """No Codex quota source excludes Codex while active."""

    def quota_policy(self, deadline=DEADLINE, sources=None, threshold=0):
        p = policy(deadline=deadline)
        p["routing"]["quota"] = {"enabled": True, "threshold": threshold, "sources": sources or {}}
        return p

    def decide(self, p, st, prof=None):
        prof = prof or profile(difficulty=5)
        decision = xs.route(prof, copy.deepcopy(st), p)
        self.assertTrue(decision["ok"], decision)
        return xs.apply_quota_routing(prof, copy.deepcopy(st), p, decision)

    def test_exhausted_quota_excludes_codex_by_default(self):
        st = split_limit_state()
        with Clock(BEFORE):
            decision, info = self.decide(self.quota_policy(deadline=None), st)
        self.assertNotEqual(decision["backend"], "codex")
        self.assertTrue(info["applied"])
        self.assertEqual(info["quota_before"]["codex"], 0)

    def test_exhausted_quota_keeps_codex_while_active(self):
        st = split_limit_state()
        with Clock(BEFORE):
            decision, info = self.decide(self.quota_policy(), st)
        self.assertEqual(decision["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertIsNone(info["quota_before"]["codex"])  # unknown, never zero
        self.assertIn("usage-independent window", info["reason"])

    def test_quota_filter_returns_at_the_deadline(self):
        st = split_limit_state()
        with Clock(AT):
            decision, info = self.decide(self.quota_policy(), st)
        self.assertNotEqual(decision["backend"], "codex")
        self.assertTrue(info["applied"])

    def test_explicit_quota_source_is_bypassed_until_deadline(self):
        with tempfile.TemporaryDirectory(prefix="openswarm-codex-usage-independent-") as tmp:
            path = os.path.join(tmp, "quota.json")
            with open(path, "w") as fh:
                json.dump({"remaining": 0}, fh)
            p = self.quota_policy(sources={"codex": {"source": "file", "file": path}})
            st = state(codex_used=0.1)
            with Clock(BEFORE), mock.patch.object(xs, "quota_remaining_from_source", side_effect=AssertionError("source invoked")):
                decision, info = self.decide(p, st)
                self.assertEqual(decision["backend"], "codex")
                self.assertIsNone(info["quota_before"]["codex"])
            with Clock(AT):
                decision, info = self.decide(p, st)
                self.assertNotEqual(decision["backend"], "codex")
                self.assertTrue(info["applied"])

    def test_failed_codex_source_is_not_invoked_while_active(self):
        p = self.quota_policy()
        st = state(codex_used=0.1)
        with mock.patch.object(xs, "quota_remaining_for_backend", return_value=(None, "synthetic failure")) as reader:
            with Clock(BEFORE):
                decision, info = self.decide(p, st)
            reader.assert_not_called()
            self.assertEqual(decision["backend"], "codex")
            self.assertNotIn("quota_error", info)
            with Clock(AT):
                decision, info = self.decide(p, st)
            reader.assert_called_once()
            self.assertEqual(info["quota_error"]["codex"], "synthetic failure")

    def test_disabled_quota_routing_is_unchanged(self):
        st = state(codex_used=1.0)
        p = policy()
        with Clock(BEFORE):
            prof = profile(difficulty=5)
            decision = xs.route(prof, copy.deepcopy(st), p)
            decision, info = xs.apply_quota_routing(prof, copy.deepcopy(st), p, decision)
        self.assertIsNone(info)
        self.assertEqual(decision["backend"], "codex")


class HealthGate(unittest.TestCase):
    def test_only_codex_financial_check_is_waived(self):
        p = policy()
        p["routing"]["health_gate"] = {"enabled": True, "on_unknown": "block"}
        p["routing"]["quota"]["sources"] = {
            "codex": {"source": "env", "var": "SYNTHETIC_CODEX_QUOTA"},
            "ocgo": {"source": "env", "var": "SYNTHETIC_OCGO_QUOTA"}}
        args = argparse.Namespace(health_gate=True)
        st = state()
        good_binary = xs._health_check("binary", "pass", "synthetic binary", "mock")
        good_login = xs._health_check("login", "pass", "synthetic login", "mock")
        with mock.patch.object(xs, "_health_check_binary", return_value=good_binary), \
             mock.patch.object(xs, "_health_codex_login", return_value=good_login) as login, \
             mock.patch.object(xs, "quota_remaining_from_source") as reader:
            for result, status, blocked in (((None, "401 unauthorized"), "fail", True),
                                             ((None, None), "unknown", True),
                                             ((0, None), "pass", False)):
                with self.subTest(result=result):
                    reader.reset_mock()
                    reader.return_value = result
                    with Clock(BEFORE):
                        record, denied = xs.apply_health_gate("codex", st, p, args)
                    self.assertFalse(denied)
                    self.assertEqual(next(c for c in record["checks"] if c["name"] == "quota")["status"],
                                     "not_applicable")
                    reader.assert_not_called()
                    with Clock(AT):
                        record, denied = xs.apply_health_gate("codex", st, p, args)
                    self.assertEqual(next(c for c in record["checks"] if c["name"] == "quota")["status"], status)
                    self.assertEqual(denied, blocked)
                    reader.assert_called_once()
            login.return_value = xs._health_check("login", "fail", "synthetic login failure", "mock")
            reader.reset_mock()
            with Clock(BEFORE):
                record, denied = xs.apply_health_gate("codex", st, p, args)
            self.assertTrue(denied)
            self.assertEqual(record["verdict"], "unhealthy")
            reader.assert_not_called()
            reader.return_value = (None, "401 unauthorized")
            with Clock(BEFORE):
                record, denied = xs.apply_health_gate("ocgo", st, p, args)
            self.assertTrue(denied)
            self.assertEqual(record["verdict"], "unhealthy")
            reader.assert_called_once()


class FreshProbe(unittest.TestCase):
    def test_financial_collection_skipped_and_cache_keeps_original_time(self):
        p = policy()
        old = state()
        old["generated_at"] = BEFORE - 10000
        old["codex"]["limits_source"] = "local"
        models = [{"id": "synthetic-model"}]
        with mock.patch.object(xs, "jload", return_value=old), \
             mock.patch.object(xs, "envvar", return_value=None), \
             mock.patch.object(xs, "jsave"), \
             mock.patch.object(xs, "find_codex", return_value=("synthetic", "1")), \
             mock.patch.object(xs, "codex_models", return_value=(models, "mock")) as discover, \
             mock.patch.object(xs, "codex_plan", return_value="mock"), \
             mock.patch.object(xs, "codex_config_model", return_value=None), \
             mock.patch.object(xs, "_codex_limits", side_effect=OSError("synthetic stat")) as limits, \
             mock.patch.object(xs, "codex_snapshot_status", side_effect=AssertionError("snapshot read")) as snapshot, \
             mock.patch.object(xs, "probe_claude", return_value={}), \
             mock.patch.object(xs, "probe_ocgo", return_value={}) as ocgo, \
             mock.patch.object(xs, "probe_deepseek", return_value={}), \
             mock.patch.object(xs, "probe_cursor", return_value={}), \
             mock.patch.object(xs, "probe_agy", return_value={}), \
             mock.patch.object(xs, "probe_pi", return_value={}):
            with Clock(BEFORE):
                fresh = xs.get_state(p, refresh=True)
            self.assertEqual(fresh["codex"]["models"], models)
            self.assertEqual(fresh["codex"]["limits"]["codex"]["windows"],
                             old["codex"]["limits"]["codex"]["windows"])
            self.assertIsNone(fresh["codex"]["limits"]["codex"]["age_minutes"])
            self.assertEqual(fresh["codex"]["limits_source"], "cached")
            self.assertEqual(fresh["codex"]["limits_cached_at"], old["generated_at"])
            xs.jload.return_value = fresh
            with Clock(BEFORE + 10):
                again = xs.get_state(p, refresh=True)
            self.assertEqual(again["codex"]["limits_cached_at"], old["generated_at"])
            limits.assert_not_called()
            self.assertEqual(discover.call_count, 2)
            self.assertEqual(ocgo.call_count, 2)
            fresh["generated_at"] = AT - 1  # still inside the normal state TTL
            with Clock(AT), self.assertRaisesRegex(OSError, "synthetic stat"):
                xs.get_state(p)
            limits.assert_called_once_with(p)
            limits.side_effect = None
            limits.return_value = ({"fresh": {}}, "local")
            snapshot.side_effect = None
            snapshot.return_value = None
            with Clock(AT):
                restored = xs.get_state(p)
            self.assertEqual(restored["codex"]["limits_source"], "local")
            self.assertEqual(restored["codex"]["limits"], {"fresh": {}})

    def test_no_cache_does_not_invent_observation(self):
        with Clock(BEFORE), mock.patch.object(xs, "jload", return_value={}), \
             mock.patch.object(xs, "find_codex", return_value=(None, None)), \
             mock.patch.object(xs, "codex_models", return_value=([], "mock")), \
             mock.patch.object(xs, "codex_plan", return_value=None), \
             mock.patch.object(xs, "codex_config_model", return_value=None), \
             mock.patch.object(xs, "_codex_limits", side_effect=AssertionError("limits read")), \
             mock.patch.object(xs, "codex_snapshot_status", side_effect=AssertionError("snapshot read")):
            result = xs.probe_codex(policy())
        self.assertEqual(result["limits"], {})
        self.assertEqual(result["limits_source"], "not_collected")
        self.assertNotIn("limits_cached_at", result)
        self.assertNotIn("limits_snapshot", result)


class Isolation(unittest.TestCase):
    """The feature reads no account data and writes nothing."""

    def test_no_usage_probe_and_no_writes(self):
        with tempfile.TemporaryDirectory(prefix="openswarm-codex-usage-independent-home-") as home:
            saved_home = os.environ.get("HOME")
            saved_data = xs.DATA
            os.environ["HOME"] = home
            xs.DATA = home
            try:
                st = state(codex_used=1.0)
                with Clock(BEFORE):
                    xs.concurrency(st, policy())
                    decision = xs.route(profile(difficulty=5), st, policy())
                    self.assertTrue(decision["ok"], decision)
                self.assertEqual(os.listdir(home), [])  # no state, no ledger, no codex reads
            finally:
                xs.DATA = saved_data
                if saved_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = saved_home

    def test_no_codex_cli_is_invoked(self):
        # The window is a policy decision, not a probe: nothing shells
        # out to a provider.
        st = state(codex_used=1.0)
        with Clock(BEFORE):
            xs.concurrency(st, policy())
            xs.route(profile(difficulty=5), st, policy())


if __name__ == "__main__":
    unittest.main()
