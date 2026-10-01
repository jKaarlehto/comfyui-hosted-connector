import os
import types
import unittest
from unittest.mock import Mock, patch

from . import auth
from .client import V2Client


class AuthTests(unittest.TestCase):
    def test_os_credentials_are_endpoint_scoped_and_never_written_as_plaintext(self):
        backend = Mock()
        with patch.object(auth, "credential_store", return_value=backend), patch.dict(os.environ, {}, clear=True):
            auth.store_key("https://one.run.comfy.app", "private-value")
            auth.store_key("https://two.run.comfy.app", "other-private-value")
            first, second = backend.set_password.call_args_list
            self.assertNotEqual(first.args[1], second.args[1])
            self.assertEqual(first.args[0], "Notch.ComfyAPI")
            backend.get_password.return_value = "private-value"
            self.assertEqual(auth.load_key("https://one.run.comfy.app"), "private-value")
            auth.remove_key("https://one.run.comfy.app")
            backend.delete_password.assert_called_once()

    def test_credential_backend_failure_does_not_fall_back_to_a_plaintext_file(self):
        with patch.object(auth, "credential_store", side_effect=RuntimeError("unavailable")):
            with self.assertRaisesRegex(ValueError, "no plaintext key was saved"):
                auth.store_key("https://one.run.comfy.app", "private-value")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(auth.load_key("https://one.run.comfy.app"), "")

    def test_plaintext_keyring_backend_is_not_selected(self):
        module = types.SimpleNamespace(get_keyring=lambda: types.SimpleNamespace())
        with patch.dict("sys.modules", {"keyring": module}):
            with self.assertRaises(ValueError):
                auth.credential_store()

    def test_environment_override_needs_no_interactive_login(self):
        with (
            patch.dict(os.environ, {"COMFY_API_KEY": "session-value"}),
            patch.object(auth, "credential_store") as store,
        ):
            self.assertEqual(auth.load_key("https://one.run.comfy.app"), "session-value")
            store.assert_not_called()


class RedirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_bearer_is_removed_on_signed_storage_redirect(self):
        class Content:
            async def iter_chunked(self, size):
                yield b"image-bytes"

        class Response:
            def __init__(self, status, headers=None):
                self.status, self.headers, self.content = status, headers or {}, Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        session = Mock()
        session.get.side_effect = [
            Response(302, {"Location": "https://storage.example/output?signature=example"}),
            Response(200),
        ]
        client = V2Client("https://one.run.comfy.app", "private-value", session)
        result = await client.download("/api/v2/assets/output/content")
        self.assertEqual(result, b"image-bytes")
        first, second = session.get.call_args_list
        self.assertEqual(first.kwargs["headers"], {"Authorization": "Bearer private-value"})
        self.assertEqual(second.kwargs["headers"], {})
        self.assertFalse(first.kwargs["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
