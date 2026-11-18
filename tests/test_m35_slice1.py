"""M35 slice 1 (#275): bound Pi event-log growth (#218/#274).

A fake Pi prints the incident's event shape -- every `message_update`
repeating the full cumulative assistant text as both
`assistantMessageEvent.partial` and the top-level `message` snapshot --
so the suite can measure bytes and memory without a real Pi binary, a
real provider, or any account. No network is used.
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
POLICY = xs.load_policy()
POLICY["pi"].update({"provider": None, "default_model": None, "require_isolation": False})

FAKE_PI = r"""#!/usr/bin/env python3
import json, os, sys, time
n = int(os.environ.get("FAKE_PI_N", "120"))
chunk = int(os.environ.get("FAKE_PI_CHUNK", "80"))
mode = os.environ.get("FAKE_PI_MODE", "ok")
def emit(obj):
    print(json.dumps(obj), flush=True)
emit({"type": "session", "id": "s1"})
emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
emit({"type": "message_update", "assistantMessageEvent": {"type": "text_start", "contentIndex": 0, "partial": ""}})
text = ""
for i in range(1, n + 1):
    delta = ("w%04d " % i) * (chunk // 5)
    text += delta[:chunk]
    msg = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    emit({"type": "message_update", "usage": {"input": 100 + i, "output": i},
          "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": delta[:chunk], "partial": text},
          "message": msg})
    if mode == "sleep-after-half" and i == n // 2:
        time.sleep(30)
emit({"type": "message_update", "assistantMessageEvent": {"type": "text_end", "contentIndex": 0, "content": text, "partial": text},
      "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
emit({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash", "args": {"command": "git status"}})
emit({"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash", "result": "ok", "isError": False})
body = json.dumps({"status": "done", "summary": "s"})
emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": body}],
      "usage": {"input": 400, "output": 300, "cacheRead": 5, "cacheWrite": 1}}})
emit({"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": body}]}]})
"""


def build_stream(n=300, chunk=80):
    """Sanitized incident-shape stream: cumulative snapshots per update.

    Returns (lines, final_text, body). Each update appends exactly one
    `chunk`-char delta, so the raw stream is O(n * final_text) bytes.
    """
    lines = [json.dumps({"type": "session", "id": "s1"}),
             json.dumps({"type": "message_start", "message": {"role": "assistant", "content": []}}),
             json.dumps({"type": "message_update", "assistantMessageEvent":
                         {"type": "text_start", "contentIndex": 0, "partial": ""}})]
    text = ""
    for i in range(1, n + 1):
        delta = (("w%04d " % i) * (chunk // 5))[:chunk]
        text += delta
        msg = {"role": "assistant", "content": [{"type": "text", "text": text}]}
        lines.append(json.dumps({"type": "message_update", "usage": {"input": 100 + i, "output": i},
                                 "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0,
                                                           "delta": delta, "partial": text},
                                 "message": msg}))
    lines.append(json.dumps({"type": "message_update", "assistantMessageEvent":
                             {"type": "text_end", "contentIndex": 0, "content": text, "partial": text},
                             "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}))
    lines.append(json.dumps({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash",
                             "args": {"command": "git status"}}))
    lines.append(json.dumps({"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash",
                             "result": "ok", "isError": False}))
    body = json.dumps({"status": "done", "summary": "s"})
    lines.append(json.dumps({"type": "message_end", "message":
                             {"role": "assistant", "content": [{"type": "text", "text": body}],
                              "usage": {"input": 400, "output": 300, "cacheRead": 5, "cacheWrite": 1}}}))
    lines.append(json.dumps({"type": "agent_end", "messages":
                             [{"role": "assistant", "content": [{"type": "text", "text": body}]}]}))
    return lines, text, body


def compact(lines):
    compactor = xs.PiEventCompactor()
    out, peak = [], 0
    for line in lines:
        out.extend(compactor.filter_line(line))
        peak = max(peak, compactor.state_bytes())
    return out, compactor, peak


def read_events(path):
    with open(path, errors="ignore", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def reconstruct(events):
    """Assistant text from compacted deltas, per contentIndex."""
    parts = {}
    for ev in events:
        if ev.get("type") != "message_update":
            continue
        ame = ev.get("assistantMessageEvent") or {}
        if ame.get("type") == "text_delta":
            parts.setdefault(ame.get("contentIndex", 0), "")
            parts[ame.get("contentIndex", 0)] += ame.get("delta", "")
    return "".join(parts[k] for k in sorted(parts))


class CompactorTests(unittest.TestCase):
    def test_output_bytes_and_memory_are_bounded(self):
        lines, final_text, _ = build_stream()
        raw_bytes = sum(len(line) + 1 for line in lines)
        out, compactor, peak = compact(lines)
        out_bytes = sum(len(line) + 1 for line in out)
        self.assertEqual(len(out), len(lines))  # every event kept, none dropped
        self.assertLess(out_bytes, raw_bytes // 20)  # measured ~59x
        self.assertLessEqual(peak, len(final_text))  # one live snapshot, never the log
        self.assertEqual(compactor.events_compacted, 301)  # 300 deltas + text_end slimmed

    def test_reconstruction_usage_and_final_are_exact(self):
        lines, final_text, body = build_stream()
        out, _, _ = compact(lines)
        self.assertEqual(reconstruct([json.loads(line) for line in out]), final_text)
        with tempfile.TemporaryDirectory() as tmp:
            raw_path = os.path.join(tmp, "raw.jsonl")
            out_path = os.path.join(tmp, "out.jsonl")
            with open(raw_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(out) + "\n")
            self.assertEqual(xs.pi_usage(out_path), xs.pi_usage(raw_path))
            usage, text, errors = xs.pi_usage(out_path)
            self.assertEqual(usage, {"input": 400, "output": 300, "cacheRead": 5, "cacheWrite": 1})
            self.assertEqual(json.loads(text), json.loads(body))
            self.assertEqual(errors, [])

    def test_non_update_events_pass_through_byte_identical(self):
        lines, _, _ = build_stream()
        compactor = xs.PiEventCompactor()
        for line in lines:
            out = compactor.filter_line(line)
            ev = json.loads(line)
            if ev.get("type") != "message_update":
                self.assertEqual(out, [line], line[:120])

    def test_non_append_snapshot_passes_through_and_rebaselines(self):
        compactor = xs.PiEventCompactor()
        first = {"type": "message_update", "assistantMessageEvent":
                 {"type": "text_delta", "contentIndex": 0, "delta": "hello ", "partial": "hello "},
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "hello "}]}}
        # First sight compacts too: with an empty baseline the delta IS the
        # whole text so far, so nothing is lost by dropping the snapshots.
        slim_first = compactor.compact_event(first)
        self.assertEqual(slim_first["assistantMessageEvent"],
                         {"type": "text_delta", "contentIndex": 0, "delta": "hello "})
        self.assertTrue(slim_first["_swrm_compacted"])
        regen = {"type": "message_update", "assistantMessageEvent":
                 {"type": "text_delta", "contentIndex": 0, "delta": "bye", "partial": "bye"},
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "bye"}]}}
        self.assertIs(compactor.compact_event(regen), regen)  # regeneration, not an append
        nxt = {"type": "message_update", "assistantMessageEvent":
               {"type": "text_delta", "contentIndex": 0, "delta": "!", "partial": "bye!"},
               "message": {"role": "assistant", "content": [{"type": "text", "text": "bye!"}]}}
        slim = compactor.compact_event(nxt)
        self.assertEqual(slim["assistantMessageEvent"], {"type": "text_delta", "contentIndex": 0, "delta": "!"})
        self.assertTrue(slim["_swrm_compacted"])

    def test_non_text_content_and_unknown_shapes_pass_through(self):
        compactor = xs.PiEventCompactor()
        tool_call = {"type": "message_update", "assistantMessageEvent":
                     {"type": "text_delta", "contentIndex": 0, "delta": "x", "partial": "x"},
                     "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "bash"}]}}
        self.assertIs(compactor.compact_event(tool_call), tool_call)
        unknown = {"type": "message_update", "assistantMessageEvent":
                   {"type": "mystery_kind", "partial": "x" * 1000},
                   "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 1000}]}}
        self.assertIs(compactor.compact_event(unknown), unknown)

    def test_delta_only_stream_passes_through_unchanged(self):
        # Newer Pi versions already emit delta-only updates (no snapshots).
        compactor = xs.PiEventCompactor()
        line = json.dumps({"type": "message_update", "usage": {"input": 7, "output": 3},
                           "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "hi"}})
        self.assertEqual(compactor.filter_line(line), [line])
        self.assertEqual(compactor.events_compacted, 0)

    def test_interrupted_replay_keeps_partial_text_and_tool_receipts(self):
        lines, final_text, _ = build_stream()
        out, _, _ = compact(lines)
        cut = out[:153]  # session + start + text_start + 150 deltas, no message_end
        events = [json.loads(line) for line in cut]
        self.assertEqual(reconstruct(events), final_text[:150 * 80])
        self.assertEqual([e for e in events if e.get("type") == "tool_execution_start"], [])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cut.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(cut) + "\n")
            usage, text, errors = xs.pi_usage(path)
            self.assertEqual(usage, {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0})
            self.assertIsNone(text)
            self.assertEqual(errors, [])
        rows = []
        messages = xs.WatchMessages()
        for ev in events:
            rows.extend(messages.feed(ev))
        self.assertEqual(rows, [])  # partial text is not a completed message

    def test_error_message_end_keeps_usage_and_errors(self):
        lines = [
            json.dumps({"type": "message_update", "assistantMessageEvent":
                        {"type": "text_delta", "contentIndex": 0, "delta": "ab", "partial": "ab"},
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "ab"}]}}),
            json.dumps({"type": "message_end", "message":
                        {"role": "assistant", "content": [{"type": "text", "text": "ab"}],
                         "usage": {"input": 10, "output": 2, "cacheRead": 0, "cacheWrite": 0},
                         "stopReason": "error", "errorMessage": "provider blew up"}}),
            json.dumps({"type": "agent_end", "messages": []}),
        ]
        out, _, _ = compact(lines)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "err.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(out) + "\n")
            usage, text, errors = xs.pi_usage(path)
        self.assertEqual(usage["input"], 10)
        self.assertEqual(text, "ab")
        self.assertEqual(errors, ["provider blew up"])
        self.assertEqual(xs.summarize_event(json.loads(out[-1])), ("end", ""))

    def test_reader_compatibility_raw_vs_compacted(self):
        lines, _, _ = build_stream()
        out, _, _ = compact(lines)
        raw_events = [json.loads(line) for line in lines]
        out_events = [json.loads(line) for line in out]

        def watch(evs):
            messages = xs.WatchMessages()
            rows = []
            for ev in evs:
                rows.extend(messages.feed(ev))
            rows.extend(messages.flush())
            return rows

        self.assertEqual(watch(raw_events), watch(out_events))
        self.assertEqual([xs.summarize_event(e) for e in raw_events],
                         [xs.summarize_event(e) for e in out_events])

    def test_lane_path_compacts_the_same_way(self):
        lines, final_text, _ = build_stream()
        compactor = xs.PiEventCompactor()
        recorded = [compactor.compact_event(json.loads(line)) for line in lines]
        raw_bytes = sum(len(line) + 1 for line in lines)
        out_bytes = sum(len(json.dumps(e, ensure_ascii=False)) + 1 for e in recorded)
        self.assertLess(out_bytes, raw_bytes // 20)
        self.assertEqual(reconstruct(recorded), final_text)
        end = [e for e in recorded if e.get("type") == "message_end"][0]
        self.assertEqual(end["message"]["usage"],
                         {"input": 400, "output": 300, "cacheRead": 5, "cacheWrite": 1})
        self.assertEqual([e for e in recorded if e.get("type") == "tool_execution_start"][0]["args"],
                         {"command": "git status"})


class RunProcFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-m35-")
        self.script = os.path.join(self.tmp, "pi")
        with open(self.script, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(FAKE_PI))
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self.cwd = os.path.join(self.tmp, "work")
        os.makedirs(self.cwd)
        self._env = {k: os.environ.get(k) for k in ("OPENSWARM_PI_BIN", "OPENSWARM_STATE_FILE",
                                                    "FAKE_PI_MODE", "FAKE_PI_N", "FAKE_PI_CHUNK")}
        os.environ["OPENSWARM_PI_BIN"] = self.script
        os.environ["OPENSWARM_STATE_FILE"] = _load.FIXTURE_STATE
        os.environ["FAKE_PI_N"] = "120"
        os.environ["FAKE_PI_CHUNK"] = "80"
        os.environ["FAKE_PI_MODE"] = "ok"
        if os.path.exists(xs.BREAKERS):
            os.remove(xs.BREAKERS)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_proc_filter_bounds_bytes_and_keeps_receipts(self):
        evp = os.path.join(self.tmp, "events.jsonl")
        compactor = xs.PiEventCompactor()
        rc = xs._run_proc([self.script], self.cwd, os.environ.copy(), 60, evp,
                          stream_filter=compactor.filter_line)
        self.assertEqual(rc, 0)
        events = read_events(evp)
        self.assertEqual(len(events), 128)  # 120 updates + session/start/end/tool x2/end/agent
        size = os.path.getsize(evp)
        self.assertLess(size, 60_000)
        usage, text, errors = xs.pi_usage(evp)
        self.assertEqual(usage, {"input": 400, "output": 300, "cacheRead": 5, "cacheWrite": 1})
        self.assertEqual(json.loads(text), {"status": "done", "summary": "s"})
        self.assertEqual(errors, [])
        self.assertEqual(reconstruct(events), "".join(
            (("w%04d " % i) * 16)[:80] for i in range(1, 121)))
        self.assertGreater(compactor.events_compacted, 100)

    def test_run_proc_without_filter_reproduces_unbounded_growth(self):
        evp = os.path.join(self.tmp, "events-raw.jsonl")
        rc = xs._run_proc([self.script], self.cwd, os.environ.copy(), 60, evp)
        self.assertEqual(rc, 0)
        self.assertGreater(os.path.getsize(evp), 1_000_000)  # the bug, measured

    def test_run_proc_timeout_flushes_partial_tail(self):
        os.environ["FAKE_PI_MODE"] = "sleep-after-half"
        evp = os.path.join(self.tmp, "events-timeout.jsonl")
        compactor = xs.PiEventCompactor()
        rc = xs._run_proc([self.script], self.cwd, os.environ.copy(), 3, evp,
                          stream_filter=compactor.filter_line)
        self.assertEqual(rc, 124)
        events = read_events(evp)  # every line still parses
        self.assertEqual(len(events), 63)  # session + start + text_start + 60 deltas
        self.assertEqual(reconstruct(events), "".join(
            (("w%04d " % i) * 16)[:80] for i in range(1, 61)))
        self.assertLess(os.path.getsize(evp), 30_000)

    def test_run_proc_cancel_flushes_partial_tail(self):
        os.environ["FAKE_PI_MODE"] = "sleep-after-half"
        evp = os.path.join(self.tmp, "events-cancel.jsonl")
        rdir = self.tmp

        def stopper():
            for _ in range(100):
                if os.path.exists(os.path.join(rdir, "status.json")):
                    break
                threading.Event().wait(0.05)
            threading.Event().wait(0.5)
            xs.set_status(rdir, stop_requested=True)

        thread = threading.Thread(target=stopper, daemon=True)
        thread.start()
        compactor = xs.PiEventCompactor()
        rc = xs._run_proc([self.script], self.cwd, os.environ.copy(), 60, evp,
                          stream_filter=compactor.filter_line)
        self.assertEqual(rc, 130)
        events = read_events(evp)
        self.assertEqual(len(events), 63)
        self.assertEqual(reconstruct(events), "".join(
            (("w%04d " % i) * 16)[:80] for i in range(1, 61)))

    def test_exec_pi_wires_the_filter(self):
        evp = os.path.join(self.tmp, "events-exec-pi.jsonl")
        rc, final, meta, err_text = xs.exec_pi({"model": None}, {}, "do it", self.cwd,
                                               self.tmp, 60, evp)
        self.assertEqual(rc, 0, err_text)
        self.assertEqual(final, {"status": "done", "summary": "s"})
        self.assertEqual(meta["usage"], {"input": 400, "output": 300, "cacheRead": 5, "cacheWrite": 1})
        self.assertLess(os.path.getsize(evp), 60_000)
        self.assertEqual(reconstruct(read_events(evp)), "".join(
            (("w%04d " % i) * 16)[:80] for i in range(1, 121)))


if __name__ == "__main__":
    unittest.main()
