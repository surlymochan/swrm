"""M29 slice 1 (#242): per-repo intake-policy + evalid allowlist + cross-repo dedup core + guardrails.

Covers: per-repo `repo_policies` (independent allowlist + policy
version per repo, swrm behaviour byte-identical without an entry);
the evalid allowlist (authorized evalid issue dispatches in active
mode, out-of-scope / unlisted stays shadow / ignore); cross-repo
dedup (same author + same text, or a citation of the same issue, in
two repos builds one Mandate; idempotent replay builds no new Ticket
and dispatches once; a withdrawn Mandate no longer dedups); and the
guardrails (per-thread dispatch-count and budget caps, health-gate
failure blocks before any Assignment, non-allowlist default shadow).

Run: python3 -m unittest tests.test_m29_slice1
     (or: python3 tests/test_m29_slice1.py)
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


def trig(trigger_id, author="surlymochan", body="@openswarm fix the flaky test in fleet dispatch",
         kind="issue", number=1, bot=False, repo="surlymochan/swrm"):
    return {"id": trigger_id, "source": "github",
            "thread": {"repo": repo, "kind": kind, "number": number},
            "author": author, "author_is_bot": bot, "body": body,
            "created_at": "2026-11-16T01:00:00Z"}


def fake_runner(result_status="done"):
    calls = []

    def runner(ticket, trigger, decision, policy, data_dir, cwd):
        calls.append((ticket["id"], trigger["id"]))
        return {"run_id": "20261116-010000-abcdef",
                "status": result_status,
                "result": {"id": "20261116-010000-abcdef", "status": result_status,
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


def active_policy(repos=("surlymochan/swrm", "surlymochan/evalid"), users=None, **overrides):
    policy = xs.default_intake_policy()
    policy["accept_mode"] = "active"
    scope = {"repos": list(repos) if isinstance(repos, (list, tuple)) else repos}
    if users is not None:
        scope["users"] = list(users) if isinstance(users, (list, tuple)) else users
    policy["active_scope"] = scope
    policy.update(overrides)
    return policy


def two_repo_policy():
    """Both own repos active, with different per-repo versions and
    allowlists: alice is authorized in swrm only."""
    policy = active_policy()
    policy["repo_policies"] = {
        "surlymochan/swrm": {"version": 2,
                             "allowlist": {"users": ["surlymochan", "alice"], "bots": []}},
        "surlymochan/evalid": {"version": 3,
                               "allowlist": {"users": ["surlymochan"], "bots": []}},
    }
    return policy


class M29Slice1Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m29s1-")
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


class RepoPolicyValidationTests(M29Slice1Base):
    def test_accepts_valid_repo_policies(self):
        policy = two_repo_policy()
        xs.validate_intake_policy(policy)
        swrm = xs.intake_policy_for_repo(policy, "surlymochan/swrm")
        self.assertEqual(swrm["version"], 2)
        self.assertEqual(swrm["allowlist"]["users"], ["surlymochan", "alice"])
        evalid = xs.intake_policy_for_repo(policy, "surlymochan/evalid")
        self.assertEqual(evalid["version"], 3)
        self.assertEqual(evalid["allowlist"]["users"], ["surlymochan"])
        # Repo-name matching is case-insensitive.
        self.assertEqual(xs.intake_policy_for_repo(policy, "Surlymochan/EVALID")["version"], 3)

    def test_rejects_bad_repo_policy_keys(self):
        for bad_key in ("noslash", "a/b/c", "", "/", "owner/", "/repo", 123):
            bad = active_policy()
            bad["repo_policies"] = {bad_key: {"version": 1}}
            with self.assertRaises(ValueError, msg=f"key={bad_key!r}"):
                xs.validate_intake_policy(bad)

    def test_rejects_bad_repo_policy_entries(self):
        for bad_entry in ("not-a-dict",
                          {"version": 0}, {"version": -1}, {"version": "2"},
                          {"version": True},
                          {"allowlist": {"users": "surlymochan"}},
                          {"allowlist": {"users": [""]}},
                          {"allowlist": {"users": [123]}},
                          {"allowlist": "surlymochan"},
                          {"accept_mode": "active"}):
            bad = active_policy()
            bad["repo_policies"] = {"surlymochan/evalid": bad_entry}
            with self.assertRaises(ValueError, msg=f"entry={bad_entry!r}"):
                xs.validate_intake_policy(bad)

    def test_rejects_non_object_repo_policies(self):
        for bad in ("surlymochan/evalid", ["surlymochan/evalid"]):
            policy = active_policy()
            policy["repo_policies"] = bad
            with self.assertRaises(ValueError, msg=f"repo_policies={bad!r}"):
                xs.validate_intake_policy(policy)

    def test_effective_policy_does_not_mutate_source(self):
        policy = two_repo_policy()
        before = copy.deepcopy(policy)
        xs.intake_policy_for_repo(policy, "surlymochan/swrm")
        xs.intake_policy_for_repo(policy, "surlymochan/evalid")
        xs.intake_policy_for_repo(policy, "other/repo")
        self.assertEqual(policy, before)


class PerRepoIsolationTests(M29Slice1Base):
    def setUp(self):
        super().setUp()
        # M32 (#259): shipped default is now health-gate on; these tests pin
        # intake behaviour with a fake runner, so pin the gate off explicitly.
        patch = mock.patch.object(xs, "load_policy",
                                  return_value=gate_policy(enabled=False))
        patch.start()
        self.addCleanup(patch.stop)
    def test_swrm_default_behaviour_is_unchanged(self):
        # Regression: with no repo_policies entry for swrm, the Gate
        # decision for a swrm Trigger is byte-identical to the pre-M29
        # policy (same decision, strength, rules, version).
        policy = xs.default_intake_policy()
        decision = xs.intake_gate_decide(trig("s1"), policy)
        self.assertEqual(decision, {
            "decision": "accept", "strength": "explicit",
            "rules": ["admission:listed_mentioned:explicit"],
            "reason": "admitted as explicit (allowlisted author)",
            "draft_only": False, "policy_version": 1,
            "trigger_id": "s1", "accept_mode": "shadow"})
        # And the effective policy for swrm is the policy itself.
        self.assertIs(xs.intake_policy_for_repo(policy, "surlymochan/swrm"), policy)

    def test_default_policy_allowists_evalid_per_repo(self):
        policy = xs.default_intake_policy()
        entries = policy.get("repo_policies") or {}
        self.assertIn("surlymochan/evalid", entries)
        self.assertEqual(entries["surlymochan/evalid"]["allowlist"]["users"],
                         ["surlymochan"])
        self.assertEqual(entries["surlymochan/evalid"]["version"], 1)

    def test_per_repo_versions_are_sealed_on_mandates(self):
        policy = two_repo_policy()
        swrm = self.ingest(trig("v1"), policy=policy)
        evalid = self.ingest(trig("v2", repo="surlymochan/evalid", number=7,
                                  body="@openswarm fix the retry backoff bug in batch dispatch"),
                             policy=policy)
        mandates = {m["ticket_id"]: m for m in self.state()["mandates"]}
        self.assertEqual(mandates[swrm["ticket_id"]]["policy_version"], 2)
        self.assertEqual(mandates[evalid["ticket_id"]]["policy_version"], 3)
        # A repo with no entry keeps the global version.
        other = self.ingest(trig("v3", repo="other/repo", number=3,
                                 body="@openswarm fix the stale cache bug in the consult path"),
                            policy=policy)
        mandates = {m["ticket_id"]: m for m in self.state()["mandates"]}
        self.assertEqual(mandates[other["ticket_id"]]["policy_version"], 1)

    def test_per_repo_allowlist_isolation(self):
        # alice is allowlisted in swrm only: accepted there, ignored in
        # evalid (unlisted + unmentioned -> ignore, no Ticket, no run).
        policy = two_repo_policy()
        runner = fake_runner()
        swrm = self.ingest(trig("i1", author="alice"), policy=policy,
                           dispatch_runner=runner, channel_poster=fake_channel())
        self.assertEqual(swrm["decision"]["decision"], "accept")
        evalid = self.ingest(trig("i2", author="alice", repo="surlymochan/evalid",
                                  number=7, body="fix the flaky test please"),
                             policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertEqual(evalid["decision"]["decision"], "ignore")
        self.assertIsNone(evalid["ticket_id"])
        self.assertFalse(evalid["dispatched"])
        self.assertEqual(len(self.state()["tickets"]), 1)
        self.assertEqual(len(runner.calls), 1)
        # alice dispatches in swrm: the per-repo allowlist also drives
        # the active-scope users default for that repo.
        self.assertTrue(swrm["dispatched"])

    def test_unlisted_author_ignored_in_both_repos(self):
        policy = two_repo_policy()
        runner = fake_runner()
        for tid, repo in (("u1", "surlymochan/swrm"), ("u2", "surlymochan/evalid")):
            result = self.ingest(
                trig(tid, author="mallory", repo=repo, number=9,
                     body="fix the flaky test please"),
                policy=policy, dispatch_runner=runner, channel_poster=fake_channel())
            self.assertEqual(result["decision"]["decision"], "ignore")
            self.assertIsNone(result["ticket_id"])
        self.assertEqual(self.state()["tickets"], [])
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.ledger_kinds().count("decision"), 2)


class EvalidAllowlistTests(M29Slice1Base):
    def setUp(self):
        super().setUp()
        # M32 (#259): shipped default is now health-gate on; these tests pin
        # intake behaviour with a fake runner, so pin the gate off explicitly.
        patch = mock.patch.object(xs, "load_policy",
                                  return_value=gate_policy(enabled=False))
        patch.start()
        self.addCleanup(patch.stop)
    def test_evalid_in_scope_dispatches(self):
        policy = active_policy()
        runner = fake_runner()
        channel = fake_channel()
        result = self.ingest(trig("e1", repo="surlymochan/evalid", number=7),
                             policy=policy, dispatch_runner=runner,
                             channel_poster=channel)
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["active_scope"]["in_scope"], True)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(channel.calls), 1)
        self.assertEqual(len(self.state()["assignments"]), 1)
        self.assertNotIn("active_scope_shadow", self.ledger_kinds())

    def test_evalid_outside_active_scope_stays_shadow(self):
        policy = active_policy(repos=["surlymochan/swrm"])
        runner = fake_runner()
        result = self.ingest(trig("e2", repo="surlymochan/evalid", number=7),
                             policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["active_scope"]["in_scope"], False)
        self.assertIn("active_scope_shadow", self.ledger_kinds())
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.state()["assignments"], [])

    def test_non_allowlist_default_is_shadow(self):
        # accept_mode=active with no active_scope configured: nothing
        # dispatches, in either repo -- the shipped safe default.
        policy = xs.default_intake_policy()
        policy["accept_mode"] = "active"
        runner = fake_runner()
        for tid, repo, body in (
                ("n1", "surlymochan/swrm", "@openswarm fix the flaky test in fleet dispatch"),
                ("n2", "surlymochan/evalid", "@openswarm fix the retry backoff bug in batch dispatch")):
            result = self.ingest(trig(tid, repo=repo, number=5, body=body), policy=policy,
                                 dispatch_runner=runner, channel_poster=fake_channel())
            self.assertIsNotNone(result["ticket_id"])
            self.assertFalse(result["dispatched"])
            self.assertEqual(result["active_scope"]["in_scope"], False)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.ledger_kinds().count("active_scope_shadow"), 2)


class CrossRepoDedupTests(M29Slice1Base):
    def setUp(self):
        super().setUp()
        # M32 (#259): shipped default is now health-gate on; these tests pin
        # intake behaviour with a fake runner, so pin the gate off explicitly.
        patch = mock.patch.object(xs, "load_policy",
                                  return_value=gate_policy(enabled=False))
        patch.start()
        self.addCleanup(patch.stop)
    def test_same_text_two_repos_builds_one_mandate(self):
        policy = active_policy()
        runner = fake_runner()
        channel = fake_channel()
        first = self.ingest(trig("x1"), policy=policy, dispatch_runner=runner,
                            channel_poster=channel)
        self.assertTrue(first["dispatched"])
        second = self.ingest(trig("x2", repo="surlymochan/evalid", number=7),
                             policy=policy, dispatch_runner=runner,
                             channel_poster=channel)
        self.assertTrue(second["duplicate"])
        self.assertTrue(second["cross_repo_duplicate"])
        self.assertEqual(second["ticket_id"], first["ticket_id"])
        self.assertIsNone(second["decision"])
        self.assertFalse(second["dispatched"])
        self.assertEqual(len(self.state()["tickets"]), 1)
        self.assertEqual(len(self.state()["mandates"]), 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(channel.calls), 1)
        self.assertEqual(self.ledger_kinds().count("cross_repo_duplicate_skipped"), 1)
        self.assertEqual(self.ledger_kinds().count("duplicate_skipped"), 0)

    def test_same_text_normalized_mentions_and_case_dedup(self):
        policy = active_policy()
        first = self.ingest(trig("y1"), policy=policy)
        # Same instruction, different delivery: no @-mention, extra
        # whitespace, different case -- still the same source.
        second = self.ingest(
            trig("y2", repo="surlymochan/evalid", number=7,
                 body="  FIX   the flaky test in fleet dispatch  "),
            policy=policy)
        self.assertTrue(second.get("cross_repo_duplicate"))
        self.assertEqual(second["ticket_id"], first["ticket_id"])
        self.assertEqual(len(self.state()["tickets"]), 1)

    def test_same_text_different_author_is_a_different_source(self):
        policy = two_repo_policy()
        first = self.ingest(trig("z1"), policy=policy)
        second = self.ingest(trig("z2", author="alice", repo="surlymochan/swrm",
                                  number=2), policy=policy)
        self.assertFalse(second["duplicate"])
        self.assertNotEqual(second["ticket_id"], first["ticket_id"])
        self.assertEqual(len(self.state()["tickets"]), 2)

    def test_same_text_same_repo_different_threads_are_distinct(self):
        # Cross-repo only: within one repo, two distinct threads with
        # identical text are distinct work items (M27 slice 3 opens
        # two such Tickets); dedup must not collapse them.
        policy = active_policy()
        first = self.ingest(trig("s1"), policy=policy)
        second = self.ingest(trig("s2", number=2), policy=policy)
        self.assertFalse(second["duplicate"])
        self.assertNotEqual(second["ticket_id"], first["ticket_id"])
        self.assertEqual(len(self.state()["tickets"]), 2)

    def test_citation_of_same_issue_dedups(self):
        policy = active_policy()
        runner = fake_runner()
        first = self.ingest(trig("r1", number=42), policy=policy,
                            dispatch_runner=runner, channel_poster=fake_channel())
        self.assertTrue(first["dispatched"])
        # A second issue, in the other repo, that just points at the
        # first issue by reference: same source, one Mandate.
        second = self.ingest(
            trig("r2", repo="surlymochan/evalid", number=7,
                 body="@openswarm please do the same fix as surlymochan/swrm#42"),
            policy=policy, dispatch_runner=runner, channel_poster=fake_channel())
        self.assertTrue(second.get("cross_repo_duplicate"))
        self.assertEqual(second["ticket_id"], first["ticket_id"])
        self.assertEqual(len(self.state()["tickets"]), 1)
        self.assertEqual(len(runner.calls), 1)

    def test_citation_of_a_different_issue_does_not_dedup(self):
        policy = active_policy()
        first = self.ingest(trig("r3", number=42), policy=policy)
        second = self.ingest(
            trig("r4", repo="surlymochan/evalid", number=7,
                 body="@openswarm please do the same fix as surlymochan/swrm#99"),
            policy=policy)
        self.assertFalse(second["duplicate"])
        self.assertNotEqual(second["ticket_id"], first["ticket_id"])
        self.assertEqual(len(self.state()["tickets"]), 2)

    def test_replay_is_idempotent_across_ids_and_repos(self):
        policy = active_policy()
        runner = fake_runner()
        triggers = [
            trig("p1"),
            trig("p1"),
            trig("p2", repo="surlymochan/evalid", number=7),
        ]
        results = xs.intake_replay(triggers, policy=policy, data_dir=str(self.data))
        self.assertFalse(results[0]["duplicate"])
        self.assertTrue(results[1]["duplicate"])
        self.assertEqual(results[1]["ticket_id"], results[0]["ticket_id"])
        self.assertTrue(results[2].get("cross_repo_duplicate"))
        self.assertEqual(results[2]["ticket_id"], results[0]["ticket_id"])
        self.assertEqual(len(self.state()["tickets"]), 1)
        self.assertEqual(len(self.state()["mandates"]), 1)
        self.assertEqual(self.ledger_kinds().count("duplicate_skipped"), 1)
        self.assertEqual(self.ledger_kinds().count("cross_repo_duplicate_skipped"), 1)

    def test_followup_on_open_thread_with_new_body_still_opens_a_ticket(self):
        # Slice-3 behaviour preserved: a follow-up on a live thread
        # with a different task goes through the normal path (the
        # Trigger's own thread is not a lookup key).
        policy = active_policy()
        first = self.ingest(trig("f1"), policy=policy)
        second = self.ingest(
            trig("f2", body="@openswarm also fix the retry backoff bug in batch dispatch"),
            policy=policy)
        self.assertFalse(second["duplicate"])
        self.assertNotEqual(second["ticket_id"], first["ticket_id"])
        self.assertEqual(len(self.state()["tickets"]), 2)

    def test_withdrawn_mandate_no_longer_dedups(self):
        policy = active_policy()
        runner = fake_runner()
        first = self.ingest(trig("w1"), policy=policy, dispatch_runner=runner,
                            channel_poster=fake_channel())
        self.assertTrue(first["dispatched"])
        stop = self.ingest(trig("w2", body="@openswarm stop"), policy=policy)
        self.assertTrue(stop["stopped"])
        self.assertTrue(stop["mandate_withdrawn"])
        # Same source, other repo, after the stop: the Mandate was
        # retired, so this is a new task with a new Ticket.
        second = self.ingest(trig("w3", repo="surlymochan/evalid", number=7),
                             policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertFalse(second["duplicate"])
        self.assertNotEqual(second["ticket_id"], first["ticket_id"])
        self.assertEqual(len(self.state()["tickets"]), 2)
        mandates = {m["ticket_id"]: m for m in self.state()["mandates"]}
        self.assertEqual(xs.intake_mandate_status(mandates[first["ticket_id"]]),
                         "withdrawn")
        self.assertEqual(xs.intake_mandate_status(mandates[second["ticket_id"]]),
                         "active")


class CapsTests(M29Slice1Base):
    def setUp(self):
        super().setUp()
        # M32 (#259): shipped default is now health-gate on; these tests pin
        # intake behaviour with a fake runner, so pin the gate off explicitly.
        patch = mock.patch.object(xs, "load_policy",
                                  return_value=gate_policy(enabled=False))
        patch.start()
        self.addCleanup(patch.stop)
    def test_dispatch_count_cap_blocks_second_dispatch_on_evalid_thread(self):
        policy = active_policy(caps={"max_dispatches_per_thread": 1})
        runner = fake_runner()
        first = self.ingest(trig("k1", repo="surlymochan/evalid", number=7),
                            policy=policy, dispatch_runner=runner,
                            channel_poster=fake_channel())
        self.assertTrue(first["dispatched"])
        second = self.ingest(
            trig("k2", repo="surlymochan/evalid", number=7,
                 body="@openswarm fix the retry backoff bug in batch dispatch"),
            policy=policy, dispatch_runner=runner, channel_poster=fake_channel())
        self.assertIsNotNone(second["ticket_id"])
        self.assertFalse(second["dispatched"])
        self.assertTrue(second["cap_blocked"])
        self.assertEqual(second["caps"]["dispatches"], 1)
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
        first = self.ingest(trig("k3", repo="surlymochan/evalid", number=7),
                            policy=policy, dispatch_runner=runner,
                            channel_poster=fake_channel())
        self.assertTrue(first["dispatched"])
        second = self.ingest(
            trig("k4", repo="surlymochan/evalid", number=7,
                 body="@openswarm fix the retry backoff bug in batch dispatch"),
            policy=policy, dispatch_runner=runner, channel_poster=fake_channel())
        self.assertFalse(second["dispatched"])
        self.assertTrue(second["cap_blocked"])
        self.assertEqual(second["caps"]["budget_minutes"], 10)
        self.assertEqual(len(runner.calls), 1)


class HealthGateTests(M29Slice1Base):
    def _gate_env(self, enabled=True, on_unknown="allow", binary=None,
                  login_status="pass"):
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

    def test_unhealthy_backend_blocks_evalid_dispatch(self):
        self._gate_env(enabled=True, binary=None, login_status="unknown")
        policy = active_policy(dispatch={"backend": "codex"})
        runner = fake_runner()
        result = self.ingest(trig("h1", repo="surlymochan/evalid", number=7),
                             policy=policy, dispatch_runner=runner,
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

    def test_healthy_backend_dispatches_evalid(self):
        self._gate_env(enabled=True, binary=sys.executable, login_status="pass")
        policy = active_policy(dispatch={"backend": "codex"})
        runner = fake_runner()
        result = self.ingest(trig("h2", repo="surlymochan/evalid", number=7),
                             policy=policy, dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["health_gate"]["verdict"], "healthy")
        self.assertEqual(len(runner.calls), 1)


class CliTests(M29Slice1Base):
    def _run_cli(self, argv, env_extra=None):
        env = dict(os.environ)
        env["OPENSWARM_HOME"] = str(self.data)
        if env_extra:
            env.update(env_extra)
        return subprocess.run([sys.executable, str(HERE.parent / "bin" / "openswarm"), *argv],
                              capture_output=True, text=True, encoding="utf-8", env=env)

    def test_cli_policy_shows_repo_policies(self):
        policy = two_repo_policy()
        with open(self.data / "intake-policy.json", "w") as f:
            json.dump(policy, f)
        proc = self._run_cli(["intake", "policy"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("repo_policy surlymochan/swrm version=2 users=surlymochan,alice",
                      proc.stdout)
        self.assertIn("repo_policy surlymochan/evalid version=3 users=surlymochan",
                      proc.stdout)
        proc = self._run_cli(["intake", "policy", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["policy"]["repo_policies"]["surlymochan/evalid"]["version"], 3)

    def test_cli_policy_init_writes_evalid_repo_policy(self):
        proc = self._run_cli(["intake", "policy", "--init"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.data / "intake-policy.json") as f:
            policy = json.load(f)
        self.assertIn("surlymochan/evalid", policy.get("repo_policies") or {})
        self.assertEqual(policy["repo_policies"]["surlymochan/evalid"]["allowlist"]["users"],
                         ["surlymochan"])


if __name__ == "__main__":
    unittest.main()
