"""M28 slice 1 (#232): active-mode core -- allowlist scope + caps + idempotency + health-gate pre-check.

Covers: accept_mode=active dispatching only inside the allowlisted
scope (repo + authorized account, both must hold) with non-scope
staying shadow; per-thread dispatch-count and budget caps; no duplicate
Mandate / no second dispatch on a re-delivered Trigger (and a direct
double dispatch is refused); the M24/M25 health gate blocking a
dispatch before any Assignment or run exists (reused, not re-implemented);
policy validation for the new active_scope / caps blocks; and the
`intake policy` display of the new blocks. `accept_mode=on` keeps its
M27 meaning (dispatch anything accepted) and is not scope-gated.

Run: python3 -m unittest tests.test_m28_slice1
     (or: python3 tests/test_m28_slice1.py)
"""
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
HERE = pathlib.Path(__file__).resolve().parent

POLICY = xs.load_policy()
with open(_load.FIXTURE_STATE) as f:
    BASE = json.load(f)


def trig(trigger_id, author="surlymochan", body="please fix the flaky test in fleet dispatch",
         kind="issue", number=1, bot=False, repo="surlymochan/swrm"):
    return {"id": trigger_id, "source": "github",
            "thread": {"repo": repo, "kind": kind, "number": number},
            "author": author, "author_is_bot": bot, "body": body,
            "created_at": "2026-11-15T13:00:00Z"}


def fake_runner(result_status="done"):
    calls = []

    def runner(ticket, trigger, decision, policy, data_dir, cwd):
        calls.append((ticket["id"], trigger["id"]))
        return {"run_id": "20261115-130000-abcdef",
                "status": result_status,
                "result": {"id": "20261115-130000-abcdef", "status": result_status,
                           "branch": "intake/" + ticket["id"], "commit": "abc1234",
                           "summary": "did the thing"}}
    runner.calls = calls
    return runner


def fake_channel():
    calls = []

    def poster(repo, number, body):
        calls.append((repo, number, body))
        return {"id": 999, "html_url": f"https://github.com/{repo}/issues/{number}#issuecomment-999"}
    poster.calls = calls
    return poster


def gate_policy(enabled=True, timeout_seconds=10, on_unknown="allow"):
    policy = copy.deepcopy(POLICY)
    policy["routing"]["weighted"] = False
    policy["routing"]["quota"] = {"enabled": False, "threshold": 0, "sources": {}}
    policy["routing"]["health_gate"] = {"enabled": enabled,
                                        "timeout_seconds": timeout_seconds,
                                        "on_unknown": on_unknown}
    return policy


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


def active_policy(repos=("surlymochan/swrm",), users=None, **overrides):
    policy = xs.default_intake_policy()
    policy["accept_mode"] = "active"
    scope = {"repos": list(repos) if isinstance(repos, (list, tuple)) else repos}
    if users is not None:
        scope["users"] = list(users) if isinstance(users, (list, tuple)) else users
    policy["active_scope"] = scope
    policy.update(overrides)
    return policy


class M28Slice1Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m28s1-")
        self.data = pathlib.Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.patch = mock.patch.object(xs, "DATA", str(self.data))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def ingest(self, trigger, policy, **kw):
        return xs.intake_ingest_trigger(trigger, policy=policy,
                                        data_dir=str(self.data), **kw)

    def ledger_kinds(self):
        return [e["kind"] for e in xs.intake_ledger_entries(str(self.data))]

    def ledger_entries(self, kind=None):
        return xs.intake_ledger_entries(str(self.data), kind=kind)

    def state(self):
        return xs.load_intake_state(str(self.data))


class PolicyValidationTests(M28Slice1Base):
    def test_active_mode_validates(self):
        xs.validate_intake_policy(active_policy())

    def test_active_mode_without_scope_block_validates(self):
        policy = xs.default_intake_policy()
        policy["accept_mode"] = "active"
        xs.validate_intake_policy(policy)

    def test_rejects_bad_active_scope_repos(self):
        for bad_repos in ("surlymochan/swrm", ["noslash"], ["a/b/c"],
                          [""], ["owner/"], ["/repo"], [123]):
            bad = active_policy(repos=bad_repos)
            with self.assertRaises(ValueError, msg=f"repos={bad_repos!r}"):
                xs.validate_intake_policy(bad)

    def test_rejects_bad_active_scope_users(self):
        for bad_users in ("surlymochan", [""], [123]):
            bad = active_policy(users=bad_users)
            with self.assertRaises(ValueError, msg=f"users={bad_users!r}"):
                xs.validate_intake_policy(bad)

    def test_rejects_bad_caps(self):
        for bad_caps in ({"max_dispatches_per_thread": 0},
                         {"max_dispatches_per_thread": -1},
                         {"max_dispatches_per_thread": 1.5},
                         {"max_dispatches_per_thread": True},
                         {"max_dispatches_per_thread": "3"},
                         {"max_budget_minutes_per_thread": 0},
                         {"max_budget_minutes_per_thread": -5},
                         {"max_budget_minutes_per_thread": "60"},
                         {"max_budget_minutes_per_thread": True},
                         "not-a-dict"):
            bad = active_policy(caps=bad_caps)
            with self.assertRaises(ValueError, msg=f"caps={bad_caps!r}"):
                xs.validate_intake_policy(bad)

    def test_accepts_valid_caps_and_scope(self):
        policy = active_policy(
            repos=["surlymochan/swrm", "surlymochan/evalid"],
            users=["surlymochan"],
            caps={"max_dispatches_per_thread": 3,
                  "max_budget_minutes_per_thread": 60})
        xs.validate_intake_policy(policy)
        self.assertEqual(xs.intake_active_scope(policy),
                         {"repos": ["surlymochan/swrm", "surlymochan/evalid"],
                          "users": ["surlymochan"]})


class ActiveScopeTests(M28Slice1Base):
    def test_in_scope_dispatches(self):
        policy = active_policy()
        runner = fake_runner()
        result = self.ingest(trig("a1"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["active_scope"]["in_scope"], True)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(self.state()["assignments"]), 1)
        self.assertNotIn("active_scope_shadow", self.ledger_kinds())

    def test_repo_outside_scope_stays_shadow(self):
        policy = active_policy(repos=["surlymochan/evalid"])
        runner = fake_runner()
        result = self.ingest(trig("a2"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["active_scope"]["in_scope"], False)
        self.assertIn("active_scope_shadow", self.ledger_kinds())
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.state()["assignments"], [])
        shadow = self.ledger_entries("active_scope_shadow")[0]
        self.assertIn("surlymochan/swrm", shadow["reason"])

    def test_user_outside_scope_stays_shadow(self):
        policy = active_policy(users=["someone-else"])
        runner = fake_runner()
        result = self.ingest(trig("a3"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["active_scope"]["in_scope"], False)
        self.assertIn("active_scope_shadow", self.ledger_kinds())
        self.assertEqual(runner.calls, [])

    def test_active_mode_without_scope_configured_stays_shadow(self):
        # No active_scope block at all: repos defaults to empty, so
        # nothing is active -- the safe default.
        policy = xs.default_intake_policy()
        policy["accept_mode"] = "active"
        runner = fake_runner()
        result = self.ingest(trig("a4"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["active_scope"]["in_scope"], False)
        self.assertIn("active_scope_shadow", self.ledger_kinds())
        self.assertEqual(runner.calls, [])

    def test_users_default_to_allowlist_users(self):
        # active_scope without a users key authorizes the allowlist
        # users; the repo still has to be listed explicitly.
        policy = active_policy()
        runner = fake_runner()
        result = self.ingest(trig("a5"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertEqual(len(runner.calls), 1)

    def test_scope_match_is_case_insensitive(self):
        policy = active_policy(repos=["Surlymochan/SWRM"], users=["SurlyMochan"])
        runner = fake_runner()
        result = self.ingest(trig("a6"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])

    def test_on_mode_is_not_scope_gated(self):
        # accept_mode=on keeps its M27 meaning: dispatch anything
        # accepted, even for a repo outside the active scope.
        policy = xs.default_intake_policy()
        policy["accept_mode"] = "on"
        policy["active_scope"] = {"repos": ["other/repo"], "users": ["surlymochan"]}
        runner = fake_runner()
        result = self.ingest(trig("a7"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertNotIn("active_scope", result)
        self.assertNotIn("active_scope_shadow", self.ledger_kinds())


class CapsTests(M28Slice1Base):
    def test_dispatch_count_cap_blocks_second_dispatch_on_thread(self):
        policy = active_policy(caps={"max_dispatches_per_thread": 1})
        runner = fake_runner()
        first = self.ingest(trig("c1"), policy=policy, dispatch_runner=runner,
                            channel_poster=fake_channel())
        self.assertTrue(first["dispatched"])
        second = self.ingest(
            trig("c2", body="fix the retry backoff bug in batch dispatch"),
            policy=policy, dispatch_runner=runner, channel_poster=fake_channel())
        self.assertIsNotNone(second["ticket_id"])
        self.assertFalse(second["dispatched"])
        self.assertTrue(second["cap_blocked"])
        self.assertEqual(second["caps"]["dispatches"], 1)
        self.assertEqual(second["caps"]["max_dispatches_per_thread"], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(self.state()["assignments"]), 1)
        self.assertEqual(self.ledger_kinds().count("cap_blocked"), 1)

    def test_budget_cap_blocks_when_next_dispatch_would_exceed(self):
        # Default dispatch budget is 10m; a 15m per-thread cap lets one
        # dispatch through and blocks the second (10 + 10 > 15).
        policy = xs.default_intake_policy()
        policy["accept_mode"] = "on"
        policy["caps"] = {"max_budget_minutes_per_thread": 15}
        runner = fake_runner()
        first = self.ingest(trig("c3"), policy=policy, dispatch_runner=runner,
                            channel_poster=fake_channel())
        self.assertTrue(first["dispatched"])
        second = self.ingest(
            trig("c4", body="fix the retry backoff bug in batch dispatch"),
            policy=policy, dispatch_runner=runner, channel_poster=fake_channel())
        self.assertFalse(second["dispatched"])
        self.assertTrue(second["cap_blocked"])
        self.assertEqual(second["caps"]["budget_minutes"], 10)
        self.assertEqual(second["caps"]["max_budget_minutes_per_thread"], 15)
        self.assertEqual(len(runner.calls), 1)

    def test_no_caps_block_means_no_enforcement(self):
        policy = xs.default_intake_policy()
        policy["accept_mode"] = "on"
        runner = fake_runner()
        first = self.ingest(trig("c5"), policy=policy, dispatch_runner=runner,
                            channel_poster=fake_channel())
        second = self.ingest(
            trig("c6", body="fix the retry backoff bug in batch dispatch"),
            policy=policy, dispatch_runner=runner, channel_poster=fake_channel())
        self.assertTrue(first["dispatched"])
        self.assertTrue(second["dispatched"])
        self.assertEqual(len(runner.calls), 2)
        self.assertNotIn("cap_blocked", self.ledger_kinds())

    def test_single_cap_key_leaves_the_other_off(self):
        # Only a generous budget cap: two dispatches on one thread both
        # go through (10m + 10m <= 100m), no count cap applies.
        policy = xs.default_intake_policy()
        policy["accept_mode"] = "on"
        policy["caps"] = {"max_budget_minutes_per_thread": 100}
        runner = fake_runner()
        first = self.ingest(trig("c7"), policy=policy, dispatch_runner=runner,
                            channel_poster=fake_channel())
        second = self.ingest(
            trig("c8", body="fix the retry backoff bug in batch dispatch"),
            policy=policy, dispatch_runner=runner, channel_poster=fake_channel())
        self.assertTrue(first["dispatched"])
        self.assertTrue(second["dispatched"])
        self.assertEqual(len(runner.calls), 2)


class IdempotencyTests(M28Slice1Base):
    def test_redelivery_after_dispatch_dispatches_once(self):
        policy = active_policy()
        runner = fake_runner()
        channel = fake_channel()
        first = self.ingest(trig("d1"), policy=policy, dispatch_runner=runner,
                            channel_poster=channel)
        self.assertTrue(first["dispatched"])
        second = self.ingest(trig("d1"), policy=policy, dispatch_runner=runner,
                             channel_poster=channel)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["ticket_id"], first["ticket_id"])
        self.assertFalse(second["dispatched"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(channel.calls), 1)
        self.assertEqual(len(self.state()["assignments"]), 1)
        kinds = self.ledger_kinds()
        self.assertEqual(kinds.count("duplicate_skipped"), 1)
        self.assertEqual(kinds.count("assignment_created"), 1)
        self.assertEqual(kinds.count("dispatch_started"), 1)

    def test_direct_double_dispatch_refused(self):
        policy = xs.default_intake_policy()
        policy["accept_mode"] = "on"
        runner = fake_runner()
        result = self.ingest(trig("d2"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        ticket = next(t for t in self.state()["tickets"]
                      if t["id"] == result["ticket_id"])
        with self.assertRaises(ValueError):
            xs.intake_dispatch(ticket, xs.normalize_trigger(trig("d2")),
                               result["decision"], policy=policy,
                               data_dir=str(self.data), dispatch_runner=runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(self.state()["assignments"]), 1)

    def test_replay_marks_duplicate_and_keeps_one_ticket(self):
        policy = active_policy()
        triggers = [
            trig("r1"),
            trig("r2", repo="other/repo", number=2,
                 body="fix the flaky test in the other repo please"),
            trig("r1"),
        ]
        results = xs.intake_replay(triggers, policy=policy, data_dir=str(self.data))
        self.assertEqual(results[0]["decision"]["decision"], "accept")
        self.assertTrue(results[0]["active_scope"]["in_scope"])
        self.assertEqual(results[1]["decision"]["decision"], "accept")
        self.assertFalse(results[1]["active_scope"]["in_scope"])
        self.assertTrue(results[2]["duplicate"])
        self.assertEqual(results[2]["ticket_id"], results[0]["ticket_id"])
        self.assertEqual(len(self.state()["tickets"]), 2)
        self.assertEqual(len(self.state()["mandates"]), 2)
        self.assertEqual(self.ledger_kinds().count("duplicate_skipped"), 1)
        self.assertEqual(self.ledger_kinds().count("active_scope_shadow"), 1)


class HealthGateTests(M28Slice1Base):
    def _gate_env(self, enabled=True, on_unknown="allow", binary=None,
                  login_status="pass"):
        # codex_state(binary=None) falls back to sys.executable (the M24
        # helper's "default" meaning); a missing binary has to be set
        # explicitly after construction.
        st = codex_state(binary=binary, limits=limits_with(0.05))
        if binary is None:
            st["codex"]["binary"] = None
        patches = [
            mock.patch.object(xs, "load_policy",
                              return_value=gate_policy(enabled=enabled,
                                                       on_unknown=on_unknown)),
            mock.patch.object(xs, "get_state", return_value=st),
            mock.patch.object(xs, "_health_codex_login",
                              return_value=xs._health_check("login", login_status,
                                                            "canned", "cmd")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_unhealthy_backend_blocks_before_any_assignment(self):
        self._gate_env(enabled=True, binary=None, login_status="unknown")
        policy = active_policy(dispatch={"backend": "codex"})
        runner = fake_runner()
        result = self.ingest(trig("g1"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertTrue(result["health_gate_blocked"])
        self.assertEqual(result["health_gate"]["verdict"], "unhealthy")
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.state()["assignments"], [])
        blocked = self.ledger_entries("intake_health_gate_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["health_gate"]["backend"], "codex")

    def test_healthy_backend_dispatches_and_records_gate(self):
        self._gate_env(enabled=True, binary=sys.executable, login_status="pass")
        policy = active_policy(dispatch={"backend": "codex"})
        runner = fake_runner()
        result = self.ingest(trig("g2"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["health_gate"]["verdict"], "healthy")
        self.assertNotIn("intake_health_gate_blocked", self.ledger_kinds())
        self.assertEqual(len(runner.calls), 1)

    def test_unknown_with_block_posture_blocks(self):
        self._gate_env(enabled=True, on_unknown="block",
                       binary=sys.executable, login_status="unknown")
        policy = active_policy(dispatch={"backend": "codex"})
        runner = fake_runner()
        result = self.ingest(trig("g3"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertFalse(result["dispatched"])
        self.assertTrue(result["health_gate_blocked"])
        self.assertEqual(result["health_gate"]["verdict"], "unknown")
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.state()["assignments"], [])

    def test_unknown_with_allow_posture_dispatches(self):
        self._gate_env(enabled=True, on_unknown="allow",
                       binary=sys.executable, login_status="unknown")
        policy = active_policy(dispatch={"backend": "codex"})
        runner = fake_runner()
        result = self.ingest(trig("g4"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["health_gate"]["verdict"], "unknown")
        self.assertFalse(result.get("health_gate_blocked", False))

    def test_routed_backend_is_resolved_then_gated(self):
        self._gate_env(enabled=True, binary=None, login_status="unknown")
        route_patch = mock.patch.object(
            xs, "route",
            return_value={"ok": True, "backend": "codex", "model": "gpt-6-luna",
                          "effort": "high", "tier": "balanced", "fallbacks": [],
                          "reasons": [], "skipped": {}, "skipped_details": {}})
        route_patch.start()
        self.addCleanup(route_patch.stop)
        policy = active_policy()
        runner = fake_runner()
        result = self.ingest(trig("g5"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertFalse(result["dispatched"])
        self.assertTrue(result["health_gate_blocked"])
        self.assertEqual(result["health_gate"]["backend"], "codex")
        self.assertEqual(runner.calls, [])

    def test_unroutable_defers_to_run_path(self):
        # Routing finds no backend: there is nothing to gate, so the
        # pre-check passes and the run child refuses as unroutable, as
        # today. (The fake runner stands in for the child here.)
        self._gate_env(enabled=True, binary=None, login_status="unknown")
        route_patch = mock.patch.object(xs, "route", return_value={"ok": False})
        route_patch.start()
        self.addCleanup(route_patch.stop)
        policy = active_policy()
        runner = fake_runner()
        result = self.ingest(trig("g6"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertNotIn("health_gate", result)

    def test_gate_off_dispatches_unchanged(self):
        # Shipped default (health gate disabled in policy.json): the
        # pre-check is a no-op and dispatch proceeds exactly as in M27.
        policy = active_policy()
        runner = fake_runner()
        result = self.ingest(trig("g7"), policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertNotIn("health_gate", result)
        self.assertNotIn("intake_health_gate_blocked", self.ledger_kinds())


class CliTests(M28Slice1Base):
    def _run_cli(self, argv, env_extra=None):
        env = dict(os.environ)
        env["OPENSWARM_HOME"] = str(self.data)
        if env_extra:
            env.update(env_extra)
        return subprocess.run([sys.executable, str(HERE.parent / "bin" / "openswarm"), *argv],
                              capture_output=True, text=True, encoding="utf-8", env=env)

    def test_cli_policy_shows_active_scope_and_caps(self):
        policy = active_policy(
            repos=["surlymochan/swrm"], users=["surlymochan"],
            caps={"max_dispatches_per_thread": 2,
                  "max_budget_minutes_per_thread": 30})
        with open(self.data / "intake-policy.json", "w") as f:
            json.dump(policy, f)
        proc = self._run_cli(["intake", "policy"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("active_scope repos=surlymochan/swrm users=surlymochan",
                      proc.stdout)
        self.assertIn("caps max_dispatches_per_thread=2 "
                      "max_budget_minutes_per_thread=30", proc.stdout)
        proc = self._run_cli(["intake", "policy", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["policy"]["accept_mode"], "active")
        self.assertEqual(out["policy"]["active_scope"]["repos"], ["surlymochan/swrm"])
        self.assertEqual(out["policy"]["caps"]["max_dispatches_per_thread"], 2)


if __name__ == "__main__":
    unittest.main()
