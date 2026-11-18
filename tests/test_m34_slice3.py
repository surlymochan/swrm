"""M34 slice 3 (#272): non-ASCII prompt dispatch under a POSIX C locale.

Regression: under LC_ALL=C with PYTHONCOERCECLOCALE=0 and PYTHONUTF8=0,
CPython reports sys.getfilesystemencoding() == "ascii", and subprocess.Popen
fails to encode a non-ASCII argv element (a Chinese task title inside the
executor prompt) with UnicodeEncodeError, so `openswarm run` died with
exit 2 before dispatch. The fix re-execs the process once in UTF-8 mode
(PYTHONUTF8=1) when the filesystem encoding is ascii; see
`_ensure_utf8_filesystem_encoding` in bin/openswarm.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _load

xs = _load.load()

BIN = os.path.join(_load.ROOT, "bin", "openswarm")
TITLE = "中文任务：验证账本编码往返一致"
FRAGMENT = "café — ünïcödé ✓"

ECHO_CLI = (
    "#!/usr/bin/env python3\n"
    "import argparse, json, os\n"
    "p = argparse.ArgumentParser()\n"
    "p.add_argument('--prompt', default='')\n"
    "p.add_argument('--workdir', default='.')\n"
    "p.add_argument('--model', default='')\n"
    "a = p.parse_args()\n"
    "with open(os.path.join(a.workdir, 'echo-output.txt'), 'w', encoding='utf-8') as fh:\n"
    "    fh.write('echo: %s\\nmodel: %s\\nfragment: %s\\n' % (a.prompt, a.model, "
    + repr(FRAGMENT) + "))\n"
    "print(json.dumps({'status': 'done', 'summary': 'echoed: ' + "
    + repr(FRAGMENT) + ",\n"
    "                  'files_changed': ['echo-output.txt'], 'tests': 'no tests claimed'},\n"
    "                 ensure_ascii=True))\n"
)


class Utf8ReexecTests(unittest.TestCase):
    def test_reexecs_once_in_utf8_mode_when_fsencoding_is_ascii(self):
        with mock.patch.object(sys, "getfilesystemencoding", return_value="ascii"), \
             mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("os.execve", create=True) as execve:
            os.environ.pop(xs._UTF8_REEXEC_MARKER, None)
            os.environ.pop("PYTHONUTF8", None)
            xs._ensure_utf8_filesystem_encoding()
        self.assertEqual(execve.call_count, 1)
        argv = execve.call_args.args[1]
        env = execve.call_args.args[2]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], os.path.realpath(BIN))
        self.assertEqual(argv[2:], sys.argv[1:])
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env[xs._UTF8_REEXEC_MARKER], "1")

    def test_no_reexec_when_marker_already_set(self):
        with mock.patch.object(sys, "getfilesystemencoding", return_value="ascii"), \
             mock.patch.dict(os.environ, {xs._UTF8_REEXEC_MARKER: "1"}), \
             mock.patch("os.execve", create=True) as execve:
            xs._ensure_utf8_filesystem_encoding()
        execve.assert_not_called()

    def test_no_reexec_when_fsencoding_is_utf8(self):
        with mock.patch.object(sys, "getfilesystemencoding", return_value="utf-8"), \
             mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("os.execve", create=True) as execve:
            os.environ.pop(xs._UTF8_REEXEC_MARKER, None)
            xs._ensure_utf8_filesystem_encoding()
        execve.assert_not_called()

    def test_failed_exec_falls_back_to_current_process(self):
        with mock.patch.object(sys, "getfilesystemencoding", return_value="ascii"), \
             mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("os.execve", side_effect=OSError("no exec"), create=True):
            os.environ.pop(xs._UTF8_REEXEC_MARKER, None)
            xs._ensure_utf8_filesystem_encoding()  # must not raise


class ChineseTitleRunTests(unittest.TestCase):
    def test_chinese_title_run_dispatches_under_c_locale(self):
        with tempfile.TemporaryDirectory(prefix="openswarm-m34s3-") as tmp:
            root = Path(tmp)
            home = root / "home"
            echo_cli = root / "fake-echo-cli.py"
            echo_cli.write_text(ECHO_CLI, encoding="utf-8")
            # Verify via a script file invoked with the current interpreter:
            # the backend command must not assume a `python3` binary on PATH
            # (absent on the Windows runners) and the verify shell is cmd.exe
            # on Windows, where `test -f ...` does not exist.
            verify_py = root / "verify.py"
            verify_py.write_text(
                "import os, sys\n"
                "sys.exit(0 if os.path.exists(os.path.join(os.getcwd(), "
                "'echo-output.txt')) else 1)\n",
                encoding="utf-8")
            backends = {"backends": [{"id": "echo", "name": "Echo CLI (test)",
                                       "model": "echo-1", "quota_source": "none",
                                       "command": [sys.executable, str(echo_cli), "--prompt",
                                                   "{prompt}", "--workdir", "{worktree}",
                                                   "--model", "{model}"],
                                       "env": {"ECHO_MODEL": "echo-1"}}]}
            backends_file = root / "backends.json"
            backends_file.write_text(json.dumps(backends), encoding="utf-8")
            task = root / "task-a.md"
            task.write_text(TITLE + "\nWrite a one-line summary of this task card "
                            "into echo-output.txt. No code changes.\n", encoding="utf-8")
            target = root / "target-repo"
            target.mkdir()
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            subprocess.run(["git", "-C", str(target), "config", "user.email",
                            "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(target), "config", "user.name",
                            "Test"], check=True)
            (target / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "."],
                           check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-qm", "base"],
                           check=True)
            env = dict(os.environ)
            env.update({"LC_ALL": "C", "LANG": "C",
                        "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0",
                        "OPENSWARM_HOME": str(home)})
            env.pop("PYTHONIOENCODING", None)
            proc = subprocess.run(
                [sys.executable, BIN, "run", "--backend", "echo",
                 "--backends-file", str(backends_file),
                 "--cwd", str(target), "--task-file", str(task),
                 "--verify", '"%s" "%s"' % (sys.executable, verify_py),
                 "--budget-minutes", "5", "--no-fallback", "--no-auto-review"],
                capture_output=True, text=True, encoding="utf-8", env=env,
                timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            runs = sorted((home / "runs").iterdir())
            self.assertEqual(len(runs), 1)
            result = json.loads((runs[0] / "result.json").read_bytes()
                                .decode("utf-8"))
            self.assertEqual(result["title"], TITLE)
            self.assertEqual(result["status"], "done")
            ledger = (home / "ledger.jsonl").read_bytes().decode("utf-8")
            lines = [json.loads(line) for line in ledger.splitlines()
                     if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["title"], TITLE)
            self.assertEqual(lines[0]["id"], result["id"])
            self.assertEqual(lines[0].get("summary"),
                             result["result"]["summary"])
            self.assertEqual(result["result"]["summary"],
                             "echoed: " + FRAGMENT)


if __name__ == "__main__":
    unittest.main()
