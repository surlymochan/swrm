"""Batch-path --verify-evalid (M13 slice 1, #151).

Per-card `verify-evalid: true` declarations and the batch-level
--verify-evalid flag forward evalid verification to each card's remote
run; the remote run's claim-vs-evidence record is copied into the batch
row and the controller ledger. No network and no real evalid checkout
needed: batch_launch is exercised with mocked transports, and the
ledger record is exercised against a temporary ledger file. The
end-to-end batch tests live in test_batch.py (FakeFleetFixture).
"""
import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()


def spec(**fields):
    values = {"hosts": {"b1": {"name": "b1", "kind": "instabox", "box": "b1-box"}},
              "repos": {"b1": "/tmp/repo"}, "local": {"name": "local", "kind": "local"},
              "repo_path": "/tmp/repo", "scope": "repo", "base": "base0",
              "paths": [], "card_type": None, "verify": "true"}
    values.update(fields)
    return values


class ParseCardVerifyEvalid(unittest.TestCase):
    def test_true_values(self):
        for text in ("verify-evalid: true\ntask", "verify-evalid: YES\ntask",
                     "verify-evalid: 1\ntask", "task\nverify-evalid: True\n"):
            self.assertTrue(xs.parse_card_verify_evalid(text), text)

    def test_false_values_and_missing_line(self):
        for text in ("verify-evalid: false\ntask", "verify-evalid: no\ntask",
                     "verify-evalid: 0\ntask", "verify-evalid:\ntask", "task"):
            self.assertFalse(xs.parse_card_verify_evalid(text), text)

    def test_invalid_value_is_a_loud_error(self):
        with self.assertRaises(ValueError) as ctx:
            xs.parse_card_verify_evalid("verify-evalid: maybe\ntask")
        self.assertIn("maybe", str(ctx.exception))
        self.assertIn("verify-evalid: true", str(ctx.exception))


class StripCardPlannerLines(unittest.TestCase):
    def test_verify_evalid_line_is_stripped(self):
        self.assertEqual(xs.strip_card_verify_evalid("verify-evalid: true\nDo the thing\n"),
                         "Do the thing\n")
        self.assertEqual(xs.strip_card_verify_evalid("Do the thing\n"), "Do the thing\n")
        self.assertEqual(xs.strip_card_verify_evalid(b"verify-evalid: true\nDo the thing\n"),
                         b"Do the thing\n")

    def test_planner_lines_strips_both_declarations(self):
        text = "depends_on: a.md\nverify-evalid: true\nDo the thing\n"
        self.assertEqual(xs.strip_card_planner_lines(text), "Do the thing\n")
        self.assertEqual(xs.strip_card_planner_lines(b"depends_on: a.md\nverify-evalid: true\nDo it\n"),
                         b"Do it\n")

    def test_depends_on_strip_is_unchanged(self):
        self.assertEqual(xs.strip_card_dependencies("depends_on: a.md\nDo the thing\n"),
                         "Do the thing\n")


class BatchVerifyEvalidRequested(unittest.TestCase):
    def test_batch_flag_or_card_declaration_each_request_it(self):
        self.assertTrue(xs.batch_verify_evalid_requested(spec(verify_evalid=True), {"card": "a.md"}))
        self.assertTrue(xs.batch_verify_evalid_requested(spec(), {"card": "a.md", "verify_evalid": True}))
        self.assertFalse(xs.batch_verify_evalid_requested(spec(), {"card": "a.md"}))
        self.assertFalse(xs.batch_verify_evalid_requested(spec(), {"card": "a.md", "verify_evalid": False}))


class BatchLaunchVerifyEvalid(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m13-launch-")
        self.card = Path(self.tmp.name) / "one.md"
        self.addCleanup(self.tmp.cleanup)

    def launch(self, card_text, sp, row_extra=None):
        self.card.write_text(card_text)
        row = {"host": "b1", "path": str(self.card), "run_id": "run1"}
        row.update(row_extra or {})
        with mock.patch.object(xs, "host_python", side_effect=[b"remote-card.md", b""]) as host_python, \
                mock.patch.object(xs, "host_require", side_effect=lambda host, output, context: output), \
                mock.patch.object(xs, "host_json", return_value={"id": "run1"}) as host_json:
            result = xs.batch_launch(sp, row)
            card_input = host_python.call_args_list[0].kwargs["input"]
        return result, host_json.call_args.args[1], card_input

    def test_card_declaration_forwards_flag_and_strips_line(self):
        result, remote_args, card_bytes = self.launch(
            "verify-evalid: true\nDo the thing", spec(), {"verify_evalid": True})
        self.assertEqual(result, "run1")
        self.assertIn("--verify-evalid", remote_args)
        self.assertNotIn(b"verify-evalid:", card_bytes)
        self.assertIn(b"Do the thing", card_bytes)

    def test_batch_flag_forwards_flag_and_evalid_bin(self):
        result, remote_args, _ = self.launch(
            "Do the thing", spec(verify_evalid=True, evalid_bin="/tmp/fake-xflow"),
            {"verify_evalid": True})
        self.assertEqual(result, "run1")
        self.assertIn("--verify-evalid", remote_args)
        self.assertEqual(remote_args[remote_args.index("--evalid-bin") + 1], "/tmp/fake-xflow")

    def test_unrequested_card_gets_no_flag(self):
        result, remote_args, card_bytes = self.launch("Do the thing", spec(), {})
        self.assertEqual(result, "run1")
        self.assertNotIn("--verify-evalid", remote_args)
        self.assertNotIn("--evalid-bin", remote_args)

    def test_requested_without_verify_is_a_loud_error(self):
        self.card.write_text("verify-evalid: true\nDo the thing")
        row = {"card": "one.md", "host": "b1", "path": str(self.card), "run_id": "run1", "verify_evalid": True}
        with self.assertRaises(ValueError) as ctx:
            xs.batch_launch(spec(verify=None), row)
        self.assertIn("--verify-evalid requires --verify", str(ctx.exception))
        self.assertIn("one.md", str(ctx.exception))

    def test_batch_flag_without_verify_is_a_loud_error(self):
        self.card.write_text("Do the thing")
        row = {"host": "b1", "path": str(self.card), "run_id": "run1"}
        with self.assertRaises(ValueError) as ctx:
            xs.batch_launch(spec(verify=None, verify_evalid=True), row)
        self.assertIn("--verify-evalid requires --verify", str(ctx.exception))


class BatchCollectVerifyEvalid(unittest.TestCase):
    def test_collect_carries_the_remote_record(self):
        # No branch/commit: a failed run with no changes still carries its
        # verify_evalid record, and collection must not need a transfer.
        result = {"status": "done", "verify": {"rc": 0}, "branch": None, "commit": None,
                  "seconds": 1.5, "result": {"summary": "ok"},
                  "verify_evalid": {"mode": "verify-evalid", "verdict": "pass",
                                    "claim": "Ran `true`", "replay": {"verdict": "verified"}}}
        fields = xs.batch_collect(spec(), {"card": "a.md", "host": "b1"}, result)
        self.assertEqual(fields["status"], "done")
        self.assertEqual(fields["verify_evalid_verdict"], "pass")
        self.assertEqual(fields["verify_evalid"]["replay"]["verdict"], "verified")

    def test_collect_without_a_record_adds_no_fields(self):
        result = {"status": "done", "verify": {"rc": 0}, "branch": None, "commit": None,
                  "seconds": 1.5, "result": {"summary": "ok"}}
        fields = xs.batch_collect(spec(), {"card": "a.md", "host": "b1"}, result)
        self.assertNotIn("verify_evalid", fields)
        self.assertNotIn("verify_evalid_verdict", fields)


class BatchRecordVerifyEvalid(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-m13-ledger-")
        self.data = Path(self.tmp.name)
        self.patches = [mock.patch.object(xs, "DATA", str(self.data)),
                        mock.patch.object(xs, "LEDGER", str(self.data / "ledger.jsonl"))]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self.tmp.cleanup)
        for patch in self.patches:
            self.addCleanup(patch.stop)

    def entries(self):
        path = self.data / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def record(self, result, row=None):
        summary = {"id": "batch1", "scope": "repo"}
        row = row or {"card": "a.md", "run_id": "run1", "backend": "pi", "seconds": 1.5}
        xs.batch_record_verify_evalid(summary, row, result, "fix")
        return self.entries()

    def test_pass_record_carries_claim_and_replay(self):
        ve = {"mode": "verify-evalid", "verdict": "pass", "claim": "Ran `true` — all pass",
              "claims_text": "Ran `true` — all pass",
              "replay": {"verdict": "verified", "claims": [{"command": "true", "match": True}]},
              "error": None}
        entries = self.record({"verify_evalid": ve})
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertTrue(entry["batch_verify_evalid"])
        self.assertEqual(entry["verify_evalid_verdict"], "pass")
        self.assertEqual(entry["verify_evalid"], ve)
        self.assertEqual(entry["verify_evalid_replay_verdict"], "verified")
        self.assertEqual(entry["run_id"], "run1")
        self.assertEqual(entry["status"], "dispatched")
        self.assertEqual(entry["card_type"], "fix")

    def test_mismatch_record_carries_the_verdict(self):
        ve = {"mode": "verify-evalid", "verdict": "mismatch", "claim": "Ran `true`",
              "replay": {"verdict": "mismatch"}, "error": "evalid audit verdict mismatch"}
        entries = self.record({"verify_evalid": ve})
        self.assertEqual(entries[0]["verify_evalid_verdict"], "mismatch")
        self.assertEqual(entries[0]["verify_evalid_replay_verdict"], "mismatch")
        self.assertIn("mismatch", entries[0]["error"])

    def test_missing_record_is_an_explicit_error_not_a_pass(self):
        entries = self.record({})
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["verify_evalid_verdict"], "error")
        self.assertIn("no verify_evalid record", entry["error"])
        self.assertEqual(entry["verify_evalid"]["verdict"], "error")

    def test_record_does_not_inflate_routing_weights(self):
        ve = {"mode": "verify-evalid", "verdict": "pass", "claim": "Ran `true`",
              "replay": {"verdict": "verified"}, "error": None}
        entries = self.record({"verify_evalid": ve})
        run = {"backend": "pi", "model": None, "card_type": "fix", "status": "done",
               "exec_pass": True}
        weights = xs.compute_routing_weights(entries + [run], 8)["entries"]
        self.assertEqual([(row["backend"], row["runs"]) for row in weights], [("pi", 1)])

    def test_ledger_write_failure_does_not_fail_the_batch(self):
        with mock.patch.object(xs, "append_ledger", side_effect=OSError("disk full")):
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                xs.batch_record_verify_evalid(
                    {"id": "batch1", "scope": "repo"},
                    {"card": "a.md", "run_id": "run1"}, {"verify_evalid": {"verdict": "pass"}}, "fix")


if __name__ == "__main__":
    unittest.main()
