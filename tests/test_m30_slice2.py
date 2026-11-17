"""M30 slice 2 (#251): fleet-path intake core + real-run evidence.

Covers: policy.dispatch.mode "fleet" (accepted now; bad values still
refused); intake_dispatch_fleet (one Assignment, executor "fleet",
ledger kinds in order, per-card results mirrored, idempotent second
call refused, runner failure ends the Assignment, first run id
returned); the default fleet runner run_openswarm_fleet (topo-order
dispatch via fleet_dispatch_once, per-card health gate, no-runner
block, dependency block, no exec behind a block, refuses cleanly
with no repo checkout); ingest wiring (fleet mode dispatches via the
fleet runner and never via the run/batch runners, missing fleet
runner stays Ticket-only, out-of-scope stays shadow, caps and the
health gate block before any Assignment, an explicit --backend is
gated exactly like a routed pick, fleet dispatches post to the
Channel with the fleet id + per-card run ids); the M30 slice-2
evidence checker; and the fleet Channel body.

Run: python3 -m unittest tests.test_m30_slice2
     (or: python3 tests/test_m30_slice2.py)
"""
import argparse
import copy
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
HERE = pathlib.Path(__file__).resolve().parent

POLICY = xs.load_policy()
with open(_load.FIXTURE_STATE) as f:
    BASE = json.load(f)


def trig(trigger_id, author="surlymochan",
         body="@openswarm fix the flaky test in fleet dispatch",
         kind="issue", number=1, bot=False, repo="surlymochan/swrm"):
    return {"id": trigger_id, "source": "github",
            "thread": {"repo": repo, "kind": kind, "number": number},
            "author": author, "author_is_bot": bot, "body": body,
            "created_at": "2026-11-16T01:00:00Z"}


def checklist_body(n=3, checked=0):
    lines = ["@openswarm do these three things, in order:"]
    for i in range(checked):
        lines.append(f"- [x] already done step {i + 1}")
    for i in range(n):
        lines.append(f"- [ ] fix the flaky test number {i + 1}")
    return "\n".join(lines)


def fake_fleet_runner(summary=None, fleet_id="20261117-090000-ffffff"):
    calls = []

    def runner(ticket, trigger, decision, policy, data_dir, cards_dir, cwd):
        calls.append((ticket["id"], trigger["id"], cards_dir))
        cards = sorted(os.listdir(cards_dir)) if os.path.isdir(cards_dir) else []
        if summary is None:
            rows = [{"card": name, "runner": "local", "status": "done",
                     "run_id": f"run-{i}", "branch": f"intake/{ticket['id']}",
                     "commit": "abc1234", "summary": "did the thing"}
                    for i, name in enumerate(cards)]
            return {"fleet_id": fleet_id, "status": "finished",
                    "summary": {"id": fleet_id, "state": "finished", "cards": rows}}
        return {"fleet_id": fleet_id, "status": summary.get("state", "finished"),
                "summary": summary}
    runner.calls = calls
    return runner


def fake_batch_runner(batch_id="20261117-090000-bbbbbb"):
    calls = []

    def runner(ticket, trigger, decision, policy, data_dir, cards_dir, cwd):
        calls.append((ticket["id"], trigger["id"], cards_dir))
        cards = sorted(os.listdir(cards_dir)) if os.path.isdir(cards_dir) else []
        rows = [{"card": name, "status": "done", "run_id": f"run-{i}",
                 "branch": f"intake/{ticket['id']}", "commit": "abc1234",
                 "summary": "did the thing"}
                for i, name in enumerate(cards)]
        return {"batch_id": batch_id, "status": "finished",
                "summary": {"id": batch_id, "state": "finished", "cards": rows}}
    runner.calls = calls
    return runner


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


def fleet_policy(**dispatch_overrides):
    dispatch = {"mode": "fleet"}
    dispatch.update(dispatch_overrides)
    return active_policy(dispatch=dispatch)


def batch_policy(**dispatch_overrides):
    dispatch = {"mode": "batch"}
    dispatch.update(dispatch_overrides)
    return active_policy(dispatch=dispatch)


class M30Slice2Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m30s2-")
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

    def ticket_for(self, trigger, policy=None):
        policy = policy or xs.default_intake_policy()
        decision = {"decision": "accept", "strength": "explicit",
                    "rules": ["admission:listed_mentioned:explicit"],
                    "policy_version": 1}
        ticket = xs.new_intake_ticket(trigger, decision)
        mandate = xs.new_intake_mandate(ticket, trigger, decision)
        return ticket, mandate


class DispatchModeTests(M30Slice2Base):
    def test_default_mode_is_run(self):
        policy = xs.default_intake_policy()
        self.assertNotIn("mode", policy.get("dispatch") or {})
        self.assertEqual(xs.intake_dispatch_config(policy)["mode"], "run")

    def test_fleet_mode_validates(self):
        policy = xs.default_intake_policy()
        policy["dispatch"] = {"mode": "fleet"}
        xs.validate_intake_policy(policy)
        self.assertEqual(xs.intake_dispatch_config(policy)["mode"], "fleet")

    def test_batch_mode_still_validates(self):
        policy = xs.default_intake_policy()
        policy["dispatch"] = {"mode": "batch"}
        xs.validate_intake_policy(policy)
        self.assertEqual(xs.intake_dispatch_config(policy)["mode"], "batch")

    def test_bad_mode_refused(self):
        for bad in ("Fleet", "BATCH", "", 3, None, ["fleet"]):
            policy = xs.default_intake_policy()
            policy["dispatch"] = {"mode": bad}
            with self.assertRaises(ValueError, msg=f"mode={bad!r}"):
                xs.validate_intake_policy(policy)

    def test_policy_show_prints_mode(self):
        policy = fleet_policy()
        with open(self.data / "intake-policy.json", "w") as f:
            json.dump(policy, f)
        args = argparse.Namespace(init=False, json=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            xs.cmd_intake_policy(args, data_dir=str(self.data))
        self.assertIn("dispatch mode=fleet", buf.getvalue())


class FleetDispatchTests(M30Slice2Base):
    def _dispatch(self, trigger, runner=None, policy=None):
        policy = policy or fleet_policy()
        ticket, _ = self.ticket_for(trigger)
        result = xs.intake_dispatch_fleet(
            ticket, trigger, {"decision": "accept"}, policy=policy,
            data_dir=str(self.data), fleet_runner=runner or fake_fleet_runner())
        return ticket, result

    def test_happy_path_writes_cards_and_ledger(self):
        trigger = trig("d1", body=checklist_body(2))
        ticket, result = self._dispatch(trigger)
        self.assertEqual(result["fleet_id"], "20261117-090000-ffffff")
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["run_id"], "run-0")
        state = self.state()
        self.assertEqual(len(state["assignments"]), 1)
        self.assertEqual(state["assignments"][0]["executor"], "fleet")
        self.assertEqual(state["assignments"][0]["status"], "ended")
        cards_dir = (self.data / "intake-fleets" / ticket["id"] / "cards")
        self.assertEqual(sorted(os.listdir(cards_dir)), [
            "01-fix-the-flaky-test-number-1.md",
            "02-fix-the-flaky-test-number-2.md"])
        kinds = self.ledger_kinds()
        for kind in ("assignment_created", "intake_fleet_created",
                     "fleet_dispatch_started", "fleet_dispatch_finished"):
            self.assertIn(kind, kinds)
        created = self.ledger_entries("intake_fleet_created")[0]
        self.assertEqual(created["order"], created["cards"])
        card_results = self.ledger_entries("intake_fleet_card_result")
        self.assertEqual(len(card_results), 2)
        self.assertTrue(all(r["status"] == "done" for r in card_results))
        self.assertTrue(all(r["run_id"] for r in card_results))
        self.assertTrue(all(r["runner"] == "local" for r in card_results))
        self.assertLess(kinds.index("assignment_created"),
                        kinds.index("fleet_dispatch_started"))
        self.assertLess(kinds.index("fleet_dispatch_started"),
                        kinds.index("fleet_dispatch_finished"))

    def test_second_dispatch_refused_idempotent(self):
        trigger = trig("d2", body=checklist_body(2))
        ticket, _ = self._dispatch(trigger)
        runner = fake_fleet_runner()
        with self.assertRaises(ValueError):
            xs.intake_dispatch_fleet(
                ticket, trigger, {"decision": "accept"},
                policy=fleet_policy(), data_dir=str(self.data),
                fleet_runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(len(self.ledger_entries("assignment_created")), 1)
        self.assertEqual(len(self.ledger_entries("fleet_dispatch_started")), 1)

    def test_runner_failure_ends_assignment(self):
        def failing(ticket, trigger, decision, policy, data_dir, cards_dir, cwd):
            raise ValueError("fleet is down")
        trigger = trig("d3", body=checklist_body(1))
        ticket, _ = self.ticket_for(trigger)
        with self.assertRaises(ValueError):
            xs.intake_dispatch_fleet(
                ticket, trigger, {"decision": "accept"},
                policy=fleet_policy(), data_dir=str(self.data),
                fleet_runner=failing)
        self.assertEqual(self.state()["assignments"][0]["status"], "ended")
        failed = self.ledger_entries("fleet_dispatch_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("fleet is down", failed[0]["error"])
        cards_dir = self.data / "intake-fleets" / ticket["id"] / "cards"
        self.assertEqual(len(os.listdir(cards_dir)), 1)

    def test_runner_without_fleet_id_refused(self):
        def no_id(ticket, trigger, decision, policy, data_dir, cards_dir, cwd):
            return {"status": "finished"}
        trigger = trig("d4", body=checklist_body(1))
        ticket, _ = self.ticket_for(trigger)
        with self.assertRaises(ValueError):
            xs.intake_dispatch_fleet(
                ticket, trigger, {"decision": "accept"},
                policy=fleet_policy(), data_dir=str(self.data),
                fleet_runner=no_id)
        self.assertEqual(len(self.ledger_entries("fleet_dispatch_failed")), 1)

    def test_non_fleet_mode_refused(self):
        trigger = trig("d5", body=checklist_body(1))
        ticket, _ = self.ticket_for(trigger)
        with self.assertRaises(ValueError):
            xs.intake_dispatch_fleet(
                ticket, trigger, {"decision": "accept"},
                policy=xs.default_intake_policy(), data_dir=str(self.data),
                fleet_runner=fake_fleet_runner())

    def test_blocked_card_recorded_with_health_gate(self):
        summary = {"id": "20261117-090000-eeeeee", "state": "finished", "cards": [
            {"card": "01-fix-the-flaky-test-number-1.md", "runner": "local",
             "status": "done", "run_id": "run-0", "branch": "b1",
             "commit": "abc1234", "summary": "ok"},
            {"card": "02-fix-the-flaky-test-number-2.md", "runner": None,
             "status": "blocked", "run_id": None,
             "blocked_reason": "health gate: codex login expired",
             "health_gate": {"verdict": "unhealthy", "backend": "codex",
                            "reason": "codex login expired"},
             "summary": "blocked by health gate"},
        ]}
        trigger = trig("d6", body=checklist_body(2))
        runner = fake_fleet_runner(summary=summary,
                                   fleet_id="20261117-090000-eeeeee")
        ticket, result = self._dispatch(trigger, runner=runner)
        self.assertEqual(result["fleet_id"], "20261117-090000-eeeeee")
        self.assertEqual(result["run_id"], "run-0")
        rows = {r["card"]: r for r in self.ledger_entries("intake_fleet_card_result")}
        blocked = rows["02-fix-the-flaky-test-number-2.md"]
        self.assertTrue(blocked["blocked"])
        self.assertIsNone(blocked["run_id"])
        self.assertEqual(blocked["health_gate"]["verdict"], "unhealthy")
        self.assertIn("health gate", blocked["reason"])
        self.assertFalse(rows["01-fix-the-flaky-test-number-1.md"]["blocked"])

    def test_all_blocked_run_id_is_none(self):
        summary = {"id": "20261117-090000-dddddd", "state": "finished", "cards": [
            {"card": "01-fix-the-flaky-test-number-1.md", "runner": None,
             "status": "blocked", "run_id": None,
             "blocked_reason": "no runner can take it", "summary": "blocked"},
        ]}
        trigger = trig("d7", body=checklist_body(1))
        runner = fake_fleet_runner(summary=summary,
                                   fleet_id="20261117-090000-dddddd")
        _, result = self._dispatch(trigger, runner=runner)
        self.assertIsNone(result["run_id"])

    def test_card_outcomes_pure_function(self):
        self.assertEqual(xs.intake_fleet_card_outcomes(None), [])
        self.assertEqual(xs.intake_fleet_card_outcomes({}), [])
        rows = xs.intake_fleet_card_outcomes({"cards": [
            {"card": "a.md", "runner": None, "status": "blocked", "run_id": None,
             "blocked_reason": "dep failed", "health_gate": None},
            {"card": "b.md", "runner": "local", "status": "done", "run_id": "r1",
             "summary": "ok"},
        ]})
        self.assertTrue(rows[0]["blocked"])
        self.assertEqual(rows[0]["reason"], "dep failed")
        self.assertFalse(rows[1]["blocked"])
        self.assertEqual(rows[1]["runner"], "local")


class DefaultRunnerTests(M30Slice2Base):
    def _git_repo(self):
        repo = pathlib.Path(self.tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                       check=True)
        (repo / "README.md").write_text("hi\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"],
                       check=True)
        return repo

    def _cards_dir(self, trigger, n=2):
        ticket, _ = self.ticket_for(trigger)
        cards_dir = pathlib.Path(self.tmp.name) / "cards"
        xs.intake_write_batch_cards(ticket, trigger, str(cards_dir))
        return ticket, cards_dir

    def _fake_plan(self, host=None):
        host = host or {"name": "local", "kind": "local"}
        calls = []

        def plan(hosts, backend, repo, remaining):
            calls.append((backend, repo, list(remaining)))
            return ([(name, host) for name in remaining],
                    [{"name": host["name"], "eligible": True, "host": host,
                      "active": 0, "capacity": 2, "assigned": len(remaining)}])
        plan.calls = calls
        return plan

    def _fake_dispatch_once(self, status="done", run_prefix="run"):
        calls = []

        def dispatch_once(host, card, *, repo, backend, title=None, verify=None,
                          base=None, backends_content=None, timeout=900,
                          card_file=None, relay=True):
            calls.append((host["name"], card_file, backend, base))
            n = len(calls)
            return {"ok": True, "runner": host["name"], "transport": "local",
                    "remote_run_id": f"{run_prefix}-{n}", "remote_rc": 0,
                    "remote_status": status, "verify_rc": None,
                    "remote_base": base, "remote_run_dir": None,
                    "ledger_line": None, "entry_id": f"fleet-local-{n}"}
        dispatch_once.calls = calls
        return dispatch_once

    def test_default_runner_refuses_without_checkout(self):
        trigger = trig("r1", body=checklist_body(1))
        ticket, _ = self.ticket_for(trigger)
        plain = pathlib.Path(self.tmp.name) / "not-a-repo"
        plain.mkdir()
        with self.assertRaises(ValueError) as ctx:
            xs.run_openswarm_fleet(ticket, trigger, {"decision": "accept"},
                                   fleet_policy(), str(self.data),
                                   str(self.data / "cards"), cwd=str(plain))
        self.assertIn("base commit", str(ctx.exception))

    def test_default_runner_dispatches_cards_in_order(self):
        repo = self._git_repo()
        trigger = trig("r2", body=checklist_body(2))
        ticket, cards_dir = self._cards_dir(trigger, n=2)
        plan = self._fake_plan()
        once = self._fake_dispatch_once()
        patches = [
            mock.patch.object(xs, "fleet_dispatch_many_plan", plan),
            mock.patch.object(xs, "fleet_dispatch_once", once),
            mock.patch.object(xs, "fleet_health_gate",
                              return_value=(None, False)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        outcome = xs.run_openswarm_fleet(ticket, trigger, {"decision": "accept"},
                                        fleet_policy(backend="codex"),
                                        str(self.data), str(cards_dir),
                                        cwd=str(repo))
        self.assertEqual(outcome["status"], "finished")
        self.assertTrue(outcome["fleet_id"])
        rows = outcome["summary"]["cards"]
        self.assertEqual([r["status"] for r in rows], ["done", "done"])
        self.assertEqual([r["run_id"] for r in rows], ["run-1", "run-2"])
        self.assertEqual([r["runner"] for r in rows], ["local", "local"])
        self.assertEqual(len(plan.calls), 2)
        self.assertEqual(len(once.calls), 2)
        # Second card stacks on the first card's commit... the fake
        # returns no commit, so it stacks on the batch base; both
        # dispatches still carry a base.
        self.assertTrue(all(call[3] for call in once.calls))
        saved = (self.data / "fleets" / outcome["fleet_id"] / "summary.json")
        self.assertTrue(saved.is_file())

    def test_default_runner_health_gate_blocks_card(self):
        repo = self._git_repo()
        trigger = trig("r3", body=checklist_body(1))
        ticket, cards_dir = self._cards_dir(trigger, n=1)
        plan = self._fake_plan()
        once = self._fake_dispatch_once()
        record = {"verdict": "unhealthy", "backend": "codex",
                  "reason": "codex login expired", "checks": []}
        patches = [
            mock.patch.object(xs, "fleet_dispatch_many_plan", plan),
            mock.patch.object(xs, "fleet_dispatch_once", once),
            mock.patch.object(xs, "fleet_health_gate",
                              return_value=(record, True)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        outcome = xs.run_openswarm_fleet(ticket, trigger, {"decision": "accept"},
                                        fleet_policy(backend="codex"),
                                        str(self.data), str(cards_dir),
                                        cwd=str(repo))
        row = outcome["summary"]["cards"][0]
        self.assertEqual(row["status"], "blocked")
        self.assertIsNone(row["run_id"])
        self.assertEqual(row["health_gate"]["verdict"], "unhealthy")
        self.assertEqual(once.calls, [])

    def test_default_runner_no_runner_blocks_card(self):
        repo = self._git_repo()
        trigger = trig("r4", body=checklist_body(1))
        ticket, cards_dir = self._cards_dir(trigger, n=1)

        def empty_plan(hosts, backend, repo, remaining):
            return ([], [{"name": "local", "eligible": False,
                         "reason": "does not offer backend 'pi'",
                         "active": None, "capacity": 2}])
        once = self._fake_dispatch_once()
        patches = [
            mock.patch.object(xs, "fleet_dispatch_many_plan", empty_plan),
            mock.patch.object(xs, "fleet_dispatch_once", once),
            mock.patch.object(xs, "fleet_health_gate",
                              return_value=(None, False)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        outcome = xs.run_openswarm_fleet(ticket, trigger, {"decision": "accept"},
                                        fleet_policy(backend="pi"),
                                        str(self.data), str(cards_dir),
                                        cwd=str(repo))
        row = outcome["summary"]["cards"][0]
        self.assertEqual(row["status"], "blocked")
        self.assertIsNone(row["run_id"])
        self.assertIn("does not offer backend", row["blocked_reason"])
        self.assertEqual(once.calls, [])

    def test_default_runner_dependency_failure_blocks_successor(self):
        repo = self._git_repo()
        trigger = trig("r5", body=checklist_body(2))
        ticket, cards_dir = self._cards_dir(trigger, n=2)
        plan = self._fake_plan()
        once = self._fake_dispatch_once(status="failed")
        patches = [
            mock.patch.object(xs, "fleet_dispatch_many_plan", plan),
            mock.patch.object(xs, "fleet_dispatch_once", once),
            mock.patch.object(xs, "fleet_health_gate",
                              return_value=(None, False)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        outcome = xs.run_openswarm_fleet(ticket, trigger, {"decision": "accept"},
                                        fleet_policy(backend="codex"),
                                        str(self.data), str(cards_dir),
                                        cwd=str(repo))
        rows = outcome["summary"]["cards"]
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[1]["status"], "blocked")
        self.assertIsNone(rows[1]["run_id"])
        self.assertEqual(len(once.calls), 1)


class IngestWiringTests(M30Slice2Base):
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

    def test_fleet_mode_dispatches_via_fleet_runner(self):
        # M32 (#259): shipped default is now health-gate on; this test pins
        # dispatch wiring with a fake runner, so pin the gate off explicitly.
        self._gate_env(enabled=False)
        policy = fleet_policy()
        fleet = fake_fleet_runner()
        single = fake_runner()
        batch = fake_batch_runner()
        channel = fake_channel()
        result = self.ingest(trig("w1", body=checklist_body(2)), policy=policy,
                             dispatch_runner=single, batch_runner=batch,
                             fleet_runner=fleet, channel_poster=channel)
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["dispatch_mode"], "fleet")
        self.assertEqual(result["dispatch"]["fleet_id"], "20261117-090000-ffffff")
        self.assertEqual(len(fleet.calls), 1)
        self.assertEqual(single.calls, [])
        self.assertEqual(batch.calls, [])
        self.assertIn("fleet_dispatch_started", self.ledger_kinds())
        self.assertNotIn("dispatch_started", self.ledger_kinds())
        self.assertNotIn("batch_dispatch_started", self.ledger_kinds())
        # Fleet dispatches post to the Channel (unlike batch).
        self.assertEqual(len(channel.calls), 1)
        body = channel.calls[0][2]
        self.assertIn("fleet: `20261117-090000-ffffff`", body)
        self.assertIn("run=`run-0`", body)
        posted = self.ledger_entries("channel_posted")
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["fleet_id"], "20261117-090000-ffffff")

    def test_fleet_mode_without_fleet_runner_stays_ticket_only(self):
        policy = fleet_policy()
        single = fake_runner()
        batch = fake_batch_runner()
        result = self.ingest(trig("w2", body=checklist_body(2)), policy=policy,
                             dispatch_runner=single, batch_runner=batch,
                             fleet_runner=None, channel_poster=fake_channel())
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertIn("no dispatch runner", result["dispatch_note"])
        self.assertEqual(single.calls, [])
        self.assertEqual(batch.calls, [])
        self.assertEqual(self.state()["assignments"], [])

    def test_run_mode_ignores_fleet_runner(self):
        # M32 (#259): shipped default is now health-gate on; this test pins
        # dispatch wiring with a fake runner, so pin the gate off explicitly.
        self._gate_env(enabled=False)
        policy = active_policy()
        single = fake_runner()
        fleet = fake_fleet_runner()
        result = self.ingest(trig("w3"), policy=policy,
                             dispatch_runner=single, fleet_runner=fleet,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["dispatch_mode"], "run")
        self.assertEqual(len(single.calls), 1)
        self.assertEqual(fleet.calls, [])
        self.assertIn("dispatch_started", self.ledger_kinds())
        self.assertNotIn("fleet_dispatch_started", self.ledger_kinds())

    def test_batch_mode_ignores_fleet_runner(self):
        # M32 (#259): shipped default is now health-gate on; this test pins
        # dispatch wiring with a fake runner, so pin the gate off explicitly.
        self._gate_env(enabled=False)
        policy = batch_policy()
        batch = fake_batch_runner()
        fleet = fake_fleet_runner()
        result = self.ingest(trig("w4", body=checklist_body(1)), policy=policy,
                             batch_runner=batch, fleet_runner=fleet,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["dispatch_mode"], "batch")
        self.assertEqual(fleet.calls, [])
        self.assertIn("channel_note", result)

    def test_out_of_scope_stays_shadow(self):
        policy = fleet_policy(repos=("surlymochan/swrm",))
        fleet = fake_fleet_runner()
        result = self.ingest(trig("w5", repo="other/repo", number=9,
                                  body=checklist_body(1)),
                             policy=policy, fleet_runner=fleet,
                             channel_poster=fake_channel())
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(fleet.calls, [])
        self.assertIn("active_scope_shadow", self.ledger_kinds())

    def test_caps_block_fleet_before_assignment(self):
        # M32 (#259): shipped default is now health-gate on; this test pins
        # dispatch wiring with a fake runner, so pin the gate off explicitly.
        self._gate_env(enabled=False)
        policy = fleet_policy()
        policy["caps"] = {"max_dispatches_per_thread": 1,
                          "max_budget_minutes_per_thread": 10}
        fleet = fake_fleet_runner()
        first = self.ingest(trig("w6a", body=checklist_body(1)), policy=policy,
                            fleet_runner=fleet, channel_poster=fake_channel())
        self.assertTrue(first["dispatched"])
        second = self.ingest(trig("w6b", body="@openswarm fix another flaky test"),
                             policy=policy, fleet_runner=fleet,
                             channel_poster=fake_channel())
        self.assertFalse(second["dispatched"])
        self.assertTrue(second["cap_blocked"])
        self.assertEqual(len(fleet.calls), 1)
        self.assertEqual(len(self.state()["assignments"]), 1)

    def test_draft_only_never_dispatches_fleet(self):
        policy = fleet_policy()
        fleet = fake_fleet_runner()
        body = ("@openswarm please pay $50 to the contributor who fixes "
                "the flaky test")
        result = self.ingest(trig("w7", body=body), policy=policy,
                             fleet_runner=fleet, channel_poster=fake_channel())
        self.assertFalse(result["dispatched"])
        self.assertEqual(fleet.calls, [])

    def test_health_gate_blocks_fleet_before_assignment(self):
        self._gate_env(enabled=True, binary=None, login_status="unknown")
        policy = fleet_policy(backend="codex")
        fleet = fake_fleet_runner()
        single = fake_runner()
        result = self.ingest(trig("w8", body=checklist_body(2)), policy=policy,
                             dispatch_runner=single, fleet_runner=fleet,
                             channel_poster=fake_channel())
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertTrue(result["health_gate_blocked"])
        self.assertEqual(result["health_gate"]["verdict"], "unhealthy")
        # Explicit --backend does not exempt the gate: no runner ran.
        self.assertEqual(fleet.calls, [])
        self.assertEqual(single.calls, [])
        self.assertEqual(self.state()["assignments"], [])
        blocked = self.ledger_entries("intake_health_gate_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["health_gate"]["backend"], "codex")

    def test_healthy_backend_dispatches_fleet(self):
        self._gate_env(enabled=True, binary=sys.executable, login_status="pass")
        policy = fleet_policy(backend="codex")
        fleet = fake_fleet_runner()
        result = self.ingest(trig("w9", body=checklist_body(1)), policy=policy,
                             fleet_runner=fleet, channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["health_gate"]["verdict"], "healthy")
        self.assertEqual(len(fleet.calls), 1)


class ChannelBodyTests(M30Slice2Base):
    def test_fleet_body_lists_cards(self):
        ticket, _ = self.ticket_for(trig("c1"))
        dispatch = {"fleet_id": "20261117-090000-ffffff", "status": "finished",
                    "run_id": "run-0", "assignment": {"executor": "fleet"},
                    "summary": {"cards": [
                        {"card": "01-a.md", "runner": "local", "status": "done",
                         "run_id": "run-0"},
                        {"card": "02-b.md", "runner": "local", "status": "blocked",
                         "run_id": None},
                    ]}}
        body = xs.intake_channel_body(ticket, trig("c1"), dispatch)
        self.assertIn("fleet: `20261117-090000-ffffff`", body)
        self.assertIn("card `01-a.md` @local: done run=`run-0`", body)
        self.assertIn("card `02-b.md` @local: blocked run=`None`", body)
        self.assertIn("fleets/20261117-090000-ffffff/summary.json", body)

    def test_run_body_unchanged(self):
        ticket, _ = self.ticket_for(trig("c2"))
        dispatch = {"run_id": "20261116-010000-abcdef", "status": "done",
                    "assignment": {"executor": "routed"},
                    "result": {"id": "20261116-010000-abcdef", "status": "done",
                               "branch": "intake/c2", "commit": "abc1234"}}
        body = xs.intake_channel_body(ticket, trig("c2"), dispatch)
        self.assertIn("- run: `20261116-010000-abcdef` (status: done)", body)
        self.assertIn("- branch: `intake/c2`", body)
        self.assertNotIn("fleet:", body)


class EvidenceCheckTests(M30Slice2Base):
    def _good_entries(self):
        # Two real-shape batch dispatches...
        for i, tid in enumerate(("e1", "e2")):
            trigger = trig(tid, body=f"@openswarm batch evidence task {i}")
            ticket, _ = self.ticket_for(trigger)
            xs.intake_dispatch_batch(
                ticket, trigger, {"decision": "accept"}, policy=batch_policy(),
                data_dir=str(self.data),
                batch_runner=fake_batch_runner(
                    batch_id=f"20261117-090000-b{i}"))
        # ...and two fleet dispatches, one of them with a blocked card.
        # The blocked card reuses the single-card name on purpose:
        # intake_fleet_card_outcomes mirrors rows verbatim; the checker
        # counts distinct run_ids, not rows.
        trigger = trig("e3", body="@openswarm fleet evidence task 0")
        ticket, _ = self.ticket_for(trigger)
        xs.intake_dispatch_fleet(
            ticket, trigger, {"decision": "accept"}, policy=fleet_policy(),
            data_dir=str(self.data),
            fleet_runner=fake_fleet_runner(summary={
                "id": "20261117-090000-e1", "state": "finished", "cards": [
                    {"card": "01-task.md", "runner": "local", "status": "done",
                     "run_id": "fleet-run-1", "summary": "ok"},
                ]}, fleet_id="20261117-090000-e1"))
        trigger = trig("e4", body="@openswarm fleet evidence task 1")
        ticket, _ = self.ticket_for(trigger)
        xs.intake_dispatch_fleet(
            ticket, trigger, {"decision": "accept"}, policy=fleet_policy(),
            data_dir=str(self.data),
            fleet_runner=fake_fleet_runner(summary={
                "id": "20261117-090000-e2", "state": "finished", "cards": [
                    {"card": "01-task.md", "runner": "local", "status": "done",
                     "run_id": "fleet-run-2", "summary": "ok"},
                    {"card": "01-task.md", "runner": None, "status": "blocked",
                     "run_id": None, "blocked_reason": "health gate",
                     "health_gate": {"verdict": "unhealthy"},
                     "summary": "blocked"},
                ]}, fleet_id="20261117-090000-e2"))
        return xs.intake_ledger_entries(str(self.data))

    def test_good_record_passes(self):
        result = xs.intake_m30_slice2_evidence_check(self._good_entries())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["failures"], [])

    def test_only_one_batch_run_fails(self):
        entries = [e for e in self._good_entries()
                   if not (e.get("kind") == "batch_dispatch_finished"
                           and e.get("batch_id", "").endswith("b1"))]
        result = xs.intake_m30_slice2_evidence_check(entries)
        self.assertFalse(result["ok"])
        self.assertIn("live_batch_runs_at_least_2", result["failures"])

    def test_only_one_fleet_run_fails(self):
        entries = self._good_entries()
        for e in entries:
            if e.get("kind") == "intake_fleet_card_result" and e.get("run_id") == "fleet-run-2":
                e["run_id"] = "fleet-run-1"
        result = xs.intake_m30_slice2_evidence_check(entries)
        self.assertFalse(result["ok"])
        self.assertIn("live_fleet_runs_at_least_2", result["failures"])

    def test_no_blocked_fails(self):
        entries = self._good_entries()
        for e in entries:
            if e.get("kind") == "intake_fleet_card_result":
                e["blocked"] = False
        result = xs.intake_m30_slice2_evidence_check(entries)
        self.assertFalse(result["ok"])
        self.assertIn("live_blocked_at_least_1", result["failures"])

    def test_blocked_card_with_run_id_fails(self):
        entries = self._good_entries()
        for e in entries:
            if e.get("kind") == "intake_fleet_card_result" and e.get("blocked"):
                e["run_id"] = "run-should-not-exist"
        result = xs.intake_m30_slice2_evidence_check(entries)
        self.assertFalse(result["ok"])
        self.assertIn("fleet_blocked_card_no_exec", result["failures"])

    def test_missing_card_result_fails(self):
        entries = [e for e in self._good_entries()
                   if e.get("kind") != "intake_fleet_card_result"]
        result = xs.intake_m30_slice2_evidence_check(entries)
        self.assertFalse(result["ok"])
        self.assertIn("fleet_card_result_per_card", result["failures"])

    def test_started_without_finish_fails(self):
        entries = [e for e in self._good_entries()
                   if e.get("kind") != "fleet_dispatch_finished"]
        result = xs.intake_m30_slice2_evidence_check(entries)
        self.assertFalse(result["ok"])
        self.assertIn("fleet_started_without_finish", result["failures"])

    def test_empty_record_fails(self):
        result = xs.intake_m30_slice2_evidence_check([])
        self.assertFalse(result["ok"])
        self.assertIn("fleet_cards_created", result["failures"])


if __name__ == "__main__":
    unittest.main()
