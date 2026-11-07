import argparse
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _load

xs = _load.load()


def update(kind, **fields):
    return {"method": "session/update", "params": {"update": {"sessionUpdate": kind, **fields}}}


class WatchTests(unittest.TestCase):
    def test_pi_only_emits_completed_assistant_messages(self):
        messages = xs.WatchMessages()
        self.assertEqual(messages.feed({"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "delta": "hello"}}), [])
        self.assertEqual(messages.feed({"type": "message_end", "message": {
            "role": "user", "content": [{"type": "text", "text": "ignore"}]}}), [])
        self.assertEqual(messages.feed({"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "hello world"}]}}),
            [("msg", "hello world")])

    def test_cursor_merges_at_tools_and_prompt_end(self):
        messages = xs.WatchMessages()
        for chunk in ("hello", " world"):
            self.assertEqual(messages.feed(update("agent_message_chunk", content={"type": "text", "text": chunk})), [])
        self.assertEqual(messages.feed(update("tool_call", toolCallId="one", title="edit")),
                         [("msg", "hello world"), ("cmd", "edit")])
        self.assertEqual(messages.feed(update("tool_call_update", toolCallId="one", title="edit")), [])
        messages.feed(update("agent_message_chunk", content={"type": "text", "text": "finished"}))
        self.assertEqual(messages.feed({"id": "prompt-1", "result": {"stopReason": "end_turn"}}),
                         [("msg", "finished")])
        self.assertEqual(messages.flush(), [])

    def test_cursor_one_shot_and_codex_messages(self):
        messages = xs.WatchMessages()
        for text in ("first", " second"):
            self.assertEqual(messages.feed({"type": "assistant", "message": {"content": [
                {"type": "text", "text": text}]}}), [])
        self.assertEqual(messages.feed({"type": "result", "result": "first second"}), [("msg", "first second")])
        self.assertEqual(messages.feed({"type": "item.completed", "item": {
            "type": "agent_message", "text": "codex"}}), [("msg", "codex")])

    def test_quiet_keeps_alerts_and_heartbeat_has_lane_diff_and_last_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            repo = root / "lanes" / "L1" / "wt"
            repo.mkdir(parents=True)
            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            git("init", "-q")
            (repo / "a").write_text("base\n")
            git("add", "a")
            git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
            (repo / "a").write_text("base\nchange\n")
            rdir = root / "runs" / "r1"
            rdir.mkdir(parents=True)
            events = rdir / "events.jsonl"
            events.write_text("\n".join(json.dumps(e) for e in [
                {"type": "message_end", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "last message " + "x" * 250}]}},
                {"type": "tool_execution_start", "args": {"command": "git push"}},
                {"type": "risk_blocked", "risk": "secret print", "command": "echo [redacted]"},
                {"type": "error", "error": "expected test error"}]) + "\n")
            xs.jsave(str(rdir / "status.json"), {"state": "running", "backend": "pi", "lane": "L1",
                                                "events": str(events), "started": xs.NOW() - 120})
            args = argparse.Namespace(run_id=["r1"], label=None, quiet=True, risk=[], from_end=False,
                                      heartbeat_minutes=-1, stall_minutes=-1, max_minutes=-1)
            output = io.StringIO()
            with mock.patch.object(xs, "DATA", tmp), mock.patch.object(xs, "RUNS", str(root / "runs")), \
                    contextlib.redirect_stdout(output):
                xs.cmd_watch(args, {})
            lines = output.getvalue().splitlines()
            self.assertTrue(any("RISK[" in line for line in lines))
            self.assertTrue(any("RISK[secret print]" in line and "(blocked)" in line for line in lines))
            self.assertTrue(any("ERROR:" in line for line in lines))
            beat = next(line for line in lines if "HEARTBEAT:" in line)
            self.assertIn("2m/", beat)
            self.assertIn("cmds=1", beat)
            self.assertIn("1 file changed", beat)
            self.assertTrue(beat.endswith(xs.short("last message " + "x" * 250, 200)))
            self.assertFalse(any(token in output.getvalue() for token in ("MSG:", "CMD:", "WATCH-EXPIRED:")))

    def test_wake_reads_actionable_event_and_persists_byte_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            rdir = root / "runs" / "r1"
            rdir.mkdir(parents=True)
            events = rdir / "events.jsonl"
            events.write_text(json.dumps({"type": "message_end", "message": {"role": "user"}}) + "\n" +
                              json.dumps({"type": "error", "error": "wake me"}) + "\n")
            xs.jsave(str(rdir / "status.json"), {"state": "running", "events": str(events)})
            cursor = root / "cursor.json"
            args = argparse.Namespace(run_id=["r1"], all=False, consumer=None, cursor_file=str(cursor),
                                      risk=[], label=None)
            output = io.StringIO()
            with mock.patch.object(xs, "DATA", str(root)), mock.patch.object(xs, "RUNS", str(root / "runs")), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(xs.cmd_watch_wake(args), 0)
            self.assertIn("ERROR: wake me", output.getvalue())
            saved = json.loads(cursor.read_text())
            self.assertEqual(saved["version"], 1)
            self.assertEqual(next(iter(saved["files"].values()))["offset"], events.stat().st_size)

    def test_wake_idle_wait_uses_negligible_cpu(self):
        start = __import__("time").process_time()
        xs.watch_wait_for_change(None)
        self.assertLess(__import__("time").process_time() - start, 0.1)

    def test_wake_large_event_file_is_consumed_incrementally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            rdir = root / "runs" / "r1"
            rdir.mkdir(parents=True)
            events = rdir / "events.jsonl"
            noise = json.dumps({"type": "message_end", "message": {"role": "user"}}) + "\n"
            events.write_text(noise * 10000 + json.dumps({"type": "error", "error": "done"}) + "\n")
            xs.jsave(str(rdir / "status.json"), {"state": "running", "events": str(events)})
            args = argparse.Namespace(run_id=["r1"], all=False, consumer=None, cursor_file=None,
                                      risk=[], label=None)
            start = __import__("time").monotonic()
            with mock.patch.object(xs, "DATA", str(root)), mock.patch.object(xs, "RUNS", str(root / "runs")), \
                    contextlib.redirect_stdout(io.StringIO()):
                xs.cmd_watch_wake(args)
            self.assertLess(__import__("time").monotonic() - start, 2.0)


class WakeRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.cursor = self.root / "cursor.json"
        self.args = argparse.Namespace(run_id=["r1"], all=False, consumer=None,
                                       cursor_file=str(self.cursor), risk=[], label=None, from_end=False,
                                       stall_minutes=0)
        for name, value in (("RUNS", str(self.runs)), ("DATA", str(self.root))):
            patcher = mock.patch.object(xs, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_fixture(self, rid="r1", events=(), state="running"):
        rdir = self.runs / rid
        rdir.mkdir(exist_ok=True)
        path = rdir / "events.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        xs.jsave(str(rdir / "status.json"), {"state": state, "events": str(path)})
        return path

    def wake(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(xs.cmd_watch_wake(self.args), 0)
        return output.getvalue()

    def test_all_checkpoints_done_and_reaches_other_run_on_restart(self):
        self.run_fixture("z-finished", state="finished")
        self.run_fixture("a-running", [{"type": "error", "error": "next run"}])
        self.args.run_id, self.args.all = [], True
        self.assertIn("DONE:", self.wake())
        self.assertIn("ERROR: next run", self.wake())
        with mock.patch.object(xs, "watch_wait_for_change", side_effect=InterruptedError("idle")):
            with self.assertRaises(InterruptedError):
                self.wake()

    def test_same_inode_truncation_and_regrowth(self):
        for replacement in ("short", "regrown" * 100):
            with self.subTest(replacement=replacement[:7]):
                path = self.run_fixture(events=[{"type": "error", "error": "old" * 80}])
                self.assertIn("ERROR:", self.wake())
                inode = path.stat().st_ino
                path.write_text(json.dumps({"type": "error", "error": replacement}) + "\n")
                self.assertEqual(path.stat().st_ino, inode)
                self.assertIn(replacement[:20], self.wake())

    def test_all_discovers_runs_created_while_waiting(self):
        self.args.run_id, self.args.all = [], True
        def create_run(_):
            self.run_fixture("new", [{"type": "error", "error": "created later"}])
        with mock.patch.object(xs, "watch_wait_for_change", side_effect=create_run):
            self.assertIn("created later", self.wake())

    def test_legacy_path_cursor_migrates_before_file_is_renamed(self):
        path = self.run_fixture(events=[{"type": "error", "error": "already seen"}])
        stat = path.stat()
        self.cursor.write_text(json.dumps({"version": 1, "files": {
            f"r1:{path}": {"identity": f"{stat.st_dev}:{stat.st_ino}", "offset": stat.st_size}}}))
        with mock.patch.object(xs, "watch_wait_for_change", side_effect=InterruptedError("idle")):
            with self.assertRaises(InterruptedError):
                self.wake()
        path.rename(path.with_name("events.1.jsonl"))
        path.write_text(json.dumps({"type": "error", "error": "fresh"}) + "\n")
        self.assertIn("ERROR: fresh", self.wake())
        with mock.patch.object(xs, "watch_wait_for_change", side_effect=InterruptedError("idle")):
            with self.assertRaises(InterruptedError):
                self.wake()

    def test_rotation_and_incomplete_line(self):
        path = self.run_fixture(events=[{"type": "error", "error": "old"}])
        self.wake()
        path.rename(path.with_name("events.1.jsonl"))
        path.write_text('{"type":"error","error":"new"}')
        def append_newline(_):
            with path.open("a") as f:
                f.write("\n")
        with mock.patch.object(xs, "watch_wait_for_change", side_effect=append_newline):
            self.assertIn("ERROR: new", self.wake())

    def test_normalized_backend_errors_are_not_consumed_silently(self):
        events = [
            {"method": "turn/failed", "params": {"error": "rpc turn failed"}},
            {"method": "error", "params": {"error": "rpc error"}},
            {"type": "result", "status": "ERROR", "error": "agy error"},
            {"type": "result", "status": "INVALID", "response": "agy invalid"},
        ]
        path = self.run_fixture(events=events)
        for expected in ("rpc turn failed", "rpc error", "agy error", "agy invalid"):
            self.assertIn(expected, self.wake())
        saved = json.loads(self.cursor.read_text())
        self.assertEqual(next(iter(saved["files"].values()))["offset"], path.stat().st_size)

    def test_checkpoint_failure_emits_nothing_and_cleans_temp(self):
        self.run_fixture(events=[{"type": "error", "error": "retry"}])
        output = io.StringIO()
        with mock.patch.object(xs.os, "replace", side_effect=OSError("disk failure")), \
                contextlib.redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "cannot save watch cursor"):
            xs.cmd_watch_wake(self.args)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(list(self.root.glob(".watch-cursor-*")), [])
        self.assertIn("retry", self.wake())

    def test_interruption_at_output_does_not_replay_checkpointed_event(self):
        self.run_fixture(events=[{"type": "error", "error": "first"},
                                 {"type": "error", "error": "second"}])
        with mock.patch("builtins.print", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            xs.cmd_watch_wake(self.args)
        self.assertIn("ERROR: second", self.wake())

    def test_from_end_skips_history_but_preserves_saved_progress(self):
        path = self.run_fixture(events=[{"type": "error", "error": "history"}])
        self.args.from_end = True
        def append(_):
            with path.open("a") as f:
                f.write(json.dumps({"type": "error", "error": "fresh"}) + "\n")
                f.write(json.dumps({"type": "error", "error": "pending"}) + "\n")
        with mock.patch.object(xs, "watch_wait_for_change", side_effect=append):
            output = self.wake()
        self.assertIn("fresh", output)
        self.assertNotIn("history", output)
        self.assertIn("pending", self.wake())

    def test_concurrent_cli_watchers_share_consumer_without_duplicate_or_lost_offset(self):
        path = self.run_fixture()
        env = {**os.environ, "OPENSWARM_HOME": str(self.root)}
        command = [sys.executable, xs.__file__, "watch", "r1", "--wake", "--consumer", "parallel",
                   "--stall-minutes", "0"]
        procs = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(2)]
        def cleanup():
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()
        self.addCleanup(cleanup)
        # Both start against an empty log; append only after they have had time
        # to load it, exercising ownership rather than just atomic file replace.
        time.sleep(0.4)
        with path.open("a") as f:
            for name in ("first", "second"):
                f.write(json.dumps({"type": "error", "error": name}) + "\n")
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=8)
            self.assertEqual(proc.returncode, 0, err)
            outputs.append(out)
        self.assertEqual(sum("ERROR: first" in out for out in outputs), 1)
        self.assertEqual(sum("ERROR: second" in out for out in outputs), 1)
        saved = json.loads((self.root / "cursors" / "parallel").read_text())
        self.assertEqual(next(iter(saved["files"].values()))["offset"], path.stat().st_size)

    @unittest.skipUnless(hasattr(xs.select, "kqueue"), "requires real macOS kqueue")
    def test_real_kqueue_existing_file_waits_without_busy_spin(self):
        path = self.run_fixture()
        wall, cpu = time.monotonic(), time.process_time()
        for _ in range(4):
            xs.watch_wait_for_change(str(path))
        elapsed, used = time.monotonic() - wall, time.process_time() - cpu
        self.assertGreaterEqual(elapsed, 3.6)
        self.assertLess(used, 0.2)


if __name__ == "__main__":
    unittest.main()
