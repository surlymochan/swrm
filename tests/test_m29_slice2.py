"""M29 slice 2 (#244): evalid live-rollout evidence check.

Covers `intake_m29_slice2_evidence_check`: the ledger-only validator for
epic #241 acceptance ①–⑤ — per-repo allowlist scope (evalid dispatched
only for allowlisted repo + author), ≥2 evalid tickets dispatched with a
Channel post each and an operator-verified PR per dispatch, ≥1 Gate
ignore with zero run, ≥1 Mandate withdrawn by stop with no dispatch
after it, ≥1 cross-repo duplicate resolving to the first Ticket, at
most one Assignment per ticket, and per-thread caps respected. The
validator certifies only what the ledger proves; PR existence and
branch verify are operator-checked against GitHub and recorded in
docs/m29-slice2-real-evidence.md. The live M29 record itself does NOT
pass this checker (only 1 of 2 finished dispatches opened a PR, and
the #201 dispatch was started but never finished on the ledger — a
platform restart killed the poll mid-flight, so its run has no
dispatch_finished and no channel_posted entry) — the failing checks
are the honest verdict, covered here by the negative tests.

Run: python3 -m unittest tests.test_m29_slice2
     (or: python3 tests/test_m29_slice2.py)
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
    "repo_policies": {
        "surlymochan/evalid": {
            "version": 1, "allowlist": {"users": ["surlymochan"], "bots": []},
        },
    },
    "admission": {"listed_mentioned": "explicit", "listed_unmentioned": "inferred",
                  "unlisted_mentioned": "inferred", "unlisted_unmentioned": "ignore"},
    "money_draft_only": True,
    "active_scope": {"repos": ["surlymochan/swrm", "surlymochan/evalid"],
                     "users": ["surlymochan"]},
    "caps": {"max_dispatches_per_thread": 3, "max_budget_minutes_per_thread": 60},
    "dispatch": {"backend": "pi", "budget_minutes": 10},
}


def dispatched_ticket(ticket_id, run_id, thread, trigger_id, commit="abc1234"):
    return [
        {"kind": "trigger_received", "trigger_id": trigger_id, "thread": thread,
         "author": "surlymochan", "mentioned_bot": True},
        {"kind": "decision", "trigger_id": trigger_id, "decision": "accept",
         "strength": "explicit"},
        {"kind": "ticket_created", "trigger_id": trigger_id, "ticket_id": ticket_id,
         "thread": thread, "strength": "explicit"},
        {"kind": "mandate_sealed", "trigger_id": trigger_id, "ticket_id": ticket_id,
         "mandate_id": "mnd-" + ticket_id[4:], "policy_version": 1,
         "source_identity": "surlymochan"},
        {"kind": "assignment_created", "ticket_id": ticket_id,
         "assignment_id": "asg-" + ticket_id, "budget_minutes": 10},
        {"kind": "dispatch_started", "ticket_id": ticket_id,
         "assignment_id": "asg-" + ticket_id, "budget_minutes": 10},
        {"kind": "dispatch_finished", "ticket_id": ticket_id,
         "assignment_id": "asg-" + ticket_id, "run_id": run_id, "status": "done"},
        {"kind": "channel_posted", "ticket_id": ticket_id, "run_id": run_id,
         "comment_id": 1, "url": "https://github.com/surlymochan/evalid/issues/201#issuecomment-1"},
    ]


def passing_entries():
    entries = []
    entries += dispatched_ticket("tkt-aaaa", "20261116-030000-aaaa01",
                                 "surlymochan/evalid#201 (issue)",
                                 "github:issue:6398437849")
    entries += dispatched_ticket("tkt-bbbb", "20261116-030000-bbbb02",
                                 "surlymochan/evalid#5 (issue)",
                                 "github:comment:6910487910")
    entries.append({"kind": "decision", "trigger_id": "github:comment:6731930505",
                    "decision": "ignore",
                    "reason": "admission table says ignore for unlisted_unmentioned"})
    entries.append({"kind": "ticket_created", "trigger_id": "github:issue:6401194642",
                    "ticket_id": "tkt-dddd", "thread": "surlymochan/evalid#202 (issue)"})
    entries.append({"kind": "mandate_withdrawn", "ticket_id": "tkt-dddd",
                    "mandate_id": "mnd-dddd", "reason": "human stop"})
    entries.append({"kind": "cross_repo_duplicate_skipped",
                    "trigger_id": "github:comment:6910966241",
                    "ticket_id": "tkt-aaaa", "dedup_key": "text:687db0885143fbe9",
                    "thread": "surlymochan/swrm#244 (issue)"})
    return entries


def passing_prs():
    return {
        "tkt-aaaa": {"pr": "https://github.com/surlymochan/evalid/pull/207",
                     "commit": "52e00d2"},
        "tkt-bbbb": {"pr": "https://github.com/surlymochan/evalid/pull/208",
                     "commit": "def5678"},
    }


class EvidenceCheckTest(unittest.TestCase):
    def test_passing_record_is_ok(self):
        out = xs.intake_m29_slice2_evidence_check(
            passing_entries(), policy=POLICY, prs=passing_prs())
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["failures"], [])

    def test_dispatch_started_without_finish_fails(self):
        # The live M29 record: the #201 dispatch was started but the poll
        # was killed by a platform restart before dispatch_finished.
        entries = [e for e in passing_entries()
                   if not (e.get("kind") == "dispatch_finished"
                           and e.get("ticket_id") == "tkt-aaaa")]
        out = xs.intake_m29_slice2_evidence_check(
            entries, policy=POLICY, prs=passing_prs())
        self.assertFalse(out["ok"])
        self.assertIn("dispatch_started_without_finish", out["failures"])

    def test_dispatch_failed_closes_a_started_dispatch(self):
        entries = [e for e in passing_entries()
                   if not (e.get("kind") == "dispatch_finished"
                           and e.get("ticket_id") == "tkt-aaaa")]
        entries.append({"kind": "dispatch_failed", "ticket_id": "tkt-aaaa",
                        "assignment_id": "asg-tkt-aaaa", "error": "run killed"})
        out = xs.intake_m29_slice2_evidence_check(
            entries, policy=POLICY, prs=passing_prs())
        self.assertTrue(out["checks"]["dispatch_started_without_finish"]["ok"])

    def test_one_pr_is_not_enough(self):
        # The live M29 record: only the #201 dispatch opened a PR.
        prs = {"tkt-aaaa": passing_prs()["tkt-aaaa"]}
        out = xs.intake_m29_slice2_evidence_check(
            passing_entries(), policy=POLICY, prs=prs)
        self.assertFalse(out["ok"])
        self.assertIn("prs_opened_for_two_dispatches", out["failures"])

    def test_one_dispatch_is_not_enough(self):
        entries = [e for e in passing_entries()
                   if e.get("ticket_id") != "tkt-bbbb"]
        out = xs.intake_m29_slice2_evidence_check(
            entries, policy=POLICY, prs=passing_prs())
        self.assertFalse(out["ok"])
        self.assertIn("dispatched_evalid_at_least_two", out["failures"])

    def test_missing_channel_post_fails(self):
        # The live M29 record: the #201 run's channel_posted ledger entry
        # is missing (platform restart killed the poll after dispatch).
        entries = [e for e in passing_entries()
                   if not (e.get("kind") == "channel_posted"
                           and e.get("run_id") == "20261116-030000-aaaa01")]
        out = xs.intake_m29_slice2_evidence_check(
            entries, policy=POLICY, prs=passing_prs())
        self.assertFalse(out["ok"])
        self.assertIn("channel_posted_per_dispatch", out["failures"])

    def test_no_gate_ignore_fails(self):
        entries = [e for e in passing_entries()
                   if not (e.get("kind") == "decision" and e.get("decision") == "ignore")]
        out = xs.intake_m29_slice2_evidence_check(
            entries, policy=POLICY, prs=passing_prs())
        self.assertFalse(out["ok"])
        self.assertIn("gate_ignore", out["failures"])

    def test_ignored_trigger_with_ticket_fails(self):
        entries = passing_entries()
        entries.append({"kind": "ticket_created",
                        "trigger_id": "github:comment:6731930505",
                        "ticket_id": "tkt-eeee",
                        "thread": "surlymochan/evalid#87 (issue)"})
        out = xs.intake_m29_slice2_evidence_check(
            entries, policy=POLICY, prs=passing_prs())
        self.assertFalse(out["ok"])
        self.assertIn("gate_ignore", out["failures"])

    def test_no_withdraw_fails(self):
        entries = [e for e in passing_entries() if e.get("kind") != "mandate_withdrawn"]
        out = xs.intake_m29_slice2_evidence_check(
            entries, policy=POLICY, prs=passing_prs())
        self.assertFalse(out["ok"])
        self.assertIn("mandate_withdrawn", out["failures"])

    def test_dispatch_after_withdraw_fails(self):
        entries = passing_entries()
        entries.append({"kind": "dispatch_finished", "ticket_id": "tkt-dddd",
                        "assignment_id": "asg-tkt-dddd", "run_id": "20261116-030000-dddd03",
                        "status": "done"})
        out = xs.intake_m29_slice2_evidence_check(
            entries, policy=POLICY, prs=passing_prs())
        self.assertFalse(out["ok"])
        self.assertIn("no_dispatch_after_withdraw", out["failures"])

    def test_no_cross_repo_duplicate_fails(self):
        entries = [e for e in passing_entries()
                   if e.get("kind") != "cross_repo_duplicate_skipped"]
        out = xs.intake_m29_slice2_evidence_check(
            entries, policy=POLICY, prs=passing_prs())
        self.assertFalse(out["ok"])
        self.assertIn("cross_repo_duplicate", out["failures"])

    def test_duplicate_opening_ticket_fails(self):
        entries = passing_entries()
        entries.append({"kind": "ticket_created",
                        "trigger_id": "github:comment:6910966241",
                        "ticket_id": "tkt-ffff",
                        "thread": "surlymochan/swrm#244 (issue)"})
        out = xs.intake_m29_slice2_evidence_check(
            entries, policy=POLICY, prs=passing_prs())
        self.assertFalse(out["ok"])
        self.assertIn("cross_repo_duplicate", out["failures"])

    def test_second_assignment_for_one_ticket_fails(self):
        entries = passing_entries()
        entries.insert(2, {"kind": "assignment_created", "ticket_id": "tkt-aaaa",
                           "assignment_id": "asg-tkt-aaaa-2", "budget_minutes": 10})
        out = xs.intake_m29_slice2_evidence_check(
            entries, policy=POLICY, prs=passing_prs())
        self.assertFalse(out["ok"])
        self.assertIn("one_assignment_per_ticket", out["failures"])

    def test_dispatch_outside_scope_fails(self):
        policy = copy.deepcopy(POLICY)
        policy["active_scope"] = {"repos": ["surlymochan/swrm"],
                                  "users": ["surlymochan"]}
        out = xs.intake_m29_slice2_evidence_check(
            passing_entries(), policy=policy, prs=passing_prs())
        self.assertFalse(out["ok"])
        self.assertIn("allowlist_scope", out["failures"])

    def test_cap_exceeded_fails(self):
        policy = copy.deepcopy(POLICY)
        policy["caps"] = {"max_dispatches_per_thread": 1,
                          "max_budget_minutes_per_thread": 60}
        entries = passing_entries()
        second = dispatched_ticket("tkt-eeee", "20261116-030000-eeee04",
                                   "surlymochan/evalid#5 (issue)",
                                   "github:comment:6910726042")
        entries = entries + second
        out = xs.intake_m29_slice2_evidence_check(
            entries, policy=policy, prs=passing_prs())
        self.assertFalse(out["ok"])
        self.assertIn("caps_respected", out["failures"])

    def test_no_caps_configured_passes_caps_check(self):
        out = xs.intake_m29_slice2_evidence_check(
            passing_entries(), policy={}, prs=passing_prs())
        self.assertTrue(out["checks"]["caps_respected"]["ok"])


class Slice1DocPointerTest(unittest.TestCase):
    def test_slice1_doc_points_at_slice2_issue_244(self):
        text = (ROOT / "docs" / "m29-slice1-per-repo-intake.md").read_text(encoding="utf-8")
        self.assertIn("Slice 2 (#244)", text)
        self.assertNotIn("Slice 2 (#242", text)


if __name__ == "__main__":
    unittest.main()
