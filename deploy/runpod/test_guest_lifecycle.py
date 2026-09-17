"""Check revocation, local cleanup and fresh credentials when a tester is reinvited."""

import base64
import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import hosted


class GuestLifecycleTests(unittest.TestCase):
    def test_revoke_then_reinvite_uses_fresh_key_and_removes_old_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = types.SimpleNamespace(state_dir=root, guest="alice")
            state = {"broker_id": "starter", "pod_id": "pod", "guests": {}, "invitation_site": "https://example.test/"}
            identities = []

            def create_key(path, comment):
                self.assertFalse(path.exists(), "New invitation reused an existing private key")
                identity = "fresh-private-key-" + str(len(identities) + 1)
                identities.append(identity)
                path.write_text(identity)
                path.with_suffix(".pub").write_text("ssh-ed25519 public-key-" + str(len(identities)))

            def graphql(key, query, variables):
                if "createApiKeyNew" in query:
                    return {"createApiKeyNew": {"id": "api-" + str(len(identities)), "rawKey": "private-api-key"}}
                return {"deleteApiKey": {"count": 1}}

            with (
                patch.object(hosted, "require_running", return_value={}),
                patch.object(hosted, "update_guests"),
                patch.object(hosted.owner, "create_key", side_effect=create_key),
                patch.object(hosted.owner, "graphql", side_effect=graphql),
                patch.object(hosted.owner, "private_file"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hosted.invite(args, "owner", state, root / "state.json")
                folder = hosted.guest_folder(args)
                (folder / "notes.txt").write_text("Keep this unrelated file")
                old_public = state["guests"]["alice"]["public_key"]
                hosted.revoke(args, "owner", state, root / "state.json")
                self.assertEqual(state["guests"], {})
                for name in ("id_ed25519", "id_ed25519.pub", "invitation.txt", "invitation-link.txt"):
                    self.assertFalse((folder / name).exists())
                self.assertTrue((folder / "notes.txt").exists())
                (folder / "id_ed25519").write_text(identities[0])
                hosted.invite(args, "owner", state, root / "state.json")
                new_payload = json.loads(base64.b64decode((folder / "invitation.txt").read_text()))
                self.assertEqual(new_payload["ssh_key"], identities[1])
                self.assertNotEqual(new_payload["ssh_key"], identities[0])
                self.assertNotEqual(state["guests"]["alice"]["public_key"], old_public)

    def test_cloud_revoke_failure_keeps_local_credentials_and_control_state(self):
        args = types.SimpleNamespace(guest="alice")
        state = {"guests": {"alice": {"api_key_id": "api", "public_key": "public"}}}
        with (
            patch.object(hosted, "require_running", return_value={}),
            patch.object(hosted, "update_guests") as registry,
            patch.object(hosted.owner, "graphql", side_effect=RuntimeError("API unavailable")),
            patch.object(hosted, "clear_guest_credentials") as clear,
            patch.object(hosted.owner, "save_state") as save,
        ):
            with self.assertRaisesRegex(RuntimeError, "API unavailable"):
                hosted.revoke(args, "owner", state, Path("unused"))
        registry.assert_called_once()
        clear.assert_not_called()
        save.assert_called_once()
        self.assertIn("alice", state["guests"])
        self.assertTrue(state["guests"]["alice"]["revoking"])

    def test_cleanup_failure_keeps_guest_record_for_retry(self):
        args = types.SimpleNamespace(guest="alice")
        state = {"guests": {"alice": {"api_key_id": "api"}}}
        with (
            patch.object(hosted, "require_running", return_value={}),
            patch.object(hosted, "update_guests"),
            patch.object(hosted.owner, "graphql", return_value={"deleteApiKey": {"count": 1}}),
            patch.object(hosted, "clear_guest_credentials", side_effect=OSError("File locked")),
            patch.object(hosted.owner, "save_state") as save,
        ):
            with self.assertRaisesRegex(OSError, "File locked"):
                hosted.revoke(args, "owner", state, Path("unused"))
        save.assert_called_once()
        self.assertIn("alice", state["guests"])
        self.assertTrue(state["guests"]["alice"]["revoking"])

    def test_pending_revocation_cannot_regenerate_or_reshare_link(self):
        args = types.SimpleNamespace(guest="alice")
        state = {"broker_id": "starter", "guests": {"alice": {"revoking": True}}}
        with patch.object(hosted, "require_running", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "revocation is pending"):
                hosted.invite(args, "owner", state, Path("unused"))
        with self.assertRaisesRegex(RuntimeError, "revocation is pending"):
            hosted.write_invitation_link(args, state)

    def test_other_invitation_updates_cannot_restore_pending_guest_ssh_access(self):
        args = types.SimpleNamespace(state_dir=Path("unused"))
        state = {
            "config": {"storage": "global", "comfy_port": 8188},
            "guests": {
                "revoked": {"public_key": "ssh-ed25519 revoked-key", "revoking": True},
                "active": {"public_key": "ssh-ed25519 active-key"},
            },
        }
        with (
            patch.object(hosted, "require_running", return_value={"host": "example.test", "port": 22}),
            patch.object(hosted.subprocess, "run") as command,
        ):
            hosted.update_guests(args, "owner", state)
        self.assertNotIn("revoked-key", command.call_args.kwargs["input"])
        self.assertIn("active-key", command.call_args.kwargs["input"])

    def test_cleanup_rejects_paths_outside_guest_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "id_ed25519"
            identity.write_text("outside")
            with self.assertRaisesRegex(RuntimeError, "Invalid tester name"):
                hosted.clear_guest_credentials(types.SimpleNamespace(state_dir=root, guest="../"))
            self.assertEqual(identity.read_text(), "outside")

    def test_list_is_offline_and_does_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "state.json").write_text(json.dumps({"guests": {"alice": {"api_key_id": "private"}}}))
            output = io.StringIO()
            with (
                patch.object(hosted.sys, "argv", ["hosted.py", "list", "--state-dir", directory]),
                patch.object(hosted.owner, "api_key", side_effect=AssertionError("No cloud credentials needed")),
                contextlib.redirect_stdout(output),
            ):
                hosted.main()
            self.assertIn("alice", output.getvalue())
            self.assertNotIn("private", output.getvalue())

    def test_cleanup_rejects_guest_directory_link_to_another_tester(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = root / "shares" / "bob"
            other.mkdir(parents=True)
            identity = other / "id_ed25519"
            identity.write_text("other-tester")
            try:
                (root / "shares" / "alice").symlink_to(other, target_is_directory=True)
            except OSError:
                self.skipTest("Creating directory links is unavailable in this environment")
            with self.assertRaisesRegex(RuntimeError, "private shares folder"):
                hosted.clear_guest_credentials(types.SimpleNamespace(state_dir=root, guest="alice"))
            self.assertEqual(identity.read_text(), "other-tester")


if __name__ == "__main__":
    unittest.main()
