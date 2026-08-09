"""Tests for the public page builder.

The interesting surface here is small but consequential: check_publishable_key
is what stands between a mistyped GitHub variable and a service_role key
published on the open web inside index.html.
"""
import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))

import render_public_html as R  # noqa: E402


def fake_jwt(claims: dict) -> str:
    """A JWT-shaped string. Unsigned: the code reads claims, it does not verify."""
    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}.notarealsignature"


class CheckPublishableKey(unittest.TestCase):
    def test_accepts_new_style_publishable_key(self):
        self.assertIsNone(R.check_publishable_key("sb_publishable_h5e8DRCXaaaaaaaaaaaa"))

    def test_accepts_legacy_anon_jwt(self):
        self.assertIsNone(R.check_publishable_key(fake_jwt({"role": "anon", "iss": "supabase"})))

    def test_rejects_new_style_secret_key(self):
        problem = R.check_publishable_key("sb_secret_thisWouldBypassEveryPolicy")
        self.assertIsNotNone(problem)
        self.assertIn("SECRET", problem)

    def test_rejects_service_role_jwt(self):
        problem = R.check_publishable_key(fake_jwt({"role": "service_role"}))
        self.assertIsNotNone(problem)
        self.assertIn("service_role", problem)

    def test_rejects_unknown_format(self):
        problem = R.check_publishable_key("hunter2")
        self.assertIsNotNone(problem)
        self.assertIn("unrecognised", problem)

    def test_rejects_jwt_shaped_but_undecodable(self):
        self.assertIsNotNone(R.check_publishable_key("aaa.!!!!not-base64!!!!.ccc"))

    def test_rejects_jwt_without_a_role_claim(self):
        self.assertIsNotNone(R.check_publishable_key(fake_jwt({"iss": "supabase"})))

    def test_a_length_check_alone_would_not_have_caught_these(self):
        # The failure this guard exists for: the old gate only required 20+
        # characters, and every rejected key here clears that easily.
        for key in ("sb_secret_thisWouldBypassEveryPolicy",
                    fake_jwt({"role": "service_role"})):
            self.assertGreater(len(key), 20)
            self.assertIsNotNone(R.check_publishable_key(key))

    def test_never_echoes_the_key_back(self):
        # An error message gets printed into CI logs, which for a public repo
        # are world-readable. A rejected secret must not be leaked by the very
        # thing that rejected it.
        secret = "sb_secret_DoNotEchoThisAnywhere"
        problem = R.check_publishable_key(secret)
        self.assertNotIn(secret, problem)
        self.assertNotIn("DoNotEchoThisAnywhere", problem)

        jwt = fake_jwt({"role": "service_role", "secret_bit": "AlsoDoNotEchoMe"})
        problem = R.check_publishable_key(jwt)
        self.assertNotIn(jwt, problem)
        self.assertNotIn("AlsoDoNotEchoMe", problem)


class ExtractPanelJs(unittest.TestCase):
    def test_pulls_every_panel_from_the_dashboard(self):
        js = R.extract_panel_js()
        self.assertEqual(sorted(js), ["1", "2", "3", "4"])
        for body in js.values():
            self.assertIn("context", body)


if __name__ == "__main__":
    unittest.main()
