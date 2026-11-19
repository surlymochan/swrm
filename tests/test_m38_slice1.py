"""M38 slice 1 (#289): run-time approval gate — policy, pause/resume handshake, ledger, CLI.

Tests use fixture events and fake executor children only; no backend, no network.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import _load

xs = _load.load()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tool_event(command):
    return {"type": "tool_use", "part": {"tool": "bash", "state": {"input": {"command": command}}}}


def perm_request(command, options=None):
    return {"method": "session/request_permission", "id": "req-1",
            "params": {"toolCall": {"title": command, "toolCallId": "tc-1", "kind": "exec"},
                       "options": options or [{"kind": "allow_always", "optionId": "opt-always"},
                                              {"kind": "allow_once", "optionId": "opt-once"}]}}


class FakeChild:
    def __init__(self, backend="cursor"):
        self.lane = {"backend": backend}
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


POL_ON = {"approval": {"enabled": True,
                       "categories": ["push", "publish", "destructive", "secret"],
                       "wait_seconds": 60}}


class ApprovalGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("DATA", str(self.root)), ("RUNS", str(self.root / "runs")),
                            ("LEDGER", str(self.root / "ledger.jsonl")),
                            ("APPROVALS", str(self.root / "approvals.json"))):
            patch = mock.patch.object(xs, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.rdir = self.root / "runs" / "20261119-210000-abcdef"
        self.rdir.mkdir(parents=True)
        self.event_file = str(self.rdir / "events.1.cursor.jsonl")
        Path(self.event_file).touch()

    def ledger_types(self):
        path = Path(xs.LEDGER)
        if not path.exists():
            return []
        return [json.loads(line).get("type") for line in path.read_text().splitlines() if line.strip()]

    # -- policy / classification -------------------------------------------

    def test_default_policy_gate_is_off(self):
        shipped = json.loads((Path(ROOT) / "policy.json").read_text())
        self.assertFalse(xs.approval_config(shipped)["enabled"])
        self.assertEqual(xs.approval_config(shipped)["categories"],
                         ["push", "publish", "destructive", "secret"])
        # Default off: even a git push classifies as nothing (byte-identical behaviour).
        self.assertIsNone(xs.approval_risk(tool_event("git push origin main"), {}, "/tmp/wt"))
        self.assertIsNone(xs.approval_risk(tool_event("git push origin main"), shipped, "/tmp/wt"))

    def test_categories_map_to_existing_risk_detection(self):
        cases = [
            ("git push origin main", "push", "push"),
            ("npm publish", "publish", "publish"),
            ("gh release create v1", "publish", "publish"),
            ("rm -rf /", "destructive", "rm outside worktree"),
            ("cat .env", "secret", "secret print"),
            ("sudo launchctl kickstart system/example", "destructive", "sudo"),
            ("git reset --hard HEAD", "destructive", "git destructive"),
            ("git clean -fd", "destructive", "git destructive"),
            ("git checkout -f main", "destructive", "git destructive"),
        ]
        for command, category, risk in cases:
            with self.subTest(command=command):
                gate = xs.approval_risk(tool_event(command), POL_ON, "/tmp/wt")
                self.assertIsNotNone(gate, command)
                self.assertEqual(gate["category"], category)
                self.assertEqual(gate["risk"], risk)

    def test_benign_commands_and_disabled_categories_do_not_gate(self):
        for command in ("git status", "echo hello", "npm test"):
            self.assertIsNone(xs.approval_risk(tool_event(command), POL_ON, "/tmp/wt"), command)
        push_only_secret = {"approval": {"enabled": True, "categories": ["secret"]}}
        self.assertIsNone(xs.approval_risk(tool_event("git push origin main"),
                                           push_only_secret, "/tmp/wt"))
        no_categories = {"approval": {"enabled": True, "categories": []}}
        self.assertIsNone(xs.approval_risk(tool_event("git push origin main"),
                                           no_categories, "/tmp/wt"))

    def test_host_scope_warning_is_not_gated(self):
        # Host-scope rm/sudo are warnings, not blocks; the gate must not pause them.
        self.assertIsNone(xs.approval_risk(
            tool_event("sudo launchctl kickstart system/example"), POL_ON, "/tmp/wt", scope="host"))

    def test_gate_for_block_maps_enforced_finding(self):
        block = {"risk": "push", "command": "git push [redacted] [redacted]"}
        self.assertEqual(xs.approval_gate_for_block(block, POL_ON)["category"], "push")
        self.assertIsNone(xs.approval_gate_for_block(block, {}))
        self.assertIsNone(xs.approval_gate_for_block({"risk": "computer use"}, POL_ON))
        self.assertIsNone(xs.approval_gate_for_block(
            {"risk": "sudo", "enforced": False}, POL_ON))

    # -- store / ledger ------------------------------------------------------

    def request_push(self, run_id="20261119-210000-abcdef"):
        finding = {"category": "push", "risk": "push", "command": "git push [redacted]"}
        return xs.request_approval(run_id, str(self.rdir), finding,
                                   pause_point={"run_id": run_id, "request_id": "req-1",
                                                "method": "session/request_permission"},
                                   event_file=self.event_file)

    def test_request_persists_everywhere_with_evidence_pointers(self):
        record = self.request_push()
        self.assertEqual(record["status"], "pending")
        self.assertTrue(record["id"].startswith("appr-"))
        self.assertEqual(record["pause_point"]["request_id"], "req-1")
        # Global index.
        self.assertEqual(xs.approval_load_all()[record["id"]]["status"], "pending")
        # Per-run file.
        run_entries = xs.approval_load_run(str(self.rdir))
        self.assertEqual([e["id"] for e in run_entries], [record["id"]])
        # Ledger line with evidence pointers.
        lines = [json.loads(line) for line in Path(xs.LEDGER).read_text().splitlines()]
        self.assertEqual(lines[0]["type"], "approval_requested")
        self.assertEqual(lines[0]["approval_id"], record["id"])
        self.assertEqual(lines[0]["approval_category"], "push")
        self.assertEqual(lines[0]["run_id"], "20261119-210000-abcdef")
        # Event log line.
        events = [json.loads(line) for line in Path(self.event_file).read_text().splitlines()]
        self.assertEqual(events[0]["type"], "approval_requested")
        self.assertEqual(events[0]["id"], record["id"])

    def test_decide_approve_and_reject(self):
        approved = self.request_push()
        decided = xs.decide_approval(approved["id"], "approved", by="nick", reason="ok")
        self.assertEqual(decided["status"], "approved")
        self.assertEqual(decided["decided_by"], "nick")
        self.assertIsNotNone(decided["decided_ts"])
        # Pause point survives the decision, so resume can use it.
        self.assertEqual(decided["pause_point"]["request_id"], "req-1")
        self.assertIn("approval_approved", self.ledger_types())
        self.assertEqual(xs.approval_load_run(str(self.rdir))[0]["status"], "approved")

        rejected = self.request_push()
        xs.decide_approval(rejected["id"], "rejected", by="nick", reason="too risky")
        self.assertEqual(xs.approval_load_all()[rejected["id"]]["status"], "rejected")
        self.assertIn("approval_rejected", self.ledger_types())

    def test_decide_unknown_or_twice_raises(self):
        with self.assertRaises(ValueError):
            xs.decide_approval("appr-nope", "approved")
        record = self.request_push()
        xs.decide_approval(record["id"], "approved")
        with self.assertRaises(ValueError):
            xs.decide_approval(record["id"], "rejected")
        with self.assertRaises(ValueError):
            xs.decide_approval(record["id"], "maybe")

    def test_list_filters_and_orders_pending_first(self):
        first = self.request_push()
        second = self.request_push()
        xs.decide_approval(first["id"], "approved")
        self.assertEqual([r["id"] for r in xs.list_approvals(status="pending")], [second["id"]])
        self.assertEqual([r["id"] for r in xs.list_approvals(status="approved")], [first["id"]])
        self.assertEqual(len(xs.list_approvals(status="all")), 2)
        self.assertEqual(len(xs.list_approvals(run_id="20261119-210000-abcdef")), 2)
        self.assertEqual(xs.list_approvals(run_id="other-run"), [])
        ordered = xs.list_approvals(status="all")
        self.assertEqual(ordered[0]["status"], "pending")

    def test_wait_for_approval_timeout_and_decided(self):
        record = self.request_push()
        self.assertIsNone(xs.wait_for_approval(record["id"], 0))
        xs.decide_approval(record["id"], "approved")
        decided = xs.wait_for_approval(record["id"], 0)
        self.assertIsNotNone(decided)
        self.assertEqual(decided["status"], "approved")

    # -- held replies --------------------------------------------------------

    def test_lane_approval_reply_shapes(self):
        child = FakeChild(backend="cursor")
        sent = xs.lane_approval_reply(child, perm_request("git push"), True, self.event_file)
        self.assertEqual(sent, "selected:opt-always")
        self.assertEqual(child.sent[0]["result"]["outcome"],
                         {"outcome": "selected", "optionId": "opt-always"})

        child = FakeChild(backend="cursor")
        self.assertEqual(xs.lane_approval_reply(child, perm_request("git push"), False), "cancelled")
        self.assertEqual(child.sent[0]["result"]["outcome"], {"outcome": "cancelled"})

        child = FakeChild(backend="codex")
        msg = {"method": "item/commandExecution/requestApproval", "id": "req-9"}
        self.assertEqual(xs.lane_approval_reply(child, msg, True), "accept")
        self.assertEqual(child.sent[0]["result"], {"decision": "accept"})
        self.assertEqual(xs.lane_approval_reply(child, msg, False), "decline")

        child = FakeChild(backend="codex")
        msg = {"method": "execCommandApproval", "id": "req-8"}
        self.assertEqual(xs.lane_approval_reply(child, msg, True), "approved")
        self.assertEqual(xs.lane_approval_reply(child, msg, False), "abort")

        # Unknown method / missing id: no reply shape, caller keeps the block.
        child = FakeChild(backend="codex")
        self.assertIsNone(xs.lane_approval_reply(
            child, {"method": "item/fileChange/requestApproval", "id": "req-7"}, True))
        self.assertEqual(child.sent, [])
        self.assertIsNone(xs.lane_approval_reply(
            child, {"method": "session/request_permission"}, True))

    def test_acp_select_option_prefers_always_and_copilot_once(self):
        child = FakeChild(backend="cursor")
        selected = xs.lane_acp_select_option(child, perm_request("git push"))
        self.assertEqual(selected["kind"], "allow_always")
        child = FakeChild(backend="copilot")
        selected = xs.lane_acp_select_option(child, perm_request("git push"))
        self.assertEqual(selected["kind"], "allow_once")
        # lane_acp_request still behaves (selected option recorded + sent).
        child = FakeChild(backend="cursor")
        events = str(self.root / "acp-events.jsonl")
        self.assertTrue(xs.lane_acp_request(child, perm_request("git push"), events))
        self.assertEqual(child.sent[0]["result"]["outcome"],
                         {"outcome": "selected", "optionId": "opt-always"})

    # -- pause / resume handshake --------------------------------------------

    def pause(self, policy, msg=None):
        spec = {"run_id": "20261119-210000-abcdef"}
        gate = {"category": "push", "risk": "push", "command": "git push [redacted]"}
        child = FakeChild(backend="cursor")
        resumed = xs.lane_approval_pause(
            child, spec, msg or perm_request("git push"), gate,
            str(self.rdir), self.event_file, None, policy)
        return resumed, child

    def test_pause_approve_resumes_from_pause_point(self):
        record_holder = {}

        def decide_later():
            time.sleep(0.2)
            pending = xs.list_approvals(status="pending")
            self.assertEqual(len(pending), 1)
            record_holder["id"] = pending[0]["id"]
            xs.decide_approval(pending[0]["id"], "approved", by="nick")

        thread = threading.Thread(target=decide_later)
        thread.start()
        resumed, child = self.pause(POL_ON)
        thread.join()
        self.assertTrue(resumed)
        self.assertEqual(child.sent[0]["result"]["outcome"],
                         {"outcome": "selected", "optionId": "opt-always"})
        events = [json.loads(line) for line in Path(self.event_file).read_text().splitlines()]
        kinds = [e["type"] for e in events]
        self.assertIn("approval_requested", kinds)
        self.assertIn("approval_approved", kinds)
        self.assertIn("approval_reply", kinds)
        status = json.loads((self.rdir / "status.json").read_text())
        self.assertEqual(status.get("approval_pending"), [])

    def test_pause_reject_skips_action(self):
        def decide_later():
            time.sleep(0.2)
            pending = xs.list_approvals(status="pending")
            xs.decide_approval(pending[0]["id"], "rejected", by="nick", reason="no")

        thread = threading.Thread(target=decide_later)
        thread.start()
        resumed, child = self.pause(POL_ON)
        thread.join()
        self.assertFalse(resumed)
        self.assertEqual(child.sent[0]["result"]["outcome"], {"outcome": "cancelled"})
        events = [json.loads(line) for line in Path(self.event_file).read_text().splitlines()]
        self.assertIn("approval_rejected", [e["type"] for e in events])

    def test_pause_timeout_is_recorded_as_rejection(self):
        no_wait = {"approval": {"enabled": True, "categories": ["push"], "wait_seconds": 0}}
        resumed, child = self.pause(no_wait)
        self.assertFalse(resumed)
        record = xs.list_approvals(status="all")[0]
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["reason"], "timeout")
        self.assertEqual(child.sent[0]["result"]["outcome"], {"outcome": "cancelled"})

    # -- CLI ------------------------------------------------------------------

    def test_cli_list_approve_reject(self):
        record = self.request_push()
        args = argparse.Namespace(status="pending", run=None, json=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            xs.cmd_approval_list(args)
        self.assertIn(record["id"], out.getvalue())
        self.assertIn("pending", out.getvalue())

        args = argparse.Namespace(approval_id=record["id"], by="nick", reason="ok", json=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            xs.cmd_approval_decide(args, "approved")
        self.assertEqual(json.loads(out.getvalue())["status"], "approved")

        args = argparse.Namespace(status="pending", run=None, json=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            xs.cmd_approval_list(args)
        self.assertIn("no approval requests", out.getvalue())

        with self.assertRaises(ValueError):
            xs.cmd_approval_decide(
                argparse.Namespace(approval_id="appr-nope", by=None, reason=None, json=False),
                "approved")

    def test_cli_binary_parses_approval_subcommands(self):
        # Regression: the approval subparsers must not clobber the top-level
        # parser (an earlier draft reused its variable name, so `approval
        # list` was parsed as `approval reject`). Exercise the real binary.
        import subprocess
        import sys
        env = dict(os.environ, OPENSWARM_HOME=str(self.root))
        binary = os.path.join(ROOT, "bin", "openswarm")
        proc = subprocess.run([sys.executable, binary, "approval", "list"],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no approval requests", proc.stdout)
        proc = subprocess.run([sys.executable, binary, "approval", "approve", "appr-nope"],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown approval id", proc.stderr)


if __name__ == "__main__":
    unittest.main()
