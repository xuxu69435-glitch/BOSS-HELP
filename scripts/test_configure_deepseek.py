"""Offline tests using dummy keys and isolated temporary directories."""

import contextlib
import getpass
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import configure_deepseek as setup


class SetupTests(unittest.TestCase):
    def test_saves_encrypted_key_and_expected_provider(self):
        with tempfile.TemporaryDirectory(prefix="deepseek-setup-") as directory:
            target = Path(directory)
            setup.save_settings(target, "dummy-key-for-offline-test")
            config = json.loads((target / "ai" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["ai_provider"], "deepseek")
            self.assertEqual(config["ai_model"], "deepseek-flash")
            self.assertEqual(config["ai_base_url"], "https://api.deepseek.com")
            self.assertNotIn("dummy-key", (target / "ai" / "api_key.enc").read_text())
            self.assertEqual(setup.AIConfigStore(target).get_api_key(), "dummy-key-for-offline-test")

    def test_rejects_whitespace_without_creating_config(self):
        with tempfile.TemporaryDirectory(prefix="deepseek-setup-") as directory:
            target = Path(directory)
            with self.assertRaises(ValueError):
                setup.save_settings(target, "invalid key")
            self.assertFalse((target / "ai").exists())

    def test_redirected_input_is_blocked(self):
        with patch.object(setup.sys.stdin, "isatty", return_value=False), patch.object(setup.getpass, "getpass") as prompt, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(), 1)
            prompt.assert_not_called()

    def test_blank_input_cancels_without_writing(self):
        with tempfile.TemporaryDirectory(prefix="deepseek-setup-") as directory, patch.object(setup.sys.stdin, "isatty", return_value=True), patch.object(setup.getpass, "getpass", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            target = Path(directory)
            self.assertEqual(setup.main(target), 0)
            self.assertFalse((target / "ai").exists())

    def test_existing_key_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory(prefix="deepseek-setup-") as directory, patch.object(setup.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="no"), patch.object(setup.getpass, "getpass") as prompt, contextlib.redirect_stdout(io.StringIO()):
            target = Path(directory)
            setup.save_settings(target, "old-dummy-key")
            before = (target / "ai" / "api_key.enc").read_bytes()
            self.assertEqual(setup.main(target), 0)
            prompt.assert_not_called()
            self.assertEqual(before, (target / "ai" / "api_key.enc").read_bytes())

    def test_confirmation_accepts_lowercase_uppercase_and_mixed_case(self):
        for answer in ("yes", "YES", "Yes", " yEs "):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory(prefix="deepseek-setup-") as directory, patch.object(setup.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value=answer), patch.object(setup.getpass, "getpass", return_value="replacement-dummy-key") as prompt, contextlib.redirect_stdout(io.StringIO()):
                target = Path(directory)
                setup.save_settings(target, "old-dummy-key")
                self.assertEqual(setup.main(target), 0)
                prompt.assert_called_once()
                self.assertEqual(setup.AIConfigStore(target).get_api_key(), "replacement-dummy-key")

    def test_invalid_confirmation_reprompts_without_exiting(self):
        with tempfile.TemporaryDirectory(prefix="deepseek-setup-") as directory, patch.object(setup.sys.stdin, "isatty", return_value=True), patch("builtins.input", side_effect=["yess", "yes"]) as confirmation, patch.object(setup.getpass, "getpass", return_value="replacement-dummy-key") as prompt, contextlib.redirect_stdout(io.StringIO()):
            target = Path(directory)
            setup.save_settings(target, "old-dummy-key")
            self.assertEqual(setup.main(target), 0)
            self.assertEqual(confirmation.call_count, 2)
            prompt.assert_called_once()
            self.assertEqual(setup.AIConfigStore(target).get_api_key(), "replacement-dummy-key")

    def test_cancel_is_case_insensitive_and_preserves_existing_key(self):
        for answer in ("no", "NO", " No ", ""):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory(prefix="deepseek-setup-") as directory, patch.object(setup.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value=answer), patch.object(setup.getpass, "getpass") as prompt, contextlib.redirect_stdout(io.StringIO()):
                target = Path(directory)
                setup.save_settings(target, "old-dummy-key")
                before = (target / "ai" / "api_key.enc").read_bytes()
                self.assertEqual(setup.main(target), 0)
                prompt.assert_not_called()
                self.assertEqual(before, (target / "ai" / "api_key.enc").read_bytes())

    def test_key_is_never_printed_even_on_save_error(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="deepseek-setup-") as directory, patch.object(setup.sys.stdin, "isatty", return_value=True), patch.object(setup.getpass, "getpass", return_value="dummy-secret"), patch.object(setup, "save_settings", side_effect=ValueError("dummy-secret")), contextlib.redirect_stdout(output):
            self.assertEqual(setup.main(Path(directory)), 1)
        self.assertNotIn("dummy-secret", output.getvalue())
        self.assertIn("ValueError", output.getvalue())

    def test_echo_fallback_is_blocked(self):
        with tempfile.TemporaryDirectory(prefix="deepseek-setup-") as directory, patch.object(setup.sys.stdin, "isatty", return_value=True), patch.object(setup.getpass, "getpass", side_effect=getpass.GetPassWarning()), patch.object(setup, "save_settings") as save, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(Path(directory)), 1)
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
