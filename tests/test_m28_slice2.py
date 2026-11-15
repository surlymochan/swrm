"""M28 slice 2 (#234): real-rollout evidence check.

Covers `intake_m28_slice2_evidence_check`: the ledger-only validator for
epic #231 acceptance ①–⑤ — ≥2 tickets dispatched end to end with a
Channel post each, ≥1 out-of-scope thread kept shadow, ≥1 Gate ignore,
≥1 Mandate withdrawn by stop with no dispatch after it, at most one
Assignment per ticket, and per-thread caps respected. The validator
certifies only what the ledger proves; PR existence and branch verify
are operator-checked against GitHub and recorded in
docs/m28-slice2-real-evidence.md. Also locks the slice-1 doc fix that
points at this slice (#234, not the slice-1 PR #233).

Run: python3 -m unittest tests.test_m28_slice2
     (or: python3 tests/test_m28_slice2.py)
"""
import copy
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

POLICY = {
    "version": 1, "accept_mode": "active",
    "scopes": ["issue", "pr"],
    "allowlist": {"users": ["surlymochan"], "bots": []},
    "admission": {"listed_mentioned": "explicit", "listed_unmentioned": "inferred",
                  "unlisted_mentioned": "inferred", "unlisted_unmentioned": "ignore"},
    "money_draft_only": True,
    "active_scope": {"repos": ["surlymochan/swrm"], "users": ["surlymochan"]},
    "caps": {"max_dispatches_per_thread": 3, "max_budget_minutes_per_thread": 60},
    "dispatch": {"backend": "pi", "budget_minutes": 10},
}


def dispatched_ticket(ticket_id, run_id, thread="surlymochan/swrm#101 (issue)"):
    return [
        {"kind": "ticket_created", "ticket_id": ticket_id, "thread": thread},
        {"kind": "assignment_created", "ticket_id": ticket_id,
         "assignment_id": "asg-" + ticket_id, "budget_minutes": 10},
        {"kind": "dispatch_started", "ticket_id": ticket_id,
         "assignment_id": "asg-" + ticket_id, "budget_minutes": 10},
        {"kind": "dispatch_finished", "ticket_id": ticket_id,
         "assignment_id": "asg-" + ticket_id, "run_id": run_id, "status": "done"},
        {"kind": "channel_posted", "ticket_id": ticket_id, "run_id": run_id,
         "comment_id": 1, "url": "https://github.com/surlymochan/swrm/issues/101#issuecomment-1"},
    ]


def passing_entries():
    entries = []
    entries += dispatched_ticket("tkt-aaaa", "20261116-030000-aaaa01",
                                 "surlymochan/swrm#101 (issue)")
    entries += dispatched_ticket("tkt-bbbb", "20261116-030000-bbbb02",
                                 "surlymochan/swrm#102 (issue)")
    entries.append({"kind": "active_scope_shadow", "ticket_id": "tkt-cccc",
                    "thread": "surlymochan/evalid#205 (issue)",
                    "reason": "repo surlymochan/evalid is not in active_scope.repos"})
    entries.append({"kind": "decision", "trigger_id": "github:comment:1",
                    "decision": "ignore", "reason": "bot author is not on the intake allowlist"})
    entries.append({"kind": "ticket_created", "ticket_id": "tkt-dddd",
                    "thread": "surlymochan/swrm#103 (issue)"})
    entries.append({"kind": "mandate_withdrawn", "ticket_id": "tkt-dddd",
                    "mandate_id": "mnd-dddd", "reason": "human stop"})
    return entries


class EvidenceCheckTest(unittest.TestCase):
    def test_passing_record_is_ok(self):
        out = xs.intake_m28_slice2_evidence_check(passing_entries(), policy=POLICY)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["failures"], [])

    def test_one_dispatch_is_not_enough(self):
        entries = [e for e in passing_entries()
                   if e.get("ticket_id") != "tkt-bbbb"]
        out = xs.intake_m28_slice2_evidence_check(entries, policy=POLICY)
        self.assertFalse(out["ok"])
        self.assertIn("dispatched_at_least_two", out["failures"])

    def test_missing_channel_post_fails(self):
        entries = [e for e in passing_entries()
                   if not (e.get("kind") == "channel_posted"
                           and e.get("run_id") == "20261116-030000-bbbb02")]
        out = xs.intake_m28_slice2_evidence_check(entries, policy=POLICY)
        self.assertFalse(out["ok"])
        self.assertIn("channel_posted_per_dispatch", out["failures"])

    def test_no_scope_shadow_fails(self):
        entries = [e for e in passing_entries() if e.get("kind") != "active_scope_shadow"]
        out = xs.intake_m28_slice2_evidence_check(entries, policy=POLICY)
        self.assertFalse(out["ok"])
        self.assertIn("scope_shadow", out["failures"])

    def test_no_gate_ignore_fails(self):
        entries = [e for e in passing_entries()
                   if not (e.get("kind") == "decision" and e.get("decision") == "ignore")]
        out = xs.intake_m28_slice2_evidence_check(entries, policy=POLICY)
        self.assertFalse(out["ok"])
        self.assertIn("gate_ignore", out["failures"])

    def test_no_withdraw_fails(self):
        entries = [e for e in passing_entries() if e.get("kind") != "mandate_withdrawn"]
        out = xs.intake_m28_slice2_evidence_check(entries, policy=POLICY)
        self.assertFalse(out["ok"])
        self.assertIn("mandate_withdrawn", out["failures"])

    def test_dispatch_after_withdraw_fails(self):
        entries = passing_entries()
        entries.append({"kind": "dispatch_finished", "ticket_id": "tkt-dddd",
                        "assignment_id": "asg-tkt-dddd", "run_id": "20261116-030000-dddd03",
                        "status": "done"})
        out = xs.intake_m28_slice2_evidence_check(entries, policy=POLICY)
        self.assertFalse(out["ok"])
        self.assertIn("no_dispatch_after_withdraw", out["failures"])

    def test_second_assignment_for_one_ticket_fails(self):
        entries = passing_entries()
        entries.insert(2, {"kind": "assignment_created", "ticket_id": "tkt-aaaa",
                           "assignment_id": "asg-tkt-aaaa-2", "budget_minutes": 10})
        out = xs.intake_m28_slice2_evidence_check(entries, policy=POLICY)
        self.assertFalse(out["ok"])
        self.assertIn("one_assignment_per_ticket", out["failures"])

    def test_cap_exceeded_fails(self):
        policy = copy.deepcopy(POLICY)
        policy["caps"] = {"max_dispatches_per_thread": 1,
                          "max_budget_minutes_per_thread": 60}
        entries = passing_entries()
        # Second dispatch on the same thread as the first: thread key comes
        # from the ticket_created entries.
        second = dispatched_ticket("tkt-eeee", "20261116-030000-eeee04",
                                   "surlymochan/swrm#101 (issue)")
        entries = entries + second
        out = xs.intake_m28_slice2_evidence_check(entries, policy=policy)
        self.assertFalse(out["ok"])
        self.assertIn("caps_respected", out["failures"])

    def test_no_caps_configured_passes_caps_check(self):
        out = xs.intake_m28_slice2_evidence_check(passing_entries(), policy={})
        self.assertTrue(out["checks"]["caps_respected"]["ok"])


class Slice1DocPointerTest(unittest.TestCase):
    def test_slice1_doc_points_at_slice2_issue_234(self):
        text = (ROOT / "docs" / "m28-slice1-active-core.md").read_text(encoding="utf-8")
        self.assertIn("Slice 2 (#234)", text)
        self.assertNotIn("Slice 2 (#233", text)


if __name__ == "__main__":
    unittest.main()
