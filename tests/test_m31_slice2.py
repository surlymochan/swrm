"""M31 slice 2 (#256): health-gate default flip — evaluation (verdict: no flip).

M24 shipped the gate off; slice 1 (#254) moved the probe to the executor
host; this slice gathered real cross-host evidence (gocc: deepseek blocked
+ codex allowed, docs/m31-slice2-real-evidence.md) and evaluated the flip
against the #256 criterion (default-on full suite green + evidence passes).

Verdict: DO NOT FLIP. Flipping policy.json to enabled=true breaks 64
tests (55 failures + 9 errors out of 1345): every dispatch-path test that
assumed the gate-off ambient default now hits the gate with this VM's
unhealthy codex state. The flip is one value change away, but it needs a
test-suite migration first — out of scope for this slice, and the criterion
says the conclusion goes in the issue instead. The shipped default stays
off (pinned by test_m24_slice1.ConfigValidation.test_shipped_policy_is_disabled).

Pinned behaviour (the tests below pin each one):
- The flip mechanism works: a policy.local.json-style deep-merge with
  enabled=true turns the gate on.
- --health-gate forces the gate on under the shipped default off (the
  probe runs; a healthy verdict allows, source is the flag).
- --no-health-gate forces the gate off even when it is enabled by policy
  (no probe runs).
- --skip-health-gate bypasses the probe when the gate is enabled by
  policy, and the bypass itself is recorded (verdict/action "bypassed").

Run: python3 -m unittest tests.test_m31_slice2
     (or: python3 tests/test_m31_slice2.py)
"""
import argparse
import copy
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
xs.breakers = lambda: {}  # isolate from any breakers on disk

POLICY = xs.jload(f"{xs.SKILL_DIR}/policy.json", {})


def gate_args(**overrides):
    args = {"health_gate": False, "no_health_gate": False, "skip_health_gate": False}
    args.update(overrides)
    return argparse.Namespace(**args)


def enabled_policy():
    """POLICY with the gate turned on the way policy.local.json would."""
    policy = copy.deepcopy(POLICY)
    policy["routing"]["health_gate"]["enabled"] = True
    return policy


def healthy_record():
    return {"backend": "codex", "verdict": "healthy",
            "reason": "all 3 applicable check(s) passed",
            "checks": [{"name": "login", "status": "pass", "detail": "live session",
                        "evidence": "command: codex login status"}],
            "seconds": 0.01, "timeout_seconds": 10}


class FlipEvaluationTests(unittest.TestCase):
    def test_policy_local_style_override_can_enable_gate(self):
        # The flip itself is one value: a deep-merge with enabled=true
        # (what load_policy does with policy.local.json) turns it on.
        self.assertFalse(xs.health_gate_config(POLICY)["enabled"])
        self.assertTrue(xs.health_gate_config(enabled_policy())["enabled"])

    def test_health_gate_flag_forces_on_under_shipped_default_off(self):
        with mock.patch.object(xs, "health_probe_backend",
                               return_value=healthy_record()):
            record, blocked = xs.apply_health_gate(
                "codex", {}, POLICY, gate_args(health_gate=True))
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "healthy")
        self.assertEqual(record["action"], "allowed")
        self.assertEqual(record["source"], "flag --health-gate")

    def test_no_health_gate_flag_forces_off_when_enabled_by_policy(self):
        with mock.patch.object(xs, "health_probe_backend",
                               side_effect=AssertionError("gate off must not probe")):
            record, blocked = xs.apply_health_gate(
                "codex", {}, enabled_policy(), gate_args(no_health_gate=True))
        self.assertIsNone(record)
        self.assertFalse(blocked)

    def test_skip_health_gate_bypass_recorded_when_enabled_by_policy(self):
        with mock.patch.object(xs, "health_probe_backend",
                               side_effect=AssertionError("bypass must not probe")):
            record, blocked = xs.apply_health_gate(
                "codex", {}, enabled_policy(), gate_args(skip_health_gate=True))
        self.assertFalse(blocked)
        self.assertEqual(record["verdict"], "bypassed")
        self.assertEqual(record["action"], "bypassed")
        self.assertEqual(record["source"], "policy routing.health_gate")
        self.assertEqual(record["reason"],
                         "health gate bypassed by explicit --skip-health-gate; no probe ran")


if __name__ == "__main__":
    unittest.main()
