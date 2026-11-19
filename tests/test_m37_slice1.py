"""M37 slice 1 (#285): MCP server skeleton + dispatch/query tools.

Covers, in-process and over a real stdio subprocess: initialize (protocol
version 2025-06-18), tools/list, tools/call dispatch (dry-run through the
real `run` path) and query, plus the guardrails (unknown tool / bad arguments
fail loudly, never silently; the server only starts on the explicit `mcp`
command).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()

RUN_ID = "20261119-071719-m37s1x"


def handle(method, params=None, req_id=1):
    req = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        req["params"] = params
    return xs.mcp_handle_request(req, xs.load_policy())


def call_tool(name, arguments, req_id=1):
    return handle("tools/call", {"name": name, "arguments": arguments}, req_id)


def tool_text(response):
    return json.loads(response["result"]["content"][0]["text"])


def build_finished_fixture(run_id=RUN_ID):
    """A finished run dir + one ledger row, the shape query() must summarise."""
    rdir = os.path.join(xs.RUNS, run_id)
    os.makedirs(rdir, exist_ok=True)
    with open(os.path.join(rdir, "task.md"), "w", encoding="utf-8") as fh:
        fh.write("Fix the flaky test.\n")
    xs.jsave(os.path.join(rdir, "status.json"),
             {"id": run_id, "state": "finished", "status": "done", "started": 1792966800.0,
              "finished": 1792966920.0, "backend": "pi", "model": "MiniMax-M3.1-Flash-Preview",
              "title": "Fix the flaky test."})
    xs.jsave(os.path.join(rdir, "result.json"),
             {"id": run_id, "status": "done", "backend": "pi", "commit": "abc1234def5678",
              "branch": "openswarm/" + run_id,
              "verify": {"phase": "verify", "rc": 0, "seconds": 5.0, "failing": []},
              "verify_evalid": {"verdict": "pass"}})
    xs.append_ledger({"id": run_id, "ts": 1792966920.0, "title": "Fix the flaky test.",
                      "status": "done", "backend": "pi", "exec_pass": True})
    return rdir


class InitializeTests(unittest.TestCase):
    def test_initialize_returns_2025_06_18_and_server_info(self):
        resp = handle("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "0"}})
        self.assertEqual(resp["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(resp["result"]["capabilities"], {"tools": {}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "openswarm")
        self.assertTrue(resp["result"]["serverInfo"]["version"])

    def test_initialize_without_protocol_version_is_an_actionable_error(self):
        resp = handle("initialize", {"capabilities": {}})
        self.assertEqual(resp["error"]["code"], -32602)
        self.assertIn("2025-06-18", resp["error"]["message"])

    def test_ping_and_initialized_notification(self):
        self.assertEqual(handle("ping", {})["result"], {})
        self.assertIsNone(xs.mcp_handle_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, xs.load_policy()))

    def test_unknown_method_is_method_not_found(self):
        resp = handle("resources/list", {})
        self.assertEqual(resp["error"]["code"], -32601)
        self.assertIn("tools/list", resp["error"]["message"])

    def test_malformed_line_is_parse_error(self):
        resp = json.loads(xs.mcp_handle_line("{not json", xs.load_policy()))
        self.assertEqual(resp["error"]["code"], -32700)
        self.assertIsNone(resp["id"])
        self.assertIsNone(xs.mcp_handle_line("   ", xs.load_policy()))


class ToolsListTests(unittest.TestCase):
    def test_lists_dispatch_and_query_with_required_task(self):
        resp = handle("tools/list", {})
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        self.assertEqual(sorted(tools), ["dispatch", "query"])
        self.assertEqual(tools["dispatch"]["inputSchema"]["required"], ["task"])
        self.assertEqual(tools["query"]["inputSchema"]["required"], ["run_id"])
        self.assertFalse(tools["dispatch"]["inputSchema"].get("additionalProperties", True))


class ToolsCallGuardrailTests(unittest.TestCase):
    def test_unknown_tool_names_the_available_tools(self):
        resp = call_tool("bogus", {})
        self.assertEqual(resp["error"]["code"], -32602)
        self.assertIn("dispatch, query", resp["error"]["message"])

    def test_dispatch_without_task_is_an_actionable_error(self):
        resp = call_tool("dispatch", {})
        self.assertEqual(resp["error"]["code"], -32602)
        self.assertIn("'task'", resp["error"]["message"])

    def test_dispatch_rejects_empty_task_unknown_args_and_bad_types(self):
        for args, needle in (
                ({"task": "   "}, "non-empty string"),
                ({"task": "x", "nope": 1}, "unknown argument"),
                ({"task": "x", "dry_run": "yes"}, "true or false"),
                ({"task": "x", "budget_minutes": 0}, "number > 0"),
                ({"task": "x", "budget_minutes": "30"}, "number > 0")):
            with self.subTest(args=args):
                resp = call_tool("dispatch", args)
                self.assertEqual(resp["error"]["code"], -32602)
                self.assertIn(needle, resp["error"]["message"])

    def test_query_rejects_unknown_run_and_slashy_id(self):
        resp = call_tool("query", {"run_id": "20261119-000000-nope00"})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("openswarm runs -n 20 --json", tool_text(resp)["error"])
        resp = call_tool("query", {"run_id": "../etc"})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("no slashes", tool_text(resp)["error"])


class QueryTests(unittest.TestCase):
    def test_query_finished_run_returns_digest_verify_and_ledger(self):
        build_finished_fixture()
        resp = call_tool("query", {"run_id": RUN_ID})
        self.assertNotIn("isError", resp["result"])
        out = tool_text(resp)
        self.assertEqual(out["run_id"], RUN_ID)
        self.assertEqual(out["state"], "finished")
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["backend"], "pi")
        self.assertEqual(out["commit"], "abc1234def5678")
        self.assertEqual(out["branch"], "openswarm/" + RUN_ID)
        self.assertEqual(out["verify"], {"rc": 0, "failing": []})
        self.assertEqual(out["verify_evalid"], {"verdict": "pass"})
        self.assertTrue(out["ledger_found"])
        self.assertEqual(out["ledger"]["id"], RUN_ID)
        self.assertTrue(out["ledger"]["exec_pass"])


class DispatchTests(unittest.TestCase):
    def test_dispatch_dry_run_walks_the_real_run_path(self):
        # The fixture probe state routes to codex, whose binary is absent in
        # this VM, so the health gate would block: skip it explicitly (the
        # bypass is the CLI's own flag, recorded by the run path).
        old = os.environ.get("OPENSWARM_STATE_FILE")
        os.environ["OPENSWARM_STATE_FILE"] = _load.FIXTURE_STATE
        try:
            resp = call_tool("dispatch", {"task": "Write one sentence in README about MCP.",
                                          "dry_run": True, "skip_health_gate": True})
        finally:
            if old is None:
                os.environ.pop("OPENSWARM_STATE_FILE", None)
            else:
                os.environ["OPENSWARM_STATE_FILE"] = old
        self.assertNotIn("isError", resp["result"], tool_text(resp))
        out = tool_text(resp)
        self.assertTrue(out["dry_run"])
        self.assertIn("id", out)
        self.assertEqual(out["decision"]["backend"], "codex")
        rdir = os.path.join(xs.RUNS, out["id"])
        self.assertTrue(os.path.isfile(os.path.join(rdir, "task.md")))
        self.assertEqual(xs.jload(os.path.join(rdir, "status.json"), {}).get("state"), "dry-run")
        # And query() can then summarise the run dispatch just created.
        q = tool_text(call_tool("query", {"run_id": out["id"]}))
        self.assertEqual(q["state"], "dry-run")
        self.assertTrue(q["title"].startswith("Write one sentence"))

    def test_dispatch_gate_block_returns_the_failure_record_not_silence(self):
        old = os.environ.get("OPENSWARM_STATE_FILE")
        os.environ["OPENSWARM_STATE_FILE"] = _load.FIXTURE_STATE
        try:
            resp = call_tool("dispatch", {"task": "Write one sentence in README about MCP.",
                                          "dry_run": True})
        finally:
            if old is None:
                os.environ.pop("OPENSWARM_STATE_FILE", None)
            else:
                os.environ["OPENSWARM_STATE_FILE"] = old
        self.assertTrue(resp["result"]["isError"])
        out = tool_text(resp)
        self.assertIn("run_id", out)
        self.assertEqual(out["run"]["status"], "unroutable")
        self.assertEqual(out["run"]["error_class"], "health_gate_blocked")
        self.assertIn("codex", out["run"]["error"])


class CliEndToEndTests(unittest.TestCase):
    def test_stdio_session_initialize_list_dispatch_query(self):
        home = tempfile.mkdtemp(prefix="openswarm-mcp-test-")
        env = dict(os.environ, OPENSWARM_HOME=home,
                   OPENSWARM_STATE_FILE=_load.FIXTURE_STATE)
        for key in list(env):
            if key.startswith(("OPENSWARM_", "XSWARM_")) and key not in (
                    "OPENSWARM_HOME", "OPENSWARM_STATE_FILE"):
                env.pop(key)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.Popen([sys.executable, os.path.join(root, "bin", "openswarm"), "mcp"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        try:
            def send(obj):
                proc.stdin.write((obj if isinstance(obj, str) else json.dumps(obj)) + "\n")
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())

            init = send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "0"}}})
            self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
            # Notifications get no response: write it without reading one.
            proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                         "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()
            listed = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            self.assertEqual([t["name"] for t in listed["result"]["tools"]],
                             ["dispatch", "query"])
            dispatched = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "dispatch",
                                          "arguments": {"task": "Write one sentence in README about MCP.",
                                                        "dry_run": True, "skip_health_gate": True}}})
            out = tool_text(dispatched)
            self.assertTrue(out["dry_run"])
            queried = send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "query", "arguments": {"run_id": out["id"]}}})
            self.assertEqual(tool_text(queried)["state"], "dry-run")
            unknown = send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                            "params": {"name": "bogus", "arguments": {}}})
            self.assertEqual(unknown["error"]["code"], -32602)
        finally:
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=30), 0)

    def test_mcp_is_listed_and_other_commands_unchanged(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run([sys.executable, os.path.join(root, "bin", "openswarm"), "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("mcp", proc.stdout)
        proc = subprocess.run([sys.executable, os.path.join(root, "bin", "openswarm"), "mcp", "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
