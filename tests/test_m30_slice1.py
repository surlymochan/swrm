"""M30 slice 1 (#247): batch-path intake core + guardrails.

Covers: policy.dispatch.mode ("run" default, "batch" opt-in, "fleet"
refused until slice 2); Mandate -> batch card generation (checklist
items chained in order, single-card fallback, checked items skipped,
empty body / over-cap refused, no overwrite of dispatched cards);
intake_dispatch_batch (one Assignment, executor "batch", ledger kinds
in order, per-card results mirrored, idempotent second call refused,
runner failure ends the Assignment); ingest wiring (batch mode
dispatches via the batch runner and never via the single-run runner,
missing batch runner stays Ticket-only, out-of-scope stays shadow,
caps and the health gate block before any Assignment, an explicit
--backend is gated exactly like a routed pick); the M30 slice-1
evidence checker; and the default runner refusing cleanly when no
repo checkout can be resolved.

Run: python3 -m unittest tests.test_m30_slice1
     (or: python3 tests/test_m30_slice1.py)
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


def fake_batch_runner(summary=None, batch_id="20261117-030000-aaaaaa"):
    calls = []

    def runner(ticket, trigger, decision, policy, data_dir, cards_dir, cwd):
        calls.append((ticket["id"], trigger["id"], cards_dir))
        cards = sorted(os.listdir(cards_dir)) if os.path.isdir(cards_dir) else []
        if summary is None:
            rows = [{"card": name, "status": "done", "run_id": f"run-{i}",
                     "branch": f"intake/{ticket['id']}", "commit": "abc1234",
                     "summary": "did the thing"}
                    for i, name in enumerate(cards)]
            return {"batch_id": batch_id, "status": "finished",
                    "summary": {"id": batch_id, "state": "finished", "cards": rows}}
        return {"batch_id": batch_id, "status": summary.get("state", "finished"),
                "summary": summary}
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


def batch_policy(**dispatch_overrides):
    dispatch = {"mode": "batch"}
    dispatch.update(dispatch_overrides)
    return active_policy(dispatch=dispatch)


class M30Slice1Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m30s1-")
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


class DispatchModeTests(M30Slice1Base):
    def test_default_mode_is_run(self):
        policy = xs.default_intake_policy()
        self.assertNotIn("mode", policy.get("dispatch") or {})
        self.assertEqual(xs.intake_dispatch_config(policy)["mode"], "run")

    def test_batch_mode_validates(self):
        policy = xs.default_intake_policy()
        policy["dispatch"] = {"mode": "batch"}
        xs.validate_intake_policy(policy)
        self.assertEqual(xs.intake_dispatch_config(policy)["mode"], "batch")

    def test_fleet_mode_validates_slice_2(self):
        policy = xs.default_intake_policy()
        policy["dispatch"] = {"mode": "fleet"}
        xs.validate_intake_policy(policy)
        self.assertEqual(xs.intake_dispatch_config(policy)["mode"], "fleet")

    def test_bad_mode_refused(self):
        for bad in ("Fleet", "BATCH", "", 3, None, ["batch"]):
            policy = xs.default_intake_policy()
            policy["dispatch"] = {"mode": bad}
            with self.assertRaises(ValueError, msg=f"mode={bad!r}"):
                xs.validate_intake_policy(policy)

    def test_policy_show_prints_mode(self):
        policy = batch_policy()
        with open(self.data / "intake-policy.json", "w") as f:
            json.dump(policy, f)
        args = argparse.Namespace(init=False, json=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            xs.cmd_intake_policy(args, data_dir=str(self.data))
        self.assertIn("dispatch mode=batch", buf.getvalue())


class CardGenerationTests(M30Slice1Base):
    def test_single_card_without_checklist(self):
        ticket, _ = self.ticket_for(trig("c1"))
        cards = xs.intake_batch_cards(ticket, trig("c1"))
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["name"], "01-task.md")
        self.assertEqual(cards[0]["depends_on"], [])
        self.assertIn("fix the flaky test", cards[0]["text"])
        self.assertIn("tkt-", cards[0]["text"])

    def test_checklist_items_chain_in_order(self):
        trigger = trig("c2", body=checklist_body(3))
        ticket, _ = self.ticket_for(trigger)
        cards = xs.intake_batch_cards(ticket, trigger)
        self.assertEqual([c["name"] for c in cards],
                         ["01-fix-the-flaky-test-number-1.md",
                          "02-fix-the-flaky-test-number-2.md",
                          "03-fix-the-flaky-test-number-3.md"])
        self.assertEqual(cards[0]["depends_on"], [])
        self.assertEqual(cards[1]["depends_on"], [cards[0]["name"]])
        self.assertEqual(cards[2]["depends_on"], [cards[1]["name"]])
        for i, card in enumerate(cards):
            self.assertIn(f"fix the flaky test number {i + 1}", card["text"])
            self.assertIn(f"card {i + 1} of 3", card["text"])
        # The generated cards validate as a batch plan in the same order.
        cards_dir = self.data / "cards"
        xs.intake_write_batch_cards(ticket, trigger, str(cards_dir))
        plan = xs.batch_plan(str(cards_dir))
        self.assertEqual(plan["order"], [c["name"] for c in cards])

    def test_checked_items_are_skipped(self):
        trigger = trig("c3", body=checklist_body(2, checked=1))
        ticket, _ = self.ticket_for(trigger)
        cards = xs.intake_batch_cards(ticket, trigger)
        self.assertEqual(len(cards), 2)
        # The checked item is never a card step (the full body stays in
        # each card as executor context, so only the step lines count).
        steps = [line for c in cards for line in c["text"].splitlines()
                 if line.startswith("Task step")]
        self.assertEqual(len(steps), 2)
        self.assertNotIn("already done step", "\n".join(steps))

    def test_duplicate_slugs_are_disambiguated(self):
        body = "@openswarm do it twice:\n- [ ] same step\n- [ ] same step\n"
        trigger = trig("c4", body=body)
        ticket, _ = self.ticket_for(trigger)
        cards = xs.intake_batch_cards(ticket, trigger)
        self.assertEqual(len({c["name"] for c in cards}), 2)
        self.assertEqual(cards[1]["depends_on"], [cards[0]["name"]])

    def test_empty_body_refused(self):
        trigger = trig("c5", body="   ")
        ticket, _ = self.ticket_for(trigger)
        with self.assertRaises(ValueError):
            xs.intake_batch_cards(ticket, trigger)

    def test_over_cap_refused(self):
        body = "@openswarm many steps:\n" + "".join(
            f"- [ ] step {i}\n" for i in range(11))
        trigger = trig("c6", body=body)
        ticket, _ = self.ticket_for(trigger)
        with self.assertRaises(ValueError):
            xs.intake_batch_cards(ticket, trigger)
        body10 = "@openswarm ten steps:\n" + "".join(
            f"- [ ] step {i}\n" for i in range(10))
        self.assertEqual(len(xs.intake_batch_cards(
            ticket, trig("c6b", body=body10))), 10)

    def test_write_refuses_to_overwrite(self):
        trigger = trig("c7", body=checklist_body(2))
        ticket, _ = self.ticket_for(trigger)
        cards_dir = self.data / "cards7"
        xs.intake_write_batch_cards(ticket, trigger, str(cards_dir))
        self.assertEqual(sorted(os.listdir(cards_dir)), [c["name"] for c in
                         xs.intake_batch_cards(ticket, trigger)])
        with self.assertRaises(ValueError):
            xs.intake_write_batch_cards(ticket, trigger, str(cards_dir))


class BatchDispatchTests(M30Slice1Base):
    def _dispatch(self, trigger, runner=None, policy=None):
        policy = policy or batch_policy()
        ticket, _ = self.ticket_for(trigger)
        ticket = dict(ticket)
        # new_intake_ticket needs a decision-shaped dict; the helper
        # above seals the ticket the same way ingest does.
        result = xs.intake_dispatch_batch(
            ticket, trigger, {"decision": "accept"}, policy=policy,
            data_dir=str(self.data), batch_runner=runner or fake_batch_runner())
        return ticket, result

    def test_happy_path_writes_cards_and_ledger(self):
        trigger = trig("d1", body=checklist_body(2))
        ticket, result = self._dispatch(trigger)
        self.assertEqual(result["batch_id"], "20261117-030000-aaaaaa")
        self.assertEqual(result["status"], "finished")
        state = self.state()
        self.assertEqual(len(state["assignments"]), 1)
        self.assertEqual(state["assignments"][0]["executor"], "batch")
        self.assertEqual(state["assignments"][0]["status"], "ended")
        cards_dir = (self.data / "intake-batches" / ticket["id"] / "cards")
        self.assertEqual(sorted(os.listdir(cards_dir)), [
            "01-fix-the-flaky-test-number-1.md",
            "02-fix-the-flaky-test-number-2.md"])
        kinds = self.ledger_kinds()
        for kind in ("assignment_created", "intake_batch_created",
                     "batch_dispatch_started", "batch_dispatch_finished"):
            self.assertIn(kind, kinds)
        created = self.ledger_entries("intake_batch_created")[0]
        self.assertEqual(created["order"], created["cards"])
        card_results = self.ledger_entries("intake_batch_card_result")
        self.assertEqual(len(card_results), 2)
        self.assertTrue(all(r["status"] == "done" for r in card_results))
        self.assertTrue(all(r["run_id"] for r in card_results))
        # Assignment order in the ledger: created before started.
        self.assertLess(kinds.index("assignment_created"),
                        kinds.index("batch_dispatch_started"))
        self.assertLess(kinds.index("batch_dispatch_started"),
                        kinds.index("batch_dispatch_finished"))

    def test_second_dispatch_refused_idempotent(self):
        trigger = trig("d2", body=checklist_body(2))
        ticket, _ = self._dispatch(trigger)
        runner = fake_batch_runner()
        with self.assertRaises(ValueError):
            xs.intake_dispatch_batch(
                ticket, trigger, {"decision": "accept"},
                policy=batch_policy(), data_dir=str(self.data),
                batch_runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(len(self.ledger_entries("assignment_created")), 1)
        self.assertEqual(len(self.ledger_entries("batch_dispatch_started")), 1)

    def test_runner_failure_ends_assignment(self):
        def failing(ticket, trigger, decision, policy, data_dir, cards_dir, cwd):
            raise ValueError("fleet is down")
        trigger = trig("d3", body=checklist_body(1))
        ticket, _ = self.ticket_for(trigger)
        with self.assertRaises(ValueError):
            xs.intake_dispatch_batch(
                ticket, trigger, {"decision": "accept"},
                policy=batch_policy(), data_dir=str(self.data),
                batch_runner=failing)
        self.assertEqual(self.state()["assignments"][0]["status"], "ended")
        failed = self.ledger_entries("batch_dispatch_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("fleet is down", failed[0]["error"])
        # Cards are still on disk even though the runner failed.
        cards_dir = self.data / "intake-batches" / ticket["id"] / "cards"
        self.assertEqual(len(os.listdir(cards_dir)), 1)

    def test_runner_without_batch_id_refused(self):
        def no_id(ticket, trigger, decision, policy, data_dir, cards_dir, cwd):
            return {"status": "finished"}
        trigger = trig("d4", body=checklist_body(1))
        ticket, _ = self.ticket_for(trigger)
        with self.assertRaises(ValueError):
            xs.intake_dispatch_batch(
                ticket, trigger, {"decision": "accept"},
                policy=batch_policy(), data_dir=str(self.data),
                batch_runner=no_id)
        self.assertEqual(len(self.ledger_entries("batch_dispatch_failed")), 1)

    def test_non_batch_mode_refused(self):
        trigger = trig("d5", body=checklist_body(1))
        ticket, _ = self.ticket_for(trigger)
        with self.assertRaises(ValueError):
            xs.intake_dispatch_batch(
                ticket, trigger, {"decision": "accept"},
                policy=xs.default_intake_policy(), data_dir=str(self.data),
                batch_runner=fake_batch_runner())

    def test_blocked_card_recorded_with_health_gate(self):
        summary = {"id": "20261117-030000-bbbbbb", "state": "finished", "cards": [
            {"card": "01-fix-the-flaky-test-number-1.md", "status": "done",
             "run_id": "run-0", "branch": "b1", "commit": "abc1234", "summary": "ok"},
            {"card": "02-fix-the-flaky-test-number-2.md", "status": "blocked",
             "run_id": None, "blocked_reason": "health gate: codex login expired",
             "health_gate": {"verdict": "unhealthy", "backend": "codex",
                             "reason": "codex login expired"},
             "summary": "blocked by health gate"},
        ]}
        trigger = trig("d6", body=checklist_body(2))
        runner = fake_batch_runner(summary=summary,
                                   batch_id="20261117-030000-bbbbbb")
        ticket, result = self._dispatch(trigger, runner=runner)
        self.assertEqual(result["batch_id"], "20261117-030000-bbbbbb")
        rows = {r["card"]: r for r in self.ledger_entries("intake_batch_card_result")}
        blocked = rows["02-fix-the-flaky-test-number-2.md"]
        self.assertTrue(blocked["blocked"])
        self.assertIsNone(blocked["run_id"])
        self.assertEqual(blocked["health_gate"]["verdict"], "unhealthy")
        self.assertIn("health gate", blocked["reason"])
        self.assertFalse(rows["01-fix-the-flaky-test-number-1.md"]["blocked"])

    def test_card_outcomes_pure_function(self):
        self.assertEqual(xs.intake_batch_card_outcomes(None), [])
        self.assertEqual(xs.intake_batch_card_outcomes({}), [])
        rows = xs.intake_batch_card_outcomes({"cards": [
            {"card": "a.md", "status": "blocked", "run_id": None,
             "blocked_reason": "dep failed", "health_gate": None},
            {"card": "b.md", "status": "done", "run_id": "r1", "summary": "ok"},
        ]})
        self.assertTrue(rows[0]["blocked"])
        self.assertEqual(rows[0]["reason"], "dep failed")
        self.assertFalse(rows[1]["blocked"])

    def test_default_runner_refuses_without_checkout(self):
        trigger = trig("d7", body=checklist_body(1))
        ticket, _ = self.ticket_for(trigger)
        plain = pathlib.Path(self.tmp.name) / "not-a-repo"
        plain.mkdir()
        with self.assertRaises(ValueError) as ctx:
            xs.run_openswarm_batch(ticket, trigger, {"decision": "accept"},
                                   batch_policy(), str(self.data),
                                   str(self.data / "cards"), cwd=str(plain))
        self.assertIn("base commit", str(ctx.exception))


class IngestWiringTests(M30Slice1Base):
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

    def test_batch_mode_dispatches_via_batch_runner(self):
        # M32 (#259): shipped default is now health-gate on; this test pins
        # dispatch wiring with a fake runner, so pin the gate off explicitly.
        self._gate_env(enabled=False)
        policy = batch_policy()
        runner = fake_batch_runner()
        single = fake_runner()
        result = self.ingest(trig("w1", body=checklist_body(2)), policy=policy,
                             dispatch_runner=single, batch_runner=runner,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["dispatch_mode"], "batch")
        self.assertEqual(result["dispatch"]["batch_id"], "20261117-030000-aaaaaa")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(single.calls, [])
        self.assertIn("batch_dispatch_started", self.ledger_kinds())
        self.assertNotIn("dispatch_started", self.ledger_kinds())
        self.assertIn("channel_note", result)

    def test_batch_mode_without_batch_runner_stays_ticket_only(self):
        policy = batch_policy()
        single = fake_runner()
        result = self.ingest(trig("w2", body=checklist_body(2)), policy=policy,
                             dispatch_runner=single, batch_runner=None,
                             channel_poster=fake_channel())
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertIn("no dispatch runner", result["dispatch_note"])
        self.assertEqual(single.calls, [])
        self.assertEqual(self.state()["assignments"], [])

    def test_run_mode_ignores_batch_runner(self):
        # M32 (#259): shipped default is now health-gate on; this test pins
        # dispatch wiring with a fake runner, so pin the gate off explicitly.
        self._gate_env(enabled=False)
        policy = active_policy()
        single = fake_runner()
        batch = fake_batch_runner()
        result = self.ingest(trig("w3"), policy=policy,
                             dispatch_runner=single, batch_runner=batch,
                             channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["dispatch_mode"], "run")
        self.assertEqual(len(single.calls), 1)
        self.assertEqual(batch.calls, [])
        self.assertIn("dispatch_started", self.ledger_kinds())
        self.assertNotIn("batch_dispatch_started", self.ledger_kinds())

    def test_out_of_scope_stays_shadow(self):
        policy = batch_policy(repos=("surlymochan/swrm",))
        runner = fake_batch_runner()
        result = self.ingest(trig("w4", repo="other/repo", number=9,
                                  body=checklist_body(1)),
                             policy=policy, batch_runner=runner,
                             channel_poster=fake_channel())
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(runner.calls, [])
        self.assertIn("active_scope_shadow", self.ledger_kinds())

    def test_caps_block_batch_before_assignment(self):
        # M32 (#259): shipped default is now health-gate on; this test pins
        # dispatch wiring with a fake runner, so pin the gate off explicitly.
        self._gate_env(enabled=False)
        policy = batch_policy()
        policy["caps"] = {"max_dispatches_per_thread": 1,
                          "max_budget_minutes_per_thread": 10}
        runner = fake_batch_runner()
        first = self.ingest(trig("w5a", body=checklist_body(1)), policy=policy,
                            batch_runner=runner, channel_poster=fake_channel())
        self.assertTrue(first["dispatched"])
        second = self.ingest(trig("w5b", body="@openswarm fix another flaky test"),
                             policy=policy, batch_runner=runner,
                             channel_poster=fake_channel())
        self.assertFalse(second["dispatched"])
        self.assertTrue(second["cap_blocked"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(self.state()["assignments"]), 1)

    def test_draft_only_never_dispatches_batch(self):
        policy = batch_policy()
        runner = fake_batch_runner()
        body = ("@openswarm please pay $50 to the contributor who fixes "
                "the flaky test")
        result = self.ingest(trig("w6", body=body), policy=policy,
                             batch_runner=runner, channel_poster=fake_channel())
        self.assertFalse(result["dispatched"])
        self.assertEqual(runner.calls, [])

    def test_health_gate_blocks_batch_before_assignment(self):
        self._gate_env(enabled=True, binary=None, login_status="unknown")
        policy = batch_policy(backend="codex")
        batch = fake_batch_runner()
        single = fake_runner()
        result = self.ingest(trig("w7", body=checklist_body(2)), policy=policy,
                             dispatch_runner=single, batch_runner=batch,
                             channel_poster=fake_channel())
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertTrue(result["health_gate_blocked"])
        self.assertEqual(result["health_gate"]["verdict"], "unhealthy")
        # Explicit --backend does not exempt the gate: no runner ran.
        self.assertEqual(batch.calls, [])
        self.assertEqual(single.calls, [])
        self.assertEqual(self.state()["assignments"], [])
        blocked = self.ledger_entries("intake_health_gate_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["health_gate"]["backend"], "codex")

    def test_healthy_backend_dispatches_batch(self):
        self._gate_env(enabled=True, binary=sys.executable, login_status="pass")
        policy = batch_policy(backend="codex")
        batch = fake_batch_runner()
        result = self.ingest(trig("w8", body=checklist_body(1)), policy=policy,
                             batch_runner=batch, channel_poster=fake_channel())
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["health_gate"]["verdict"], "healthy")
        self.assertEqual(len(batch.calls), 1)


class EvidenceCheckTests(M30Slice1Base):
    def _good_entries(self):
        trigger = trig("e1", body=checklist_body(2))
        ticket, _ = self.ticket_for(trigger)
        xs.intake_dispatch_batch(
            ticket, trigger, {"decision": "accept"}, policy=batch_policy(),
            data_dir=str(self.data), batch_runner=fake_batch_runner())
        return xs.intake_ledger_entries(str(self.data))

    def test_good_record_passes(self):
        result = xs.intake_m30_slice1_evidence_check(self._good_entries())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["failures"], [])

    def test_duplicate_assignment_fails(self):
        entries = self._good_entries()
        entries.append(dict(entries[0], kind="assignment_created",
                            ticket_id=entries[0].get("ticket_id")))
        result = xs.intake_m30_slice1_evidence_check(entries)
        self.assertFalse(result["ok"])
        self.assertIn("one_assignment_per_ticket", result["failures"])

    def test_blocked_card_with_run_id_fails(self):
        entries = self._good_entries()
        for e in entries:
            if e.get("kind") == "intake_batch_card_result":
                e["blocked"] = True
                e["run_id"] = "run-should-not-exist"
        result = xs.intake_m30_slice1_evidence_check(entries)
        self.assertFalse(result["ok"])
        self.assertIn("blocked_card_no_exec", result["failures"])

    def test_missing_card_result_fails(self):
        entries = [e for e in self._good_entries()
                   if e.get("kind") != "intake_batch_card_result"]
        result = xs.intake_m30_slice1_evidence_check(entries)
        self.assertFalse(result["ok"])
        self.assertIn("card_result_per_card", result["failures"])

    def test_started_without_finish_fails(self):
        entries = [e for e in self._good_entries()
                   if e.get("kind") != "batch_dispatch_finished"]
        result = xs.intake_m30_slice1_evidence_check(entries)
        self.assertFalse(result["ok"])
        self.assertIn("batch_started_without_finish", result["failures"])

    def test_empty_record_fails(self):
        result = xs.intake_m30_slice1_evidence_check([])
        self.assertFalse(result["ok"])
        self.assertIn("batch_cards_created", result["failures"])


if __name__ == "__main__":
    unittest.main()
