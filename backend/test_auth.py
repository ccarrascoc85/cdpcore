"""Unit tests for backend.auth.

Run:  python -m unittest test_auth

Each test isolates state via a tmpdir CDPCORE_CONFIG_DIR so real installs
are not touched.
"""
import importlib
import os
import tempfile
import time
import unittest


def _fresh_auth(config_dir: str, mode: str | None = None):
    """Import auth.py with a fresh CONFIG_DIR (and optional mode)."""
    os.environ["CDPCORE_CONFIG_DIR"] = config_dir
    if mode is None:
        os.environ.pop("CDPCORE_ADMIN_AUTH", None)
    else:
        os.environ["CDPCORE_ADMIN_AUTH"] = mode
    import auth  # local module
    return importlib.reload(auth)


class PinRoundTripTests(unittest.TestCase):
    def test_initialize_creates_pin_once(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            first = auth.initialize_pin_if_missing()
            self.assertIsNotNone(first)
            self.assertEqual(len(first), auth.PIN_DIGITS)
            self.assertTrue(first.isdigit())

            # Second call is a no-op
            second = auth.initialize_pin_if_missing()
            self.assertIsNone(second)

    def test_verify_pin_accepts_correct_and_rejects_wrong(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            pin = auth.initialize_pin_if_missing()
            self.assertTrue(auth.verify_pin(pin))
            self.assertFalse(auth.verify_pin("000000"))
            self.assertFalse(auth.verify_pin(""))
            self.assertFalse(auth.verify_pin(None))  # type: ignore[arg-type]

    def test_rotate_changes_pin(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            old = auth.initialize_pin_if_missing()
            new = auth.rotate_pin()
            self.assertNotEqual(old, new)  # astronomically unlikely collision
            self.assertFalse(auth.verify_pin(old))
            self.assertTrue(auth.verify_pin(new))

    def test_set_pin_accepts_valid(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            auth.initialize_pin_if_missing()
            auth.set_pin("246802")
            self.assertTrue(auth.verify_pin("246802"))
            self.assertFalse(auth.verify_pin("000000"))

    def test_set_pin_rejects_bad_shape(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            for bad in ("", "12345", "1234567", "12345a", "abcdef", None, 123456):
                with self.assertRaises(ValueError):
                    auth.set_pin(bad)  # type: ignore[arg-type]

    def test_verify_without_any_pin_configured_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            self.assertFalse(auth.verify_pin("123456"))


class TokenTests(unittest.TestCase):
    def test_issued_token_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            token, exp = auth.issue_token(ttl=60)
            result = auth.verify_token(token)
            self.assertIsNotNone(result)
            self.assertEqual(result, exp)

    def test_expired_token_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            token, _ = auth.issue_token(ttl=-1)  # already expired
            self.assertIsNone(auth.verify_token(token))

    def test_tampered_signature_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            token, _ = auth.issue_token(ttl=60)
            exp_str, sig = token.split(".", 1)
            # Flip one hex nibble
            tampered_sig = ("1" if sig[0] == "0" else "0") + sig[1:]
            self.assertIsNone(auth.verify_token(f"{exp_str}.{tampered_sig}"))

    def test_tampered_expiry_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            token, _ = auth.issue_token(ttl=60)
            _, sig = token.split(".", 1)
            future = int(time.time()) + 10_000
            self.assertIsNone(auth.verify_token(f"{future}.{sig}"))

    def test_malformed_tokens_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            for bad in ("", None, "no-dot", "abc.def", ".abc", "123.", "...", "x.y.z"):
                self.assertIsNone(auth.verify_token(bad))  # type: ignore[arg-type]


class ModeTests(unittest.TestCase):
    def test_default_mode_is_pin(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            self.assertEqual(auth.get_mode(), auth.MODE_PIN)

    def test_mode_off_is_honored(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d, mode="off")
            self.assertEqual(auth.get_mode(), auth.MODE_OFF)

    def test_unknown_mode_falls_back_to_pin(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d, mode="bogus-mode")
            self.assertEqual(auth.get_mode(), auth.MODE_PIN)

    def test_mode_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d, mode="OFF")
            self.assertEqual(auth.get_mode(), auth.MODE_OFF)

    def test_set_mode_persists_and_overrides_env(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d, mode="pin")
            auth.set_mode(auth.MODE_OFF)
            # Reload with env still set to pin — the file override should win.
            auth = _fresh_auth(d, mode="pin")
            self.assertEqual(auth.get_mode(), auth.MODE_OFF)

    def test_set_mode_can_restore_pin(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d, mode="pin")
            auth.set_mode(auth.MODE_OFF)
            auth.set_mode(auth.MODE_PIN)
            self.assertEqual(auth.get_mode(), auth.MODE_PIN)

    def test_set_mode_rejects_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            for bad in ("", "bogus", "PIN-required", None, 123):
                with self.assertRaises(ValueError):
                    auth.set_mode(bad)  # type: ignore[arg-type]

    def test_corrupt_auth_conf_falls_back_to_env(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d, mode="pin")
            (auth._mode_file()).parent.mkdir(parents=True, exist_ok=True)
            auth._mode_file().write_text("not-json{{{")
            self.assertEqual(auth.get_mode(), auth.MODE_PIN)


class SetupRequiredTests(unittest.TestCase):
    def test_fresh_install_requires_setup(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            self.assertTrue(auth.setup_required())
            self.assertFalse(auth.is_pin_configured())

    def test_setting_pin_clears_setup_required(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            auth.set_pin("123456")
            self.assertTrue(auth.is_pin_configured())
            self.assertFalse(auth.setup_required())

    def test_setting_mode_clears_setup_required(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d)
            auth.set_mode(auth.MODE_OFF)
            self.assertFalse(auth.setup_required())
            # LAN-Trust path never touches the PIN file.
            self.assertFalse(auth.is_pin_configured())

    def test_env_var_alone_does_not_satisfy_setup(self):
        """Env var is only a fallback for get_mode(); a fresh install must
        still prompt in the UI until an explicit choice is persisted."""
        with tempfile.TemporaryDirectory() as d:
            auth = _fresh_auth(d, mode="off")
            self.assertTrue(auth.setup_required())


if __name__ == "__main__":
    unittest.main()
