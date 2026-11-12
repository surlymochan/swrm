"""M20 slice 1 (#181): real-backend quota probe wiring + decision recording.

Pi (MiniMax) reads its live token-plan remains (or PAYG balance for
`sk-api-` keys) in probe_pi(); codex / deepseek / ocgo read their live
probe state through built-in readers, so quota-aware routing can judge
real backends with no policy source configured. The decision records
quota_before / reason / both remainings in the ledger per run; a failed
probe read keeps the old behaviour on the record; explicit --backend
always wins; all backends exhausted refuses to dispatch.
Run: python3 -m unittest discover -s tests   (or: python3 tests/test_m20_slice1.py)
"""
import argparse, contextlib, copy, io, json, os, sys, tempfile, unittest, urllib.error, urllib.request
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

REMAINS_PAYLOAD = {
    "model_remains": [{
        "model_name": "general", "start_time": 1794450510025, "end_time": 1794468510025,
        "remains_time": 804890, "current_interval_total_count": 0,
        "current_interval_usage_count": 0, "current_weekly_total_count": 0,
        "current_weekly_usage_count": 0, "weekly_start_time": 1794335598384,
        "weekly_end_time": 1794940398384, "weekly_remains_time": 472693249,
        "current_interval_status": 1, "current_interval_remaining_percent": 93,
        "current_weekly_status": 1, "current_weekly_remaining_percent": 97,
    }],
    "base_resp": {"status_code": 0, "status_msg": "success"},
}

BALANCE_PAYLOAD = {
    "available_amount": "12.34", "cash_balance": "10.00", "voucher_balance": "2.34",
    "credit_balance": "0", "owed_amount": "0", "balance_alert_switch": False,
    "base_resp": {"status_code": 0, "status_msg": "success"},
}

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


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class MinimaxPayloadParsing(unittest.TestCase):
    def test_remains_payload_is_a_fraction(self):
        remaining, unit, detail = xs.minimax_quota_from_payload(copy.deepcopy(REMAINS_PAYLOAD))
        self.assertEqual(remaining, 0.93)
        self.assertEqual(unit, "fraction")
        self.assertEqual(detail["endpoint"], "token_plan/remains")
        self.assertEqual(detail["models"][0]["model"], "general")
        self.assertEqual(detail["models"][0]["weekly_remaining_percent"], 97)

    def test_most_binding_model_wins(self):
        payload = copy.deepcopy(REMAINS_PAYLOAD)
        payload["model_remains"].append(dict(payload["model_remains"][0],
                                            model_name="other",
                                            current_interval_remaining_percent=40))
        remaining, _, _ = xs.minimax_quota_from_payload(payload)
        self.assertEqual(remaining, 0.4)

    def test_exhausted_status_forces_zero(self):
        payload = copy.deepcopy(REMAINS_PAYLOAD)
        payload["model_remains"][0]["current_interval_status"] = 2
        remaining, _, _ = xs.minimax_quota_from_payload(payload)
        self.assertEqual(remaining, 0.0)

    def test_unlimited_status_without_percent_reads_full(self):
        payload = copy.deepcopy(REMAINS_PAYLOAD)
        entry = payload["model_remains"][0]
        entry["current_interval_status"] = 3
        del entry["current_interval_remaining_percent"]
        remaining, _, _ = xs.minimax_quota_from_payload(payload)
        self.assertEqual(remaining, 1.0)

    def test_no_percent_and_no_status_is_an_error_not_a_guess(self):
        payload = copy.deepcopy(REMAINS_PAYLOAD)
        entry = payload["model_remains"][0]
        del entry["current_interval_remaining_percent"]
        del entry["current_interval_status"]
        with self.assertRaises(ValueError):
            xs.minimax_quota_from_payload(payload)

    def test_empty_remains_is_an_error(self):
        with self.assertRaises(ValueError):
            xs.minimax_quota_from_payload({"model_remains": [], "base_resp": {"status_code": 0}})

    def test_nonzero_base_resp_is_an_error(self):
        payload = copy.deepcopy(REMAINS_PAYLOAD)
        payload["base_resp"] = {"status_code": 1001, "status_msg": "invalid key"}
        with self.assertRaises(ValueError) as ctx:
            xs.minimax_quota_from_payload(payload)
        self.assertIn("1001", str(ctx.exception))

    def test_balance_payload_is_currency(self):
        remaining, unit, detail = xs.minimax_quota_from_payload(copy.deepcopy(BALANCE_PAYLOAD))
        self.assertEqual(remaining, 12.34)
        self.assertEqual(unit, "currency")
        self.assertEqual(detail["endpoint"], "account/query_balance")

    def test_balance_non_numeric_is_an_error(self):
        payload = copy.deepcopy(BALANCE_PAYLOAD)
        payload["available_amount"] = "n/a"
        with self.assertRaises(ValueError):
            xs.minimax_quota_from_payload(payload)

    def test_unknown_payload_is_an_error(self):
        with self.assertRaises(ValueError):
            xs.minimax_quota_from_payload({"something_else": True})


class MinimaxQuotaHttp(unittest.TestCase):
    def run_quota(self, key, payload=None, error=None):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            if error is not None:
                raise error
            return FakeResponse(payload)

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            result = xs.minimax_quota(key=key)
        return result, seen

    def test_token_plan_key_hits_remains_endpoint(self):
        result, seen = self.run_quota("tok-plan-key", copy.deepcopy(REMAINS_PAYLOAD))
        self.assertEqual(seen["url"], "https://www.minimaxi.com/v1/token_plan/remains")
        self.assertEqual(seen["auth"], "Bearer tok-plan-key")
        self.assertEqual(result["remaining"], 0.93)
        self.assertEqual(result["unit"], "fraction")

    def test_payg_key_hits_balance_endpoint(self):
        result, seen = self.run_quota("sk-api-fake", copy.deepcopy(BALANCE_PAYLOAD))
        self.assertEqual(seen["url"], "https://www.minimaxi.com/account/query_balance")
        self.assertEqual(result["remaining"], 12.34)
        self.assertEqual(result["unit"], "currency")

    def test_http_error_is_recorded_not_raised(self):
        result, _ = self.run_quota(
            "tok-plan-key", error=urllib.error.HTTPError(
                "https://www.minimaxi.com/v1/token_plan/remains", 401, "Unauthorized", {}, None))
        self.assertIn("error", result)
        self.assertIn("401", result["error"])

    def test_network_failure_is_recorded_not_raised(self):
        result, _ = self.run_quota("tok-plan-key", error=urllib.error.URLError("boom"))
        self.assertIn("error", result)
        self.assertIn("URLError", result["error"])

    def test_no_key_is_an_error(self):
        with mock.patch.object(xs, "minimax_key_from_pi", return_value=None):
            result = xs.minimax_quota()
        self.assertIn("error", result)
        self.assertIn("no MiniMax credential", result["error"])

    def test_bad_payload_is_an_error(self):
        result, _ = self.run_quota("tok-plan-key", {"unexpected": True})
        self.assertIn("error", result)


class ProbePi(unittest.TestCase):
    def test_probe_pi_carries_live_quota(self):
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=FakeResponse(copy.deepcopy(REMAINS_PAYLOAD))):
            with mock.patch.object(xs, "minimax_key_from_pi", return_value="tok-plan-key"):
                probed = xs.probe_pi(POLICY)
        self.assertEqual(probed["quota"]["remaining"], 0.93)
        self.assertEqual(probed["provider"], "minimax-cn")

    def test_probe_pi_failure_is_recorded_not_raised(self):
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("boom")):
            with mock.patch.object(xs, "minimax_key_from_pi", return_value="tok-plan-key"):
                probed = xs.probe_pi(POLICY)
        self.assertIn("error", probed["quota"])
        self.assertIn("URLError", probed["quota"]["error"])

    def test_plan_pi_note_reports_real_remaining(self):
        st = state_with_pi({"remaining": 0.93, "unit": "fraction",
                            "source": "minimax token-plan remains"})
        notes = []
        decision, why = xs.plan_pi({"type": "impl", "difficulty": 3}, st, POLICY, False, notes)
        self.assertIsNotNone(decision)
        self.assertTrue(any("93%" in note for note in notes), notes)

    def test_plan_pi_note_reports_unreadable_quota(self):
        st = state_with_pi({"error": "MiniMax quota request failed: HTTP 401"})
        notes = []
        decision, why = xs.plan_pi({"type": "impl", "difficulty": 3}, st, POLICY, False, notes)
        self.assertIsNotNone(decision)
        self.assertTrue(any("unreadable" in note for note in notes), notes)


class BuiltinQuotaReaders(unittest.TestCase):
    def test_codex_remaining_is_one_minus_worst_window(self):
        # Fixture: five_hour used 0.12, seven_day used 0.31 → 1 - 0.31 = 0.69.
        remaining, error, text = xs.builtin_quota_remaining("codex", copy.deepcopy(BASE))
        self.assertEqual(remaining, 0.69)
        self.assertIsNone(error)
        self.assertIn("fraction", text)

    def test_codex_no_limits_is_unknown_not_zero(self):
        st = copy.deepcopy(BASE)
        st["codex"]["limits"] = {}
        remaining, error, text = xs.builtin_quota_remaining("codex", st)
        self.assertIsNone(remaining)
        self.assertIsNone(error)
        self.assertIn("codex", text)  # reader exists, this machine has no data

    def test_deepseek_balance_is_currency(self):
        remaining, error, text = xs.builtin_quota_remaining("deepseek", copy.deepcopy(BASE))
        self.assertEqual(remaining, 55.0)
        self.assertIsNone(error)
        self.assertIn("CNY", text)

    def test_deepseek_error_passes_through(self):
        st = copy.deepcopy(BASE)
        st["deepseek"] = {"balance": None, "error": "no api key"}
        remaining, error, _ = xs.builtin_quota_remaining("deepseek", st)
        self.assertIsNone(remaining)
        self.assertEqual(error, "no api key")

    def test_deepseek_no_balance_no_error_is_unknown(self):
        st = copy.deepcopy(BASE)
        st["deepseek"] = {"balance": None}
        remaining, error, text = xs.builtin_quota_remaining("deepseek", st)
        self.assertIsNone(remaining)
        self.assertIsNone(error)
        self.assertIn("deepseek", text)

    def test_ocgo_remaining_is_one_minus_worst_usage(self):
        st = copy.deepcopy(BASE)
        st["ocgo"]["models"][0]["usage"] = {"5h": 0.2, "7d": 0.5, "30d": 0.1}
        remaining, error, text = xs.builtin_quota_remaining("ocgo", st)
        self.assertEqual(remaining, 0.5)
        self.assertIsNone(error)
        self.assertIn("fraction", text)

    def test_ocgo_excluded_models_do_not_count(self):
        st = copy.deepcopy(BASE)
        st["ocgo"]["models"][0]["usage"] = {"5h": 1.0, "7d": 1.0, "30d": 1.0}
        st["ocgo"]["models"][0]["excluded"] = True
        remaining, _, _ = xs.builtin_quota_remaining("ocgo", st)
        self.assertEqual(remaining, 1.0)

    def test_ocgo_no_models_is_unknown(self):
        st = copy.deepcopy(BASE)
        st["ocgo"]["models"] = []
        remaining, error, text = xs.builtin_quota_remaining("ocgo", st)
        self.assertIsNone(remaining)
        self.assertIsNone(error)
        self.assertIn("ocgo", text)

    def test_pi_reading_from_state(self):
        st = state_with_pi({"remaining": 0.93, "unit": "fraction",
                            "source": "minimax token-plan remains"})
        remaining, error, text = xs.builtin_quota_remaining("pi", st)
        self.assertEqual(remaining, 0.93)
        self.assertIsNone(error)
        self.assertIn("MiniMax", text)

    def test_pi_error_passes_through(self):
        st = state_with_pi({"error": "MiniMax quota request failed: HTTP 401"})
        remaining, error, _ = xs.builtin_quota_remaining("pi", st)
        self.assertIsNone(remaining)
        self.assertIn("401", error)

    def test_pi_absent_from_old_state_is_unknown(self):
        remaining, error, text = xs.builtin_quota_remaining("pi", copy.deepcopy(BASE))
        self.assertIsNone(remaining)
        self.assertIsNone(error)
        self.assertIn("pi", text)

    def test_backends_without_a_reader_are_unknown(self):
        for backend in ("cursor", "agy", "copilot", "hermes", "claude"):
            self.assertEqual(xs.builtin_quota_remaining(backend, copy.deepcopy(BASE)),
                             (None, None, None))

    def test_policy_source_wins_over_builtin(self):
        os.environ["OPENSWARM_M20_TEST_PI"] = "0"
        try:
            cfg = xs.quota_config(quota_policy(["codex", "pi"], sources={
                "pi": {"source": "env", "var": "OPENSWARM_M20_TEST_PI"}}))
            remaining, error = xs.quota_remaining_for_backend(
                "pi", cfg, (), state_with_pi({"remaining": 0.93}))
            self.assertEqual(remaining, 0)
            self.assertIsNone(error)
        finally:
            os.environ.pop("OPENSWARM_M20_TEST_PI", None)

    def test_builtin_used_when_no_policy_source(self):
        cfg = xs.quota_config(quota_policy(["codex", "pi"]))
        remaining, error = xs.quota_remaining_for_backend(
            "pi", cfg, (), state_with_pi({"remaining": 0.93}))
        self.assertEqual(remaining, 0.93)
        self.assertIsNone(error)


class BuiltinQuotaRouting(unittest.TestCase):
    def decide(self, policy, st, prof=None):
        prof = prof or {"type": "impl", "difficulty": 3}
        decision = xs.route(prof, st, policy)
        self.assertTrue(decision["ok"], decision)
        return xs.apply_quota_routing(prof, st, policy, decision)

    def test_stay_records_real_codex_remaining(self):
        st = state_with_pi({"remaining": 0.93, "unit": "fraction"})
        decision, info = self.decide(quota_policy(["codex", "pi"]), st)
        self.assertEqual(decision["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertEqual(info["quota_before"], {"codex": 0.69})
        self.assertIn("0.69", info["reason"])

    def test_reroute_records_both_real_remainings(self):
        # codex used 0.75 → remaining 0.25, below the 0.3 threshold but still
        # routable by plan_codex (its own hard gate is at 95% used).
        st = state_with_pi({"remaining": 0.93, "unit": "fraction"}, codex_used=0.75)
        decision, info = self.decide(quota_policy(["codex", "pi"], threshold=0.3), st)
        self.assertEqual(decision["backend"], "pi")
        self.assertEqual(decision.get("model"), "MiniMax-M3.1-Flash")
        self.assertTrue(info["applied"])
        self.assertEqual(info["from"], "codex")
        self.assertEqual(info["to"], "pi")
        self.assertEqual(info["quota_before"], {"codex": 0.25, "pi": 0.93})

    def test_fraction_threshold_reroutes_below_it(self):
        # threshold 0.7 in fraction units: codex 0.69 is below, pi 0.93 above.
        st = state_with_pi({"remaining": 0.93, "unit": "fraction"})
        decision, info = self.decide(quota_policy(["codex", "pi"], threshold=0.7), st)
        self.assertEqual(decision["backend"], "pi")
        self.assertTrue(info["applied"])
        self.assertEqual(info["quota_before"], {"codex": 0.69, "pi": 0.93})

    def test_explicit_backend_wins_over_real_quota(self):
        st = state_with_pi({"remaining": 0.93, "unit": "fraction"}, codex_used=0.75)
        decision, info = self.decide(
            quota_policy(["codex", "pi"], threshold=0.3), st,
            prof={"type": "impl", "difficulty": 3, "backend": "codex"})
        self.assertEqual(decision["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertIn("explicit --backend wins", info["reason"])

    def test_all_real_backends_exhausted_refuses(self):
        st = state_with_pi({"remaining": 0.0, "unit": "fraction"}, codex_used=0.75)
        decision, info = self.decide(quota_policy(["codex", "pi"], threshold=0.3), st)
        self.assertEqual(decision["backend"], "codex")
        self.assertTrue(info["all_exhausted"])
        self.assertEqual(info["quota_before"], {"codex": 0.25, "pi": 0.0})
        self.assertIn("refusing to dispatch", info["reason"])

    def test_default_read_failure_keeps_old_behaviour(self):
        st = state_with_pi({"error": "MiniMax quota request failed: HTTP 401"})
        decision, info = self.decide(quota_policy(["pi", "codex"]), st)
        self.assertEqual(decision["backend"], "pi")
        self.assertFalse(info["applied"])
        self.assertIn("pi", info["quota_error"])
        self.assertIn("401", info["quota_error"]["pi"])
        self.assertIn("kept the default pick", info["reason"])

    def test_reroute_target_read_failure_keeps_default_on_record(self):
        st = state_with_pi({"error": "MiniMax quota request failed: HTTP 401"}, codex_used=0.75)
        decision, info = self.decide(quota_policy(["codex", "pi"], threshold=0.3), st)
        self.assertEqual(decision["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertEqual(info["quota_before"], {"codex": 0.25})
        self.assertIn("pi", info["quota_error"])
        self.assertIn("no fallback with readable quota", info["reason"])

    def test_disabled_quota_is_a_no_op(self):
        st = state_with_pi({"remaining": 0.93, "unit": "fraction"}, codex_used=0.75)
        decision, info = self.decide(quota_policy(["codex", "pi"], threshold=0.3, enabled=False), st)
        self.assertEqual(decision["backend"], "codex")
        self.assertIsNone(info)


class BuiltinQuotaProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-m20-probe-")
        self._saved_state_file = os.environ.get("OPENSWARM_STATE_FILE")
        self.state_path = os.path.join(self.tmp, "state.json")
        with open(self.state_path, "w") as fh:
            json.dump(state_with_pi({"remaining": 0.93, "unit": "fraction",
                                     "source": "minimax token-plan remains"}), fh)
        os.environ["OPENSWARM_STATE_FILE"] = self.state_path

    def tearDown(self):
        if self._saved_state_file is None:
            os.environ.pop("OPENSWARM_STATE_FILE", None)
        else:
            os.environ["OPENSWARM_STATE_FILE"] = self._saved_state_file
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def probe(self, policy):
        args = argparse.Namespace(backends_file=None, json=True)
        buf = io.StringIO()
        code = None
        with contextlib.redirect_stdout(buf):
            try:
                xs.cmd_quota(args, policy, custom_backends=[])
            except SystemExit as e:
                code = e.code
        return code, json.loads(buf.getvalue())

    def test_probe_rows_carry_live_builtin_readings(self):
        code, data = self.probe(quota_policy(["codex", "pi"]))
        self.assertIsNone(code)
        rows = {row["backend"]: row for row in data["backends"]}
        self.assertEqual(rows["pi"]["remaining"], 0.93)
        self.assertEqual(rows["pi"]["status"], "ok")
        self.assertIn("MiniMax", rows["pi"]["source"])
        self.assertEqual(rows["codex"]["remaining"], 0.69)
        self.assertEqual(rows["codex"]["status"], "ok")
        self.assertEqual(rows["deepseek"]["remaining"], 55.0)
        self.assertEqual(rows["deepseek"]["status"], "ok")
        self.assertEqual(rows["ocgo"]["remaining"], 1.0)
        self.assertEqual(rows["ocgo"]["status"], "ok")
        self.assertEqual(rows["cursor"]["status"], "unknown")

    def test_builtin_read_failure_is_a_row_not_a_command_failure(self):
        with open(self.state_path, "w") as fh:
            json.dump(state_with_pi({"error": "MiniMax quota request failed: HTTP 401"}), fh)
        code, data = self.probe(quota_policy(["codex", "pi"]))
        self.assertIsNone(code)
        rows = {row["backend"]: row for row in data["backends"]}
        self.assertEqual(rows["pi"]["status"], "error")
        self.assertIn("401", rows["pi"]["detail"])

    def test_configured_source_failure_still_exits_1(self):
        policy = quota_policy(["codex", "pi"], sources={
            "codex": {"source": "command", "command": ["false"]}})
        args = argparse.Namespace(backends_file=None, json=False)
        buf = io.StringIO()
        code = None
        with contextlib.redirect_stdout(buf):
            try:
                xs.cmd_quota(args, policy, custom_backends=[])
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, 1)
        self.assertIn("error", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
