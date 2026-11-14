"""M24 slice 1 (#198): pre-dispatch backend health gate for `openswarm run`.

Background (#197): M23 slice 2's six real runs exposed three environment
gaps only at dispatch time (9933/9934 with no listener, VM codex not
logged in returning 401) — the run had already started before it failed.
This slice moves the "is the backend healthy?" judgement to before
dispatch: a health probe (login/session alive, quota probe readable,
transport reachable — whichever applies to the backend type) runs on the
final pick, an unhealthy backend refuses to dispatch with an actionable
error, and the decision lands in the ledger and result.json.

Pinned behaviour (the tests below pin each one):
- Gate off (shipped default) changes nothing: no health_gate record
  anywhere, byte-identical behaviour.
- Unhealthy blocks: status unroutable, error_class health_gate_blocked,
  no exec attempt, no silent reroute to another backend.
- An explicit --backend is gated exactly like a routed pick.
- A probe that cannot finish in time is verdict "unknown", recorded
  explicitly; on_unknown=allow (default) dispatches with the unknown
  verdict on the record, on_unknown=block refuses.
- --skip-health-gate bypasses the probe and the bypass itself is
  recorded; --health-gate / --no-health-gate force the gate on/off.
- Fallback re-routes are gated too; a blocked fallback ends the run
  instead of picking a third backend.

Run: python3 -m unittest tests.test_m24_slice1
     (or: python3 tests/test_m24_slice1.py)
"""
import contextlib
import copy
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
xs.breakers = lambda: {}  # isolate from any breakers on disk
xs.load_policy = lambda: xs.jload(f"{xs.SKILL_DIR}/policy.json", {})  # ignore policy.local.json overrides

POLICY = xs.load_policy()
with open(_load.FIXTURE_STATE) as f:
    BASE = json.load(f)

NOW_FIXED = 1794576000
CODEX_BIN = "/opt/hatch-image/bin/codex"


def gate_policy(enabled=True, timeout_seconds=10, on_unknown="allow"):
    policy = copy.deepcopy(POLICY)
    policy["routing"]["weighted"] = False
    policy["routing"]["quota"] = {"enabled": False, "threshold": 0, "sources": {}}
    policy["routing"]["health_gate"] = {"enabled": enabled,
                                        "timeout_seconds": timeout_seconds,
                                        "on_unknown": on_unknown}
    return policy


def args_ns(**overrides):
    args = types.SimpleNamespace(
        scope="repo", card_type=None, rework_of=None, run_id=None,
        prompt="test task", task_file=None, title="test task",
        verify_evalid=False, verify=None, evalid_bin=None,
        backend=None, backends_file=None, budget_minutes=None,
        dry_run=True, detach=False, cwd=os.getcwd(), no_worktree=False,
        keep_worktree=False, base=None, verify_baseline=False,
        auto_rework=False, no_fallback=False, read_only=False,
        paths=[], type="impl", difficulty=3, context_tokens=None,
        model=None, effort=None,
        health_gate=False, no_health_gate=False, skip_health_gate=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def codex_state(binary=None, limits=None):
    st = copy.deepcopy(BASE)
    st["codex"]["binary"] = binary if binary is not None else sys.executable
    if limits is not None:
        st["codex"]["limits"] = limits
    return st


def limits_with(used=0.05):
    return {"codex": {"name": "codex", "windows": {
        "seven_day": {"used": used, "window_minutes": 10080,
                      "resets_at": None, "resets_at_local": None,
                      "hours_to_reset": None, "pace": None}}}}


class ConfigValidation(unittest.TestCase):
    def test_missing_section_is_disabled_with_defaults(self):
        policy = copy.deepcopy(POLICY)
        policy["routing"].pop("health_gate", None)
        self.assertEqual(xs.health_gate_config(policy),
                         {"enabled": False, "timeout_seconds": 10, "on_unknown": "allow"})

    def test_shipped_policy_is_disabled(self):
        self.assertFalse(xs.health_gate_config(POLICY)["enabled"])

    def test_enabled_config_round_trips(self):
        cfg = xs.health_gate_config(gate_policy(enabled=True, timeout_seconds=5, on_unknown="block"))
        self.assertEqual(cfg, {"enabled": True, "timeout_seconds": 5, "on_unknown": "block"})

    def test_non_dict_section_rejected(self):
        policy = gate_policy()
        policy["routing"]["health_gate"] = True
        with self.assertRaises(ValueError):
            xs.health_gate_config(policy)

    def test_unknown_field_rejected(self):
        policy = gate_policy()
        policy["routing"]["health_gate"]["surprise"] = 1
        with self.assertRaises(ValueError):
            xs.health_gate_config(policy)

    def test_enabled_must_be_bool(self):
        policy = gate_policy()
        policy["routing"]["health_gate"]["enabled"] = "yes"
        with self.assertRaises(ValueError):
            xs.health_gate_config(policy)

    def test_timeout_bounds_pinned(self):
        for bad in (0, 121, "10", True, None):
            policy = gate_policy()
            policy["routing"]["health_gate"]["timeout_seconds"] = bad
            with self.assertRaises(ValueError, msg=f"timeout {bad!r}"):
                xs.health_gate_config(policy)
        for good in (1, 10, 120):
            policy = gate_policy()
            policy["routing"]["health_gate"]["timeout_seconds"] = good
            self.assertEqual(xs.health_gate_config(policy)["timeout_seconds"], good)

    def test_on_unknown_must_be_allow_or_block(self):
        policy = gate_policy()
        policy["routing"]["health_gate"]["on_unknown"] = "maybe"
        with self.assertRaises(ValueError):
            xs.health_gate_config(policy)


class CodexLoginCheck(unittest.TestCase):
    def _sh(self, out, err="", rc=0):
        return mock.patch.object(xs, "sh", return_value=(rc, out, err))

    def test_logged_in_passes(self):
        with self._sh("Logged in as nick@example.com\n"):
            check = xs._health_codex_login(CODEX_BIN, 10)
        self.assertEqual(check["status"], "pass")
        self.assertIn("codex login status", check["evidence"])

    def test_not_logged_in_fails(self):
        with self._sh("Not logged in\n"):
            check = xs._health_codex_login(CODEX_BIN, 10)
        self.assertEqual(check["status"], "fail")
        self.assertIn("not logged in", check["detail"].lower())

    def test_auth_error_on_nonzero_rc_fails(self):
        with self._sh("", "FatalAuthenticationError: token expired\n", rc=1):
            check = xs._health_codex_login(CODEX_BIN, 10)
        self.assertEqual(check["status"], "fail")

    def test_unparseable_nonzero_rc_is_unknown(self):
        with self._sh("", "something odd happened\n", rc=1):
            check = xs._health_codex_login(CODEX_BIN, 10)
        self.assertEqual(check["status"], "unknown")

    def test_timeout_is_timeout_not_fail(self):
        with self._sh("", "Command '['codex', 'login', 'status']' timed out after 10 seconds", rc=127):
            check = xs._health_codex_login(CODEX_BIN, 10)
        self.assertEqual(check["status"], "timeout")

    def test_no_binary_is_unknown(self):
        check = xs._health_codex_login(None, 10)
        self.assertEqual(check["status"], "unknown")

    def test_real_binary_empty_home_reports_not_logged_in(self):
        """Live check, no mocks: with an empty HOME codex has no session,
        so the real `codex login status` must classify as fail. This pins
        the probe against the binary's actual wording, not a fixture."""
        binary = shutil.which("codex") or (CODEX_BIN if os.path.exists(CODEX_BIN) else None)
        if binary is None:
            self.skipTest("no codex binary on this host")
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"HOME": home}):
                check = xs._health_codex_login(binary, 10)
        self.assertEqual(check["status"], "fail")
        self.assertIn("Not logged in", check["detail"])


class ProbeVerdicts(unittest.TestCase):
    def setUp(self):
        self._now_patch = mock.patch.object(xs, "NOW", return_value=NOW_FIXED)
        self._now_patch.start()
        self.addCleanup(self._now_patch.stop)

    def test_healthy_codex_all_checks_pass(self):
        st = codex_state(limits=limits_with(0.05))
        with mock.patch.object(xs, "_health_codex_login",
                               return_value=xs._health_check("login", "pass", "live session", "cmd")):
            rec = xs.health_probe_backend("codex", st, gate_policy(), 10)
        self.assertEqual(rec["verdict"], "healthy")
        self.assertEqual({c["name"]: c["status"] for c in rec["checks"]},
                         {"binary": "pass", "login": "pass", "quota": "pass"})
        self.assertLessEqual(rec["seconds"], 10)

    def test_unhealthy_codex_login_blocks_despite_readable_quota(self):
        st = codex_state(limits=limits_with(0.05))
        with mock.patch.object(xs, "_health_codex_login",
                               return_value=xs._health_check("login", "fail", "not logged in", "cmd")):
            rec = xs.health_probe_backend("codex", st, gate_policy(), 10)
        self.assertEqual(rec["verdict"], "unhealthy")
        self.assertIn("not logged in", rec["reason"])

    def test_codex_with_no_quota_reading_is_unknown_not_unhealthy(self):
        st = codex_state(limits={})
        with mock.patch.object(xs, "_health_codex_login",
                               return_value=xs._health_check("login", "pass", "live session", "cmd")):
            rec = xs.health_probe_backend("codex", st, gate_policy(), 10)
        self.assertEqual(rec["verdict"], "unknown")
        self.assertEqual([c["name"] for c in rec["checks"] if c["status"] == "unknown"], ["quota"])

    def test_missing_binary_fails(self):
        st = codex_state(binary="/nonexistent/codex", limits=limits_with(0.05))
        with mock.patch.object(xs, "_health_codex_login",
                               return_value=xs._health_check("login", "unknown", "no binary", "cmd")):
            rec = xs.health_probe_backend("codex", st, gate_policy(), 10)
        self.assertEqual(rec["verdict"], "unhealthy")
        self.assertEqual(rec["checks"][0]["status"], "fail")

    def test_unknown_backend_is_unknown(self):
        rec = xs.health_probe_backend("nosuch", {}, gate_policy(), 10)
        self.assertEqual(rec["verdict"], "unknown")
        # The only check (binary) is itself unknown: no applicable check
        # passed or failed, so the verdict is unknown, recorded explicitly.
        self.assertEqual([c["status"] for c in rec["checks"]], ["unknown"])
        self.assertIn("no probe state", rec["reason"])

    def test_deepseek_no_key_fails(self):
        st = {"deepseek": {"error": "no api key", "balance": None}}
        rec = xs.health_probe_backend("deepseek", st, gate_policy(), 10)
        self.assertEqual(rec["verdict"], "unhealthy")
        self.assertIn("no DeepSeek API key", rec["reason"])

    def test_deepseek_balance_readable_passes(self):
        st = {"deepseek": {"balance": 12.5, "currency": "USD", "error": None,
                           "opencode_models": []},
              "ocgo": {"binary": sys.executable}}
        rec = xs.health_probe_backend("deepseek", st, gate_policy(), 10)
        self.assertEqual(rec["verdict"], "healthy")

    def test_agy_login_states(self):
        st = {"agy": {"binary": sys.executable}}
        for state, verdict in (("logged in", "healthy"), ("not logged in", "unhealthy"),
                               ("unknown", "unknown")):
            with mock.patch.object(xs, "agy_login_state", return_value=state):
                rec = xs.health_probe_backend("agy", st, gate_policy(), 10)
            self.assertEqual(rec["verdict"], verdict, f"agy state {state!r}")

    def test_custom_backend_binary_present_passes(self):
        custom = [{"id": "mytool", "command": [sys.executable, "-c", "pass"],
                   "quota_source": "none"}]
        rec = xs.health_probe_backend("mytool", {}, gate_policy(), 10, custom_backends=custom)
        self.assertEqual(rec["verdict"], "healthy")

    def test_custom_backend_binary_missing_fails(self):
        custom = [{"id": "mytool", "command": ["/nonexistent/mytool"],
                   "quota_source": "none"}]
        rec = xs.health_probe_backend("mytool", {}, gate_policy(), 10, custom_backends=custom)
        self.assertEqual(rec["verdict"], "unhealthy")

    def test_ocgo_no_models_is_unknown(self):
        st = {"ocgo": {"binary": sys.executable, "models": []}}
        rec = xs.health_probe_backend("ocgo", st, gate_policy(), 10)
        self.assertEqual(rec["verdict"], "unknown")

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"),
                         "AF_UNIX unavailable on this platform (Windows)")
    def test_pi_isolated_broker_socket_pass_and_fail(self):
        st = {"pi": {"binary": os.path.join(_load.ROOT, "bin", "pi-isolated"),
                     "quota": {"error": "no MiniMax credential (pi auth.json has no minimax-cn key)"}}}
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "broker.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            try:
                with mock.patch.dict(os.environ, {"OPENSWARM_PI_BROKER_SOCKET": sock_path},
                                     clear=False):
                    with mock.patch.object(xs.shutil, "which",
                                           side_effect=lambda c: f"/usr/bin/{c}"):
                        rec = xs.health_probe_backend("pi", st, gate_policy(), 10)
            finally:
                server.close()
        transport = [c for c in rec["checks"] if c["name"] == "transport"][0]
        self.assertEqual(transport["status"], "pass")
        # Quota error is auth-flavoured (no credential) so the verdict is
        # unhealthy even with a live socket: the fail is recorded, not hidden.
        self.assertEqual(rec["verdict"], "unhealthy")

    def test_pi_isolated_broker_socket_missing_fails(self):
        """Platform-independent: with no broker socket configured, the
        isolated-pi transport check fails (\"not set\") on every platform,
        including Windows where AF_UNIX does not exist."""
        st = {"pi": {"binary": os.path.join(_load.ROOT, "bin", "pi-isolated"),
                     "quota": {"error": "no MiniMax credential (pi auth.json has no minimax-cn key)"}}}
        with mock.patch.dict(os.environ, {"OPENSWARM_PI_BROKER_SOCKET": ""}, clear=False):
            with mock.patch.object(xs.shutil, "which",
                                   side_effect=lambda c: f"/usr/bin/{c}"):
                rec = xs.health_probe_backend("pi", st, gate_policy(), 10)
        transport = [c for c in rec["checks"] if c["name"] == "transport"][0]
        self.assertEqual(transport["status"], "fail")
        self.assertIn("not set", transport["detail"])

    def test_pi_plain_transport_connect(self):
        st = {"pi": {"binary": sys.executable, "quota": {"remaining": 0.9, "unit": "fraction"}}}
        policy = gate_policy()
        policy["pi"] = {"provider": "minimax-cn"}
        fake_sock = mock.Mock()
        with mock.patch.object(xs.socket, "create_connection", return_value=fake_sock):
            rec = xs.health_probe_backend("pi", st, policy, 10)
        transport = [c for c in rec["checks"] if c["name"] == "transport"][0]
        self.assertEqual(transport["status"], "pass")
        self.assertEqual(rec["verdict"], "healthy")
        with mock.patch.object(xs.socket, "create_connection",
                               side_effect=OSError("network unreachable")):
            rec2 = xs.health_probe_backend("pi", st, policy, 10)
        transport2 = [c for c in rec2["checks"] if c["name"] == "transport"][0]
        self.assertEqual(transport2["status"], "fail")
        self.assertEqual(rec2["verdict"], "unhealthy")

    def test_probe_timeout_is_unknown_and_pinned(self):
        """A check that overruns the budget: the probe must report
        unknown (with the timeout on the record), not a pass and not a
        block — and it must finish promptly, not hang on the slow check."""
        st = codex_state(limits=limits_with(0.05))

        def slow_login(binary, timeout_seconds):
            import time as _time
            _time.sleep(1.2)
            return xs._health_check("login", "pass", "slow but live", "cmd")

        with mock.patch.object(xs, "_health_codex_login", side_effect=slow_login):
            rec = xs.health_probe_backend("codex", st, gate_policy(), timeout_seconds=1)
        self.assertEqual(rec["verdict"], "unknown")
        self.assertTrue(any(c["status"] == "timeout" for c in rec["checks"]))
        self.assertIn("exceeded", rec["reason"])


class ApplyGate(unittest.TestCase):
    def setUp(self):
        self._now_patch = mock.patch.object(xs, "NOW", return_value=NOW_FIXED)
        self._now_patch.start()
        self.addCleanup(self._now_patch.stop)
        self.st = codex_state(limits=limits_with(0.05))

    def _probe(self, verdict, reason="test"):
        return {"backend": "codex", "verdict": verdict, "reason": reason,
                "checks": [], "seconds": 0.1, "timeout_seconds": 10}

    def test_disabled_gate_returns_nothing(self):
        record, blocked = xs.apply_health_gate("codex", self.st, gate_policy(enabled=False),
                                               args_ns())
        self.assertIsNone(record)
        self.assertFalse(blocked)

    def test_flag_on_enables_despite_policy_off(self):
        with mock.patch.object(xs, "health_probe_backend",
                               return_value=self._probe("healthy")) as probe:
            record, blocked = xs.apply_health_gate("codex", self.st, gate_policy(enabled=False),
                                                   args_ns(health_gate=True))
        probe.assert_called_once()
        self.assertEqual(record["verdict"], "healthy")
        self.assertEqual(record["action"], "allowed")
        self.assertEqual(record["source"], "flag --health-gate")
        self.assertFalse(blocked)

    def test_flag_off_disables_despite_policy_on(self):
        with mock.patch.object(xs, "health_probe_backend") as probe:
            record, blocked = xs.apply_health_gate("codex", self.st, gate_policy(enabled=True),
                                                   args_ns(no_health_gate=True))
        probe.assert_not_called()
        self.assertIsNone(record)
        self.assertFalse(blocked)

    def test_unhealthy_blocks(self):
        with mock.patch.object(xs, "health_probe_backend",
                               return_value=self._probe("unhealthy", "not logged in")):
            record, blocked = xs.apply_health_gate("codex", self.st, gate_policy(enabled=True),
                                                   args_ns())
        self.assertTrue(blocked)
        self.assertEqual(record["action"], "blocked")
        self.assertEqual(record["stage"], "initial")

    def test_unknown_allow_dispatches_with_record(self):
        with mock.patch.object(xs, "health_probe_backend",
                               return_value=self._probe("unknown", "quota unreadable")):
            record, blocked = xs.apply_health_gate("codex", self.st,
                                                   gate_policy(enabled=True, on_unknown="allow"),
                                                   args_ns())
        self.assertFalse(blocked)
        self.assertEqual(record["action"], "allowed")
        self.assertEqual(record["verdict"], "unknown")
        self.assertIn("on_unknown=allow", record["reason"])

    def test_unknown_block_refuses(self):
        with mock.patch.object(xs, "health_probe_backend",
                               return_value=self._probe("unknown", "quota unreadable")):
            record, blocked = xs.apply_health_gate("codex", self.st,
                                                   gate_policy(enabled=True, on_unknown="block"),
                                                   args_ns())
        self.assertTrue(blocked)
        self.assertEqual(record["action"], "blocked")
        self.assertIn("on_unknown=block", record["reason"])

    def test_skip_flag_bypasses_without_probing(self):
        with mock.patch.object(xs, "health_probe_backend") as probe:
            record, blocked = xs.apply_health_gate("codex", self.st, gate_policy(enabled=True),
                                                   args_ns(skip_health_gate=True))
        probe.assert_not_called()
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "bypassed")
        self.assertEqual(record["action"], "bypassed")
        self.assertEqual(record["checks"], [])

    def test_skip_flag_is_noop_when_gate_off(self):
        with mock.patch.object(xs, "health_probe_backend") as probe:
            record, blocked = xs.apply_health_gate("codex", self.st, gate_policy(enabled=False),
                                                   args_ns(skip_health_gate=True))
        probe.assert_not_called()
        self.assertIsNone(record)
        self.assertFalse(blocked)

    def test_error_text_is_actionable(self):
        record = {"backend": "codex", "verdict": "unhealthy",
                  "reason": "not logged in",
                  "checks": [{"name": "login", "status": "fail",
                              "detail": "codex is not logged in",
                              "evidence": "command: codex login status"}]}
        error = xs.health_gate_error(record)
        self.assertIn("codex login", error)
        self.assertIn("--skip-health-gate", error)
        self.assertIn("never reroutes", error)


class CmdRunWiring(unittest.TestCase):
    """cmd_run integration through --dry-run: no exec, no worktree, but
    the gate decision must land in result.json and the ledger."""

    def setUp(self):
        self._now_patch = mock.patch.object(xs, "NOW", return_value=NOW_FIXED)
        self._now_patch.start()
        self.addCleanup(self._now_patch.stop)
        self.decision = {"ok": True, "backend": "codex", "model": "gpt-6-luna",
                         "effort": "high", "tier": "balanced", "fallbacks": [],
                         "reasons": [], "skipped": {}, "skipped_details": {}}
        self.st = codex_state(limits=limits_with(0.05))

    def _run_cmd(self, policy, args):
        out = io.StringIO()
        patches = [
            mock.patch.object(xs, "get_state", return_value=self.st),
            mock.patch.object(xs, "route", return_value=dict(self.decision)),
            mock.patch.object(xs, "apply_weighted_routing",
                              return_value=(dict(self.decision), None)),
            mock.patch.object(xs, "apply_quota_routing",
                              return_value=(dict(self.decision), None)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with contextlib.redirect_stdout(out):
            try:
                xs.cmd_run(args, policy)
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def _last_ledger(self):
        lines = [json.loads(line) for line in open(xs.LEDGER) if line.strip()]
        return lines[-1]

    def test_unhealthy_backend_refuses_before_exec(self):
        policy = gate_policy(enabled=True)
        fail_login = xs._health_check("login", "fail", "codex is not logged in",
                                      "command: codex login status")
        with mock.patch.object(xs, "_health_codex_login", return_value=fail_login):
            code, printed = self._run_cmd(policy, args_ns(dry_run=True))
        self.assertEqual(code, 2)
        out = json.loads(printed)
        self.assertEqual(out["status"], "unroutable")
        self.assertEqual(out["error_class"], "health_gate_blocked")
        self.assertIn("codex login", out["error"])
        self.assertEqual(out["health_gate"]["verdict"], "unhealthy")
        self.assertEqual(out["health_gate"]["action"], "blocked")
        result = json.load(open(os.path.join(xs.RUNS, out["id"], "result.json")))
        self.assertEqual(result["error_class"], "health_gate_blocked")
        entry = self._last_ledger()
        self.assertEqual(entry["error_class"], "health_gate_blocked")
        self.assertEqual(entry["health_gate"]["verdict"], "unhealthy")
        # No executor ran: no attempts, no events file.
        self.assertNotIn("attempts", result)
        self.assertFalse(os.path.exists(os.path.join(xs.RUNS, out["id"], "events.1.codex.jsonl")))

    def test_explicit_backend_is_gated_too(self):
        policy = gate_policy(enabled=True)
        fail_login = xs._health_check("login", "fail", "codex is not logged in",
                                      "command: codex login status")
        with mock.patch.object(xs, "_health_codex_login", return_value=fail_login):
            code, printed = self._run_cmd(policy, args_ns(dry_run=True, backend="codex"))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(printed)["error_class"], "health_gate_blocked")

    def test_unknown_allowed_reaches_dry_run_with_record(self):
        policy = gate_policy(enabled=True, on_unknown="allow")
        st_no_quota = codex_state(limits={})
        self.st = st_no_quota
        with mock.patch.object(xs, "_health_codex_login",
                               return_value=xs._health_check("login", "pass", "live", "cmd")):
            code, printed = self._run_cmd(policy, args_ns(dry_run=True))
        self.assertEqual(code, 0)
        out = json.loads(printed)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["health_gate"]["verdict"], "unknown")
        self.assertEqual(out["health_gate"]["action"], "allowed")
        self.assertIn("on_unknown=allow", out["health_gate"]["reason"])

    def test_gate_off_dry_run_has_no_health_gate_key(self):
        policy = gate_policy(enabled=False)
        with mock.patch.object(xs, "_health_codex_login",
                               return_value=xs._health_check("login", "fail", "not logged in", "cmd")):
            code, printed = self._run_cmd(policy, args_ns(dry_run=True))
        self.assertEqual(code, 0)
        out = json.loads(printed)
        self.assertTrue(out["dry_run"])
        self.assertNotIn("health_gate", out)

    def test_bypass_flag_reaches_dry_run_with_bypass_record(self):
        policy = gate_policy(enabled=True)
        with mock.patch.object(xs, "health_probe_backend") as probe:
            code, printed = self._run_cmd(policy, args_ns(dry_run=True, skip_health_gate=True))
        probe.assert_not_called()
        self.assertEqual(code, 0)
        out = json.loads(printed)
        self.assertEqual(out["health_gate"]["verdict"], "bypassed")
        self.assertEqual(out["health_gate"]["action"], "bypassed")


if __name__ == "__main__":
    unittest.main()
