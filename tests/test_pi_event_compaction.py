"""Isolated, synthetic Pi one-shot streams. Never invokes the Pi executable."""
import copy
import argparse
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import tracemalloc
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# _load also allocates an isolated home at import time; keep that home inside
# this worktree even when the host acceptance command does not set TMPDIR.
with mock.patch.object(tempfile, "tempdir", str(ROOT)):
    import _load
from fixtures.pi_legacy_reader import pi_usage as legacy_pi_usage

xs = _load.load()
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "pi_cumulative_sanitized.jsonl"


def snapshot(text, **extra):
    message = {"role": "assistant", "content": [{"type": "text", "text": text, "index": 0}],
               "api": "fixture", "provider": "fixture", "model": "fixture",
               "usage": {"input": 9, "output": 1}, "stopReason": "stop",
               "timestamp": 100, "responseId": "fixture"}
    return {"type": "message_update", "message": message,
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0,
                                      "partial": message}, **extra}


def wire(event):
    return (json.dumps(event, ensure_ascii=False) + "\n").encode()


class PiCompactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pi-compaction-", dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.path = self.root / "events.jsonl"

    def adapt(self, events):
        out = io.BytesIO()
        writer = xs.PiEventWriter(out, lambda event: None)
        for event in events:
            writer.feed(wire(event))
        writer.finish()
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def fixture(self):
        with FIXTURE.open() as stream:
            return [json.loads(line) for line in stream]

    def test_fixture_roundtrips_all_fields_and_official_usage(self):
        original = self.fixture()
        compact = self.adapt(original)
        decoder = xs.PiEventCompactor()
        self.assertEqual([decoder.restore(event) for event in compact], original)
        # Normal-size records retain the base reader's message_end semantics.
        self.path.write_bytes(b"".join(wire(event) for event in original))
        expected = legacy_pi_usage(self.path)
        self.path.write_bytes(b"".join(wire(event) for event in compact))
        self.assertEqual(legacy_pi_usage(self.path), expected)
        self.assertEqual(xs.pi_usage(self.path), expected)
        self.assertEqual(expected[0], {"input": 18, "output": 9, "cacheRead": 2, "cacheWrite": 1})
        result = xs.extract_json(expected[1])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["followups"], ["verify commit receipt"])
        for before, after in zip(original, compact):
            if before["type"] != "message_update":
                self.assertEqual(before, after)

    def test_nonprefix_rewrites_transient_evidence_and_unknown_shapes_survive(self):
        events = [snapshot("pending commit receipt"), snapshot("commit uncertain"),
                  snapshot("pending commit receipt", error="cancelled", receipt={"confirmed": False}),
                  {"type": "message_update", "assistantMessageEvent": {
                      "type": "future_delta", "delta": "unique evidence", "partial": {"unknown": [1, 2]}}},
                  {"type": "future_event", "evidence": {"raw": "preserve"}}]
        compact = self.adapt(events)
        decoder = xs.PiEventCompactor()
        self.assertEqual([decoder.restore(event) for event in compact], events)
        self.assertEqual(compact[1][xs.PiEventCompactor.MARKER]["snapshots"][0]["prefix"], 0)
        self.assertEqual(compact[-2:], events[-2:])

    def test_unicode_multiple_blocks_and_message_boundaries(self):
        first = snapshot("检查")
        first["message"]["content"].append({"type": "thinking", "thinking": "private reasoning"})
        second = copy.deepcopy(first)
        second["message"]["content"][0]["text"] += "✓"
        second["message"]["content"][1]["thinking"] += " extended"
        events = [first, second, {"type": "message_end", "message": {"role": "assistant"}},
                  snapshot("检查✓")]
        compact = self.adapt(events)
        decoder = xs.PiEventCompactor()
        self.assertEqual([decoder.restore(event) for event in compact], events)
        self.assertEqual(xs.summarize_event(compact[1]), ("msg", "✓"))
        self.assertEqual(compact[-1][xs.PiEventCompactor.MARKER]["snapshots"][0]["prefix"], 0)

    def test_chunk_boundaries_and_complete_prefix_visible_before_finish(self):
        data = wire(snapshot("检查")) + b"not-json\r\n" + b"[1,2]\n"
        out, inspected = io.BytesIO(), []
        writer = xs.PiEventWriter(out, inspected.append)
        for byte in data:
            writer.feed(bytes([byte]))
        self.assertEqual(len(inspected), 1)
        self.assertIn(b"not-json\r\n[1,2]\n", out.getvalue())
        decoded = xs.PiEventCompactor().restore(json.loads(out.getvalue().splitlines()[0]))
        self.assertEqual(decoded, snapshot("检查"))
        before = out.getvalue()
        writer.finish()
        self.assertEqual(out.getvalue(), before)

    def test_interrupted_torn_record_preserved_and_reported(self):
        out = io.BytesIO()
        writer = xs.PiEventWriter(out, lambda event: None)
        writer.feed(wire(snapshot("last meaningful progress")))
        complete = out.getvalue()
        writer.feed(b'{"type":"message_end","message":')
        self.assertEqual(out.getvalue(), complete)
        writer.finish()
        self.assertIn(b'{"type":"message_end","message":\n', out.getvalue())
        self.assertIn("pi_stream_incomplete", json.loads(out.getvalue().splitlines()[-1])["error"])
        self.assertEqual(xs.summarize_event(json.loads(complete)), ("msg", "last meaningful progress"))

    def test_oversized_record_retained_raw_with_bounded_pending_and_error(self):
        out = io.BytesIO()
        writer = xs.PiEventWriter(out, lambda event: self.fail("oversized event must fail closed"))
        data = wire({"type": "tool_execution_start", "args": {"command": "x" * 5000}})
        with mock.patch.object(xs.PiEventCompactor, "MAX_EVENT_BYTES", 1024):
            for start in range(0, len(data), 256):
                writer.feed(data[start:start + 256])
                self.assertLessEqual(len(writer.pending), 1024)
        writer.finish()
        self.assertTrue(out.getvalue().startswith(data))
        self.assertIn("risk inspection unavailable", json.loads(out.getvalue().splitlines()[-1])["error"])

    def test_usage_drains_oversized_raw_record_and_retains_later_official_usage(self):
        oversized = {"type": "message_end", "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "x" * 10000}], "usage": {"input": 999}}}
        final = {"type": "message_end", "message": {"role": "assistant", "usage": {"input": 4},
                 "content": [{"type": "text", "text": "official final"}]}}
        self.path.write_bytes(wire(oversized) + b"[1,2]\n" + wire(final))
        with mock.patch.object(xs.PiEventCompactor, "MAX_EVENT_BYTES", 1024):
            usage, text, errors = xs.pi_usage(self.path)
        self.assertEqual(usage["input"], 4)
        self.assertEqual(text, "official final")
        self.assertEqual(errors, ["pi_event_too_large: retained raw; usage/final may be incomplete"])

    def test_error_cancel_and_tool_lifecycle_replay(self):
        for reason in ("error", "aborted"):
            events = [snapshot("progress before " + reason),
                      {"type": "tool_execution_start", "toolCallId": "t", "toolName": "bash",
                       "args": {"command": "git status"}},
                      {"type": "tool_execution_end", "toolCallId": "t", "isError": True,
                       "result": {"content": [{"type": "text", "text": "commit receipt unavailable"}]}},
                      {"type": "message_end", "message": {"role": "assistant", "content": [],
                       "stopReason": reason, "errorMessage": reason, "usage": {"input": 7}}},
                      {"type": "error", "error": reason}, {"type": "agent_end", "messages": []}]
            compact = self.adapt(events)
            decoder = xs.PiEventCompactor()
            self.assertEqual([decoder.restore(event) for event in compact], events)
            self.path.write_bytes(b"".join(wire(event) for event in compact))
            usage, final, errors = xs.pi_usage(self.path)
            self.assertEqual(usage["input"], 7)
            self.assertIsNone(final)
            self.assertEqual(errors, [reason])
            self.assertEqual(legacy_pi_usage(self.path), (usage, final, errors))

    def test_old_watch_reader_and_saved_cursor_need_no_decoder(self):
        original = self.fixture()
        compact = self.adapt(original)
        # Frozen Pi cases of the ba925861 summarize_event reader. Unknown update
        # fields/types are ignored; final messages and tool starts stay native.
        def old_reader(events):
            rows = []
            for event in events:
                if event.get("type") == "message_end" and event["message"].get("role") == "assistant":
                    rows.append(("msg", "".join(block.get("text", "") for block in
                                 event["message"].get("content", []) if block.get("type") == "text")))
                elif event.get("type") == "tool_execution_start":
                    rows.append(("cmd", event["args"].get("command") or event.get("toolName", "tool")))
                elif event.get("type") == "agent_end":
                    rows.append(("end", ""))
            return rows
        self.assertEqual(old_reader(original), old_reader(compact))
        self.assertEqual(xs.WatchMessages().feed(compact[2]), [("msg", " changes")])
        with self.assertRaisesRegex(ValueError, "replay from file start"):
            xs.PiEventCompactor().restore(compact[2])

    def test_report_resume_keep_partial_result_and_unknown_commit(self):
        rdir = self.root / "runs" / "fixture-run"
        rdir.mkdir(parents=True)
        self.path.write_bytes(b"".join(wire(event) for event in self.adapt(self.fixture())))
        final = xs.extract_json(xs.pi_usage(self.path)[1])
        xs.jsave(str(rdir / "status.json"), {"state": "finished", "status": "partial",
                 "backend": "pi", "events": str(self.path), "started": 90, "finished": 100,
                 "commit": None})
        xs.jsave(str(rdir / "result.json"), final)
        state = json.loads(pathlib.Path(_load.FIXTURE_STATE).read_text())
        output = io.StringIO()
        with mock.patch.object(xs, "RUNS", str(rdir.parent)), \
                mock.patch.object(xs, "LEDGER", str(self.root / "ledger.jsonl")), \
                mock.patch.object(xs, "NOW", return_value=100), \
                mock.patch.object(xs, "get_state", return_value=state), \
                mock.patch.object(xs, "lane_status_rows", return_value=[]):
            report = xs.checkin_report({}, 60)
            row = report["recent_finished"][0]
            self.assertEqual((row["status"], row["cmds"], row["commit"]), ("partial", 1, None))
            self.assertEqual(report["attention"][0]["id"], "fixture-run")
            self.assertIn("commit receipt unavailable", row["last_msg"])
            with contextlib.redirect_stdout(output):
                xs.cmd_resume(argparse.Namespace(since_hours=6, json=True))
        resumed = json.loads(output.getvalue())["recent_finished"][0]
        self.assertEqual(resumed["status"], "partial")
        self.assertEqual(resumed["result_path"], str(rdir / "result.json"))

    def test_generated_incident_scale_bytes_and_peak_memory(self):
        count, raw_bytes = 5984, 0
        text = ""
        tracemalloc.start()
        try:
            with self.path.open("wb") as out:
                writer = xs.PiEventWriter(out, lambda event: None)
                for index in range(count):
                    text += "chunk✓ "
                    raw = wire(snapshot(text))
                    raw_bytes += len(raw)
                    for start in range(0, len(raw), 65536):
                        writer.feed(raw[start:start + 65536])
                writer.finish()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        output_bytes = self.path.stat().st_size
        print(f"PI_COMPACTION updates={count} input_bytes={raw_bytes} output_bytes={output_bytes} "
              f"peak_tracemalloc_bytes={peak}", flush=True)
        self.assertGreater(raw_bytes, 240_000_000)
        self.assertLess(output_bytes, raw_bytes // 20)
        self.assertLess(peak, 16 * 1024 * 1024)
        decoder = xs.PiEventCompactor()
        with self.path.open() as stream:
            seen = 0
            for seen, line in enumerate(stream, 1):
                restored = decoder.restore(json.loads(line))
                self.assertEqual(restored, snapshot("chunk✓ " * seen))
        self.assertEqual(seen, count)


def run_script(case, body, timeout=3):
    script = case.root / "fixture_process.py"
    script.write_text("#!" + sys.executable + "\nimport json, sys, time\n" + body)
    script.chmod(0o755)
    policy = xs.load_policy()
    policy["pi"].update({"require_isolation": False})
    with mock.patch.object(xs, "pi_bin", return_value=str(script)), \
            mock.patch.object(xs, "load_policy", return_value=policy), \
            mock.patch.object(xs, "executor_worktree_root", side_effect=lambda path: path):
        return xs.exec_pi({"model": None}, {}, "fixture", str(case.root), str(case.root),
                          timeout, str(case.path))


class PiOneShotAdapterTests(unittest.TestCase):
    setUp = PiCompactionTests.setUp

    def test_actual_exec_pi_path_uses_adapter_and_preserves_result(self):
        events = PiCompactionTests.fixture(self)
        body = "events = " + repr(events) + "\nfor event in events:\n    print(json.dumps(event), flush=True)\n"
        rc, result, meta, error = run_script(self, body)
        self.assertEqual((rc, error), (0, ""))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(meta["usage"]["input"], 18)
        with self.path.open() as stream:
            written = [json.loads(line) for line in stream]
        self.assertIn(xs.PiEventCompactor.MARKER, written[1])
        decoder = xs.PiEventCompactor()
        self.assertEqual([decoder.restore(event) for event in written], events)

    def test_risk_fast_exit_still_inspects_original_and_retains_evidence(self):
        event = {"type": "tool_execution_start", "toolName": "bash", "args": {"command": "git push fixture"}}
        rc, final, _, _ = run_script(self, "print(" + repr(json.dumps(event)) + ", flush=True)\n")
        self.assertEqual(rc, 133)
        self.assertIsNone(final)
        self.assertEqual(xs.jload(str(self.root / "status.json"))["risk_blocks"][0]["risk"], "push")
        with self.path.open() as stream:
            self.assertEqual(json.loads(next(stream)), event)

    def test_native_cancel_or_error_with_zero_exit_does_not_report_success(self):
        for mode in ("aborted", "error"):
            with self.subTest(mode=mode):
                events = [{"type": "message_end", "message": {"role": "assistant",
                          "content": [{"type": "text", "text": '{"status":"done"}'}],
                          "usage": {"input": 7}, "stopReason": "aborted" if mode == "aborted" else "stop"}}]
                if mode == "error":
                    events.append({"type": "error", "error": "fixture stream failed"})
                body = "for event in " + repr(events) + ":\n    print(json.dumps(event), flush=True)\n"
                rc, _, meta, error = run_script(self, body)
                self.assertEqual(rc, 1)
                self.assertEqual(meta["usage"]["input"], 7)
                self.assertIn("aborted" if mode == "aborted" else "fixture stream failed", error)

    def test_timeout_cancel_and_error_exit_keep_progress(self):
        for mode in ("timeout", "cancel", "error", "interrupted"):
            with self.subTest(mode=mode):
                (self.root / "status.json").unlink(missing_ok=True)
                body = "print(" + repr(json.dumps(snapshot("pending receipt"))) + ", flush=True)\n"
                body += "sys.exit(9)\n" if mode in ("error", "interrupted") else "time.sleep(30)\n"
                if mode == "interrupted":
                    body = body.replace("sys.exit(9)", "sys.stdout.write('{\\\"type\\\":'); sys.stdout.flush(); sys.exit(9)")
                # Cancel after the first event has been persisted, as a real stop
                # request would be; no timing race or real Pi is involved.
                feed = xs.PiEventWriter.feed
                def cancel(writer, data):
                    feed(writer, data)
                    if mode == "cancel":
                        xs.set_status(str(self.root), stop_requested=True)
                with mock.patch.object(xs.PiEventWriter, "feed", cancel):
                    rc, final, usage, _ = run_script(self, body, timeout=0.2)
                self.assertEqual(rc, {"timeout": 124, "cancel": 130, "error": 9, "interrupted": 9}[mode])
                self.assertIsNone(final)
                self.assertEqual(usage["usage"]["input"], 0)
                with self.path.open() as stream:
                    first = json.loads(next(stream))
                self.assertEqual(xs.PiEventCompactor().restore(first), snapshot("pending receipt"))
                if mode == "interrupted":
                    self.assertIn(b"pi_stream_incomplete", self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
