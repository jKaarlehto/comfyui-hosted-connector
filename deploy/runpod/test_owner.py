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

    def test_resolver_adopts_only_owned_pod_and_preserves_other_state(self):
        state = {"deployment_id": "setup", "pod_id": "old", "guests": {"tester": {}}}
        pods = [
            {"id": "other", "env": {"NOTCH_DEPLOYMENT_ID": "another-setup"}},
            {"id": "new", "env": {"NOTCH_DEPLOYMENT_ID": "setup"}},
        ]
        with patch.object(runpod, "request", return_value=pods) as request, patch.object(runpod, "save_state") as save:
            self.assertEqual(runpod.resolve_pod("key", state, Path("unused")), "new")
        request.assert_called_once_with("key", "GET", "/pods", base="https://rest.runpod.io/v1")
        self.assertEqual(state, {"deployment_id": "setup", "pod_id": "new", "guests": {"tester": {}}})
        save.assert_called_once_with(Path("unused"), state)

    def test_resolver_does_not_rewrite_current_or_missing_pod(self):
        for pods, expected in (([{"id": "old", "env": {"NOTCH_DEPLOYMENT_ID": "setup"}}], "old"), ([], None)):
            with self.subTest(expected=expected):
                state = {"deployment_id": "setup", "pod_id": "old"}
                with patch.object(runpod, "request", return_value=pods), patch.object(runpod, "save_state") as save:
                    self.assertEqual(runpod.resolve_pod("key", state, Path("unused")), expected)
                self.assertEqual(state, {"deployment_id": "setup", "pod_id": "old"})
                save.assert_not_called()

    def test_resolver_does_not_hide_retained_pod_duplicates(self):
        state = {"deployment_id": "setup", "pod_id": "old", "previous_pods": ["old"]}
        pods = [{"id": name, "env": {"NOTCH_DEPLOYMENT_ID": "setup"}} for name in ("old", "new")]
        with patch.object(runpod, "request", return_value=pods), patch.object(runpod, "save_state") as save:
            with self.assertRaisesRegex(RuntimeError, "Multiple Pods"):
                runpod.resolve_pod("key", state, Path("unused"))
        self.assertEqual(state, {"deployment_id": "setup", "pod_id": "old", "previous_pods": ["old"]})
        save.assert_not_called()

    def test_resolver_refuses_invalid_or_incomplete_lists(self):
        for response in (None, {"pods": [], "nextCursor": "more"}, [{}], [{"id": "new", "env": []}]):
            with self.subTest(response=response):
                state = {"deployment_id": "setup", "pod_id": "old"}
                with patch.object(runpod, "request", return_value=response), patch.object(runpod, "save_state") as save:
                    with self.assertRaisesRegex(RuntimeError, "invalid Pod list"):
                        runpod.resolve_pod("key", state, Path("unused"))
                self.assertEqual(state, {"deployment_id": "setup", "pod_id": "old"})
                save.assert_not_called()

    def test_resolver_failure_preserves_recorded_pod(self):
        for operation in ("request", "save_state"):
            with self.subTest(operation=operation):
                state = {"deployment_id": "setup", "pod_id": "old"}
                with (
                    patch.object(
                        runpod, "request", return_value=[{"id": "new", "env": {"NOTCH_DEPLOYMENT_ID": "setup"}}]
                    ),
                    patch.object(runpod, "save_state"),
                    patch.object(runpod, operation, side_effect=RuntimeError("unavailable")),
                ):
                    with self.assertRaisesRegex(RuntimeError, "unavailable"):
                        runpod.resolve_pod("key", state, Path("unused"))
                self.assertEqual(state, {"deployment_id": "setup", "pod_id": "old"})

    def test_default_owner_commands_follow_migrated_pod(self):
        for command in ("status", "start", "stop", "connect", "sync", "terminate"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                state_file = Path(directory, "state.json")
                state_file.write_text(json.dumps({"deployment_id": "setup", "pod_id": "old"}))
                with (
                    patch.object(sys, "argv", ["runpod.py", command, "--state-dir", directory]),
                    patch.object(runpod, "api_key", return_value="key"),
                    patch.object(runpod, "private_file"),
                    patch.object(
                        runpod, "request", return_value=[{"id": "new", "env": {"NOTCH_DEPLOYMENT_ID": "setup"}}]
                    ) as request,
                    patch.object(runpod, "get_pod", return_value={}) as get_pod,
                    patch.object(runpod, "pod_action") as action,
                    patch.object(runpod, "connect") as connect,
                    patch.object(runpod, "sync_storage") as sync,
                    patch.object(sys, "stdout", new_callable=io.StringIO),
                ):
                    runpod.main()
                self.assertEqual(
                    json.loads(state_file.read_text()).get("pod_id"), None if command == "terminate" else "new"
                )
                if command == "status":
                    get_pod.assert_called_once_with("key", "new")
                elif command in ("start", "stop"):
                    action.assert_called_once_with("key", "new", command)
                elif command == "terminate":
                    self.assertEqual(request.call_args.args, ("key", "DELETE", "/pods/new"))
                else:
                    called = {"connect": connect, "sync": sync}[command]
                    self.assertEqual(called.call_args.args[0].pod, "new")

    def test_setup_can_update_template_without_resolving_a_pod(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(sys, "argv", ["runpod.py", "setup", "--state-dir", directory]),
                patch.object(runpod, "api_key", return_value="key"),
                patch.object(runpod, "private_file"),
                patch.object(runpod, "resolve_pod") as resolve,
                patch.object(runpod, "setup") as setup,
            ):
                runpod.main()
            resolve.assert_not_called()
            setup.assert_called_once()
            self.assertIsNone(setup.call_args.args[0].pod)

    def test_explicit_pod_command_never_rewrites_canonical_id(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory, "state.json")
            original = json.dumps({"deployment_id": "setup", "pod_id": "old"})
            state_file.write_text(original)
            with (
                patch.object(
                    sys,
                    "argv",
                    ["runpod.py", "terminate", "--pod", "old", "--storage", "pod", "--state-dir", directory],
                ),
                patch.object(runpod, "api_key", return_value="key"),
                patch.object(runpod, "resolve_pod") as resolve,
                patch.object(runpod, "save_state") as save,
                patch.object(runpod, "request") as request,
                patch.object(sys, "stdout", new_callable=io.StringIO),
            ):
                runpod.main()
            self.assertEqual(state_file.read_text(), original)
        request.assert_called_once_with("key", "DELETE", "/pods/old", base="https://rest.runpod.io/v1")
        resolve.assert_not_called()
        save.assert_not_called()

    def test_deploy_recovers_unrecorded_pod_without_creating_another(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory, "state.json")
            state_file.write_text(json.dumps({"deployment_id": "setup", "template_id": "template"}))
            with (
                patch.object(sys, "argv", ["runpod.py", "deploy", "--state-dir", directory]),
                patch.object(runpod, "api_key", return_value="key"),
                patch.object(runpod, "private_file"),
                patch.object(
                    runpod, "request", return_value=[{"id": "existing", "env": {"NOTCH_DEPLOYMENT_ID": "setup"}}]
                ) as request,
            ):
                with self.assertRaisesRegex(RuntimeError, "already has a Pod"):
                    runpod.main()
            self.assertEqual(json.loads(state_file.read_text())["pod_id"], "existing")
        self.assertEqual(request.call_count, 1)

    def test_replace_delegates_to_durable_starter_without_archiving_or_creating(self):
        recovery = types.SimpleNamespace(replace_pod=lambda *args: {"state": "migrating"})
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory, "state.json")
            original = json.dumps({"deployment_id": "setup", "pod_id": "old", "broker_id": "starter"})
            state_file.write_text(original)
            with (
                patch.object(sys, "argv", ["runpod.py", "replace", "--state-dir", directory]),
                patch.object(runpod, "api_key", return_value="key"),
                patch.dict(sys.modules, {"recovery": recovery}),
                patch.object(recovery, "replace_pod", return_value={"state": "migrating"}) as replace,
                patch.object(runpod, "request") as request,
                patch.object(runpod, "save_state") as save,
                patch.object(sys, "stdout", new_callable=io.StringIO),
            ):
                runpod.main()
            self.assertEqual(state_file.read_text(), original)
        replace.assert_called_once_with("key", "starter")
        request.assert_not_called()
        save.assert_not_called()

    def test_replace_requires_configured_starter(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory, "state.json")
            state_file.write_text(json.dumps({"deployment_id": "setup", "pod_id": "old"}))
            with (
                patch.object(sys, "argv", ["runpod.py", "replace", "--state-dir", directory]),
                patch.object(runpod, "api_key", return_value="key"),
                patch.object(runpod, "request") as request,
            ):
                with self.assertRaisesRegex(RuntimeError, "hosted.py setup"):
                    runpod.main()
        request.assert_not_called()

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
