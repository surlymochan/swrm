"""M27 slice 3 (#228): Subscription resume / stop + attention CLI.

Covers: the tightened standalone-stop rule (a quoted or tabulated
`@openswarm stop` is a report of a stop, not a stop), resume of an
ended Ticket under its existing Mandate (no new Eval, no new Ticket,
no new Mandate, no dispatch), human stop withdrawing the Mandate (and
blocking any later resume), operator close on verify evidence
(Mandate stays active), and the read-only `attention` CLI.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _load

xs = _load.load()
HERE = pathlib.Path(__file__).resolve().parent


def trig(trigger_id, author="surlymochan", body="please fix the flaky test in fleet dispatch",
         kind="issue", number=1, bot=False, repo="surlymochan/swrm"):
    return {"id": trigger_id, "source": "github",
            "thread": {"repo": repo, "kind": kind, "number": number},
            "author": author, "author_is_bot": bot, "body": body,
            "created_at": "2026-11-15T03:00:00Z"}


class M27Slice3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m27s3-")
        self.data = pathlib.Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.patch = mock.patch.object(xs, "DATA", str(self.data))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.policy = xs.default_intake_policy()

    def ingest(self, trigger, policy=None, **kw):
        return xs.intake_ingest_trigger(trigger, policy=policy or self.policy,
                                        data_dir=str(self.data), **kw)

    def state(self):
        return xs.load_intake_state(str(self.data))

    def ledger_kinds(self):
        return [e["kind"] for e in xs.intake_ledger_entries(str(self.data))]

    def decision_count(self):
        return len(xs.intake_ledger_entries(str(self.data), kind="decision"))

    def open_ticket(self, trigger_id="t1", **kw):
        result = self.ingest(trig(trigger_id, **kw))
        self.assertIsNotNone(result["ticket_id"])
        return result

    # -- Tightened stop rule --------------------------------------------

    def test_table_quoted_stop_is_not_a_stop(self):
        # Slice 2 real-data case: an evidence table quoting
        # `@openswarm stop` (gh comment 6878371088) stopped work.
        body = ("| case | expected |\n| --- | --- |\n"
                "| resume | `@openswarm stop` ends the ticket |\n"
                "please fix the flaky test in fleet dispatch")
        gate = xs.intake_gate_decide(trig("s1", body=body), self.policy)
        self.assertEqual(gate["decision"], "accept")

    def test_fenced_code_stop_is_not_a_stop(self):
        body = ("```\n@openswarm stop\n```\n"
                "please fix the flaky test in fleet dispatch")
        gate = xs.intake_gate_decide(trig("s2", body=body), self.policy)
        self.assertEqual(gate["decision"], "accept")

    def test_quoted_stop_line_is_not_a_stop(self):
        body = ("> @openswarm stop, quoted from the earlier thread\n"
                "please fix the flaky test in fleet dispatch")
        gate = xs.intake_gate_decide(trig("s3", body=body), self.policy)
        self.assertEqual(gate["decision"], "accept")

    def test_mid_sentence_stop_is_not_a_stop(self):
        gate = xs.intake_gate_decide(
            trig("s4", body="please @openswarm stop the task when ready"), self.policy)
        self.assertEqual(gate["decision"], "accept")

    def test_standalone_stop_still_stops(self):
        for body in ("@openswarm stop，别做了", "@openswarm stop, do not do this",
                     "stop", "  stop everything\nplease end it"):
            gate = xs.intake_gate_decide(trig("s5", body=body), self.policy)
            self.assertEqual(gate["decision"], "stop", body)

    def test_is_human_stop_direct(self):
        self.assertTrue(xs.intake_is_human_stop("@openswarm stop"))
        self.assertTrue(xs.intake_is_human_stop("first line\nstop\nlast line"))
        self.assertFalse(xs.intake_is_human_stop("| `@openswarm stop` |"))
        self.assertFalse(xs.intake_is_human_stop("```\nstop\n```"))

    # -- Resume -----------------------------------------------------------

    def test_human_followup_resumes_ended_ticket_under_same_mandate(self):
        first = self.open_ticket("r1")
        ticket_id = first["ticket_id"]
        mandate_id = first["mandate_id"]
        mandate_before = dict(self.state()["mandates"][0])
        xs.intake_close_ticket(ticket_id, reason="verify passed: run done",
                               evidence="runs/20261115-030000-abcdef/result.json",
                               data_dir=str(self.data), now=2000.0)
        self.assertEqual(self.state()["tickets"][0]["status"], "ended")
        decisions_before = self.decision_count()
        result = self.ingest(
            trig("r2", body="@openswarm please also record the follow-up outcome "
                            "in the ticket history"),
            now=3000.0)
        self.assertTrue(result.get("resumed"))
        self.assertEqual(result["ticket_id"], ticket_id)
        self.assertEqual(result["mandate_id"], mandate_id)
        self.assertFalse(result["dispatched"])
        state = self.state()
        self.assertEqual(len(state["tickets"]), 1)  # no second Ticket
        self.assertEqual(len(state["mandates"]), 1)  # no second Mandate
        self.assertEqual(state["tickets"][0]["status"], "open")
        mandate_after = state["mandates"][0]
        for key in ("policy_version", "scope", "strength", "rules", "source_identity"):
            self.assertEqual(mandate_after[key], mandate_before[key],
                             f"mandate {key} must not change on resume")
        self.assertEqual(mandate_after["status"], "active")
        kinds = self.ledger_kinds()
        self.assertIn("subscription_followup", kinds)
        self.assertIn("ticket_resumed", kinds)
        # Resume runs no new Eval: no fresh Decision entry.
        self.assertEqual(self.decision_count(), decisions_before)
        followup = [e for e in xs.intake_ledger_entries(str(self.data))
                    if e["kind"] == "subscription_followup"][0]
        self.assertEqual(followup["ticket_id"], ticket_id)
        self.assertEqual(followup["mandate_id"], mandate_id)
        self.assertEqual(followup["policy_version"], 1)
        self.assertIn("follow-up-actionable", followup["rules_checked"])
        self.assertIn("follow-up outcome", followup["body_excerpt"])

    def test_greeting_followup_does_not_resume(self):
        first = self.open_ticket("g1")
        xs.intake_close_ticket(first["ticket_id"], data_dir=str(self.data), now=2000.0)
        result = self.ingest(trig("g2", body="thanks!"), now=3000.0)
        self.assertFalse(result.get("resumed", False))
        self.assertEqual(result["decision"]["decision"], "ignore")
        self.assertEqual(self.state()["tickets"][0]["status"], "ended")

    def test_bot_followup_does_not_resume(self):
        first = self.open_ticket("b1")
        xs.intake_close_ticket(first["ticket_id"], data_dir=str(self.data), now=2000.0)
        result = self.ingest(trig("b2", author="ci-bot[bot]", bot=True,
                                  body="@openswarm please continue the work"),
                             now=3000.0)
        self.assertEqual(result["decision"]["decision"], "ignore")
        self.assertEqual(self.state()["tickets"][0]["status"], "ended")
        self.assertNotIn("ticket_resumed", self.ledger_kinds())

    def test_resume_check_refuses_delisted_author(self):
        first = self.open_ticket("d1")
        xs.intake_close_ticket(first["ticket_id"], data_dir=str(self.data), now=2000.0)
        state = self.state()
        ticket, mandate = state["tickets"][0], state["mandates"][0]
        policy = xs.default_intake_policy()
        policy["allowlist"]["users"] = []
        ok, reason, _rules = xs.intake_resume_check(
            ticket, mandate, trig("d2", body="@openswarm please continue the task"),
            policy)
        self.assertFalse(ok)
        self.assertIn("no longer on the intake allowlist", reason)

    def test_followup_on_open_ticket_takes_normal_path(self):
        # Conservative by design: with live work on the thread, a new
        # distinct task opens a new Ticket (scope grows only via a new
        # Ticket); only an *ended* Ticket is resumed.
        self.open_ticket("o1")
        result = self.ingest(trig("o2", body="please also update the docs for fleet dispatch"))
        self.assertFalse(result.get("resumed", False))
        self.assertIsNotNone(result["ticket_id"])
        self.assertEqual(len(self.state()["tickets"]), 2)

    # -- Stop withdraws the Mandate ---------------------------------------

    def test_stop_withdraws_mandate_and_blocks_resume(self):
        first = self.open_ticket("w1")
        ticket_id = first["ticket_id"]
        result = self.ingest(trig("w2", body="@openswarm stop，别做了"), now=2000.0)
        self.assertTrue(result["stopped"])
        self.assertTrue(result["mandate_withdrawn"])
        state = self.state()
        self.assertEqual(state["tickets"][0]["status"], "ended")
        self.assertEqual(state["mandates"][0]["status"], "withdrawn")
        self.assertIsNotNone(state["mandates"][0]["withdrawn_ts"])
        kinds = self.ledger_kinds()
        self.assertIn("ticket_ended", kinds)
        self.assertIn("mandate_withdrawn", kinds)
        withdrawn = [e for e in xs.intake_ledger_entries(str(self.data))
                     if e["kind"] == "mandate_withdrawn"][0]
        self.assertEqual(withdrawn["ticket_id"], ticket_id)
        # A later follow-up cannot resume: it opens a new Ticket instead.
        followup = self.ingest(
            trig("w3", body="@openswarm please continue the task after all"), now=3000.0)
        self.assertFalse(followup.get("resumed", False))
        self.assertNotEqual(followup["ticket_id"], ticket_id)
        state = self.state()
        self.assertEqual(len(state["tickets"]), 2)
        self.assertEqual(state["tickets"][0]["status"], "ended")
        self.assertEqual(state["tickets"][1]["status"], "open")
        self.assertEqual(self.ledger_kinds().count("mandate_withdrawn"), 1)

    def test_second_stop_does_not_rewithdraw(self):
        self.open_ticket("v1")
        self.ingest(trig("v2", body="@openswarm stop"), now=2000.0)
        result = self.ingest(trig("v3", body="@openswarm stop"), now=3000.0)
        self.assertFalse(result["stopped"])
        self.assertEqual(self.ledger_kinds().count("mandate_withdrawn"), 1)

    def test_stop_on_ended_only_thread_withdraws_mandate(self):
        first = self.open_ticket("e1")
        xs.intake_close_ticket(first["ticket_id"], data_dir=str(self.data), now=2000.0)
        result = self.ingest(trig("e2", body="@openswarm stop"), now=3000.0)
        self.assertTrue(result["stopped"])
        self.assertTrue(result["mandate_withdrawn"])
        self.assertEqual(result["ticket_id"], first["ticket_id"])
        self.assertEqual(self.state()["mandates"][0]["status"], "withdrawn")

    def test_withdraw_twice_is_refused_and_sealed_mandate_stays_frozen(self):
        first = self.open_ticket("f1")
        mandate = self.state()["mandates"][0]
        withdrawn = xs.withdraw_intake_mandate(mandate, reason="human stop", now=2000.0)
        self.assertEqual(withdrawn["status"], "withdrawn")
        with self.assertRaises(ValueError):
            xs.withdraw_intake_mandate(withdrawn, reason="again")
        with self.assertRaises(ValueError):
            xs.intake_update_mandate(withdrawn, scope={"thread": "other"})
        # Mandates sealed before slice 3 carry no status field: active.
        self.assertEqual(xs.intake_mandate_status({}), "active")
        self.assertEqual(xs.intake_mandate_status({"status": "withdrawn"}), "withdrawn")

    # -- Operator close -----------------------------------------------------

    def test_close_keeps_mandate_active_then_followup_resumes(self):
        first = self.open_ticket("c1")
        closed = xs.intake_close_ticket(
            first["ticket_id"], reason="verify passed", evidence="runs/x/result.json",
            data_dir=str(self.data), now=2000.0)
        self.assertEqual(closed["status"], "ended")
        self.assertEqual(self.state()["mandates"][0]["status"], "active")
        ended = [e for e in xs.intake_ledger_entries(str(self.data))
                 if e["kind"] == "ticket_ended"][0]
        self.assertEqual(ended["via"], "close")
        self.assertEqual(ended["evidence"], "runs/x/result.json")
        result = self.ingest(
            trig("c2", body="@openswarm please also note the close evidence was verified"),
            now=3000.0)
        self.assertTrue(result.get("resumed"))

    def test_close_refuses_ended_or_unknown_ticket(self):
        first = self.open_ticket("c3")
        xs.intake_close_ticket(first["ticket_id"], data_dir=str(self.data), now=2000.0)
        with self.assertRaises(ValueError):
            xs.intake_close_ticket(first["ticket_id"], data_dir=str(self.data), now=3000.0)
        with self.assertRaises(ValueError):
            xs.intake_close_ticket("tkt-doesnotexist", data_dir=str(self.data))

    # -- Attention ----------------------------------------------------------

    def test_attention_buckets_and_stale(self):
        old = self.open_ticket("a1")
        # Backdate the old Ticket past the 7-day stale line.
        state = self.state()
        state["tickets"][0]["created_ts"] = 1000.0
        xs.save_intake_state(state, str(self.data))
        fresh = self.open_ticket("a2", number=2)
        xs.intake_close_ticket(fresh["ticket_id"], reason="verify passed",
                               data_dir=str(self.data), now=2000.0)
        buckets = xs.intake_attention_tickets(data_dir=str(self.data), now=606000.0,
                                              stale_days=7)
        # now=606000 -> cutoff 1200: old ticket (created 1000) is stale,
        # the ticket ended at 2000 is not.
        self.assertEqual(buckets["counts"], {"open": 1, "ended_unreviewed": 1, "stale": 1})
        self.assertEqual([r["ticket_id"] for r in buckets["open"]],
                         [old["ticket_id"]])
        self.assertEqual([r["ticket_id"] for r in buckets["ended_unreviewed"]],
                         [fresh["ticket_id"]])
        self.assertEqual([r["ticket_id"] for r in buckets["stale"]],
                         [old["ticket_id"]])
        self.assertEqual(buckets["stale"][0]["mandate_status"], "active")
        self.assertIn("open for over 7d", buckets["stale"][0]["stale_reason"])
        self.assertIsNone(buckets["open"][0]["latest_assignment_status"])
        self.assertEqual(buckets["open"][0]["assignments"], 0)

    def test_attention_is_read_only(self):
        self.open_ticket("r0")
        state_path = self.data / "intake-state.json"
        ledger_path = self.data / "intake-ledger.jsonl"
        before_state = state_path.read_bytes()
        before_ledger = ledger_path.read_bytes()
        args = type("A", (), {"json": False, "stale_days": 7})()
        xs.cmd_attention(args, data_dir=str(self.data))
        self.assertEqual(state_path.read_bytes(), before_state)
        self.assertEqual(ledger_path.read_bytes(), before_ledger)

    # -- CLI smoke ------------------------------------------------------------

    def _run_cli(self, argv, env_extra=None):
        env = dict(os.environ)
        env["OPENSWARM_HOME"] = str(self.data)
        if env_extra:
            env.update(env_extra)
        return subprocess.run([sys.executable, str(HERE.parent / "bin" / "openswarm"), *argv],
                              capture_output=True, text=True, encoding="utf-8", env=env)

    def test_cli_attention_and_close(self):
        first = self.open_ticket("k1")
        proc = self._run_cli(["attention", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(out["counts"]["open"], 1)
        self.assertEqual(out["counts"]["ended_unreviewed"], 0)
        proc = self._run_cli(["intake", "close", "--ticket", first["ticket_id"],
                              "--reason", "verify passed", "--evidence", "runs/x/result.json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "ended")
        proc = self._run_cli(["attention"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("1 ended unreviewed", proc.stdout)
        self.assertIn(first["ticket_id"], proc.stdout)


if __name__ == "__main__":
    unittest.main()
