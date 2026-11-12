import argparse
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import _load

xs = _load.load()


class FleetWatchStreamTests(unittest.TestCase):
    """`fleet exec <host> -- watch` must stream lines as they arrive and not be
    killed by the buffered 300s timeout that discarded all output."""

    def run_stream(self, script, timeout=None):
        out, err = io.BytesIO(), io.BytesIO()
        class Std:
            pass
        so, se = Std(), Std()
        so.buffer, se.buffer = out, err
        with mock.patch.object(xs.sys, "stdout", so), mock.patch.object(xs.sys, "stderr", se):
            rc = xs.host_stream({"kind": "local"}, script, timeout=timeout)
        return rc, out.getvalue(), err.getvalue()

    def test_stream_relays_output_and_exit_code(self):
        rc, out, err = self.run_stream("echo DONE one; echo oops >&2; exit 3")
        self.assertEqual((rc, out, err), (3, b"DONE one\n", b"oops\n"))

    def test_stream_keeps_partial_output_on_timeout(self):
        rc, out, err = self.run_stream("echo HEARTBEAT; exec sleep 30", timeout=1)
        self.assertEqual(rc, 124)
        self.assertEqual(out, b"HEARTBEAT\n")  # host_run() would have returned b""
        self.assertIn(b"timed out", err)

    def test_stream_does_not_inherit_stdin(self):
        rc, out, _ = self.run_stream("cat; echo end")
        self.assertEqual((rc, out), (0, b"end\n"))

    def test_fleet_exec_watch_streams_and_has_no_default_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "fleet.json").write_text(json.dumps({"hosts": [{"name": "local", "kind": "local"}]}))
            seen = {}
            def fake(host, command, timeout=None):
                seen["timeout"] = timeout
                return 0
            args = argparse.Namespace(fleet_cmd="exec", host="local", args=["--", "watch", "r1", "--quiet"], timeout=None)
            with mock.patch.object(xs, "DATA", str(root)), mock.patch.object(xs, "host_stream", side_effect=fake), \
                    mock.patch.object(xs, "host_run", side_effect=AssertionError("buffered path used for watch")):
                with self.assertRaises(SystemExit) as cm:
                    xs.cmd_fleet(args)
            self.assertEqual(cm.exception.code, 0)
            self.assertIsNone(seen["timeout"])

    def test_fleet_exec_other_commands_keep_300s_default(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "fleet.json").write_text(json.dumps({"hosts": [{"name": "local", "kind": "local"}]}))
            seen = {}
            def fake(host, command, timeout=60, input=None):
                seen["timeout"] = timeout
                return 0, b"", b""
            args = argparse.Namespace(fleet_cmd="exec", host="local", args=["--", "runs"], timeout=None)
            with mock.patch.object(xs, "DATA", str(root)), mock.patch.object(xs, "host_run", side_effect=fake):
                with self.assertRaises(SystemExit):
                    xs.cmd_fleet(args)
            self.assertEqual(seen["timeout"], 300)


if __name__ == "__main__":
    unittest.main()
