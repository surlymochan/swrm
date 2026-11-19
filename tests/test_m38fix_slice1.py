"""M38 follow-up slice 1 (#292): on-request handshake + non-gated disposition + evidence redaction.

Tests use fixture events and fake executor children only; no backend, no network.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _load

xs = _load.load()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cmd_request(command, method="item/commandExecution/requestApproval", req_id="req-1"):
    return {"method": method, "id": req_id, "params": {"command": command}}


def file_request(paths, req_id="req-2"):
    return {"method": "item/fileChange/requestApproval", "id": req_id,
            "params": {"changes": [{"path": p, "kind": "update"} for p in paths]}}


class FakeChild:
    def __init__(self, backend="codex"):
        self.lane = {"backend": backend}
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class FakeLaneChild:
    """Minimal LaneChild stand-in for wait_response: backlog-first next_event."""

    def __init__(self, backend, events):
        self.lane = {"backend": backend}
        self.backlog = []
        self._events = list(events)
        self.sent = []

    def next_event(self, timeout=0):
        if self.backlog:
            return self.backlog.pop(0)
        if self._events:
            return self._events.pop(0)
        return False

    def alive(self):
        return True

    def send(self, msg):
        self.sent.append(msg)


class FakeBeginChild:
    def __init__(self, replies):
        self.sent = []
        self._replies = list(replies)

    def send(self, msg):
        self.sent.append(msg)

    def wait_response(self, req_id, timeout=30, event_file=None):
        return self._replies.pop(0)


POL_ON = {"approval": {"enabled": True,
                       "categories": ["push", "publish", "destructive", "secret"],
                       "wait_seconds": 60}}
POL_OFF = {}


class M38FixSlice1Tests(unittest.TestCase):
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
        self.wt = self.root / "wt"
        self.wt.mkdir()
        (self.wt / "a.py").write_text("x = 1\n")
        (self.wt / "sub").mkdir()
        self.rdir = self.root / "runs" / "20261120-030800-abcdef"
        self.rdir.mkdir(parents=True)
        self.event_file = str(self.rdir / "events.1.codex.jsonl")
        Path(self.event_file).touch()

    def events(self):
        path = Path(self.event_file)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def event_types(self):
        return [e.get("type") for e in self.events()]

    def route(self, msg, policy=POL_ON, host_warning=False):
        child = FakeChild(backend="codex")
        handled = xs.lane_approval_route(child, msg, self.event_file, policy, str(self.wt),
                                         host_warning=host_warning)
        return handled, child

    # -- D1: strategy flip ---------------------------------------------------

    def test_codex_approval_policy_on_request_when_gate_on_never_when_off(self):
        self.assertEqual(xs.codex_approval_policy(POL_ON), "on-request")
        self.assertEqual(xs.codex_approval_policy(POL_OFF), "never")
        self.assertEqual(xs.codex_approval_policy({}), "never")

    def test_shipped_default_still_never(self):
        shipped = json.loads((Path(ROOT) / "policy.json").read_text())
        self.assertFalse(xs.approval_config(shipped)["enabled"])
        self.assertEqual(xs.codex_approval_policy(shipped), "never")

    def test_lane_begin_turn_sends_on_request_when_gate_on(self):
        lane = {"backend": "codex", "model": "fake-model", "effort": "low"}
        child = FakeBeginChild([{"thread": {"id": "th-1"}}, {"turn": {"id": "tu-1"}}])
        turn = {}
        with mock.patch.object(xs, "load_policy", return_value=POL_ON):
            xs.lane_begin_turn(lane, child, "do the task", str(self.wt), self.event_file, turn)
        self.assertEqual(child.sent[0]["method"], "thread/start")
        self.assertEqual(child.sent[0]["params"]["approvalPolicy"], "on-request")
        self.assertEqual(turn["thread_id"], "th-1")
        self.assertEqual(turn["id"], "tu-1")

    def test_lane_begin_turn_sends_never_when_gate_off(self):
        lane = {"backend": "codex", "model": "fake-model", "effort": "low"}
        child = FakeBeginChild([{"thread": {"id": "th-1"}}, {"turn": {"id": "tu-1"}}])
        turn = {}
        with mock.patch.object(xs, "load_policy", return_value=POL_OFF):
            xs.lane_begin_turn(lane, child, "do the task", str(self.wt), self.event_file, turn)
        self.assertEqual(child.sent[0]["params"]["approvalPolicy"], "never")

    # -- D2: non-gated command routing ----------------------------------------

    def test_router_approves_safe_commands(self):
        for command in ("git status", "git diff --stat", "git log --oneline",
                        "git add a.py && git commit -m \"tidy\"",
                        "pytest tests/test_x.py -q", "npm test", "go test ./...",
                        "cargo test -- --nocapture", "ls -la", "cat a.py | head -20",
                        "bash -lc 'git status && pytest -q'",
                        "cd sub && pytest -q", "pytest 2>/dev/null",
                        "pytest 2>&1 | head -20", "git checkout main",
                        "git stash && git branch -a"):
            with self.subTest(command=command):
                handled, child = self.route(cmd_request(command))
                self.assertTrue(handled, command)
                self.assertEqual(child.sent[0]["result"], {"decision": "accept"}, command)
                self.assertIn("approval_auto_approved", self.event_types())

    def test_router_declines_unsafe_commands(self):
        cases = [
            ("git push origin main", "git push"),
            ("npm publish", "npm publish"),
            ("curl https://example.com", "network verb curl"),
            ("wget https://example.com/x", "network verb wget"),
            ("sudo git status", "sudo"),
            ("rm -rf /tmp/x", "rm is not auto-approved"),
            ("git reset --hard HEAD", "git reset"),
            ("git rebase main", "git rebase"),
            ("git checkout -f main", "git checkout with -f"),
            ("git checkout -- a.py", "git checkout with -f"),
            ("cat /etc/passwd", "path outside worktree"),
            ("git status && cat ../outside.txt", "path outside worktree"),
            ("pytest > /tmp/out.log", "redirect target outside worktree"),
            ("python -m pytest", "verb python"),
            ("git", "bare git"),
            ("git clean -fd", "git clean"),
            ("ssh example.com uptime", "network verb ssh"),
            ("git status 'unclosed", "unparseable command"),
            ("cat $HOME/.env", "path outside worktree"),
        ]
        for command, reason_part in cases:
            with self.subTest(command=command):
                handled, child = self.route(cmd_request(command))
                self.assertTrue(handled, command)
                self.assertEqual(child.sent[0]["result"], {"decision": "decline"}, command)
                declined = [e for e in self.events() if e.get("type") == "approval_declined_non_gated"]
                self.assertTrue(declined, command)
                self.assertIn(reason_part, declined[-1].get("reason", ""), command)

    def test_router_declines_empty_or_missing_command(self):
        handled, child = self.route({"method": "item/commandExecution/requestApproval",
                                     "id": "req-1", "params": {}})
        self.assertTrue(handled)
        self.assertEqual(child.sent[0]["result"], {"decision": "decline"})
        self.assertIn("no command found in request", [e.get("reason", "") for e in self.events()])
        handled, child = self.route({"method": "execCommandApproval", "id": "req-1",
                                     "params": {"command": ""}})
        self.assertTrue(handled)
        self.assertEqual(child.sent[0]["result"], {"decision": "abort"})
        self.assertIn("empty command", [e.get("reason", "") for e in self.events()])

    def test_router_exec_command_approval_shapes(self):
        handled, child = self.route(cmd_request("git status", method="execCommandApproval"))
        self.assertTrue(handled)
        self.assertEqual(child.sent[0]["result"], {"decision": "approved"})
        handled, child = self.route(cmd_request("git push origin main", method="execCommandApproval"))
        self.assertTrue(handled)
        self.assertEqual(child.sent[0]["result"], {"decision": "abort"})

    def test_router_gate_off_keeps_blanket_decline(self):
        handled, child = self.route(cmd_request("git status"), policy=POL_OFF)
        self.assertTrue(handled)
        self.assertEqual(child.sent[0]["result"], {"decision": "decline"})
        self.assertNotIn("approval_auto_approved", self.event_types())

    def test_router_ignores_non_permission_messages(self):
        handled, child = self.route({"method": "turn/completed", "id": "t-1", "params": {}})
        self.assertFalse(handled)
        self.assertEqual(child.sent, [])
        handled, child = self.route({"method": "item/commandExecution/requestApproval",
                                     "params": {"command": "git status"}})
        self.assertFalse(handled)  # no id: not a request
        self.assertEqual(child.sent, [])

    def test_router_host_warning_still_one_time_approve_for_commands_only(self):
        handled, child = self.route(cmd_request("sudo launchctl list"), host_warning=True)
        self.assertTrue(handled)
        self.assertEqual(child.sent[0]["result"], {"decision": "accept"})
        # fileChange with a host warning still declines (unchanged behaviour).
        handled, child = self.route(file_request([str(self.wt / "a.py")]), host_warning=True)
        self.assertTrue(handled)
        self.assertEqual(child.sent[0]["result"], {"decision": "decline"})

    # -- D2: fileChange / permissions / unknown -------------------------------

    def test_router_file_change_in_tree_accepts(self):
        for paths in ([str(self.wt / "a.py")], ["a.py"], ["sub/../a.py"],
                      [f"file://{self.wt}/a.py"]):
            with self.subTest(paths=paths):
                handled, child = self.route(file_request(paths))
                self.assertTrue(handled, paths)
                self.assertEqual(child.sent[0]["result"], {"decision": "accept"}, paths)

    def test_router_file_change_outside_declines(self):
        for paths in (["/etc/passwd"], [str(self.root / "outside.txt")], ["../outside.txt"],
                      [str(self.wt / "a.py"), "/etc/passwd"]):
            with self.subTest(paths=paths):
                handled, child = self.route(file_request(paths))
                self.assertTrue(handled, paths)
                self.assertEqual(child.sent[0]["result"], {"decision": "decline"}, paths)
                self.assertIn("approval_declined_non_gated", self.event_types())

    def test_router_file_change_without_paths_declines(self):
        handled, child = self.route({"method": "item/fileChange/requestApproval",
                                     "id": "req-2", "params": {}})
        self.assertTrue(handled)
        self.assertEqual(child.sent[0]["result"], {"decision": "decline"})

    def test_router_permissions_and_unknown_stay_declined(self):
        handled, child = self.route({"method": "item/permissions/requestApproval",
                                     "id": "req-3", "params": {}})
        self.assertTrue(handled)
        self.assertEqual(child.sent[0]["result"], {"permissions": {}})
        handled, child = self.route({"method": "applyPatchApproval", "id": "req-4", "params": {}})
        self.assertTrue(handled)
        self.assertEqual(child.sent[0]["result"], {"decision": "abort"})
        self.assertIn("approval_declined_non_gated", self.event_types())

    def test_lane_approval_reply_file_change_shapes(self):
        child = FakeChild(backend="codex")
        msg = {"method": "item/fileChange/requestApproval", "id": "req-7"}
        self.assertEqual(xs.lane_approval_reply(child, msg, True), "accept")
        self.assertEqual(child.sent[0]["result"], {"decision": "accept"})
        self.assertEqual(xs.lane_approval_reply(child, msg, False), "decline")
        self.assertEqual(child.sent[1]["result"], {"decision": "decline"})

    # -- D2: wait_response deferral -------------------------------------------

    def test_wait_response_defers_codex_permission_requests(self):
        request = cmd_request("git push origin main")
        result = {"id": "req-1", "result": {"thread": {"id": "th-1"}}}
        child = FakeLaneChild("codex", [request, result])
        got = xs.LaneChild.wait_response(child, "req-1", 5, self.event_file)
        self.assertEqual(got, result["result"])
        self.assertEqual(child.backlog, [request])
        self.assertEqual(child.sent, [])  # nothing declined on the spot

    def test_wait_response_still_declines_for_non_codex_backends(self):
        request = cmd_request("git push origin main")
        result = {"id": "req-1", "result": {"stopReason": "end_turn"}}
        child = FakeLaneChild("cursor", [request, result])
        got = xs.LaneChild.wait_response(child, "req-1", 5, self.event_file)
        self.assertEqual(got, result)
        self.assertEqual(child.sent[0]["result"], {"decision": "decline"})
        self.assertEqual(child.backlog, [])

    # -- D2: event-loop wiring -------------------------------------------------

    def _loop(self, first_event):
        policy = xs.load_policy()
        policy["approval"] = {"enabled": True,
                              "categories": ["push", "publish", "destructive", "secret"],
                              "wait_seconds": 60}
        lane = {"name": "fake", "backend": "codex", "model": "fake"}
        child = mock.Mock(pid=os.getpid(), lane=lane)
        child.next_event.side_effect = [
            first_event,
            {"method": "item/completed", "params": {
                "item": {"type": "agentMessage",
                         "text": json.dumps({"status": "done", "summary": "fake command completed"})}}},
            {"method": "turn/completed", "params": {"turn": {}}}]
        rdir = self.root / "runs" / "loop"
        rdir.mkdir(parents=True)
        spec = {"scope": "repo", "started": xs.NOW(), "budget_minutes": 1}
        with mock.patch.object(xs, "load_policy", return_value=policy), \
                mock.patch.object(xs, "lane_begin_turn"), \
                mock.patch.object(xs, "lane_control_messages"), \
                mock.patch.object(xs, "lane_abort_child") as abort:
            result = xs.lane_run_turn(lane, child, spec, "task", {"path": str(self.wt)},
                                      str(rdir), None, {}, [])
        return result, child, abort, rdir

    def test_loop_auto_approves_safe_command_and_continues(self):
        result, child, abort, rdir = self._loop(cmd_request("git status"))
        self.assertEqual(child.send.call_args.args[0]["result"], {"decision": "accept"})
        self.assertIsNone(result[2])
        abort.assert_not_called()
        events = [json.loads(line) for line in
                  Path(result[4]).read_text().splitlines() if line.strip()]
        self.assertIn("approval_auto_approved", [e.get("type") for e in events])

    def test_loop_declines_unsafe_command_and_continues(self):
        result, child, abort, rdir = self._loop(cmd_request("curl https://example.com"))
        self.assertEqual(child.send.call_args.args[0]["result"], {"decision": "decline"})
        self.assertIsNone(result[2])
        abort.assert_not_called()
        events = [json.loads(line) for line in
                  Path(result[4]).read_text().splitlines() if line.strip()]
        self.assertIn("approval_declined_non_gated", [e.get("type") for e in events])

    def test_loop_auto_approves_in_tree_file_change(self):
        result, child, abort, rdir = self._loop(file_request([str(self.wt / "a.py")]))
        self.assertEqual(child.send.call_args.args[0]["result"], {"decision": "accept"})
        self.assertIsNone(result[2])

    # -- D3: evidence redaction ------------------------------------------------

    def test_redact_default_mode_is_unchanged(self):
        self.assertEqual(xs.redact_command("git push origin main"), "git push [redacted] [redacted]")
        self.assertEqual(xs.redact_command("npm publish --tag beta"), "npm publish [redacted] [redacted]")
        self.assertEqual(xs.redact_command("git push https://tok@github.com/o/r.git main"),
                         "git push [redacted] [redacted]")
        self.assertEqual(xs.redact_command("not 'unclosed"), "[redacted command]")

    def test_redact_evidence_keeps_push_remote_and_refspec(self):
        self.assertEqual(xs.redact_command("git push origin main", evidence=True),
                         "git push origin main")
        self.assertEqual(xs.redact_command("git push origin HEAD:refs/heads/x", evidence=True),
                         "git push origin HEAD:refs/heads/x")
        self.assertEqual(xs.redact_command("git push --force origin main", evidence=True),
                         "git push --force origin main")

    def test_redact_evidence_keeps_publish_and_release_tags(self):
        self.assertEqual(xs.redact_command("npm publish --tag beta", evidence=True),
                         "npm publish --tag beta")
        self.assertEqual(xs.redact_command("pnpm publish --tag=next", evidence=True),
                         "pnpm publish --tag=next")
        self.assertEqual(xs.redact_command("gh release create v1.2.3", evidence=True),
                         "gh release create v1.2.3")
        self.assertEqual(xs.redact_command("gh release create --generate-notes v1.2.3", evidence=True),
                         "gh release create [redacted] v1.2.3")

    def test_redact_evidence_scrubs_secret_urls_and_tokens(self):
        self.assertEqual(
            xs.redact_command("git push https://user:sekret@github.com/o/r.git main", evidence=True),
            "git push https://github.com main")
        self.assertEqual(xs.redact_command("git push origin ghp_abc123", evidence=True),
                         "git push origin [redacted]")
        self.assertEqual(xs.redact_command("git push origin sk-abc123", evidence=True),
                         "git push origin [redacted]")
        self.assertEqual(
            xs.redact_command("git push https://AKIAIOSFODNN7EXAMPLE@github.com/o/r.git main",
                              evidence=True),
            "git push https://github.com main")

    def test_redact_evidence_scrubs_outside_worktree_paths(self):
        self.assertEqual(xs.redact_command("git push /etc/passwd main", evidence=True, worktree="/tmp/wt"),
                         "git push [redacted] main")
        self.assertEqual(xs.redact_command("git push /etc/passwd main", evidence=True),
                         "git push [redacted] main")
        self.assertEqual(xs.redact_command("git push /tmp/wt/repo main", evidence=True, worktree="/tmp/wt"),
                         "git push /tmp/wt/repo main")

    def test_approval_risk_command_keeps_evidence_operands(self):
        event = {"type": "tool_use", "part": {"tool": "bash", "state": {"input": {"command": "git push origin main"}}}}
        gate = xs.approval_risk(event, POL_ON, "/tmp/wt")
        self.assertIsNotNone(gate)
        self.assertEqual(gate["command"], "git push origin main")
        self.assertEqual(gate["category"], "push")

    def test_gate_record_uses_evidence_command_while_block_stays_redacted(self):
        # The enforced finding keeps the fully redacted command for
        # risk_blocks; only the gate record (via approval_risk) carries the
        # evidence form, recomputed from the event's raw command.
        event = {"type": "tool_use", "part": {"tool": "bash", "state": {"input": {"command": "git push origin main"}}}}
        finding = xs.enforced_risk(event, {}, "/tmp/wt")
        self.assertEqual(finding, {"risk": "push", "command": "git push [redacted] [redacted]"})
        gate = xs.approval_gate_for_block(finding, POL_ON)
        self.assertEqual(gate["command"], "git push [redacted] [redacted]")
        self.assertEqual(xs.approval_risk(event, POL_ON, "/tmp/wt")["command"],
                         "git push origin main")

    def test_request_approval_stores_event_ref_and_ledger_carries_it(self):
        finding = {"category": "push", "risk": "push", "command": "git push origin main"}
        record = xs.request_approval("20261120-030800-abcdef", str(self.rdir), finding,
                                     pause_point={"run_id": "20261120-030800-abcdef",
                                                  "request_id": "req-1",
                                                  "method": "session/request_permission"},
                                     event_file=self.event_file)
        self.assertEqual(record["event_ref"], {"file": self.event_file, "line": 1})
        events = self.events()
        self.assertEqual(events[0]["type"], "approval_requested")
        self.assertEqual(events[0]["event_ref"], {"file": self.event_file, "line": 1})
        lines = [json.loads(line) for line in Path(xs.LEDGER).read_text().splitlines()]
        self.assertEqual(lines[0]["event_ref"], {"file": self.event_file, "line": 1})
        self.assertEqual(lines[0]["command"], "git push origin main")


if __name__ == "__main__":
    unittest.main()
