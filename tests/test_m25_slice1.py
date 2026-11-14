"""M25 slice 1 (#202): batch-path health gate core + guardrails.

M24 (#198) put the pre-dispatch health gate into single `openswarm run`
only; batch cards still dispatched to an unhealthy backend and failed at
exec time. This slice reuses the same gate (apply_health_gate /
health_probe_backend) on each batch card's effective backend — after
weighted + quota routing, so an explicit --backend is gated exactly like a
routed pick. An unhealthy backend blocks just that card (ledger
blocked/health-gate with reason + evidence pointers, no exec attempt, never
rerouted); the other cards in the batch are unaffected, and a blocked card
blocks its M4 dependents like any other non-done dependency. The gate is
off by default (routing.health_gate.enabled=false); --health-gate /
--no-health-gate / --skip-health-gate flags ride on the batch spec so a
resumed or detached batch gates exactly like the batch that created it.

Pinned behaviour (the tests below pin each one):
- Gate off (shipped default) changes nothing: no health_gate record on any
  row or ledger entry, dispatch byte-identical to before.
- Unhealthy blocks: row finished/blocked with blocked_by health-gate, no
  run_id, batch_launch never called, ledger entry status blocked with the
  full gate record (reason + per-check evidence pointers).
- An explicit --backend is gated exactly like a routed pick.
- A blocked card does not block the other cards in the same batch.
- A probe that cannot finish in time is verdict "unknown" with the timeout
  on the record, and no new check starts after the deadline; on_unknown
  decides whether the card dispatches.
- --skip-health-gate bypasses the probe and the bypass itself is recorded.
- An invalid health_gate config fails loudly when the gate is on, and is
  ignored when the gate is off.

Run: python3 -m unittest tests.test_m25_slice1
     (or: python3 tests/test_m25_slice1.py)
"""
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402
import test_batch  # noqa: E402  (FakeFleetFixture for the batch end-to-end tests)

xs = _load.load()
xs.breakers = lambda: {}  # isolate from any breakers on disk
# NOTE: _load.load() builds a fresh module object per call, and
# FakeFleetFixture.run_batch executes test_batch.xs.cmd_batch — so every
# patch in the end-to-end tests below must target fxs (test_batch.xs),
# not this module's own xs. (Patching the wrong module object silently
# does nothing: the gate tests first failed exactly that way.)
fxs = test_batch.xs

POLICY = xs.jload(f"{xs.SKILL_DIR}/policy.json", {})
with open(_load.FIXTURE_STATE) as f:
    BASE = json.load(f)


def codex_state():
    """Fixture probe state with a usable codex binary and readable limits."""
    st = copy.deepcopy(BASE)
    st["codex"]["binary"] = sys.executable
    return st


def probe_record(verdict, reason, status="fail"):
    """Canned health_probe_backend record (apply_health_gate fills in
    enabled/source/stage/on_unknown/action around it)."""
    return {"backend": "codex", "verdict": verdict, "reason": reason,
            "checks": [{"name": "login", "status": status, "detail": reason,
                        "evidence": "command: codex login status"}],
            "seconds": 0.01, "timeout_seconds": 10}


UNHEALTHY = probe_record("unhealthy", "codex is not logged in (codex login status says 'Not logged in')")
HEALTHY = probe_record("healthy", "all 3 applicable check(s) passed", status="pass")
UNKNOWN = probe_record("unknown", "codex login status could not be classified", status="unknown")


class BatchHealthGateUnitTests(unittest.TestCase):
    def test_gate_off_returns_none_without_reading_state(self):
        with mock.patch.object(xs, "get_state", side_effect=AssertionError("state must not be read")):
            self.assertEqual(xs.batch_health_gate({}, POLICY, "codex"), (None, False))

    def test_no_health_gate_flag_wins_over_enabled_config(self):
        policy = copy.deepcopy(POLICY)
        policy["routing"]["health_gate"] = {"enabled": True, "timeout_seconds": 10, "on_unknown": "allow"}
        with mock.patch.object(xs, "get_state", side_effect=AssertionError("state must not be read")):
            self.assertEqual(xs.batch_health_gate({"no_health_gate": True}, policy, "codex"),
                             (None, False))

    def test_health_gate_flag_wins_over_no_health_gate_flag(self):
        # Same precedence as apply_health_gate (M24): --health-gate beats
        # --no-health-gate when both are passed.
        with mock.patch.object(xs, "get_state", return_value=codex_state()), \
                mock.patch.object(xs, "_health_codex_login",
                                  return_value=xs._health_check("login", "pass", "live session", "cmd")):
            record, blocked = xs.batch_health_gate(
                {"health_gate": True, "no_health_gate": True}, POLICY, "codex")
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "healthy")

    def test_no_backend_selected_is_not_gated(self):
        with mock.patch.object(xs, "get_state", side_effect=AssertionError("state must not be read")):
            self.assertEqual(xs.batch_health_gate({"health_gate": True}, POLICY, None), (None, False))

    def test_invalid_config_ignored_when_gate_off(self):
        policy = copy.deepcopy(POLICY)
        policy["routing"]["health_gate"] = "nonsense"
        self.assertEqual(xs.batch_health_gate({}, policy, "codex"), (None, False))

    def test_invalid_config_fails_loudly_when_gate_on(self):
        policy = copy.deepcopy(POLICY)
        policy["routing"]["health_gate"] = {"enabled": True, "timeout_seconds": 0, "on_unknown": "allow"}
        with mock.patch.object(xs, "get_state", return_value=codex_state()):
            with self.assertRaises(ValueError):
                xs.batch_health_gate({"health_gate": True}, policy, "codex")

    def test_config_enabled_probes_without_flag(self):
        policy = copy.deepcopy(POLICY)
        policy["routing"]["health_gate"] = {"enabled": True, "timeout_seconds": 10, "on_unknown": "allow"}
        with mock.patch.object(xs, "get_state", return_value=codex_state()), \
                mock.patch.object(xs, "_health_codex_login",
                                  return_value=xs._health_check("login", "pass", "live session", "cmd")):
            record, blocked = xs.batch_health_gate({}, policy, "codex")
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "healthy")
        self.assertEqual(record["action"], "allowed")
        self.assertEqual(record["stage"], "batch")

    def test_skip_flag_bypasses_probe_and_records_bypass(self):
        with mock.patch.object(xs, "health_probe_backend",
                               side_effect=AssertionError("probe must not run")):
            record, blocked = xs.batch_health_gate(
                {"health_gate": True, "skip_health_gate": True}, POLICY, "codex")
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "bypassed")
        self.assertEqual(record["action"], "bypassed")


class BatchHealthGateTests(test_batch.FakeFleetFixture, unittest.TestCase):
    def ledger_entries(self):
        path = self.data / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def launch_counter(self):
        orig = fxs.batch_launch
        patcher = mock.patch.object(fxs, "batch_launch", side_effect=orig)
        counter = patcher.start()
        self.addCleanup(patcher.stop)
        return counter

    def test_gate_off_dispatches_without_health_gate_record(self):
        (self.cards / "one.md").write_text("first card")
        (self.cards / "two.md").write_text("second card")
        rc, summary, _ = self.run_batch()
        self.assertEqual(rc, 0)
        self.assertEqual([row["status"] for row in summary["cards"]], ["done", "done"])
        for row in summary["cards"]:
            self.assertNotIn("health_gate", row)
        self.assertEqual([e for e in self.ledger_entries() if "health_gate" in e], [])

    def test_no_health_gate_flag_also_off(self):
        (self.cards / "one.md").write_text("first card")
        with mock.patch.object(fxs, "health_probe_backend",
                               side_effect=AssertionError("probe must not run")):
            rc, summary, _ = self.run_batch(no_health_gate=True)
        self.assertEqual(rc, 0)
        self.assertEqual(summary["cards"][0]["status"], "done")
        self.assertNotIn("health_gate", summary["cards"][0])

    def test_unhealthy_backend_blocks_card_without_exec(self):
        (self.cards / "one.md").write_text("first card")
        counter = self.launch_counter()
        with mock.patch.object(fxs, "health_probe_backend", return_value=copy.deepcopy(UNHEALTHY)):
            rc, summary, _ = self.run_batch(health_gate=True, backend="codex")
        self.assertEqual(rc, 1)
        row = summary["cards"][0]
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["blocked_by"], "health-gate")
        self.assertIn("not logged in", row["blocked_reason"])
        self.assertNotIn("run_id", row)
        self.assertEqual(row["health_gate"]["verdict"], "unhealthy")
        self.assertEqual(row["health_gate"]["action"], "blocked")
        self.assertEqual(counter.call_count, 0)
        blocked = [e for e in self.ledger_entries() if e.get("blocked_by") == "health-gate"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["status"], "blocked")
        self.assertIn("not logged in", blocked[0]["blocked_reason"])
        self.assertEqual(blocked[0]["health_gate"]["checks"][0]["evidence"],
                         "command: codex login status")

    def test_explicit_backend_is_not_exempt(self):
        # args backend="codex" is user-explicit (spec backend_explicit=True):
        # the gate must still refuse it, not treat it as a free pass.
        (self.cards / "one.md").write_text("first card")
        with mock.patch.object(fxs, "health_probe_backend", return_value=copy.deepcopy(UNHEALTHY)):
            rc, summary, _ = self.run_batch(health_gate=True, backend="codex")
        spec = json.loads((self.data / "batches" / summary["id"] / "spec.json").read_text())
        self.assertTrue(spec["backend_explicit"])
        self.assertEqual(rc, 1)
        self.assertEqual(summary["cards"][0]["status"], "blocked")
        self.assertNotIn("run_id", summary["cards"][0])

    def test_routed_pick_is_gated(self):
        # backend=None: the creation-time route picks the default backend
        # (mocked here); it is a routed pick, not an explicit one, and the
        # gate must still apply to it.
        (self.cards / "one.md").write_text("first card")
        with mock.patch.object(fxs, "get_state", return_value=codex_state()), \
                mock.patch.object(fxs, "route",
                                  return_value={"ok": True, "backend": "codex", "model": "cheap"}), \
                mock.patch.object(fxs, "health_probe_backend", return_value=copy.deepcopy(UNHEALTHY)):
            rc, summary, _ = self.run_batch(health_gate=True, backend=None)
        spec = json.loads((self.data / "batches" / summary["id"] / "spec.json").read_text())
        self.assertFalse(spec["backend_explicit"])
        self.assertEqual(spec["backend"], "codex")
        self.assertEqual(rc, 1)
        self.assertEqual(summary["cards"][0]["status"], "blocked")

    def test_one_blocked_card_does_not_block_others(self):
        (self.cards / "one.md").write_text("first card")
        (self.cards / "two.md").write_text("second card")
        counter = self.launch_counter()
        with mock.patch.object(fxs, "health_probe_backend",
                               side_effect=[copy.deepcopy(UNHEALTHY), copy.deepcopy(HEALTHY)]):
            rc, summary, _ = self.run_batch(health_gate=True, backend="codex")
        self.assertEqual(rc, 1)  # not all cards done
        self.assertEqual(summary["cards"][0]["status"], "blocked")
        self.assertNotIn("run_id", summary["cards"][0])
        self.assertEqual(summary["cards"][1]["status"], "done")
        self.assertIn("run_id", summary["cards"][1])
        self.assertEqual(counter.call_count, 1)

    def test_healthy_backend_dispatches_and_records_gate(self):
        (self.cards / "one.md").write_text("first card")
        (self.cards / "two.md").write_text("second card")
        with mock.patch.object(fxs, "health_probe_backend", return_value=copy.deepcopy(HEALTHY)):
            rc, summary, _ = self.run_batch(health_gate=True, backend="codex")
        self.assertEqual(rc, 0)
        for row in summary["cards"]:
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["health_gate"]["verdict"], "healthy")
            self.assertEqual(row["health_gate"]["action"], "allowed")

    def test_skip_flag_bypasses_probe_and_records_bypass(self):
        # --skip-health-gate only bypasses when the gate is on (same as
        # M24 run: with the gate off there is nothing to bypass and no
        # record); health_gate=True turns the gate on for this batch.
        (self.cards / "one.md").write_text("first card")
        with mock.patch.object(fxs, "health_probe_backend",
                               side_effect=AssertionError("probe must not run")):
            rc, summary, _ = self.run_batch(health_gate=True, skip_health_gate=True, backend="codex")
        self.assertEqual(rc, 0)
        row = summary["cards"][0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["health_gate"]["verdict"], "bypassed")
        self.assertEqual(row["health_gate"]["action"], "bypassed")

    def test_unknown_with_block_posture_blocks(self):
        (self.cards / "one.md").write_text("first card")
        cfg = {"enabled": True, "timeout_seconds": 10, "on_unknown": "block"}
        with mock.patch.object(fxs, "health_probe_backend", return_value=copy.deepcopy(UNKNOWN)), \
                mock.patch.object(fxs, "health_gate_config", return_value=cfg):
            rc, summary, _ = self.run_batch(health_gate=True, backend="codex")
        self.assertEqual(rc, 1)
        self.assertEqual(summary["cards"][0]["status"], "blocked")
        self.assertEqual(summary["cards"][0]["health_gate"]["verdict"], "unknown")
        self.assertEqual(summary["cards"][0]["health_gate"]["action"], "blocked")

    def test_unknown_with_allow_posture_dispatches(self):
        (self.cards / "one.md").write_text("first card")
        with mock.patch.object(fxs, "health_probe_backend", return_value=copy.deepcopy(UNKNOWN)):
            rc, summary, _ = self.run_batch(health_gate=True, backend="codex")
        self.assertEqual(rc, 0)
        row = summary["cards"][0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["health_gate"]["verdict"], "unknown")
        self.assertEqual(row["health_gate"]["action"], "allowed")

    def test_probe_timeout_bounds_and_unknown_verdict(self):
        """Real probe, slow login: a 1s budget must stop new checks after
        the deadline, record the timeout, and (on_unknown=block) refuse."""
        (self.cards / "one.md").write_text("first card")
        counter = self.launch_counter()

        def slow_login(binary, timeout):
            time.sleep(1.2)
            return xs._health_check("login", "pass", "live session", "command: codex login status")

        cfg = {"enabled": True, "timeout_seconds": 1, "on_unknown": "block"}
        with mock.patch.object(fxs, "get_state", return_value=codex_state()), \
                mock.patch.object(fxs, "_health_codex_login", side_effect=slow_login), \
                mock.patch.object(fxs, "health_gate_config", return_value=cfg):
            rc, summary, _ = self.run_batch(health_gate=True, backend="codex")
        self.assertEqual(rc, 1)
        row = summary["cards"][0]
        self.assertEqual(row["status"], "blocked")
        record = row["health_gate"]
        self.assertEqual(record["verdict"], "unknown")
        self.assertLess(record["seconds"], 3)
        self.assertEqual([c["name"] for c in record["checks"]], ["binary", "login", "timeout"])
        self.assertEqual(record["checks"][2]["status"], "timeout")
        self.assertIn("budget", record["checks"][2]["detail"])
        self.assertEqual(counter.call_count, 0)

    def test_blocked_card_blocks_its_dependents(self):
        (self.cards / "one.md").write_text("first card")
        (self.cards / "two.md").write_text("second card\ndepends_on: one.md")
        with mock.patch.object(fxs, "health_probe_backend", return_value=copy.deepcopy(UNHEALTHY)):
            rc, summary, _ = self.run_batch(health_gate=True, backend="codex")
        self.assertEqual(rc, 1)
        self.assertEqual(summary["cards"][0]["status"], "blocked")
        self.assertEqual(summary["cards"][1]["status"], "blocked")
        self.assertEqual(summary["cards"][1]["blocked_by"], "one.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
