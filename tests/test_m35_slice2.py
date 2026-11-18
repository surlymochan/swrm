"""M35 slice 2 (#277): real-run follow-up to slice 1 (#275).

Slice 1 compacted plain-text deltas only. A real Pi run on the merged
fix (run 20261118-152400-fd37e7, MiniMax-M3.1-Flash-Preview) showed the
same O(N*F) growth in the other two delta kinds: 2,273 thinking_delta
events held 27.5 MB of a 31.3 MB event file in ~60 seconds, and 454
toolcall_delta events held another 3.3 MB. The fixtures below reproduce
those exact wire shapes (sanitized, small): thinking deltas repeat the
full cumulative `thinking` string in both `partial` and the top-level
`message` snapshot, toolcall deltas repeat the cumulative `partialJson`
arguments string the same way. No real Pi binary, provider, or account
is used; no network.
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
POLICY = xs.load_policy()
POLICY["pi"].update({"provider": None, "default_model": None, "require_isolation": False})

FAKE_PI_MIXED = r"""#!/usr/bin/env python3
import json, os, sys
n = int(os.environ.get("FAKE_PI_N", "120"))
chunk = int(os.environ.get("FAKE_PI_CHUNK", "80"))
def emit(obj):
    print(json.dumps(obj), flush=True)
emit({"type": "session", "id": "s1"})
emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
emit({"type": "message_update", "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0,
      "partial": {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "index": 0}]}}})
thinking = ""
for i in range(1, n + 1):
    delta = (("t%04d " % i) * (chunk // 5))[:chunk]
    thinking += delta
    snap = {"role": "assistant", "content": [{"type": "thinking", "thinking": thinking, "index": 0}]}
    emit({"type": "message_update",
          "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0, "delta": delta, "partial": snap},
          "message": snap})
emit({"type": "message_update", "assistantMessageEvent": {"type": "thinking_end", "contentIndex": 0,
      "content": thinking, "partial": thinking},
      "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": thinking, "index": 0}]}})
emit({"type": "message_update", "assistantMessageEvent": {"type": "text_start", "contentIndex": 1, "partial": ""}})
text = ""
for i in range(1, 11):
    delta = "word%d " % i
    text += delta
    msg = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    emit({"type": "message_update",
          "assistantMessageEvent": {"type": "text_delta", "contentIndex": 1, "delta": delta, "partial": text},
          "message": msg})
emit({"type": "message_update", "assistantMessageEvent": {"type": "text_end", "contentIndex": 1,
      "content": text, "partial": text},
      "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
body = json.dumps({"status": "done", "summary": "s"})
emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": body}],
      "usage": {"input": 400, "output": 300, "cacheRead": 5, "cacheWrite": 1}}})
emit({"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": body}]}]})
"""


def thinking_stream(n=300, chunk=80):
    """Cumulative thinking snapshots, real-run shape. Returns (lines, thinking)."""
    lines = [json.dumps({"type": "session", "id": "s1"}),
             json.dumps({"type": "message_start", "message": {"role": "assistant", "content": []}}),
             json.dumps({"type": "message_update", "assistantMessageEvent":
                         {"type": "thinking_start", "contentIndex": 0,
                          "partial": {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "index": 0}]}}})]
    thinking = ""
    for i in range(1, n + 1):
        delta = (("t%04d " % i) * (chunk // 5))[:chunk]
        thinking += delta
        snap = {"role": "assistant",
                "content": [{"type": "thinking", "thinking": thinking, "thinkingSignature": "", "index": 0}]}
        lines.append(json.dumps({"type": "message_update",
                                 "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0,
                                                           "delta": delta, "partial": snap},
                                 "message": snap}))
    lines.append(json.dumps({"type": "message_update", "assistantMessageEvent":
                             {"type": "thinking_end", "contentIndex": 0, "content": thinking, "partial": thinking},
                             "message": {"role": "assistant",
                                         "content": [{"type": "thinking", "thinking": thinking,
                                                      "thinkingSignature": "sig", "index": 0}]}}))
    body = json.dumps({"status": "done", "summary": "s"})
    lines.append(json.dumps({"type": "message_end", "message":
                             {"role": "assistant", "content": [{"type": "text", "text": body}],
                              "usage": {"input": 400, "output": 300, "cacheRead": 5, "cacheWrite": 1}}}))
    lines.append(json.dumps({"type": "agent_end", "messages":
                             [{"role": "assistant", "content": [{"type": "text", "text": body}]}]}))
    return lines, thinking


def toolcall_stream(fragments=60, frag_len=40):
    """Cumulative partialJson snapshots, real-run shape. Returns (lines, args)."""
    args = {"command": "ls -la; " + "x" * 2000}
    full_json = json.dumps(args)
    frags = [full_json[i:i + frag_len] for i in range(0, len(full_json), frag_len)]
    lead = "I'll start by reading the three files in the repo."
    lines = [json.dumps({"type": "session", "id": "s1"}),
             json.dumps({"type": "message_start", "message": {"role": "assistant", "content": []}}),
             json.dumps({"type": "message_update", "assistantMessageEvent":
                         {"type": "toolcall_start", "contentIndex": 1,
                          "partial": {"role": "assistant",
                                      "content": [{"type": "text", "text": lead},
                                                  {"type": "toolCall", "id": "call_1", "name": "bash",
                                                   "arguments": {}, "partialJson": "", "index": 1}]}}})]
    partial = ""
    for frag in frags:
        partial += frag
        snap = {"role": "assistant",
                "content": [{"type": "text", "text": lead},
                            {"type": "toolCall", "id": "call_1", "name": "bash",
                             "arguments": {}, "partialJson": partial, "index": 1}]}
        lines.append(json.dumps({"type": "message_update",
                                 "assistantMessageEvent": {"type": "toolcall_delta", "contentIndex": 1,
                                                           "delta": frag, "partial": snap},
                                 "message": snap}))
    lines.append(json.dumps({"type": "message_update", "assistantMessageEvent":
                             {"type": "toolcall_end", "contentIndex": 1,
                              "toolCall": {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": args}},
                             "message": {"role": "assistant",
                                         "content": [{"type": "text", "text": lead},
                                                     {"type": "toolCall", "id": "call_1", "name": "bash",
                                                      "arguments": args, "index": 1}]}}))
    body = json.dumps({"status": "done", "summary": "s"})
    lines.append(json.dumps({"type": "message_end", "message":
                             {"role": "assistant", "content": [{"type": "text", "text": body}],
                              "usage": {"input": 400, "output": 300, "cacheRead": 5, "cacheWrite": 1}}}))
    return lines, args


def compact(lines):
    compactor = xs.PiEventCompactor()
    out, peak = [], 0
    for line in lines:
        out.extend(compactor.filter_line(line))
        peak = max(peak, compactor.state_bytes())
    return out, compactor, peak


def reconstruct(events, kind):
    """Concatenate per-contentIndex deltas of one delta kind."""
    parts = {}
    for ev in events:
        if ev.get("type") != "message_update":
            continue
        ame = ev.get("assistantMessageEvent") or {}
        if ame.get("type") == kind:
            idx = ame.get("contentIndex", 0)
            parts[idx] = parts.get(idx, "") + ame.get("delta", "")
    return "".join(parts[k] for k in sorted(parts))


def read_events(path):
    with open(path, errors="ignore", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class ThinkingTests(unittest.TestCase):
    def test_thinking_deltas_are_bounded_and_reconstruct(self):
        lines, thinking = thinking_stream()
        out, compactor, peak = compact(lines)
        raw_bytes = sum(len(line) + 1 for line in lines)
        out_bytes = sum(len(line) + 1 for line in out)
        self.assertLess(out_bytes, raw_bytes // 20)
        self.assertEqual(reconstruct([json.loads(l) for l in out], "thinking_delta"), thinking)
        self.assertEqual(peak, len(thinking))
        end = [json.loads(l) for l in out
               if json.loads(l).get("type") == "message_update"
               and (json.loads(l).get("assistantMessageEvent") or {}).get("type") == "thinking_end"][0]
        self.assertEqual(end["assistantMessageEvent"]["content"], thinking)
        self.assertNotIn("partial", end["assistantMessageEvent"])
        self.assertEqual(compactor.events_compacted, 301)  # 300 deltas + thinking_end partial

    def test_batched_snapshot_compacts_and_reconstructs(self):
        # Real Pi (#277): one snapshot already contains this delta AND
        # the next chunk. Both events must compact and the concatenated
        # deltas must equal the final thinking text.
        compactor = xs.PiEventCompactor()

        def ev(text, delta):
            snap = {"role": "assistant",
                    "content": [{"type": "thinking", "thinking": text, "index": 0}]}
            return {"type": "message_update",
                    "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0,
                                              "delta": delta, "partial": snap},
                    "message": snap}

        first = compactor.compact_event(ev("hello ", "hello "))
        self.assertTrue(first.get("_swrm_compacted"))
        # Snapshot jumps by two chunks ("world" + "!"), delta is only "world".
        batched = compactor.compact_event(ev("hello world!", "world"))
        self.assertTrue(batched.get("_swrm_compacted"))
        self.assertNotIn("message", batched)
        # Next delta completes the batched snapshot; snapshot has not moved.
        nxt = compactor.compact_event(ev("hello world!", "!"))
        self.assertTrue(nxt.get("_swrm_compacted"))
        self.assertEqual(
            "".join(e["assistantMessageEvent"]["delta"] for e in (first, batched, nxt)),
            "hello world!")

    def test_mismatched_delta_passes_through_and_recovers(self):
        # Duplicate/out-of-order delta: snapshot does not advance the way
        # the delta claims. It passes through verbatim with the consumed
        # prefix untouched, and the next true append compacts again.
        compactor = xs.PiEventCompactor()

        def ev(text, delta):
            snap = {"role": "assistant",
                    "content": [{"type": "thinking", "thinking": text, "index": 0}]}
            return {"type": "message_update",
                    "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0,
                                              "delta": delta, "partial": snap},
                    "message": snap}

        first = compactor.compact_event(ev("hello ", "hello "))
        self.assertTrue(first.get("_swrm_compacted"))
        dup = ev("hello ", "hello ")
        self.assertIs(compactor.compact_event(dup), dup)
        nxt = compactor.compact_event(ev("hello world", "world"))
        self.assertTrue(nxt.get("_swrm_compacted"))

    def test_thinking_regeneration_passes_through_and_rebaselines(self):
        lines, thinking = thinking_stream(n=10, chunk=80)
        compactor = xs.PiEventCompactor()
        out = []
        # Compact only up to (not including) thinking_end, so the block is
        # still live when the regeneration arrives.
        for line in lines:
            ev = json.loads(line)
            if (ev.get("assistantMessageEvent") or {}).get("type") == "thinking_end":
                break
            out.extend(compactor.filter_line(line))
        # A regenerated thinking block that does not extend the previous snapshot.
        regen = {"role": "assistant",
                 "content": [{"type": "thinking", "thinking": "fresh start", "index": 0}]}
        ev = {"type": "message_update",
              "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0,
                                        "delta": "fresh start", "partial": regen},
              "message": regen}
        kept = compactor.compact_event(ev)
        self.assertEqual(kept, ev)
        self.assertNotIn("_swrm_compacted", kept)
        # The next true append compacts again against the new baseline.
        cont = {"role": "assistant",
                "content": [{"type": "thinking", "thinking": "fresh start!", "index": 0}]}
        ev2 = {"type": "message_update",
               "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0,
                                         "delta": "!", "partial": cont},
               "message": cont}
        kept2 = compactor.compact_event(ev2)
        self.assertTrue(kept2.get("_swrm_compacted"))
        self.assertNotIn("message", kept2)

    def test_partial_only_thinking_delta_compacts(self):
        # No top-level message snapshot: fall back to ame.partial.
        compactor = xs.PiEventCompactor()
        snap = {"role": "assistant", "content": [{"type": "thinking", "thinking": "abc", "index": 0}]}
        ev = {"type": "message_update",
              "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0,
                                        "delta": "abc", "partial": snap}}
        kept = compactor.compact_event(ev)
        self.assertTrue(kept.get("_swrm_compacted"))
        self.assertEqual(kept["assistantMessageEvent"]["delta"], "abc")


class ToolcallTests(unittest.TestCase):
    def test_toolcall_deltas_are_bounded_and_reconstruct_to_arguments(self):
        lines, args = toolcall_stream()
        out, compactor, peak = compact(lines)
        raw_bytes = sum(len(line) + 1 for line in lines)
        out_bytes = sum(len(line) + 1 for line in out)
        self.assertLess(out_bytes, raw_bytes // 5)
        rebuilt = reconstruct([json.loads(l) for l in out], "toolcall_delta")
        self.assertEqual(json.loads(rebuilt), args)
        self.assertEqual(peak, len(json.dumps(args)))
        end = [json.loads(l) for l in out
               if json.loads(l).get("type") == "message_update"
               and (json.loads(l).get("assistantMessageEvent") or {}).get("type") == "toolcall_end"][0]
        self.assertEqual(end["assistantMessageEvent"]["toolCall"]["arguments"], args)
        self.assertEqual(end["message"]["content"][1]["arguments"], args)

    def test_toolcall_without_partial_json_passes_through(self):
        compactor = xs.PiEventCompactor()
        snap = {"role": "assistant",
                "content": [{"type": "text", "text": "run it"},
                            {"type": "toolCall", "id": "call_9", "name": "bash",
                             "arguments": {"command": "ls"}, "index": 1}]}
        ev = {"type": "message_update",
              "assistantMessageEvent": {"type": "toolcall_delta", "contentIndex": 1,
                                        "delta": "", "partial": snap},
              "message": snap}
        self.assertEqual(compactor.compact_event(ev), ev)


class MixedStreamTests(unittest.TestCase):
    def test_mixed_thinking_text_toolcall_stream(self):
        tlines, thinking = thinking_stream(n=120, chunk=80)
        clines, args = toolcall_stream(fragments=20, frag_len=40)
        # One message: thinking block, then toolcall block (toolcall stream
        # without its own session/message wrappers).
        lines = tlines[:-2] + [l for l in clines if '"toolcall_' in l] + tlines[-2:]
        out, compactor, _ = compact(lines)
        raw_bytes = sum(len(line) + 1 for line in lines)
        out_bytes = sum(len(line) + 1 for line in out)
        self.assertLess(out_bytes, raw_bytes // 10)
        evs = [json.loads(l) for l in out]
        self.assertEqual(reconstruct(evs, "thinking_delta"), thinking)
        self.assertEqual(json.loads(reconstruct(evs, "toolcall_delta")), args)

    def test_reader_compatibility_raw_vs_compacted(self):
        tlines, _ = thinking_stream(n=60, chunk=80)
        clines, _ = toolcall_stream(fragments=10, frag_len=40)
        lines = tlines[:-2] + [l for l in clines if '"toolcall_' in l] + tlines[-2:]
        out, _, _ = compact(lines)
        raw_events = [json.loads(line) for line in lines]
        out_events = [json.loads(line) for line in out]
        # pi_usage reads a path; compare via temp files instead.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            rp, cp = os.path.join(d, "raw.jsonl"), os.path.join(d, "out.jsonl")
            open(rp, "w").write("\n".join(lines) + "\n")
            open(cp, "w").write("\n".join(out) + "\n")
            self.assertEqual(xs.pi_usage(rp), xs.pi_usage(cp))

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


class RunProcFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-m35s2-")
        self.script = os.path.join(self.tmp, "pi")
        with open(self.script, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(FAKE_PI_MIXED))
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

    def test_exec_pi_bounds_thinking_stream(self):
        evp = os.path.join(self.tmp, "events-exec-pi.jsonl")
        rc, final, meta, err_text = xs.exec_pi({"model": None}, {}, "do it", self.cwd,
                                               self.tmp, 60, evp)
        self.assertEqual(rc, 0, err_text)
        self.assertEqual(final, {"status": "done", "summary": "s"})
        self.assertEqual(meta["usage"], {"input": 400, "output": 300, "cacheRead": 5, "cacheWrite": 1})
        self.assertLess(os.path.getsize(evp), 60_000)
        evs = read_events(evp)
        thinking = "".join((("t%04d " % i) * 16)[:80] for i in range(1, 121))
        self.assertEqual(reconstruct(evs, "thinking_delta"), thinking)
        self.assertEqual(reconstruct(evs, "text_delta"),
                         "".join("word%d " % i for i in range(1, 11)))


if __name__ == "__main__":
    unittest.main()
