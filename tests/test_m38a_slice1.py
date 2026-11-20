"""M38-A slice 1 (#288/#292): pre-exec gate — PATH shim takeover.

Tests use fixture events, fake real binaries and a stub lane child only;
no backend, no network. The shim is loaded as a module from
bin/openswarm-gate-shim; its classification/redaction/key functions must
agree with bin/openswarm on a shared corpus (the patterns are mirrored,
so any drift fails here first).
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import _load

xs = _load.load()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_shim():
    loader = importlib.machinery.SourceFileLoader(
        "openswarm_gate_shim", os.path.join(ROOT, "bin", "openswarm-gate-shim"))
    spec = importlib.util.spec_from_loader("openswarm_gate_shim", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


shim = load_shim()

POL_ON = {"approval": {"enabled": True,
                       "categories": ["push", "publish", "destructive", "secret"],
                       "wait_seconds": 30}}
POL_OFF = {}


def tool_event(command):
    return {"type": "tool_execution_start", "args": {"command": command}}


def item_started(command):
    return {"method": "item/started",
            "params": {"item": {"type": "commandExecution", "command": command}}}


class PreexecBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wt = self.root / "wt"
        self.wt.mkdir()
        for name, value in (("DATA", str(self.root)), ("RUNS", str(self.root / "runs")),
                            ("LEDGER", str(self.root / "ledger.jsonl")),
                            ("APPROVALS", str(self.root / "approvals.json")),
                            ("APPROVALS_LOCK", str(self.root / "approvals.lock"))):
            patch = mock.patch.object(xs, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.rdir = self.root / "runs" / "20261120-210000-abcdef"
        self.rdir.mkdir(parents=True)
        self.event_file = str(self.rdir / "events.1.codex.jsonl")
        Path(self.event_file).touch()
        self._old_cwd = os.getcwd()
        os.chdir(self.wt)
        self.addCleanup(os.chdir, self._old_cwd)

    def ledger_types(self):
        path = Path(xs.LEDGER)
        if not path.exists():
            return []
        return [json.loads(line).get("type") for line in path.read_text().splitlines() if line.strip()]


# -- classification agreement: shim == approval_risk ----------------------

CLASSIFY_CASES = [
    (["git", "push", "origin", "main"], ("push", "push")),
    (["git", "-C", "/tmp/x", "push"], ("push", "push")),
    (["git", "push", "--force", "origin", "main"], ("push", "push")),
    (["git", "status"], None),
    (["git", "add", "."], None),
    (["git", "reset", "--hard", "HEAD"], ("destructive", "git destructive")),
    (["git", "checkout", "-f", "main"], ("destructive", "git destructive")),
    (["git", "clean", "-fd"], ("destructive", "git destructive")),
    (["git", "log", "--oneline"], None),
    (["npm", "publish", "--tag", "beta"], ("publish", "publish")),
    (["pnpm", "publish"], ("publish", "publish")),
    (["yarn", "publish"], ("publish", "publish")),
    (["twine", "upload", "dist/*"], ("publish", "publish")),
    (["gh", "release", "create", "v1.0.0"], ("publish", "publish")),
    (["gh", "pr", "merge", "123"], ("publish", "publish")),
    (["gh", "issue", "list"], None),
    (["npm", "test"], None),
    (["rm", "-rf", "../outside"], ("destructive", "rm outside worktree")),
    (["rm", "-rf", "subdir"], None),
    (["rm", "/tmp/single-file"], None),
    (["rm", "-rf", "/tmp/somedir"], ("destructive", "rm outside worktree")),
    (["sudo", "ls"], ("destructive", "sudo")),
    (["sudo", "git", "push"], ("destructive", "sudo")),
    (["cat", ".env"], ("secret", "secret print")),
    (["cat", "README.md"], None),
    (["cat", "config/secrets.yaml"], ("secret", "secret print")),
    (["head", "-n", "5", ".env.local"], ("secret", "secret print")),
    (["printenv"], ("secret", "secret print")),
    (["printenv", "HOME"], None),
    (["cat", "token.txt"], None),
    (["git", "commit", "--no-verify", "-m", "x"], None),
    (["npm", "run", "publish-script"], ("publish", "publish")),
]


class ClassifyAgreementTests(PreexecBase):
    def test_shim_classify_matches_approval_risk(self):
        for argv, expected in CLASSIFY_CASES:
            with self.subTest(argv=argv):
                got = shim.classify(argv, str(self.wt))
                self.assertEqual(got, expected)
                risk = xs.approval_risk(tool_event(shlex.join(argv)), POL_ON, str(self.wt))
                actual = None if risk is None else (risk["category"], risk["risk"])
                self.assertEqual(actual, expected)
                self.assertEqual(got, actual)

    def test_gate_off_classifies_nothing(self):
        for argv, _ in CLASSIFY_CASES:
            with self.subTest(argv=argv):
                risk = xs.approval_risk(tool_event(shlex.join(argv)), POL_OFF, str(self.wt))
                self.assertIsNone(risk)


# -- command-key agreement: shim argv key == loop key ----------------------

KEY_CASES = [
    ("git push origin main", ["git", "push", "origin", "main"]),
    ("zsh -lc 'git push origin main'", ["git", "push", "origin", "main"]),
    ("bash -c \"git push origin main\"", ["git", "push", "origin", "main"]),
    ("sudo git push origin main", ["sudo", "git", "push", "origin", "main"]),
    ("env FOO=1 git push origin main", ["git", "push", "origin", "main"]),
    ("timeout 5 git push origin main", ["git", "push", "origin", "main"]),
    ("git push origin 'my branch'", ["git", "push", "origin", "my branch"]),
    ("FOO=1 git push origin main", ["git", "push", "origin", "main"]),
    ("git -C /tmp/x push", ["git", "-C", "/tmp/x", "push"]),
    ("sh -c 'npm publish --tag beta'", ["npm", "publish", "--tag", "beta"]),
    ("rm -rf ../outside", ["rm", "-rf", "../outside"]),
]


class KeyAgreementTests(PreexecBase):
    def test_loop_key_matches_shim_key(self):
        for command, argv in KEY_CASES:
            with self.subTest(command=command):
                self.assertEqual(xs.preexec_command_key(command),
                                 shim.command_key(argv))

    def test_multi_segment_keys_in_order(self):
        self.assertEqual(xs.preexec_command_keys("git push; echo hi"),
                         ["git\x1fpush", "echo\x1fhi"])

    def test_request_approval_record_carries_key(self):
        record = xs.request_approval(
            "run-1", str(self.rdir),
            {"category": "push", "risk": "push",
             "command": "git push origin main"})
        self.assertEqual(record["command_key"], "git\x1fpush\x1forigin\x1fmain")
        stored = xs.approval_load_all()[record["id"]]
        self.assertEqual(stored["command_key"], record["command_key"])


# -- evidence-redaction agreement ------------------------------------------

REDACT_CASES = [
    ["git", "push", "origin", "main"],
    ["git", "push", "https://user:tok123@github.com/a/b.git", "main"],
    ["npm", "publish", "--tag", "beta"],
    ["gh", "release", "create", "v1.0.0"],
    ["rm", "-rf", "../outside"],
    ["cat", ".env"],
    ["git", "push", "--force", "origin", "main"],
    ["gh", "pr", "merge", "123"],
]


class RedactAgreementTests(PreexecBase):
    def test_shim_evidence_matches_redact_command(self):
        for argv in REDACT_CASES:
            with self.subTest(argv=argv):
                self.assertEqual(shim.evidence_command(argv, str(self.wt)),
                                 xs.redact_command(shlex.join(argv), evidence=True,
                                                   worktree=str(self.wt)))


# -- install / arm / disarm -------------------------------------------------

class InstallArmTests(PreexecBase):
    def test_gate_off_installs_nothing_and_path_unchanged(self):
        before = os.environ.get("PATH", "")
        tmpdir, env = xs.configure_executor_environment(str(self.wt), policy=POL_OFF)
        self.assertEqual(env.get("PATH", ""), before)
        self.assertFalse(os.path.isdir(os.path.join(tmpdir, "bin")))

    def test_gate_on_installs_shims_first_on_path(self):
        tmpdir, env = xs.configure_executor_environment(str(self.wt), policy=POL_ON)
        bin_dir = os.path.join(tmpdir, "bin")
        self.assertTrue(env["PATH"].startswith(bin_dir + os.pathsep))
        for verb in xs.PREEXEC_GATE_VERBS:
            dest = os.path.join(bin_dir, verb + ".cmd" if os.name == "nt" else verb)
            self.assertTrue(os.path.isfile(dest), verb)
            if os.name != "nt":
                self.assertTrue(os.access(dest, os.X_OK), verb)
        # Idempotent: a second install leaves the same files.
        again = xs.install_preexec_gate_shims(str(self.wt), tmpdir)
        self.assertEqual(again, bin_dir)

    def test_cmd_wrapper_template(self):
        text = xs.preexec_gate_cmd_wrapper("git", "/repo/bin/openswarm-gate-shim",
                                           "/wt/.openswarm-tmp")
        self.assertIn("git", text)
        self.assertIn("/repo/bin/openswarm-gate-shim", text)
        self.assertIn("/wt/.openswarm-tmp", text)
        self.assertIn("--state-dir", text)

    def test_arm_writes_gate_and_disarm_removes_it(self):
        xs.configure_executor_environment(str(self.wt), policy=POL_ON)
        gate = xs.arm_preexec_gate(str(self.wt), "run-1", str(self.rdir),
                                   self.event_file, time.time() + 3600, POL_ON)
        self.assertIsNotNone(gate)
        self.assertEqual(gate["run_id"], "run-1")
        self.assertEqual(gate["categories"], POL_ON["approval"]["categories"])
        self.assertTrue(os.path.isfile(
            os.path.join(str(self.wt), ".openswarm-tmp", "gate.json")))
        xs.disarm_preexec_gate(str(self.wt))
        self.assertFalse(os.path.exists(
            os.path.join(str(self.wt), ".openswarm-tmp", "gate.json")))

    def test_arm_noop_when_gate_off(self):
        self.assertIsNone(xs.arm_preexec_gate(str(self.wt), "run-1", str(self.rdir),
                                              self.event_file, time.time() + 3600, POL_OFF))
        self.assertFalse(os.path.exists(
            os.path.join(str(self.wt), ".openswarm-tmp", "bin")))

    def test_arm_installs_shims_when_gate_on_and_shims_absent(self):
        # #296: arm used to return None here; lane_prepare_workspace's
        # `git clean -fdx` deletes the shims on every dispatch, so the
        # gate could never arm in a real lane run. arm reinstalls instead.
        gate = xs.arm_preexec_gate(str(self.wt), "run-1", str(self.rdir),
                                   self.event_file, time.time() + 3600, POL_ON)
        self.assertIsNotNone(gate)
        self.assertTrue(os.path.isfile(
            os.path.join(str(self.wt), ".openswarm-tmp", "gate.json")))
        for verb in xs.PREEXEC_GATE_VERBS:
            dest = os.path.join(str(self.wt), ".openswarm-tmp", "bin",
                                verb + ".cmd" if os.name == "nt" else verb)
            self.assertTrue(os.path.isfile(dest), verb)


# -- loop resume: a shim-approved command is not paused twice ---------------

class ResumeTests(PreexecBase):
    def _approve_push(self, run_id="run-1"):
        record = xs.request_approval(
            run_id, str(self.rdir),
            {"category": "push", "risk": "push", "command": "git push origin main"})
        return xs.decide_approval(record["id"], "approved", by="test")

    def test_resume_matches_approved_record(self):
        record = self._approve_push()
        found = xs.preexec_approved_resume(item_started("git push origin main"),
                                           str(self.wt), "run-1")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], record["id"])

    def test_resume_ignores_other_run_and_other_command(self):
        self._approve_push()
        self.assertIsNone(xs.preexec_approved_resume(item_started("git push origin main"),
                                                     str(self.wt), "run-2"))
        self.assertIsNone(xs.preexec_approved_resume(item_started("git status"),
                                                     str(self.wt), "run-1"))

    def test_resume_ignores_stale_approval(self):
        record = self._approve_push()
        index = xs.approval_load_all()
        index[record["id"]]["decided_ts"] = time.time() - 3600
        xs.approval_save_all(index)
        self.assertIsNone(xs.preexec_approved_resume(item_started("git push origin main"),
                                                     str(self.wt), "run-1"))

    def test_resume_ignores_pending_record(self):
        xs.request_approval("run-1", str(self.rdir),
                            {"category": "push", "risk": "push",
                             "command": "git push origin main"})
        self.assertIsNone(xs.preexec_approved_resume(item_started("git push origin main"),
                                                     str(self.wt), "run-1"))


# -- shim end to end (real subprocess, fake real binaries) -------------------

FAKE_BIN = """#!/usr/bin/env python3
import os, sys, time
log = os.environ["FAKE_GIT_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(f"{time.time():.6f} {' '.join(sys.argv[1:])}\\n")
print("fake binary ran")
"""


class ShimEndToEndTests(PreexecBase):
    def setUp(self):
        super().setUp()
        self.realbin = self.root / "realbin"
        self.realbin.mkdir()
        for verb in ("git", "npm", "cat"):
            if os.name == "nt":
                # Windows: shutil.which only matches PATHEXT extensions,
                # so an extensionless fake binary is invisible to the shim
                # (real binary not found -> 127). Use a .cmd wrapper that
                # logs in the same format as FAKE_BIN (epoch float first).
                # The -c code must contain no '%' (cmd.exe would expand it
                # as a variable); only the trailing %* forwards arguments.
                path = self.realbin / (verb + ".cmd")
                code = ("import os,sys,time;"
                        "open(os.environ['FAKE_GIT_LOG'],'a',encoding='utf-8')"
                        ".write(str(time.time())+' '+' '.join(sys.argv[1:])+'\\n');"
                        "print('fake binary ran')")
                path.write_text('@\"' + sys.executable + '\" -c \"' + code + '\" %*\n')
            else:
                path = self.realbin / verb
                path.write_text(FAKE_BIN)
                path.chmod(0o755)
        self.fake_log = self.root / "fake-git.log"
        self.env = dict(os.environ, FAKE_GIT_LOG=str(self.fake_log))
        self.shim_bin = None

    def _arm(self, wait_seconds=30):
        policy = {"approval": {"enabled": True,
                               "categories": ["push", "publish", "destructive", "secret"],
                               "wait_seconds": wait_seconds}}
        _tmpdir, env = xs.configure_executor_environment(str(self.wt), policy=policy)
        self.shim_bin = os.path.join(_tmpdir, "bin")
        # run_id must be the run dir's basename: decide_approval derives
        # the run file from run_id, and the shim polls that same file.
        xs.arm_preexec_gate(str(self.wt), self.rdir.name, str(self.rdir),
                            self.event_file, time.time() + 3600, policy)
        self.env["PATH"] = (self.shim_bin + os.pathsep + str(self.realbin)
                            + os.pathsep + "/usr/bin" + os.pathsep + "/bin")
        return policy

    def _run_shim(self, verb, *args):
        if os.name == "nt":
            verb = verb + ".cmd"
        return subprocess.run([os.path.join(self.shim_bin, verb), *args],
                              cwd=str(self.wt), env=self.env,
                              capture_output=True, text=True, timeout=60)

    def _fake_log_lines(self):
        if not self.fake_log.exists():
            return []
        return [line for line in self.fake_log.read_text().splitlines() if line.strip()]

    def _decide_after(self, decision, delay=0.3):
        def decide():
            time.sleep(delay)
            deadline = time.time() + 10
            pending = []
            while time.time() < deadline:
                pending = [r for r in xs.approval_load_all().values()
                           if r.get("status") == "pending"]
                if pending:
                    break
                time.sleep(0.1)
            self.assertEqual(len(pending), 1)
            xs.decide_approval(pending[0]["id"], decision, by="test")
        threading.Thread(target=decide, daemon=True).start()

    def test_gate_off_passthrough(self):
        # Shims installed but no gate.json (never armed): the shim execs
        # the real binary at once, no record, no wait.
        _tmpdir, _env = xs.configure_executor_environment(str(self.wt), policy=POL_ON)
        self.shim_bin = os.path.join(_tmpdir, "bin")
        self.env["PATH"] = (self.shim_bin + os.pathsep + str(self.realbin)
                            + os.pathsep + "/usr/bin" + os.pathsep + "/bin")
        result = self._run_shim("git", "push", "origin", "main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._fake_log_lines()), 1)
        self.assertEqual(xs.approval_load_all(), {})
        # A gate.json with enabled=false passes through the same way.
        gate_path = os.path.join(str(self.wt), ".openswarm-tmp", "gate.json")
        Path(gate_path).write_text(json.dumps({"enabled": False}))
        self.fake_log.unlink()
        result = self._run_shim("git", "push", "origin", "main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._fake_log_lines()), 1)
        self.assertEqual(xs.approval_load_all(), {})

    def test_nongated_command_runs_without_record(self):
        self._arm()
        result = self._run_shim("git", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._fake_log_lines()), 1)
        self.assertEqual(xs.approval_load_all(), {})

    def test_approved_executes_after_decision(self):
        self._arm()
        self._decide_after("approved")
        started = time.time()
        result = self._run_shim("git", "push", "origin", "main")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self._fake_log_lines()
        self.assertEqual(len(lines), 1)
        # Decision before execution: decided_ts < real binary start.
        record = next(iter(xs.approval_load_all().values()))
        self.assertEqual(record["status"], "approved")
        self.assertLess(record["decided_ts"], float(lines[0].split()[0]))
        self.assertLess(record["created_ts"], record["decided_ts"])
        self.assertIn("approval_requested", self.ledger_types())
        self.assertIn("approval_approved", self.ledger_types())
        events = [json.loads(line) for line in Path(self.event_file).read_text().splitlines()]
        self.assertTrue(any(e.get("type") == "preexec_approved" for e in events))
        self.assertGreaterEqual(time.time() - started, 0.3)

    def test_rejected_does_not_execute(self):
        self._arm()
        self._decide_after("rejected")
        result = self._run_shim("git", "push", "origin", "main")
        self.assertEqual(result.returncode, 130)
        self.assertEqual(self._fake_log_lines(), [])
        record = next(iter(xs.approval_load_all().values()))
        self.assertEqual(record["status"], "rejected")
        self.assertIn("approval_rejected", self.ledger_types())

    def test_timeout_does_not_execute(self):
        self._arm(wait_seconds=1)
        started = time.time()
        result = self._run_shim("git", "push", "origin", "main")
        self.assertEqual(result.returncode, 130)
        self.assertGreaterEqual(time.time() - started, 1.0)
        self.assertEqual(self._fake_log_lines(), [])
        record = next(iter(xs.approval_load_all().values()))
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["reason"], "timeout")

    def test_repeat_rejection_fails_fast_without_new_record(self):
        self._arm()
        self._decide_after("rejected")
        self.assertEqual(self._run_shim("git", "push", "origin", "main").returncode, 130)
        started = time.time()
        result = self._run_shim("git", "push", "origin", "main")
        self.assertEqual(result.returncode, 130)
        self.assertLess(time.time() - started, 2.0)
        self.assertEqual(len(xs.approval_load_all()), 1)
        events = [json.loads(line) for line in Path(self.event_file).read_text().splitlines()]
        self.assertTrue(any(e.get("type") == "approval_rejected_repeat" for e in events))

    def test_approved_reuse_executes_without_new_record(self):
        self._arm()
        record = xs.request_approval(
            self.rdir.name, str(self.rdir),
            {"category": "push", "risk": "push", "command": "git push origin main"})
        xs.decide_approval(record["id"], "approved", by="test")
        started = time.time()
        result = self._run_shim("git", "push", "origin", "main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.time() - started, 2.0)
        self.assertEqual(len(xs.approval_load_all()), 1)
        self.assertEqual(len(self._fake_log_lines()), 1)

    def test_missing_real_binary_is_127(self):
        self._arm()
        self.env["PATH"] = (self.shim_bin + os.pathsep + "/usr/bin"
                            + os.pathsep + "/bin")
        # twine has no real binary on PATH here: 127 before any record.
        shim = os.path.join(
            self.shim_bin, "twine.cmd" if os.name == "nt" else "twine")
        result = subprocess.run([shim, "upload", "dist/*"],
                                cwd=str(self.wt), env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertEqual(xs.approval_load_all(), {})


# -- abort grace: interrupt ignored -> killed within seconds, not 30s -------

class StubChild:
    def __init__(self):
        self.lane = {"backend": "codex"}
        self.sent = []
        self.closed = False
        self._pid = 1234

    @property
    def pid(self):
        return self._pid

    def send(self, msg):
        self.sent.append(msg)

    def wait_response(self, req_id, timeout=30, event_file=None):
        if req_id.startswith("thread-"):
            return {"thread": {"id": "th-1"}}
        return {"turn": {"id": "tu-1"}}

    def next_event(self, timeout=0):
        time.sleep(timeout)
        return False

    def alive(self):
        return not self.closed

    def close(self):
        self.closed = True


class AbortGraceTests(PreexecBase):
    def test_abort_grace_constant_is_five(self):
        self.assertEqual(xs.ABORT_STOP_GRACE_SECONDS, 5)

    def test_ignored_interrupt_is_killed_promptly(self):
        child = StubChild()
        lane = {"name": "L1", "backend": "codex", "model": "fake-model", "effort": None}
        spec = {"run_id": "run-abort", "started": time.time(),
                "budget_minutes": 60, "abort_requested": True}
        started = time.time()
        final, usage, error_class, rc, event_file, detail = xs.lane_run_turn(
            lane, child, spec, "do nothing", {"path": str(self.wt)},
            str(self.rdir), None, {}, [])
        elapsed = time.time() - started
        self.assertEqual(rc, 130)
        self.assertEqual(error_class, "stopped")
        self.assertTrue(child.closed)
        self.assertIn("5s", detail)
        self.assertLess(elapsed, 15)
        self.assertGreaterEqual(elapsed, 4.5)


# -- prepare -> arm regression (#296): git clean -fdx must not disarm -----

class PrepareArmRegressionTests(PreexecBase):
    """Real lane_prepare_workspace (git clean -fdx) then arm must arm.

    Slice 1 installed the shims at supervisor start (LaneChild init) and
    arm only checked for bin/: prepare's clean deleted bin/ on every
    dispatch, so arm returned None and the gate never engaged in a real
    lane run (#296). arm must reinstall the shims when the gate is on.
    """

    def setUp(self):
        super().setUp()
        self.realbin = self.root / "realbin"
        self.realbin.mkdir()
        if os.name == "nt":
            # Same %-free fake binary as ShimEndToEndTests (cmd.exe would
            # expand '%' in the -c code as a variable); only %* forwards.
            path = self.realbin / "git.cmd"
            code = ("import os,sys,time;"
                    "open(os.environ['FAKE_GIT_LOG'],'a',encoding='utf-8')"
                    ".write(str(time.time())+' '+' '.join(sys.argv[1:])+'\\n');"
                    "print('fake binary ran')")
            path.write_text('@\"' + sys.executable + '\" -c \"' + code + '\" %*\n')
        else:
            path = self.realbin / "git"
            path.write_text(FAKE_BIN)
            path.chmod(0o755)
        self.fake_log = self.root / "fake-git.log"
        self.env = dict(os.environ, FAKE_GIT_LOG=str(self.fake_log))

    def _git(self, *args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True, timeout=60)

    def _make_repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        self._git("init", "-q", cwd=repo)
        self._git("config", "user.email", "test@example.invalid", cwd=repo)
        self._git("config", "user.name", "test", cwd=repo)
        (repo / "README.md").write_text("test repo\n")
        self._git("add", ".", cwd=repo)
        self._git("commit", "-qm", "initial", cwd=repo)
        return repo

    def _run_shim(self, verb, *args):
        if os.name == "nt":
            verb = verb + ".cmd"
        return subprocess.run([os.path.join(self.shim_bin, verb), *args],
                              cwd=str(self.wt), env=self.env,
                              capture_output=True, text=True, timeout=60)

    def _fake_log_lines(self):
        if not self.fake_log.exists():
            return []
        return [line for line in self.fake_log.read_text().splitlines() if line.strip()]

    def test_prepare_then_arm_arms_gate(self):
        repo = self._make_repo()
        lane = {"name": "testlane", "repo": str(repo)}
        rid = "20261121-020000-test"
        # Supervisor start: child construction installs the shims.
        ws = xs.lane_prepare_workspace(lane, rid)
        wt = ws["path"]
        xs.configure_executor_environment(wt, policy=POL_ON)
        bin_dir = os.path.join(wt, ".openswarm-tmp", "bin")
        shim_name = "git.cmd" if os.name == "nt" else "git"
        self.assertTrue(os.path.isfile(os.path.join(bin_dir, shim_name)))
        # Next dispatch: prepare's `git clean -fdx` wipes .openswarm-tmp.
        xs.lane_prepare_workspace(lane, rid)
        self.assertFalse(os.path.exists(bin_dir))
        # arm must reinstall the shims and write gate.json (pre-fix: None).
        policy = {"approval": {"enabled": True,
                               "categories": ["push", "publish", "destructive", "secret"],
                               "wait_seconds": 1}}
        gate = xs.arm_preexec_gate(wt, "run-1", str(self.rdir),
                                   self.event_file, time.time() + 3600, policy)
        self.assertIsNotNone(gate)
        self.assertTrue(gate["enabled"])
        self.assertTrue(os.path.isfile(os.path.join(bin_dir, shim_name)))
        self.assertTrue(os.path.isfile(os.path.join(wt, ".openswarm-tmp", "gate.json")))
        # Takeover effective, not just files present: a gated push times
        # out (wait 1s) instead of passing through to the real binary.
        self.shim_bin = bin_dir
        self.env["PATH"] = (bin_dir + os.pathsep + str(self.realbin)
                            + os.pathsep + "/usr/bin" + os.pathsep + "/bin")
        started = time.time()
        result = self._run_shim("git", "push", "origin", "main")
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertGreaterEqual(time.time() - started, 1.0)
        self.assertEqual(self._fake_log_lines(), [])
        records = xs.approval_load_all()
        self.assertEqual(len(records), 1)
        record = next(iter(records.values()))
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["reason"], "timeout")
        self.assertEqual(record["pause_point"]["method"], "preexec")

    def test_arm_gate_off_installs_nothing(self):
        repo = self._make_repo()
        lane = {"name": "testlane", "repo": str(repo)}
        ws = xs.lane_prepare_workspace(lane, "20261121-020000-test")
        wt = ws["path"]
        gate = xs.arm_preexec_gate(wt, "run-1", str(self.rdir),
                                   self.event_file, time.time() + 3600, POL_OFF)
        self.assertIsNone(gate)
        self.assertFalse(os.path.exists(os.path.join(wt, ".openswarm-tmp", "bin")))


if __name__ == "__main__":
    unittest.main()
