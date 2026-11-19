"""M36 slice 1 (#280): OTel span model + export core (file / OTLP-HTTP).

The canonical fixture below is a finished Pi run with one failed first
attempt, one auto-rework attempt that recovered, and a passing verify —
the shape slice 1 must map. tests/fixtures/trace-sample.otlp.json is the
committed export of exactly this fixture (no ledger row), so the mapping
is reviewable without running anything.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()

RUN_ID = "20261119-090000-m36s1x"
STARTED = 1792966800.0  # 2026-11-19T01:00:00Z
FINISHED = 1792966920.5
EXEC_STARTED = 1792966802.0

EVENTS_1 = [
    {"type": "session", "id": "s1"},
    {"type": "message_start", "message": {"role": "assistant", "content": []}},
    {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta"}},
    {"type": "message_update", "assistantMessageEvent": {"type": "text_delta"}},
    {"type": "message_end", "message": {"role": "assistant",
                                         "content": [{"type": "text", "text": "{\"status\": \"done\"}"}],
                                         "usage": {"input": 800, "output": 400}}},
    {"type": "agent_end", "messages": []},
]
EVENTS_2 = [
    {"type": "session", "id": "s1"},
    {"type": "message_start", "message": {"role": "assistant", "content": []}},
    {"type": "message_end", "message": {"role": "assistant",
                                         "content": [{"type": "text", "text": "{\"status\": \"done\"}"}],
                                         "usage": {"input": 200, "output": 100}}},
]


def build_canonical_fixture(home_runs=None, run_id=RUN_ID):
    """Write the canonical finished run under xs.RUNS (or home_runs). Returns run dir."""
    runs = home_runs or xs.RUNS
    rdir = os.path.join(runs, run_id)
    os.makedirs(rdir, exist_ok=True)
    with open(os.path.join(rdir, "events.1.pi.jsonl"), "w", encoding="utf-8") as fh:
        for event in EVENTS_1:
            fh.write(json.dumps(event) + "\n")
    with open(os.path.join(rdir, "events.2.pi.jsonl"), "w", encoding="utf-8") as fh:
        for event in EVENTS_2:
            fh.write(json.dumps(event) + "\n")
    xs.jsave(os.path.join(rdir, "status.json"), {
        "id": run_id, "state": "finished", "status": "done",
        "started": STARTED, "finished": FINISHED, "execution_started": EXEC_STARTED,
        "queued_seconds": 2.0, "backend": "pi", "model": "MiniMax-M3.1-Flash-Preview",
        "attempt": 2})
    xs.jsave(os.path.join(rdir, "result.json"), {
        "id": run_id, "status": "done", "backend": "pi",
        "model": "MiniMax-M3.1-Flash-Preview", "effort": "high",
        "card_type": "feature", "rework_of": "20261119-080000-prev01",
        "commit": "abc1234def5678", "branch": "openswarm/" + run_id,
        "seconds": 120.5, "usage": {"input": 1000, "output": 500},
        "verify": {"phase": "verify", "rc": 0, "seconds": 5.0, "failing": []},
        "verify_evalid": {"verdict": "pass"},
        "auto_rework": {"attempts": 1, "max_attempts": 2, "recovered": True,
                        "rework_verify": {"rc": 0}, "verifies": [{"attempt": 1, "rc": 0}]},
        "attempts": [
            {"backend": "pi", "model": "MiniMax-M3.1-Flash-Preview", "effort": "high",
             "rc": 1, "seconds": 60.0, "error_class": "test_failure",
             "meta": {"usage": {"input": 800, "output": 400, "cacheRead": 10, "cacheWrite": 2}},
             "events": os.path.join(rdir, "events.1.pi.jsonl")},
            {"backend": "pi", "model": "MiniMax-M3.1-Flash-Preview", "effort": "high",
             "rc": 0, "seconds": 55.5, "error_class": None,
             "meta": {"usage": {"input": 200, "output": 100}},
             "events": os.path.join(rdir, "events.2.pi.jsonl")},
        ]})
    return rdir


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


def nanos(epoch):
    return str(int(round(epoch * 1_000_000_000)))


class TraceModelTests(unittest.TestCase):
    def setUp(self):
        self.rdir = build_canonical_fixture()

    def test_root_span_fields_and_deterministic_ids(self):
        trace = xs.build_run_trace(RUN_ID)
        expected_trace_id = hashlib.sha256(f"openswarm-trace:{RUN_ID}".encode()).hexdigest()[:32]
        self.assertEqual(trace["trace_id"], expected_trace_id)
        self.assertEqual(trace["span_count"], 7)
        root = span_by_name(trace, "swrm.run")[0]
        self.assertEqual(root["traceId"], expected_trace_id)
        self.assertNotIn("parentSpanId", root)
        self.assertEqual(root["startTimeUnixNano"], nanos(STARTED))
        self.assertEqual(root["endTimeUnixNano"], nanos(FINISHED))
        self.assertEqual(root["status"], {"code": 1})
        a = attrs(root)
        self.assertEqual(a["swrm.run.id"], RUN_ID)
        self.assertEqual(a["swrm.run.status"], "done")
        self.assertEqual(a["swrm.backend"], "pi")
        self.assertEqual(a["swrm.model"], "MiniMax-M3.1-Flash-Preview")
        self.assertEqual(a["swrm.card_type"], "feature")
        self.assertEqual(a["swrm.rework_of"], "20261119-080000-prev01")
        self.assertEqual(a["swrm.commit"], "abc1234def5678")
        self.assertEqual(a["swrm.verify_evalid.verdict"], "pass")
        self.assertEqual(a["swrm.usage.input_tokens"], "1000")
        self.assertEqual(a["swrm.usage.output_tokens"], "500")
        self.assertIs(a["swrm.ledger.found"], False)
        # Second build is identical: deterministic ids and times.
        self.assertEqual(xs.build_run_trace(RUN_ID)["payload"], trace["payload"])
        # Every span id is unique and every non-root span names a real parent.
        spans = trace["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        ids = {s["spanId"] for s in spans}
        self.assertEqual(len(ids), 7)
        for span in spans:
            if "parentSpanId" in span:
                self.assertIn(span["parentSpanId"], ids)

    def test_phase_parentage_and_time_monotonic(self):
        trace = xs.build_run_trace(RUN_ID)
        spans = trace["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        by_name = {s["name"]: s for s in spans}
        root = by_name["swrm.run"]
        for name in ("swrm.dispatch", "swrm.execute", "swrm.rework", "swrm.verify"):
            self.assertEqual(by_name[name]["parentSpanId"], root["spanId"])
        attempts = span_by_name(trace, "swrm.attempt")
        self.assertEqual(len(attempts), 2)
        for attempt in attempts:
            self.assertEqual(attempt["parentSpanId"], by_name["swrm.execute"]["spanId"])
        # Times: dispatch 0..2s, attempt 1 2..62s, attempt 2 62..117.5s,
        # execute 2..117.5s, rework 62..117.5s, verify 117.5..120.5s.
        self.assertEqual(by_name["swrm.dispatch"]["startTimeUnixNano"], nanos(STARTED))
        self.assertEqual(by_name["swrm.dispatch"]["endTimeUnixNano"], nanos(EXEC_STARTED))
        self.assertEqual(attempts[0]["startTimeUnixNano"], nanos(EXEC_STARTED))
        self.assertEqual(attempts[0]["endTimeUnixNano"], nanos(EXEC_STARTED + 60.0))
        self.assertEqual(attempts[1]["startTimeUnixNano"], nanos(EXEC_STARTED + 60.0))
        self.assertEqual(attempts[1]["endTimeUnixNano"], nanos(EXEC_STARTED + 115.5))
        self.assertEqual(by_name["swrm.verify"]["endTimeUnixNano"], nanos(FINISHED))
        for span in spans:
            self.assertLessEqual(int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"]))
            self.assertGreaterEqual(int(span["startTimeUnixNano"]), int(root["startTimeUnixNano"]))
            self.assertLessEqual(int(span["endTimeUnixNano"]), int(root["endTimeUnixNano"]))

    def test_attempt_attrs_events_usage_and_rework_link(self):
        trace = xs.build_run_trace(RUN_ID)
        attempts = span_by_name(trace, "swrm.attempt")
        a1, a2 = attrs(attempts[0]), attrs(attempts[1])
        self.assertEqual(a1["swrm.attempt.number"], "1")
        self.assertEqual(a1["swrm.attempt.rc"], "1")
        self.assertEqual(a1["swrm.attempt.error_class"], "test_failure")
        self.assertEqual(a1["swrm.events.file"], "events.1.pi.jsonl")
        self.assertEqual(a1["swrm.events.lines"], "6")
        self.assertEqual(a1["swrm.events.types"],
                         "agent_end=1,message_end=1,message_start=1,message_update=2,session=1")
        self.assertEqual(a1["swrm.usage.input_tokens"], "800")
        self.assertEqual(a1["swrm.usage.cache_read"], "10")
        self.assertEqual(a1["swrm.usage.cache_write"], "2")
        self.assertEqual(attempts[0]["status"], {"code": 2, "message": "test_failure"})
        self.assertEqual(a2["swrm.attempt.rc"], "0")
        self.assertEqual(a2["swrm.events.lines"], "3")
        self.assertEqual(attempts[1]["status"], {"code": 1})
        rework = span_by_name(trace, "swrm.rework")[0]
        ra = attrs(rework)
        self.assertEqual(ra["swrm.rework.attempts"], "1")
        self.assertEqual(ra["swrm.rework.max_attempts"], "2")
        self.assertIs(ra["swrm.rework.recovered"], True)
        self.assertEqual(ra["swrm.rework.final_verify_rc"], "0")
        self.assertEqual([link["spanId"] for link in rework["links"]], [attempts[1]["spanId"]])
        verify = span_by_name(trace, "swrm.verify")[0]
        va = attrs(verify)
        self.assertEqual(va["swrm.verify.phase"], "verify")
        self.assertEqual(va["swrm.verify.rc"], "0")
        self.assertEqual(va["swrm.verify.failing_count"], "0")

    def test_ledger_pointer_matches_file_position(self):
        run_id = "20261119-090000-m36led"
        build_canonical_fixture(run_id=run_id)
        xs.append_ledger({"id": run_id, "ts": STARTED, "title": "t", "status": "done",
                          "backend": "pi", "seconds": 120.5})
        trace = xs.build_run_trace(run_id)
        root = span_by_name(trace, "swrm.run")[0]
        a = attrs(root)
        self.assertIs(a["swrm.ledger.found"], True)
        with open(xs.LEDGER, "rb") as fh:
            raw_lines = fh.readlines()
        line_no = int(a["swrm.ledger.line"])
        offset = int(a["swrm.ledger.offset"])
        self.assertEqual(json.loads(raw_lines[line_no - 1])["id"], run_id)
        self.assertEqual(sum(len(line) for line in raw_lines[:line_no - 1]), offset)

    def test_minimal_run_no_verify_no_rework_missing_events(self):
        run_id = "20261119-090000-m36min"
        rdir = os.path.join(xs.RUNS, run_id)
        os.makedirs(rdir, exist_ok=True)
        xs.jsave(os.path.join(rdir, "status.json"),
                 {"id": run_id, "state": "finished", "status": "failed",
                  "started": STARTED, "finished": STARTED + 30.0,
                  "execution_started": STARTED + 1.0, "queued_seconds": 1.0})
        xs.jsave(os.path.join(rdir, "result.json"),
                 {"id": run_id, "status": "failed", "backend": "codex",
                  "model": "gpt-5", "seconds": 30.0, "error_class": "budget_exceeded",
                  "attempts": [{"backend": "codex", "model": "gpt-5", "rc": 124,
                                "seconds": 29.0, "error_class": "budget_exceeded",
                                "meta": {}, "events": os.path.join(rdir, "events.1.codex.jsonl")}]})
        trace = xs.build_run_trace(run_id)
        self.assertEqual(trace["span_count"], 4)
        self.assertEqual(span_by_name(trace, "swrm.verify"), [])
        self.assertEqual(span_by_name(trace, "swrm.rework"), [])
        root = span_by_name(trace, "swrm.run")[0]
        self.assertEqual(root["status"], {"code": 2, "message": "budget_exceeded"})
        attempt = span_by_name(trace, "swrm.attempt")[0]
        aa = attrs(attempt)
        self.assertEqual(aa["swrm.events.lines"], "0")
        self.assertIs(aa["swrm.events.missing"], True)
        self.assertEqual(attempt["status"]["code"], 2)

    def test_missing_run_and_missing_result(self):
        with self.assertRaises(xs.TraceExportError) as ctx:
            xs.build_run_trace("20261119-000000-doesnotexist")
        self.assertIn("run not found", str(ctx.exception))
        run_id = "20261119-090000-m36nores"
        os.makedirs(os.path.join(xs.RUNS, run_id), exist_ok=True)
        with self.assertRaises(xs.TraceExportError) as ctx:
            xs.build_run_trace(run_id)
        self.assertIn("no result.json", str(ctx.exception))


def _namespace(**kwargs):
    args = argparse.Namespace(run_id=RUN_ID, out=None, endpoint=None,
                              no_file=False, timeout=5.0)
    args.__dict__.update(kwargs)
    return args


class TraceExportTests(unittest.TestCase):
    def setUp(self):
        self.rdir = build_canonical_fixture()

    def _run_export(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                xs.cmd_trace_export(args, xs.load_policy())
            except SystemExit as exit_:
                return exit_.code, out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def test_export_file_default_path_and_shape(self):
        code, out, _ = self._run_export(_namespace())
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertTrue(summary["exported"])
        self.assertEqual(summary["spans"], 7)
        self.assertIsNone(summary["endpoint"])
        self.assertIsNone(summary["http_status"])
        self.assertEqual(summary["file"], os.path.join(self.rdir, "trace.otlp.json"))
        with open(summary["file"], encoding="utf-8") as fh:
            payload = json.load(fh)
        resource_spans = payload["resourceSpans"]
        self.assertEqual(len(resource_spans), 1)
        resource_attrs = {a["key"]: a["value"].get("stringValue")
                          for a in resource_spans[0]["resource"]["attributes"]}
        self.assertEqual(resource_attrs["service.name"], "openswarm")
        scope_spans = resource_spans[0]["scopeSpans"]
        self.assertEqual(scope_spans[0]["scope"]["name"], "openswarm")
        self.assertEqual(len(scope_spans[0]["spans"]), 7)
        for span in scope_spans[0]["spans"]:
            self.assertEqual(len(span["traceId"]), 32)
            self.assertEqual(len(span["spanId"]), 16)
            self.assertEqual(span["kind"], 1)
            int(span["startTimeUnixNano"])
            int(span["endTimeUnixNano"])

    def test_export_explicit_out(self):
        out_path = os.path.join(self.rdir, "custom", "trace.json")
        code, out, _ = self._run_export(_namespace(out=out_path))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["file"], out_path)
        self.assertTrue(os.path.isfile(out_path))

    def test_export_http_success_and_body(self):
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                received["body"] = self.rfile.read(length)
                received["content_type"] = self.headers.get("Content-Type")
                received["path"] = self.path
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1/traces"
            code, out, _ = self._run_export(_namespace(endpoint=endpoint))
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["http_status"], 202)
        self.assertEqual(summary["endpoint"], endpoint)
        self.assertTrue(summary["exported"])
        self.assertEqual(received["content_type"], "application/json")
        self.assertEqual(received["path"], "/v1/traces")
        body = json.loads(received["body"])
        self.assertEqual(len(body["resourceSpans"][0]["scopeSpans"][0]["spans"]), 7)

    def test_export_http_unreachable_is_an_error_not_a_success(self):
        endpoint = "http://127.0.0.1:1/v1/traces"  # nothing listens on port 1
        code, out, err = self._run_export(_namespace(endpoint=endpoint))
        self.assertNotEqual(code, 0)
        self.assertIn("failed", err)
        self.assertIn(endpoint, err)
        self.assertNotIn('"exported": true', out)
        self.assertNotIn("exported", out)
        # The local file is still the honest local record, not a send claim.
        self.assertTrue(os.path.isfile(os.path.join(self.rdir, "trace.otlp.json")))

    def test_export_missing_run_exits_nonzero(self):
        code, out, err = self._run_export(_namespace(run_id="20261119-000000-doesnotexist"))
        self.assertNotEqual(code, 0)
        self.assertIn("run not found", err)
        self.assertNotIn("exported", out)

    def test_export_no_file_requires_endpoint(self):
        code, _, err = self._run_export(_namespace(no_file=True))
        self.assertNotEqual(code, 0)
        self.assertIn("--no-file requires --endpoint", err)

    def test_fixture_sample_matches_committed_file(self):
        sample_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "fixtures", "trace-sample.otlp.json")
        with open(sample_path, encoding="utf-8") as fh:
            committed = json.load(fh)
        # The committed sample has no ledger row for this run id, so the
        # builder must agree with it exactly in this fresh test home.
        self.assertEqual(xs.build_run_trace(RUN_ID)["payload"], committed)

    def test_cli_trace_export_end_to_end(self):
        import tempfile
        home = tempfile.mkdtemp(prefix="openswarm-cli-trace-")
        build_canonical_fixture(home_runs=os.path.join(home, "runs"))
        env = dict(os.environ, OPENSWARM_HOME=home)
        for key in list(env):
            if key.startswith(("OPENSWARM_", "XSWARM_")) and key != "OPENSWARM_HOME":
                env.pop(key)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run([sys.executable, os.path.join(root, "bin", "openswarm"),
                               "trace", "export", RUN_ID],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertTrue(summary["exported"])
        self.assertEqual(summary["spans"], 7)
        self.assertTrue(os.path.isfile(os.path.join(home, "runs", RUN_ID, "trace.otlp.json")))


if __name__ == "__main__":
    unittest.main()
