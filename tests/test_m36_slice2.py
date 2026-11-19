"""M36 slice 2 (#282): real-run export evidence gaps found on real runs.

Slice 1 (#280) was verified against a hand-built fixture. Slice 2 ran the
exporter against real run directories and found three mapping gaps, fixed
here (see docs/m36-slice1-trace-export.md):

1. agy backends record usage as input_tokens/output_tokens/
   cache_read_tokens/cache_write_tokens; slice 1 only read the pi-style
   keys, so agy attempt and root usage exported as absent.
2. _find_ledger_pointer matched id OR run_id and took the last row, so a
   later fleet dispatch row referencing the run beat the run's own row.
3. With auto_rework.escalation the rework chain has more attempts than
   auto_rework.attempts counts, so the rework span covered only the tail N
   and dropped the first rework attempt from its links.

Also locked in: a verify dict without `seconds` (the shape every real run
examined in slice 2 has) exports as a zero-length span at the run finish
-- the completion instant, not an invented duration.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()

RUN_ID = "20261112-020000-m36s2x"
STARTED = 1794448800.0
FINISHED = 1794448801.6
EXEC_STARTED = 1794448800.01


def span_by_name(trace, name):
    spans = trace["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    return [s for s in spans if s["name"] == name]


def attrs(span):
    out = {}
    for item in span.get("attributes") or []:
        value = item["value"]
        out[item["key"]] = value.get("stringValue", value.get("intValue",
                                   value.get("doubleValue", value.get("boolValue"))))
    return out


def write_run(run_id, result, status, events=None):
    rdir = os.path.join(xs.RUNS, run_id)
    os.makedirs(rdir, exist_ok=True)
    for name, lines in (events or {}).items():
        with open(os.path.join(rdir, name), "w", encoding="utf-8") as fh:
            for event in lines:
                fh.write(json.dumps(event) + "\n")
    xs.jsave(os.path.join(rdir, "status.json"), status)
    xs.jsave(os.path.join(rdir, "result.json"), result)
    return rdir


def write_ledger(rows):
    with open(xs.LEDGER, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def base_status():
    return {"id": RUN_ID, "state": "finished", "status": "done",
            "started": STARTED, "finished": FINISHED,
            "execution_started": EXEC_STARTED, "queued_seconds": 0.01,
            "backend": "agy", "model": "gemini-3.8-flash-medium", "attempt": 4}


def attempt(number, backend, seconds, usage, events_name):
    return {"backend": backend, "model": "gemini-3.8-flash-medium",
            "rc": 0, "seconds": seconds, "error_class": None,
            "meta": {"usage": usage},
            "events": os.path.join(xs.RUNS, RUN_ID, events_name)}


class RealRunGapTests(unittest.TestCase):
    def test_agy_usage_keys_mapped_at_attempt_and_root(self):
        """agy-style usage keys must land in swrm.usage.* (slice 2, run 924da3)."""
        rdir = os.path.join(xs.RUNS, RUN_ID)
        events = {"events.4.agy.jsonl": [
            {"type": "init", "conversation_id": "conv-esc"},
            {"type": "result", "conversation_id": "conv-esc", "status": "SUCCESS"}]}
        write_run(RUN_ID, {
            "id": RUN_ID, "status": "done", "backend": "agy",
            "model": "gemini-3.8-flash-medium", "seconds": 0.126,
            "usage": {"input_tokens": 50, "output_tokens": 20,
                      "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 70},
            "verify": {"rc": 0, "failing": []},
            "attempts": [attempt(4, "agy", 0.126,
                                 {"input_tokens": 50, "output_tokens": 20,
                                  "thinking_tokens": 0, "cache_read_tokens": 0,
                                  "total_tokens": 70},
                                 "events.4.agy.jsonl")]},
            base_status(), events)
        trace = xs.build_run_trace(RUN_ID)
        root = span_by_name(trace, "swrm.run")[0]
        self.assertEqual(attrs(root)["swrm.usage.input_tokens"], "50")
        self.assertEqual(attrs(root)["swrm.usage.output_tokens"], "20")
        attempt_span = span_by_name(trace, "swrm.attempt")[0]
        self.assertEqual(attrs(attempt_span)["swrm.usage.input_tokens"], "50")
        self.assertEqual(attrs(attempt_span)["swrm.usage.output_tokens"], "20")
        self.assertEqual(attrs(attempt_span)["swrm.usage.cache_read"], "0")
        self.assertEqual(attrs(attempt_span)["swrm.events.lines"], "2")

    def test_ledger_prefers_run_own_row_over_fleet_reference(self):
        """A later fleet row referencing the run must not beat the run row."""
        write_ledger([
            {"id": RUN_ID, "ts": FINISHED, "status": "done"},
            {"id": "fleet-local-65122b45", "run_id": RUN_ID, "status": "dispatched"},
        ])
        write_run(RUN_ID, {
            "id": RUN_ID, "status": "done", "backend": "pi", "seconds": 0.1,
            "attempts": [{"backend": "pi", "rc": 0, "seconds": 0.1,
                          "meta": {"usage": {"input": 1, "output": 1}}}]},
            base_status())
        trace = xs.build_run_trace(RUN_ID)
        root = span_by_name(trace, "swrm.run")[0]
        self.assertEqual(attrs(root)["swrm.ledger.line"], "1")
        self.assertEqual(attrs(root)["swrm.ledger.found"], True)
        # Offset of line 1 is 0.
        self.assertEqual(attrs(root)["swrm.ledger.offset"], "0")

    def test_ledger_falls_back_to_reference_row_when_no_own_row(self):
        write_ledger([
            {"id": "fleet-local-65122b45", "run_id": RUN_ID, "status": "dispatched"},
        ])
        write_run(RUN_ID, {
            "id": RUN_ID, "status": "done", "backend": "pi", "seconds": 0.1,
            "attempts": [{"backend": "pi", "rc": 0, "seconds": 0.1,
                          "meta": {"usage": {"input": 1, "output": 1}}}]},
            base_status())
        trace = xs.build_run_trace(RUN_ID)
        root = span_by_name(trace, "swrm.run")[0]
        self.assertEqual(attrs(root)["swrm.ledger.line"], "1")
        self.assertEqual(attrs(root)["swrm.ledger.found"], True)

    def test_rework_with_escalation_covers_whole_rework_chain(self):
        """Escalation adds attempts auto_rework.attempts does not count
        (slice 2, run 924da3: 4 attempts, N=2, escalation -> links 2,3,4)."""
        events = {f"events.{n}.pi.jsonl": [{"type": "session", "id": "s1"}]
                  for n in (1, 2, 3)}
        events["events.4.agy.jsonl"] = [{"type": "init", "conversation_id": "c"}]
        write_run(RUN_ID, {
            "id": RUN_ID, "status": "done", "backend": "agy",
            "model": "gemini-3.8-flash-medium", "seconds": 0.308,
            "usage": {"input_tokens": 50, "output_tokens": 20},
            "verify": {"rc": 0, "failing": []},
            "auto_rework": {"attempts": 2, "max_attempts": 2, "recovered": True,
                            "rework_verify": {"rc": 0},
                            "escalation": {"from_backend": "pi", "to_backend": "agy",
                                           "tier": "deep", "recovered": True}},
            "attempts": [
                attempt(1, "pi", 0.061, {"input": 120, "output": 45}, "events.1.pi.jsonl"),
                attempt(2, "pi", 0.06, {"input": 120, "output": 45}, "events.2.pi.jsonl"),
                attempt(3, "pi", 0.061, {"input": 120, "output": 45}, "events.3.pi.jsonl"),
                attempt(4, "agy", 0.126, {"input_tokens": 50, "output_tokens": 20},
                        "events.4.agy.jsonl")]},
            base_status(), events)
        trace = xs.build_run_trace(RUN_ID)
        self.assertEqual(trace["span_count"], 9)
        rework = span_by_name(trace, "swrm.rework")[0]
        attempt_spans = span_by_name(trace, "swrm.attempt")
        linked = {link["spanId"] for link in rework.get("links", [])}
        self.assertEqual(linked, {s["spanId"] for s in attempt_spans[1:]})
        self.assertEqual(attrs(rework)["swrm.rework.attempts"], "2")
        self.assertEqual(attrs(rework)["swrm.rework.recovered"], True)

    def test_rework_without_escalation_still_covers_tail_n(self):
        write_run(RUN_ID, {
            "id": RUN_ID, "status": "done", "backend": "pi", "seconds": 0.2,
            "verify": {"rc": 0, "failing": []},
            "auto_rework": {"attempts": 1, "max_attempts": 2, "recovered": True,
                            "rework_verify": {"rc": 0}},
            "attempts": [
                {"backend": "pi", "rc": 1, "seconds": 0.1, "error_class": "test_failure",
                 "meta": {"usage": {"input": 10, "output": 5}}},
                {"backend": "pi", "rc": 0, "seconds": 0.1,
                 "meta": {"usage": {"input": 10, "output": 5}}}]},
            base_status())
        trace = xs.build_run_trace(RUN_ID)
        rework = span_by_name(trace, "swrm.rework")[0]
        attempt_spans = span_by_name(trace, "swrm.attempt")
        linked = {link["spanId"] for link in rework.get("links", [])}
        self.assertEqual(linked, {attempt_spans[1]["spanId"]})

    def test_verify_without_seconds_is_zero_length_at_finish(self):
        """Real runs carry no verify.seconds; the span marks the finish
        instant instead of inventing a duration."""
        write_run(RUN_ID, {
            "id": RUN_ID, "status": "done", "backend": "pi", "seconds": 0.1,
            "verify": {"rc": 0, "failing": []},
            "attempts": [{"backend": "pi", "rc": 0, "seconds": 0.1,
                          "meta": {"usage": {"input": 1, "output": 1}}}]},
            base_status())
        trace = xs.build_run_trace(RUN_ID)
        verify = span_by_name(trace, "swrm.verify")[0]
        self.assertEqual(verify["startTimeUnixNano"], verify["endTimeUnixNano"])
        self.assertEqual(verify["endTimeUnixNano"], str(int(round(FINISHED * 1_000_000_000))))


if __name__ == "__main__":
    unittest.main()
