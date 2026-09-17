"""Check deployment recovery, invitation revocation and storage integrity without cloud mutations."""

import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import bootstrap
import hosted
import runpod


class OwnerTests(unittest.TestCase):
    def test_environment_update_preserves_unrelated_values(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory, ".env")
            env.write_text("# owner settings\nOTHER_TOKEN=keep-me\nRUNPOD_API_KEY: old\n", encoding="utf-8")
            with patch.object(runpod, "private_file"):
                runpod.save_env_value(env, "RUNPOD_API_KEY", "new")
            self.assertEqual(env.read_text(), "# owner settings\nOTHER_TOKEN=keep-me\nRUNPOD_API_KEY=new\n")
            self.assertEqual(runpod.read_env_value(env, "RUNPOD_API_KEY"), "new")

    def test_prompted_key_is_saved_and_next_call_skips_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory, ".env")
            with (
                patch.dict(runpod.os.environ, {}, clear=True),
                patch.object(runpod.sys.stdin, "isatty", return_value=True),
                patch.object(runpod.getpass, "getpass", return_value="private-value") as prompt,
                patch.object(runpod, "private_file"),
            ):
                self.assertEqual(runpod.api_key(env), "private-value")
                self.assertEqual(runpod.api_key(env), "private-value")
            prompt.assert_called_once()

    def test_operational_graphql_error_retains_capacity_reason(self):
        response = io.BytesIO(json.dumps({"errors": [{"message": "No GPU capacity. key=private-key"}]}).encode())
        with patch.object(runpod.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "No GPU capacity") as caught:
                runpod.graphql("private-key", "mutation { podFindAndDeployOnDemand { id } }")
        self.assertNotIn("private-key", str(caught.exception))

    def test_secret_graphql_error_hides_rejected_value(self):
        response = io.BytesIO(json.dumps({"errors": [{"message": "Rejected value private-secret"}]}).encode())
        with patch.object(runpod.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "operation rejected") as caught:
                runpod.graphql("key", "mutation { secretValueUpdate { id } }")
        self.assertNotIn("private-secret", str(caught.exception))

    def test_http_requirements_preserve_torch_bindings_and_original_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "requirements.txt")
            target = Path(directory, "http-requirements.txt")
            content = "cuda-python>=13.1.1\njsonschema>=4.17.3\npackaging>=24.0\n"
            source.write_text(content, encoding="utf-8")
            bootstrap.prepare_http_requirements(source, target)
            self.assertEqual(source.read_text(encoding="utf-8"), content)
            self.assertEqual(target.read_text(encoding="utf-8"), "jsonschema>=4.17.3\npackaging>=24.0\n")

    def test_ssh_allows_only_local_tcp_forwarding(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory, "sshd_config")
            config.write_text("Port 22\n")
            bootstrap.configure_ssh(config)
            bootstrap.configure_ssh(config)
            self.assertEqual(config.read_text(), "AllowTcpForwarding local\nAllowStreamLocalForwarding no\nPort 22\n")

    def test_replace_rejects_running_pod_before_modifying_state(self):
        state = {"pod_id": "old"}
        args = types.SimpleNamespace(storage="global", pod="old")
        with (
            patch.object(runpod, "get_pod", return_value={"status": "RUNNING"}),
            patch.object(runpod, "save_state") as save,
        ):
            with self.assertRaisesRegex(RuntimeError, "Stop the Pod"):
                runpod.prepare_replacement(args, "key", state, Path("unused"))
        self.assertEqual(state, {"pod_id": "old"})
        save.assert_not_called()

    def test_replace_archives_stopped_pod_without_deleting_it(self):
        state = {"pod_id": "old"}
        args = types.SimpleNamespace(storage="global", pod="old")
        with (
            patch.object(runpod, "get_pod", return_value={"status": "EXITED"}),
            patch.object(runpod, "save_state") as save,
            patch.object(runpod, "request") as request,
        ):
            runpod.prepare_replacement(args, "key", state, Path("unused"))
        self.assertEqual(state, {"previous_pods": ["old"]})
        save.assert_called_once()
        request.assert_not_called()

    def test_recovery_ignores_retained_old_pod(self):
        records = [{"id": name, "env": {"NOTCH_DEPLOYMENT_ID": "setup"}} for name in ("old", "new")]
        with patch.object(runpod, "request", return_value={"pods": records}):
            found = runpod.find_resource("key", "/pods", "pods", "setup", ["old"])
        self.assertEqual(found["id"], "new")

    def test_replacement_refuses_local_pod_storage(self):
        args = types.SimpleNamespace(storage="pod", pod="old")
        with self.assertRaisesRegex(RuntimeError, "cannot move"):
            runpod.prepare_replacement(args, "key", {"pod_id": "old"}, Path("unused"))

    def test_ambiguous_template_create_recovers_without_duplicate(self):
        found = {"id": "existing", "env": {"NOTCH_DEPLOYMENT_ID": "our-setup"}}
        with patch.object(
            runpod, "request", side_effect=[{"templates": []}, RuntimeError("HTTP 503"), {"templates": [found]}]
        ) as request:
            result = runpod.create_resource("key", "/templates", "templates", {}, "our-setup")
        self.assertEqual(result["id"], "existing")
        self.assertEqual(sum(call.args[1] == "POST" for call in request.call_args_list), 1)

    def test_secret_name_collision_is_not_overwritten(self):
        found = {"id": "not-ours", "name": "shared", "description": "another setup"}
        with patch.object(runpod, "graphql", return_value={"myself": {"secrets": [found]}}) as graphql:
            with self.assertRaisesRegex(RuntimeError, "outside this setup"):
                runpod.ensure_secret("key", "shared", "private", "our setup", None)
        self.assertEqual(graphql.call_count, 1)

    def test_owned_secret_value_is_rotated(self):
        found = {"id": "ours", "name": "owned"}
        with patch.object(runpod, "graphql", side_effect=[{"myself": {"secrets": [found]}}, {}]) as graphql:
            result = runpod.ensure_secret("key", "owned", "new-value", "our setup", "ours")
        self.assertEqual(result, "ours")
        self.assertIn("secretValueUpdate", graphql.call_args.args[1])

    def test_failed_secret_create_is_reconciled_by_ownership(self):
        found = {"id": "created", "name": "owned", "description": "our setup"}
        with patch.object(
            runpod,
            "graphql",
            side_effect=[{"myself": {"secrets": []}}, RuntimeError("HTTP 503"), {"myself": {"secrets": [found]}}],
        ):
            self.assertEqual(runpod.ensure_secret("key", "owned", "value", "our setup", None), "created")

    def test_api_error_does_not_print_echoed_credentials(self):
        response = io.BytesIO(b'{"detail":"secret value must-not-appear"}')
        error = urllib.error.HTTPError("url", 422, "bad request", {}, response)
        with patch.object(runpod.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 422") as caught:
                runpod.request("private-key", "POST", "/templates", {"private": "value"})
        self.assertNotIn("must-not-appear", str(caught.exception))

    def test_stopped_revoke_leaves_invitation_and_registry_intact(self):
        state = {"pod_id": "pod", "guests": {"test": {"api_key_id": "api"}}}
        args = types.SimpleNamespace(guest="test")
        with (
            patch.object(runpod, "get_pod", return_value={"status": "EXITED", "ssh": {}}),
            patch.object(runpod, "graphql") as graphql,
            patch.object(hosted, "update_guests") as update,
        ):
            with self.assertRaisesRegex(RuntimeError, "Start the Pod"):
                hosted.revoke(args, "key", state, Path("unused"))
        graphql.assert_not_called()
        update.assert_not_called()
        self.assertIn("test", state["guests"])

    def test_registry_failure_does_not_revoke_key_or_forget_guest(self):
        state = {"pod_id": "pod", "guests": {"test": {"api_key_id": "api"}}}
        args = types.SimpleNamespace(guest="test")
        with (
            patch.object(hosted, "require_running", return_value={}),
            patch.object(hosted, "update_guests", side_effect=RuntimeError("SSH failed")),
            patch.object(runpod, "graphql") as graphql,
            patch.object(runpod, "save_state"),
        ):
            with self.assertRaisesRegex(RuntimeError, "SSH failed"):
                hosted.revoke(args, "key", state, Path("unused"))
        graphql.assert_not_called()
        self.assertIn("test", state["guests"])

    def test_revoke_persists_ssh_removal_before_deleting_api_key(self):
        state = {"pod_id": "pod", "guests": {"test": {"api_key_id": "api"}}}
        args = types.SimpleNamespace(guest="test")
        events = []
        with (
            patch.object(hosted, "require_running", return_value={}),
            patch.object(hosted, "update_guests", side_effect=lambda *args: events.append("ssh")),
            patch.object(runpod, "graphql", side_effect=lambda *args: events.append("api")),
            patch.object(hosted, "clear_guest_credentials", side_effect=lambda *args: events.append("files")),
            patch.object(runpod, "save_state", side_effect=lambda *args: events.append("state")),
        ):
            hosted.revoke(args, "key", state, Path("unused"))
        self.assertEqual(events, ["state", "ssh", "api", "files", "state"])
        self.assertEqual(state["guests"], {})

    def test_v1_pod_maps_to_shared_endpoint(self):
        with patch.object(
            runpod,
            "request",
            return_value={"desiredStatus": "RUNNING", "publicIp": "192.0.2.1", "portMappings": {"22": 1234}},
        ):
            pod = runpod.get_pod("key", "pod")
        self.assertEqual(pod["ssh"]["direct"], {"host": "192.0.2.1", "port": 1234, "username": "root"})


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import fcntl
        except ImportError:
            fcntl = types.SimpleNamespace(flock=lambda *args: None, LOCK_EX=2)
        spec = importlib.util.spec_from_file_location("model_store", Path(__file__).with_name("model_store.py"))
        cls.store = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"fcntl": fcntl}):
            spec.loader.exec_module(cls.store)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.local = Path(self.directory.name, "local")
        self.remote = Path(self.directory.name, "remote")
        (self.local / "models").mkdir(parents=True)
        for name in ("blobs", "files"):
            (self.remote / name).mkdir(parents=True)

    def test_restore_round_trip_and_exclude_unfinished_download(self):
        (self.local / "models" / "model.bin").write_bytes(b"model-data")
        (self.local / "models" / "unfinished.part").write_bytes(b"partial")
        self.store.sync(self.local, self.remote, minimum_age=0)
        restored = Path(self.directory.name, "restored")
        self.store.restore(restored, self.remote)
        self.assertEqual((restored / "models" / "model.bin").read_bytes(), b"model-data")
        self.assertFalse((restored / "models" / "unfinished.part").exists())

    def test_restore_rejects_corrupted_blob(self):
        (self.local / "models" / "model.bin").write_bytes(b"good")
        self.store.sync(self.local, self.remote, minimum_age=0)
        next((self.remote / "blobs").iterdir()).write_bytes(b"evil")
        restored = Path(self.directory.name, "restored")
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            self.store.restore(restored, self.remote)
        self.assertFalse((restored / "models" / "model.bin").exists())

    def test_explicit_sync_includes_files_with_a_future_timestamp(self):
        source = self.local / "models" / "model.bin"
        source.write_bytes(b"model-data")
        with patch.object(self.store.time, "time", return_value=source.stat().st_mtime - 1):
            self.store.sync(self.local, self.remote, minimum_age=30)
            self.assertFalse(list((self.remote / "files").iterdir()))
            self.store.sync(self.local, self.remote, minimum_age=0)
        restored = Path(self.directory.name, "restored")
        self.store.restore(restored, self.remote)
        self.assertEqual((restored / "models" / "model.bin").read_bytes(), b"model-data")

    def test_restore_rejects_path_traversal(self):
        (self.remote / "files" / "bad.json").write_text(
            json.dumps({"path": "../escape", "sha256": "0" * 64, "size": 0})
        )
        with self.assertRaisesRegex(ValueError, "Invalid"):
            self.store.restore(self.local, self.remote)


if __name__ == "__main__":
    unittest.main()
