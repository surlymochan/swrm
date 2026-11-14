"""M27 slice 2 (#224): GitHub Source + Assessment + real dispatch + Channel.

Covers: the gh event -> Trigger translator (issues, PRs, comments,
own-post marker, own-bot filter, malformed events), polling with a
persisted cursor and dedup, Assessment (gate precedence, explicit
fast path, deterministic default, model hook + fallback), ingest wiring
(shadow no-dispatch, on-mode fake dispatch + channel, draft_only block,
dispatch/channel failure recording, assessment downgrade), policy
validation for the new blocks, and one real end-to-end dispatch through
the subprocess `run` path with the echo custom backend.
"""
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _load

xs = _load.load()
HERE = pathlib.Path(__file__).resolve().parent

FAKE_ECHO_CLI = r"""#!/usr/bin/env python3
import argparse, json, os, sys
p = argparse.ArgumentParser()
p.add_argument("--prompt", required=True)
p.add_argument("--workdir", default=".")
p.add_argument("--model", default="")
a = p.parse_args()
with open(os.path.join(a.workdir, "echo-output.txt"), "w") as fh:
    fh.write("echo: %s\nmodel: %s\n" % (a.prompt, a.model))
print(json.dumps({"status": "done", "summary": "echoed the prompt",
                  "files_changed": ["echo-output.txt"], "tests": "no tests claimed"}))
"""


def trig(trigger_id, author="surlymochan", body="please fix the flaky test in fleet dispatch",
         kind="issue", number=1, bot=False, repo="surlymochan/swrm"):
    return {"id": trigger_id, "source": "github",
            "thread": {"repo": repo, "kind": kind, "number": number},
            "author": author, "author_is_bot": bot, "body": body,
            "created_at": "2026-11-15T03:00:00Z"}


def event(event_id, author="surlymochan", body="please fix this", kind="issue",
          number=1, bot=False, updated="2026-11-15T03:00:00Z", pull_request=False):
    user = {"login": author, "type": "Bot" if bot else "User"}
    if kind == "comment":
        return {"id": event_id, "user": user, "body": body,
                "issue_url": f"https://api.github.com/repos/surlymochan/swrm/issues/{number}",
                "html_url": f"https://github.com/surlymochan/swrm/issues/{number}#issuecomment-{event_id}",
                "created_at": updated, "updated_at": updated}
    out = {"id": event_id, "user": user, "body": body, "number": number,
           "html_url": f"https://github.com/surlymochan/swrm/issues/{number}",
           "created_at": updated, "updated_at": updated}
    if pull_request:
        out["pull_request"] = {"url": "https://api.github.com/repos/surlymochan/swrm/pulls/1"}
    return out


def fake_runner(result_status="done"):
    calls = []

    def runner(ticket, trigger, decision, policy, data_dir, cwd):
        calls.append((ticket["id"], trigger["id"]))
        return {"run_id": "20261115-030000-abcdef",
                "status": result_status,
                "result": {"id": "20261115-030000-abcdef", "status": result_status,
                           "branch": "intake/" + ticket["id"], "commit": "abc1234",
                           "summary": "did the thing"}}
    runner.calls = calls
    return runner


def fake_channel():
    calls = []

    def poster(repo, number, body):
        calls.append((repo, number, body))
        return {"id": 999, "html_url": f"https://github.com/{repo}/issues/{number}#issuecomment-999"}
    poster.calls = calls
    return poster


class M27Slice2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m27s2-")
        self.data = pathlib.Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.patch = mock.patch.object(xs, "DATA", str(self.data))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.policy = xs.default_intake_policy()

    def on_policy(self, **overrides):
        policy = xs.default_intake_policy()
        policy.update(overrides)
        return policy

    def ingest(self, trigger, policy=None, **kw):
        return xs.intake_ingest_trigger(trigger, policy=policy or self.policy,
                                        data_dir=str(self.data), **kw)

    # -- Source translator -----------------------------------------------

    def test_issue_event_becomes_trigger(self):
        t = xs.github_event_to_trigger(
            event(101, body="new bug: fleet dispatch stalls", number=42), "surlymochan/swrm")
        self.assertEqual(t["id"], "github:issue:101")
        self.assertEqual(t["thread"], {"repo": "surlymochan/swrm", "kind": "issue", "number": 42})
        self.assertEqual(t["author"], "surlymochan")
        self.assertFalse(t["author_is_bot"])

    def test_pr_issue_event_becomes_pr_thread(self):
        t = xs.github_event_to_trigger(
            event(102, body="review note", number=13, pull_request=True), "surlymochan/swrm")
        self.assertEqual(t["thread"]["kind"], "pr")

    def test_comment_event_uses_parent_kind(self):
        kinds = {13: "pr"}
        t = xs.github_event_to_trigger(
            event(103, kind="comment", body="looks good", number=13), "surlymochan/swrm",
            thread_kinds=kinds)
        self.assertEqual(t["id"], "github:comment:103")
        self.assertEqual(t["thread"], {"repo": "surlymochan/swrm", "kind": "pr", "number": 13})

    def test_comment_event_defaults_to_issue_thread(self):
        t = xs.github_event_to_trigger(
            event(104, kind="comment", body="thanks", number=7), "surlymochan/swrm")
        self.assertEqual(t["thread"]["kind"], "issue")

    def test_channel_marker_post_is_not_a_trigger(self):
        body = xs.INTAKE_CHANNEL_MARKER + "\nopenswarm intake: ticket `tkt-abc`"
        self.assertIsNone(xs.github_event_to_trigger(
            event(105, kind="comment", body=body, number=7), "surlymochan/swrm"))

    def test_own_bot_message_is_not_a_trigger(self):
        self.assertIsNone(xs.github_event_to_trigger(
            event(106, author="openswarm[bot]", bot=True, body="hello"), "surlymochan/swrm"))

    def test_null_body_becomes_empty_trigger(self):
        t = xs.github_event_to_trigger(
            event(107, body=None, number=8), "surlymochan/swrm")
        self.assertEqual(t["body"], "")

    def test_malformed_event_refused(self):
        with self.assertRaises(ValueError):
            xs.github_event_to_trigger({"id": 1, "body": "x"}, "surlymochan/swrm")
        with self.assertRaises(ValueError):
            xs.github_event_to_trigger(event("not-an-int", body="x"), "surlymochan/swrm")

    # -- Polling: cursor + dedup ------------------------------------------

    def test_poll_translates_skips_own_and_persists_cursor(self):
        events = [
            event(201, body="fix the flaky test please", number=10, updated="2026-11-15T03:01:00Z"),
            event(202, kind="comment", body=xs.INTAKE_CHANNEL_MARKER + "\nreport",
                  number=10, updated="2026-11-15T03:02:00Z"),
            event(203, kind="comment", body="second task here", number=11,
                  updated="2026-11-15T03:03:00Z"),
        ]
        out = xs.poll_github_source("surlymochan/swrm", fetcher=lambda r, s: events,
                                    data_dir=str(self.data))
        self.assertEqual(len(out["triggers"]), 2)
        self.assertEqual(out["skipped_own"], 1)
        self.assertEqual(out["duplicates"], 0)
        self.assertEqual(out["cursor"]["since"], "2026-11-15T03:03:00Z")
        state = xs.load_intake_state(str(self.data))
        self.assertEqual(state["cursors"]["surlymochan/swrm"]["since"],
                         "2026-11-15T03:03:00Z")

    def test_repoll_yields_only_duplicates(self):
        events = [event(201, body="fix the flaky test please", number=10,
                        updated="2026-11-15T03:01:00Z")]
        xs.poll_github_source("surlymochan/swrm", fetcher=lambda r, s: events,
                              data_dir=str(self.data))
        self.ingest(trig("github:issue:201", body="fix the flaky test please", number=10))
        out = xs.poll_github_source("surlymochan/swrm", fetcher=lambda r, s: events,
                                    data_dir=str(self.data))
        self.assertEqual(out["triggers"], [])
        self.assertEqual(out["duplicates"], 1)

    def test_poll_thread_filter_reaches_fetcher(self):
        seen = {}

        def fetcher(repo, since):
            seen["thread"] = True
            return [event(204, body="scoped task", number=5)]
        out = xs.poll_github_source("surlymochan/swrm", fetcher=fetcher,
                                    data_dir=str(self.data), thread=("issue", 5))
        self.assertTrue(seen.get("thread"))
        self.assertEqual(len(out["triggers"]), 1)

    # -- Assessment ---------------------------------------------------------

    def test_assessment_never_overrides_gate_ignore(self):
        # unlisted author who did NOT mention the bot: Gate ignore even
        # for a concrete task; Assessment must not revive it.
        gate = xs.intake_gate_decide(
            trig("a1", author="random-user", body="please rebuild the whole CI pipeline now"),
            self.policy)
        self.assertEqual(gate["decision"], "ignore")
        verdict = xs.intake_assess_trigger(
            trig("a1", author="random-user", body="please rebuild the whole CI pipeline now"),
            gate, data_dir=str(self.data))
        self.assertEqual((verdict["verdict"], verdict["source"]), ("ignore", "gate"))

    def test_assessment_never_overrides_gate_stop(self):
        gate = xs.intake_gate_decide(
            trig("a2", body="@openswarm stop, do not do this"), self.policy)
        self.assertEqual(gate["decision"], "stop")
        verdict = xs.intake_assess_trigger(
            trig("a2", body="@openswarm stop, do not do this"), gate, data_dir=str(self.data))
        self.assertEqual((verdict["verdict"], verdict["source"]), ("stop", "gate"))

    def test_explicit_mandate_confirmed_without_model(self):
        gate = xs.intake_gate_decide(
            trig("a3", body="@openswarm fix the flaky fleet test"), self.policy)
        self.assertEqual((gate["decision"], gate["strength"]), ("accept", "explicit"))
        verdict = xs.intake_assess_trigger(
            trig("a3", body="@openswarm fix the flaky fleet test"), gate, data_dir=str(self.data))
        self.assertEqual((verdict["verdict"], verdict["source"]), ("accept", "explicit"))

    def test_inferred_concrete_task_accepted_by_default(self):
        gate = xs.intake_gate_decide(trig("a4"), self.policy)
        self.assertEqual((gate["decision"], gate["strength"]), ("accept", "inferred"))
        verdict = xs.intake_assess_trigger(trig("a4"), gate, data_dir=str(self.data))
        self.assertEqual((verdict["verdict"], verdict["source"]), ("accept", "default"))

    def test_inferred_greeting_only_ignored_by_default(self):
        # Unlisted author mentioning the bot is inferred (not explicit),
        # so the default assessor sees it: a bare mention is no task.
        gate = xs.intake_gate_decide(
            trig("a5", author="random-user", body="@openswarm"), self.policy)
        self.assertEqual((gate["decision"], gate["strength"]), ("accept", "inferred"))
        verdict = xs.intake_assess_trigger(
            trig("a5", author="random-user", body="@openswarm"), gate, data_dir=str(self.data))
        self.assertEqual((verdict["verdict"], verdict["source"]), ("ignore", "default"))

    def test_inferred_repeat_of_open_ticket_ignored(self):
        first = self.ingest(trig("a6", body="please fix the flaky test in fleet dispatch"))
        self.assertIsNotNone(first["ticket_id"])
        gate = xs.intake_gate_decide(
            trig("a7", body="please fix the flaky test in fleet dispatch"), self.policy)
        verdict = xs.intake_assess_trigger(
            trig("a7", body="please fix the flaky test in fleet dispatch"),
            gate, data_dir=str(self.data))
        self.assertEqual(verdict["verdict"], "ignore")
        self.assertIn(first["ticket_id"], verdict["reason"])

    def _model_script(self, body):
        path = os.path.join(self.tmp.name, "fake-assess.py")
        with open(path, "w") as f:
            f.write("import json, sys\n"
                    "json.load(sys.stdin)\n"
                    f"print(json.dumps({body!r}))\n")
        return [sys.executable, path]

    def test_model_hook_accept_and_ignore(self):
        gate = xs.intake_gate_decide(trig("a8"), self.policy)
        verdict = xs.intake_assess_trigger(
            trig("a8"), gate, data_dir=str(self.data),
            model_command=self._model_script({"verdict": "accept", "reason": "model: clear task"}))
        self.assertEqual((verdict["verdict"], verdict["source"]), ("accept", "model"))
        verdict = xs.intake_assess_trigger(
            trig("a8"), gate, data_dir=str(self.data),
            model_command=self._model_script({"verdict": "ignore", "reason": "model: too vague"}))
        self.assertEqual((verdict["verdict"], verdict["source"]), ("ignore", "model"))

    def test_model_hook_can_veto_explicit(self):
        gate = xs.intake_gate_decide(
            trig("a9", body="@openswarm fix the flaky fleet test"), self.policy)
        verdict = xs.intake_assess_trigger(
            trig("a9", body="@openswarm fix the flaky fleet test"), gate,
            data_dir=str(self.data),
            model_command=self._model_script({"verdict": "ignore", "reason": "model: out of scope"}))
        self.assertEqual(verdict["verdict"], "ignore")

    def test_model_hook_failure_falls_back_to_default(self):
        path = os.path.join(self.tmp.name, "fake-assess-fail.py")
        with open(path, "w") as f:
            f.write("import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
        gate = xs.intake_gate_decide(trig("a10"), self.policy)
        verdict = xs.intake_assess_trigger(
            trig("a10"), gate, data_dir=str(self.data),
            model_command=[sys.executable, path])
        self.assertEqual((verdict["verdict"], verdict["source"]), ("accept", "default"))
        self.assertIn("fallback_error", verdict)

    def test_model_hook_bad_verdict_falls_back(self):
        gate = xs.intake_gate_decide(trig("a11"), self.policy)
        verdict = xs.intake_assess_trigger(
            trig("a11"), gate, data_dir=str(self.data),
            model_command=self._model_script({"verdict": "maybe", "reason": "x"}))
        self.assertEqual((verdict["verdict"], verdict["source"]), ("accept", "default"))
        self.assertIn("fallback_error", verdict)

    # -- Ingest wiring ------------------------------------------------------

    def test_shadow_mode_never_dispatches(self):
        runner = fake_runner()
        result = self.ingest(trig("b1"), dispatch_runner=runner,
                             channel_poster=fake_channel())
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(runner.calls, [])
        state = xs.load_intake_state(str(self.data))
        self.assertEqual(state["assignments"], [])

    def test_on_mode_dispatches_and_posts_channel(self):
        policy = self.on_policy(accept_mode="on")
        runner = fake_runner()
        channel = fake_channel()
        result = self.ingest(trig("b2"), policy=policy, dispatch_runner=runner,
                             channel_poster=channel)
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["dispatch"]["run_id"], "20261115-030000-abcdef")
        self.assertEqual(result["dispatch"]["status"], "done")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(channel.calls), 1)
        repo, number, body = channel.calls[0]
        self.assertEqual((repo, number), ("surlymochan/swrm", 1))
        self.assertIn(xs.INTAKE_CHANNEL_MARKER, body)
        self.assertIn(result["ticket_id"], body)
        self.assertIn("20261115-030000-abcdef", body)
        state = xs.load_intake_state(str(self.data))
        self.assertEqual(len(state["assignments"]), 1)
        self.assertEqual(state["assignments"][0]["status"], "ended")
        self.assertEqual(state["assignments"][0]["executor"], "routed")
        kinds = [e["kind"] for e in xs.intake_ledger_entries(str(self.data))]
        for kind in ("assignment_created", "dispatch_started", "dispatch_finished",
                     "channel_posted"):
            self.assertIn(kind, kinds)

    def test_on_mode_draft_only_never_dispatches(self):
        policy = self.on_policy(accept_mode="on")
        runner = fake_runner()
        result = self.ingest(
            trig("b3", body="@openswarm buy 10 USDT of BTC now please"), policy=policy,
            dispatch_runner=runner, channel_poster=fake_channel())
        self.assertTrue(result["decision"]["draft_only"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(runner.calls, [])

    def test_on_mode_dispatch_failure_ends_assignment_and_records(self):
        policy = self.on_policy(accept_mode="on")

        def failing(ticket, trigger, decision, policy, data_dir, cwd):
            raise ValueError("executor exploded")
        result = self.ingest(trig("b4"), policy=policy, dispatch_runner=failing,
                             channel_poster=fake_channel())
        self.assertFalse(result["dispatched"])
        self.assertIn("dispatch_error", result)
        state = xs.load_intake_state(str(self.data))
        self.assertEqual(len(state["assignments"]), 1)
        self.assertEqual(state["assignments"][0]["status"], "ended")
        kinds = [e["kind"] for e in xs.intake_ledger_entries(str(self.data))]
        self.assertEqual(kinds.count("dispatch_failed"), 1)

    def test_channel_failure_does_not_fail_ingest(self):
        policy = self.on_policy(accept_mode="on")

        def failing_poster(repo, number, body):
            raise ValueError("gh is down")
        result = self.ingest(trig("b5"), policy=policy, dispatch_runner=fake_runner(),
                             channel_poster=failing_poster)
        self.assertTrue(result["dispatched"])
        self.assertIn("channel_error", result)
        kinds = [e["kind"] for e in xs.intake_ledger_entries(str(self.data))]
        self.assertIn("channel_failed", kinds)

    def test_assessment_downgrade_creates_no_ticket_or_dispatch(self):
        policy = self.on_policy(accept_mode="on")
        runner = fake_runner()
        # Unlisted author, bare mention: inferred -> default assessment ignores.
        result = self.ingest(trig("b6", author="random-user", body="@openswarm"), policy=policy,
                             dispatch_runner=runner, channel_poster=fake_channel())
        self.assertEqual(result["decision"]["decision"], "ignore")
        self.assertEqual(result["decision"]["gate_decision"], "accept")
        self.assertIsNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(runner.calls, [])

    def test_assess_false_keeps_gate_only_behaviour(self):
        result = self.ingest(trig("b7", body="@openswarm"), assess=False)
        self.assertEqual(result["decision"]["decision"], "accept")
        self.assertIsNotNone(result["ticket_id"])

    def test_on_mode_without_runner_creates_ticket_only(self):
        policy = self.on_policy(accept_mode="on")
        result = self.ingest(trig("b8"), policy=policy)
        self.assertIsNotNone(result["ticket_id"])
        self.assertFalse(result["dispatched"])
        self.assertIn("dispatch_note", result)

    # -- Policy validation --------------------------------------------------

    def test_policy_rejects_bad_dispatch_block(self):
        bad = self.on_policy(dispatch={"backend": 3})
        with self.assertRaises(ValueError):
            xs.validate_intake_policy(bad)
        bad = self.on_policy(dispatch={"budget_minutes": 0})
        with self.assertRaises(ValueError):
            xs.validate_intake_policy(bad)

    def test_policy_rejects_bad_assessment_block(self):
        bad = self.on_policy(assessment={"command": "not-a-list"})
        with self.assertRaises(ValueError):
            xs.validate_intake_policy(bad)
        bad = self.on_policy(assessment={"timeout_seconds": -1})
        with self.assertRaises(ValueError):
            xs.validate_intake_policy(bad)

    def test_policy_accepts_valid_dispatch_and_assessment_blocks(self):
        policy = self.on_policy(
            dispatch={"backend": "echo", "budget_minutes": 5},
            assessment={"command": ["assess", "--stdin"], "timeout_seconds": 30})
        xs.validate_intake_policy(policy)
        self.assertEqual(xs.intake_dispatch_config(policy)["backend"], "echo")
        self.assertEqual(xs.intake_assessment_config(policy)["command"],
                         ["assess", "--stdin"])

    # -- CLI -----------------------------------------------------------------

    def _run_cli(self, argv, env_extra=None):
        env = dict(os.environ)
        env["OPENSWARM_HOME"] = str(self.data)
        if env_extra:
            env.update(env_extra)
        return subprocess.run([sys.executable, str(HERE.parent / "bin" / "openswarm"), *argv],
                              capture_output=True, text=True, env=env)

    def test_cli_assess_prints_verdict_without_state_change(self):
        trigger_file = os.path.join(self.tmp.name, "trigger.json")
        with open(trigger_file, "w") as f:
            json.dump(trig("c1"), f)
        proc = self._run_cli(["intake", "assess", "--trigger-file", trigger_file])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out["gate_decision"], out["assessment"]["verdict"]),
                         ("accept", "accept"))
        self.assertEqual(xs.load_intake_state(str(self.data))["tickets"], [])

    def test_cli_poll_rejects_issue_and_pr_together(self):
        import argparse
        args = argparse.Namespace(
            repo="surlymochan/swrm", issue=1, pr=2, since=None, limit=100,
            dry_run=True, policy_file=None, model_command=None, backend=None,
            backends_file=None, verify=None, budget_minutes=None,
            no_dispatch=False, no_channel=False, cwd=None, json=False)
        with self.assertRaises(ValueError):
            xs.cmd_intake_poll(args, data_dir=str(self.data))

    def test_cli_poll_dry_run_with_fake_gh(self):
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir)
        gh = os.path.join(bindir, "gh")
        issues = [event(301, body="fix the flaky test please", number=10,
                        updated="2026-11-15T03:01:00Z")]
        comments = [event(302, kind="comment", body="a follow-up task here", number=10,
                          updated="2026-11-15T03:02:00Z")]
        with open(gh, "w") as f:
            f.write("#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    "path = sys.argv[2]\n"
                    f"issues = {json.dumps(issues)}\n"
                    f"comments = {json.dumps(comments)}\n"
                    "print(json.dumps(comments if 'issues/comments' in path else issues))\n")
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC)
        env_extra = {"PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
        proc = self._run_cli(["intake", "poll", "--repo", "surlymochan/swrm",
                              "--dry-run", "--json"], env_extra=env_extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["triggers_found"], 2)
        self.assertEqual(out["results"], [])
        # dry-run ingests nothing, but the cursor is persisted.
        state = xs.load_intake_state(str(self.data))
        self.assertEqual(state["cursors"]["surlymochan/swrm"]["since"],
                         "2026-11-15T03:02:00Z")
        self.assertEqual(state["tickets"], [])


class M27Slice2EchoDispatchTests(unittest.TestCase):
    """One real dispatch through the subprocess `run` path with the echo
    custom backend: proves intake dispatch reuses quota routing, the
    executor, and result.json -- not a fake runner."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m27s2-e2e-")
        self.data = pathlib.Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.patch = mock.patch.object(xs, "DATA", str(self.data))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.script = os.path.join(self.tmp.name, "fake-echo-cli.py")
        with open(self.script, "w") as f:
            f.write(FAKE_ECHO_CLI)
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self.config = os.path.join(self.tmp.name, "backends.json")
        with open(self.config, "w") as f:
            json.dump({"backends": [{"id": "echo", "name": "Echo CLI (test backend)",
                                     "command": [sys.executable, self.script, "--prompt", "{prompt}",
                                                 "--workdir", "{worktree}", "--model", "{model}"],
                                     "env": {"ECHO_MODEL": "echo-1"}, "quota_source": "none",
                                     "model": "echo-1"}]}, f)
        self.cwd = os.path.join(self.tmp.name, "work")
        os.makedirs(self.cwd)
        subprocess.run(["git", "init", "-q"], cwd=self.cwd, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.cwd, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=self.cwd, check=True)
        with open(os.path.join(self.cwd, "README.md"), "w") as f:
            f.write("# demo\n")
        subprocess.run(["git", "add", "."], cwd=self.cwd, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.cwd, check=True)
        self._env = {k: os.environ.get(k) for k in ("OPENSWARM_STATE_FILE",)}
        os.environ["OPENSWARM_STATE_FILE"] = _load.FIXTURE_STATE

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @unittest.skipIf(os.name == "nt", "worktree dispatch relies on POSIX shell/git semantics")
    def test_echo_backend_real_dispatch_end_to_end(self):
        policy = xs.default_intake_policy()
        policy["accept_mode"] = "on"
        policy["dispatch"] = {"backend": "echo", "backends_file": self.config,
                              "verify": "test -f echo-output.txt", "budget_minutes": 5}
        result = xs.intake_ingest_trigger(
            trig("e1", body="add a file echo-output.txt with a greeting"),
            policy=policy, data_dir=str(self.data),
            dispatch_runner=xs.run_openswarm_run,
            channel_poster=fake_channel(), dispatch_cwd=self.cwd)
        self.assertTrue(result["dispatched"], result.get("dispatch_error", ""))
        self.assertEqual(result["dispatch"]["status"], "done")
        run_id = result["dispatch"]["run_id"]
        result_path = self.data / "runs" / run_id / "result.json"
        self.assertTrue(result_path.exists())
        with open(result_path) as f:
            saved = json.load(f)
        self.assertEqual(saved.get("backend"), "echo")
        self.assertEqual(saved.get("status"), "done")
        branch = saved.get("branch")
        self.assertTrue(branch)
        shown = subprocess.run(["git", "show", f"{branch}:echo-output.txt"], cwd=self.cwd,
                               capture_output=True, text=True, check=True).stdout
        self.assertIn("add a file echo-output.txt", shown)
        state = xs.load_intake_state(str(self.data))
        self.assertEqual(state["assignments"][0]["status"], "ended")
        self.assertEqual(state["assignments"][0]["executor"], "echo")


if __name__ == "__main__":
    unittest.main()
