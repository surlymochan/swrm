"""M33 slice 3 (#266): AUTH_ERROR_RE covers the real agy sign-in error.

Slice 2 (#265) proved an unhealthy escalation target is blocked, but the
verdict was unknown, not unhealthy: real agy 1.5.10 on gocc reports an
unlogged-in session as "Please sign in to view available models.", which
the old regex did not match, so the probe said unknown and only the
run's explicit on_unknown=block stopped the dispatch. Under the shipped
default (on_unknown=allow) the same backend would have been dispatched.

These tests pin the fix without mocking the gate itself: the fake agy
emits the exact 1.5.10 error text on `agy models`, and the real
agy_login_state / health_probe_backend / apply_health_gate path must
land on "not logged in" / unhealthy / blocked under the shipped
on_unknown=allow posture. No network and no real CLI.
"""
import argparse, copy, json, os, shutil, stat, sys, tempfile, textwrap, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
POLICY = xs.load_policy()

# Real agy 1.5.10 stderr on gocc when not logged in (slice 2 evidence,
# #265): `agy models` exits 1 with this text.
REAL_AGY_SIGNIN_ERROR = (
    "Error: Please sign in to view available models. "
    "Launch the CLI without arguments to sign in.\n"
)
REAL_AGY_SIGNIN_SHORT = "Error: Please sign in to view available models.\n"

FAKE_AGY = r"""#!/usr/bin/env python3
import os, sys
if sys.argv[1:2] == ["models"]:
    sys.stderr.write(os.environ.get("FAKE_AGY_MODELS_TEXT", ""))
    sys.exit(int(os.environ.get("FAKE_AGY_MODELS_RC", "0")))
sys.exit(0)
"""


def shipped_gate_policy():
    """The shipped routing.health_gate posture from policy.json:
    enabled, 10s timeout, on_unknown=allow (M32 slice 1, #259)."""
    policy = copy.deepcopy(POLICY)
    policy["routing"]["health_gate"] = {"enabled": True, "timeout_seconds": 10,
                                        "on_unknown": "allow"}
    return policy


def gate_args():
    return argparse.Namespace(health_gate=False, no_health_gate=False,
                              skip_health_gate=False)


class AuthErrorRegex(unittest.TestCase):
    def test_real_agy_signin_error_matches(self):
        for text in (REAL_AGY_SIGNIN_ERROR, REAL_AGY_SIGNIN_SHORT):
            self.assertIsNotNone(xs.AUTH_ERROR_RE.search(text), text)

    def test_signin_family_matches(self):
        for text in ("Please sign in to continue",
                     "Sign-in required",
                     "Sign in required",
                     "You are not signed in",
                     "You must sign in to continue",
                     "please sign-in to view available models"):
            self.assertIsNotNone(xs.AUTH_ERROR_RE.search(text), text)

    def test_existing_phrases_still_match(self):
        for text in ("authentication required",
                     "not authenticated",
                     "unauthenticated",
                     "not logged in",
                     "please log in",
                     "please login",
                     "login required",
                     "auth error",
                     "authentication error",
                     "FatalAuthenticationError: authentication required"):
            self.assertIsNotNone(xs.AUTH_ERROR_RE.search(text), text)

    def test_benign_text_does_not_match(self):
        # Success / unrelated-failure text must never be read as auth.
        for text in ("Signed in as nick",
                     "Login successful",
                     "authentication succeeded",
                     "error: model not found",
                     "429 rate limit exceeded",
                     "quota exceeded",
                     "connection reset by peer"):
            self.assertIsNone(xs.AUTH_ERROR_RE.search(text), text)


class AgySigninProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openswarm-m33s3-")
        self.script = os.path.join(self.tmp, "agy")
        with open(self.script, "w") as f:
            f.write(textwrap.dedent(FAKE_AGY))
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self._env = {k: os.environ.get(k) for k in
                     ("OPENSWARM_AGY_BIN", "FAKE_AGY_MODELS_TEXT", "FAKE_AGY_MODELS_RC")}
        os.environ["OPENSWARM_AGY_BIN"] = self.script
        os.environ["FAKE_AGY_MODELS_TEXT"] = REAL_AGY_SIGNIN_ERROR
        os.environ["FAKE_AGY_MODELS_RC"] = "1"
        xs._AGY_LOGIN_CACHE.clear()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        xs._AGY_LOGIN_CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_login_state_is_not_logged_in(self):
        self.assertEqual(xs.agy_login_state(), "not logged in")

    def test_probe_verdict_is_unhealthy(self):
        record = xs.health_probe_backend("agy", {}, POLICY)
        self.assertEqual(record["verdict"], "unhealthy")
        login = [c for c in record["checks"] if c["name"] == "login"]
        self.assertEqual(len(login), 1)
        self.assertEqual(login[0]["status"], "fail")

    def test_shipped_default_blocks_unhealthy(self):
        # Shipped default is on_unknown=allow: an unhealthy verdict must
        # still block — the posture only decides unknown, never unhealthy.
        record, blocked = xs.apply_health_gate("agy", {}, shipped_gate_policy(),
                                               gate_args(), stage="escalation")
        self.assertTrue(blocked)
        self.assertEqual(record["verdict"], "unhealthy")
        self.assertEqual(record["action"], "blocked")
        self.assertEqual(record["stage"], "escalation")
        self.assertEqual(record["on_unknown"], "allow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
