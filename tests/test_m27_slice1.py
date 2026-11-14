"""M27 slice 1 (#213): intake domain core + ledger + Gate (shadow).

Covers: the admission table (all four listed/mentioned combinations),
bot handling, scope and accept_mode, money/commitment draft-only, human
stop, Mandate immutability and policy-version pinning, Ticket/Assignment
state machines, idempotent ingest, shadow mode (no Assignment, no
dispatch), the append-only intake ledger, and the real-comment replay
fixture (tests/fixtures/m27-replay.json) end to end.
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
FIXTURE = HERE / "fixtures" / "m27-replay.json"


def trig(trigger_id, author="surlymochan", body="please fix this", kind="issue",
         number=1, bot=False, repo="surlymochan/swrm"):
    return {"id": trigger_id, "source": "github",
            "thread": {"repo": repo, "kind": kind, "number": number},
            "author": author, "author_is_bot": bot, "body": body,
            "created_at": "2026-11-14T12:00:00Z"}


class M27Slice1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m27s1-")
        self.data = pathlib.Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.patch = mock.patch.object(xs, "DATA", str(self.data))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.policy = xs.default_intake_policy()

    # -- Gate: admission table -------------------------------------------

    def test_listed_mentioned_is_explicit(self):
        d = xs.intake_gate_decide(
            trig("t1", body="@openswarm please fix the flaky test"), self.policy)
        self.assertEqual((d["decision"], d["strength"]), ("accept", "explicit"))
        self.assertIn("admission:listed_mentioned:explicit", d["rules"])
        self.assertFalse(d["draft_only"])

    def test_listed_unmentioned_is_inferred(self):
        d = xs.intake_gate_decide(trig("t1", body="please fix the flaky test"), self.policy)
        self.assertEqual((d["decision"], d["strength"]), ("accept", "inferred"))
        self.assertIn("admission:listed_unmentioned:inferred", d["rules"])

    def test_unlisted_mentioned_is_inferred(self):
        d = xs.intake_gate_decide(
            trig("t1", author="alice-example", body="@openswarm please fix this"), self.policy)
        self.assertEqual((d["decision"], d["strength"]), ("accept", "inferred"))
        self.assertIn("admission:unlisted_mentioned:inferred", d["rules"])

    def test_unlisted_unmentioned_is_ignored(self):
        d = xs.intake_gate_decide(
            trig("t1", author="bob-example", body="nice work, keep it up"), self.policy)
        self.assertEqual((d["decision"], d["strength"]), ("ignore", None))
        self.assertIn("admission:unlisted_unmentioned:ignore", d["rules"])

    def test_bot_message_is_ignored_context_only(self):
        d = xs.intake_gate_decide(
            trig("t1", author="chatgpt-codex-connector[bot]",
                 body="Codex Review: Didn't find any major issues.", bot=True), self.policy)
        self.assertEqual((d["decision"], d["strength"]), ("ignore", None))
        self.assertIn("bot-message", d["rules"])

    def test_own_bot_message_is_ignored(self):
        d = xs.intake_gate_decide(
            trig("t1", author="openswarm[bot]", body="@openswarm done", bot=True), self.policy)
        self.assertEqual(d["decision"], "ignore")
        self.assertIn("own-bot-message", d["rules"])

    def test_allowlisted_bot_is_inferred_never_explicit(self):
        policy = xs.default_intake_policy()
        policy["allowlist"]["bots"] = ["ci-bot[bot]"]
        d = xs.intake_gate_decide(
            trig("t1", author="ci-bot[bot]", body="@openswarm CI is green", bot=True), policy)
        self.assertEqual((d["decision"], d["strength"]), ("accept", "inferred"))
        self.assertIn("allowlisted-bot-inferred", d["rules"])

    def test_scope_outside_is_ignored(self):
        policy = xs.default_intake_policy()
        policy["scopes"] = ["issue"]
        d = xs.intake_gate_decide(
            trig("t1", body="@openswarm fix this", kind="pr", number=3), policy)
        self.assertEqual(d["decision"], "ignore")
        self.assertIn("scope-outside", d["rules"])

    def test_accept_mode_off_is_unavailable(self):
        policy = xs.default_intake_policy()
        policy["accept_mode"] = "off"
        d = xs.intake_gate_decide(trig("t1", body="@openswarm fix this"), policy)
        self.assertEqual((d["decision"], d["strength"]), ("unavailable", None))
        self.assertIn("accept-mode-off", d["rules"])

    # -- Gate: money / commitment / stop ---------------------------------

    def test_money_request_is_accepted_but_draft_only(self):
        d = xs.intake_gate_decide(
            trig("t1", body="@openswarm 帮我买 10 USDT 的 BTC"), self.policy)
        self.assertEqual((d["decision"], d["strength"]), ("accept", "explicit"))
        self.assertTrue(d["draft_only"])
        self.assertIn("money-draft-only", d["rules"])

    def test_commitment_request_is_draft_only(self):
        d = xs.intake_gate_decide(
            trig("t1", body="@openswarm 对外承诺下周交付这个功能"), self.policy)
        self.assertEqual(d["decision"], "accept")
        self.assertTrue(d["draft_only"])
        self.assertIn("commitment-draft-only", d["rules"])

    def test_plain_task_is_not_draft_only(self):
        d = xs.intake_gate_decide(
            trig("t1", body="@openswarm fix the flaky test in test_batch.py"), self.policy)
        self.assertFalse(d["draft_only"])

    def test_listed_stop_is_stop(self):
        d = xs.intake_gate_decide(trig("t1", body="@openswarm stop，别做了"), self.policy)
        self.assertEqual((d["decision"], d["strength"]), ("stop", None))
        self.assertIn("human-stop", d["rules"])

    def test_unlisted_stop_text_is_not_a_stop(self):
        # Only a listed author can stop work; anyone else saying "stop"
        # goes through the normal admission table (here: ignore).
        d = xs.intake_gate_decide(
            trig("t1", author="mallory-example", body="stop, don't do this"), self.policy)
        self.assertEqual(d["decision"], "ignore")

    # -- Policy validation -------------------------------------------------

    def test_policy_validation_rejects_bad_mode(self):
        policy = xs.default_intake_policy()
        policy["accept_mode"] = "auto"
        with self.assertRaises(ValueError):
            xs.validate_intake_policy(policy)

    def test_policy_validation_rejects_bad_admission(self):
        policy = xs.default_intake_policy()
        policy["admission"]["listed_mentioned"] = "maybe"
        with self.assertRaises(ValueError):
            xs.validate_intake_policy(policy)

    def test_policy_validation_rejects_missing_version(self):
        policy = xs.default_intake_policy()
        del policy["version"]
        with self.assertRaises(ValueError):
            xs.validate_intake_policy(policy)

    def test_load_policy_defaults_when_no_file(self):
        policy, source = xs.load_intake_policy(data_dir=str(self.data))
        self.assertEqual(source, "default")
        self.assertEqual(policy["version"], 1)
        self.assertEqual(policy["accept_mode"], "shadow")

    def test_load_policy_reads_file_and_rejects_malformed(self):
        path = self.data / "intake-policy.json"
        path.write_text(json.dumps({"version": 1, "accept_mode": "nope"}))
        with self.assertRaises(ValueError):
            xs.load_intake_policy(data_dir=str(self.data))

    # -- Mandate immutability + state machines ----------------------------

    def test_mandate_is_sealed_and_update_is_refused(self):
        trigger = xs.normalize_trigger(trig("t1", body="@openswarm fix this"))
        decision = xs.intake_gate_decide(trigger, self.policy)
        ticket = xs.new_intake_ticket(trigger, decision, now=1000.0)
        mandate = xs.new_intake_mandate(ticket, trigger, decision, now=1000.0)
        self.assertTrue(mandate["_sealed"])
        self.assertEqual(mandate["policy_version"], 1)
        self.assertEqual(mandate["strength"], "explicit")
        self.assertEqual(mandate["source_identity"], "surlymochan")
        with self.assertRaises(ValueError):
            xs.intake_update_mandate(mandate, scope={"thread": "other#9 (issue)"})

    def test_ticket_state_machine(self):
        trigger = xs.normalize_trigger(trig("t1", body="fix this"))
        ticket = xs.new_intake_ticket(
            trigger, xs.intake_gate_decide(trigger, self.policy), now=1000.0)
        self.assertEqual(ticket["status"], "open")
        ended = xs.transition_intake_ticket(ticket, "ended", reason="human stop", now=1001.0)
        self.assertEqual((ended["status"], ended["ended_ts"]), ("ended", 1001.0))
        resumed = xs.transition_intake_ticket(ended, "open", reason="resume", now=1002.0)
        self.assertEqual((resumed["status"], resumed["ended_ts"]), ("open", None))
        with self.assertRaises(ValueError):
            xs.transition_intake_ticket(ticket, "open")  # open -> open is not a move

    def test_assignment_state_machine(self):
        trigger = xs.normalize_trigger(trig("t1", body="fix this"))
        ticket = xs.new_intake_ticket(
            trigger, xs.intake_gate_decide(trigger, self.policy), now=1000.0)
        asg = xs.new_intake_assignment(ticket, "pi", now=1000.0)
        self.assertEqual(asg["status"], "pending")
        running = xs.transition_intake_assignment(asg, "running", now=1001.0)
        ended = xs.transition_intake_assignment(running, "ended", now=1002.0)
        self.assertEqual(ended["status"], "ended")
        with self.assertRaises(ValueError):
            xs.transition_intake_assignment(ended, "running")  # ended is terminal
        pending_cancel = xs.transition_intake_assignment(asg, "ended", now=1003.0)
        self.assertEqual(pending_cancel["status"], "ended")  # pending -> ended is a cancel

    def test_normalize_trigger_rejects_malformed(self):
        with self.assertRaises(ValueError):
            xs.normalize_trigger({"id": "t1", "author": "x", "body": "hi"})
        with self.assertRaises(ValueError):
            xs.normalize_trigger(trig("t1", kind="discussion", number=1))

    # -- Ingest: idempotency, shadow, stop, ledger ------------------------

    def test_ingest_accept_creates_ticket_and_mandate_no_assignment(self):
        result = xs.intake_ingest_trigger(
            trig("t1", body="@openswarm fix the flaky test"), policy=self.policy,
            data_dir=str(self.data), now=1000.0)
        self.assertFalse(result["duplicate"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["decision"]["decision"], "accept")
        state = xs.load_intake_state(str(self.data))
        self.assertEqual(len(state["tickets"]), 1)
        self.assertEqual(len(state["mandates"]), 1)
        self.assertEqual(len(state["assignments"]), 0)
        self.assertEqual(state["tickets"][0]["mandate_id"], state["mandates"][0]["id"])
        self.assertEqual(state["mandates"][0]["policy_version"], 1)

    def test_ingest_is_idempotent(self):
        first = xs.intake_ingest_trigger(
            trig("t1", body="@openswarm fix this"), policy=self.policy,
            data_dir=str(self.data), now=1000.0)
        second = xs.intake_ingest_trigger(
            trig("t1", body="@openswarm fix this"), policy=self.policy,
            data_dir=str(self.data), now=1001.0)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["ticket_id"], first["ticket_id"])
        state = xs.load_intake_state(str(self.data))
        self.assertEqual(len(state["tickets"]), 1)
        kinds = [e["kind"] for e in xs.intake_ledger_entries(str(self.data))]
        self.assertIn("duplicate_skipped", kinds)
        self.assertEqual(kinds.count("ticket_created"), 1)

    def test_ingest_ignore_creates_no_ticket(self):
        result = xs.intake_ingest_trigger(
            trig("t1", author="bob-example", body="nice work"), policy=self.policy,
            data_dir=str(self.data))
        self.assertEqual(result["decision"]["decision"], "ignore")
        self.assertIsNone(result["ticket_id"])
        self.assertEqual(xs.load_intake_state(str(self.data))["tickets"], [])

    def test_stop_ends_open_ticket_on_same_thread(self):
        xs.intake_ingest_trigger(
            trig("t1", body="@openswarm fix this", kind="pr", number=13),
            policy=self.policy, data_dir=str(self.data), now=1000.0)
        result = xs.intake_ingest_trigger(
            trig("t2", body="@openswarm stop，别做了", kind="pr", number=13),
            policy=self.policy, data_dir=str(self.data), now=1001.0)
        self.assertEqual(result["decision"]["decision"], "stop")
        self.assertTrue(result["stopped"])
        state = xs.load_intake_state(str(self.data))
        self.assertEqual(state["tickets"][0]["status"], "ended")
        self.assertEqual(len(state["tickets"]), 1)  # stop creates no ticket

    def test_stop_with_no_open_ticket_is_recorded_not_an_error(self):
        result = xs.intake_ingest_trigger(
            trig("t1", body="@openswarm stop，别做了", kind="issue", number=42),
            policy=self.policy, data_dir=str(self.data))
        self.assertEqual(result["decision"]["decision"], "stop")
        self.assertFalse(result["stopped"])
        self.assertIsNone(result["ticket_id"])

    def test_ledger_is_append_only_and_sequenced(self):
        before = xs.intake_ingest_trigger(
            trig("t1", body="@openswarm fix this"), policy=self.policy,
            data_dir=str(self.data))
        path = self.data / "intake-ledger.jsonl"
        snapshot = path.read_bytes()
        xs.intake_ingest_trigger(
            trig("t1", body="@openswarm fix this"), policy=self.policy,
            data_dir=str(self.data))
        after = path.read_bytes()
        self.assertTrue(after.startswith(snapshot))  # earlier entries untouched
        entries = xs.intake_ledger_entries(str(self.data))
        self.assertEqual([e["seq"] for e in entries], list(range(1, len(entries) + 1)))
        kinds = [e["kind"] for e in entries]
        self.assertEqual(kinds[:4], ["trigger_received", "decision",
                                     "ticket_created", "mandate_sealed"])
        self.assertEqual(kinds[-1], "duplicate_skipped")
        self.assertIsNotNone(before["ticket_id"])

    def test_policy_version_is_pinned_per_mandate(self):
        xs.intake_ingest_trigger(
            trig("t1", body="@openswarm fix this"), policy=self.policy,
            data_dir=str(self.data), now=1000.0)
        v2 = xs.default_intake_policy()
        v2["version"] = 2
        xs.intake_ingest_trigger(
            trig("t2", body="@openswarm fix that"), policy=v2,
            data_dir=str(self.data), now=1001.0)
        state = xs.load_intake_state(str(self.data))
        versions = [m["policy_version"] for m in state["mandates"]]
        self.assertEqual(versions, [1, 2])  # old Mandate keeps v1

    # -- Replay fixture ----------------------------------------------------

    def test_replay_fixture_matches_hand_derived_decisions(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        results = xs.intake_replay(fixture["triggers"], policy=self.policy,
                                   data_dir=str(self.data))
        self.assertEqual(len(results), 11)
        by_id = {}
        for result in results:
            if result["duplicate"]:
                continue
            by_id[result["trigger_id"]] = result
        for trigger_id, want in fixture["expected"].items():
            got = by_id[trigger_id]["decision"]
            self.assertEqual(got["decision"], want["decision"], trigger_id)
            self.assertEqual(got["strength"], want["strength"], trigger_id)
            self.assertEqual(got["draft_only"], want["draft_only"], trigger_id)
        duplicates = [r for r in results if r["duplicate"]]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["ticket_id"],
                         by_id["github:surlymochan/swrm#13:comment:6510574536"]["ticket_id"])
        state = xs.load_intake_state(str(self.data))
        self.assertEqual(len(state["tickets"]), 6)
        self.assertEqual(sum(1 for t in state["tickets"] if t["status"] == "open"), 5)
        self.assertEqual(sum(1 for t in state["tickets"] if t["status"] == "ended"), 1)
        self.assertEqual(state["assignments"], [])
        # The duplicate re-delivery created no second ticket for PR #13.
        pr13 = [t for t in state["tickets"]
                if t["thread"]["number"] == 13 and t["thread"]["kind"] == "pr"]
        self.assertEqual(len(pr13), 1)
        self.assertEqual(pr13[0]["status"], "ended")  # ended by the stop case
        # Real comments are verbatim in the fixture (provenance recorded).
        real = [t for t in fixture["triggers"] if t.get("provenance", {}).get("verbatim")]
        self.assertEqual(len(real), 7)  # 6 unique + 1 duplicate delivery
        self.assertTrue(all(t["provenance"].get("comment_id") for t in real))

    # -- CLI smoke ---------------------------------------------------------

    def run_cli(self, *argv):
        env = dict(os.environ, OPENSWARM_HOME=str(self.data))
        proc = subprocess.run([sys.executable, str(HERE.parent / "bin" / "openswarm"), *argv],
                              capture_output=True, text=True, env=env, timeout=60)
        return proc

    def test_cli_policy_init_show_and_replay_check(self):
        proc = self.run_cli("intake", "policy", "--init")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("shadow", proc.stdout)
        proc = self.run_cli("intake", "policy")  # show is the default action
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("accept_mode=shadow", proc.stdout)
        proc = self.run_cli("intake", "replay", "--fixture", str(FIXTURE), "--check")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("replay check OK: 11 triggers", proc.stdout)
        proc = self.run_cli("intake", "tickets", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(json.loads(proc.stdout)["tickets"]), 6)
        proc = self.run_cli("intake", "decisions", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        decisions = json.loads(proc.stdout)["decisions"]
        self.assertEqual(len(decisions), 10)  # one decision per non-duplicate trigger
        self.assertTrue(all(d["decision"] in xs.INTAKE_DECISIONS for d in decisions))

    def test_cli_ingest_single_trigger(self):
        trigger_file = self.data / "trigger.json"
        trigger_file.write_text(json.dumps(trig("cli-1", body="@openswarm fix this")))
        proc = self.run_cli("intake", "ingest", "--trigger-file", str(trigger_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"]["decision"], "accept")
        self.assertFalse(result["dispatched"])


if __name__ == "__main__":
    unittest.main()
