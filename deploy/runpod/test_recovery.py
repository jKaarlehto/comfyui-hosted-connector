"""Check durable migration recovery, storage preservation and workspace boundaries."""

import copy
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import recovery


class Provider:
    def __init__(self):
        self.env = {
            "NOTCH_DEPLOYMENT_ID": "workspace",
            "NOTCH_POD_ID": "source",
            "NOTCH_COMFY_PORT": "8188",
            "NOTCH_SSH_HOST_KEY": "ssh-ed25519 public-host",
            "NOTCH_CONTROL_KEY": "{{ RUNPOD_SECRET_control }}",
        }
        self.pods = {
            "source": {
                "id": "source",
                "status": "EXITED",
                "cloud": "SECURE",
                "cost": 0.49,
                "gpu": {"id": "NVIDIA A40", "count": 1},
                "image": "image@sha256:pinned",
                "args": "startup",
                "disk": 32,
                "ports": ["22/tcp"],
                "mounts": {},
                "env": {
                    "NOTCH_DEPLOYMENT_ID": "workspace",
                    "PUBLIC_KEY": "public-owner",
                    "NOTCH_GIT_KEY_B64": "{{ RUNPOD_SECRET_git }}",
                },
            }
        }
        self.mounts = {
            "source": [
                {"volumeId": "global-volume", "volumeType": "OBJECT_STORE_VOLUME", "mountPath": "/workspace-global"}
            ]
        }
        self.migration = {
            "id": "migration",
            "sourcePodId": "source",
            "targetPodId": "target",
            "status": "COPYING",
            "progress": 0.5,
        }
        self.active = []
        self.calls = []
        self.price = 0.49
        self.start_error = recovery.CapacityUnavailable()
        self.migrate_error = None
        self.workers = 1

    def request(self, key, method, path, body=None, base=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/serverless/starter":
            if method == "GET":
                return {"workers": {"max": self.workers}, "env": copy.deepcopy(self.env)}
            if method == "PATCH":
                self.env = copy.deepcopy(body["env"])
                return {"id": "starter"}
        if path == "/pods" and method == "GET":
            return {"pods": copy.deepcopy(list(self.pods.values()))}
        parts = path.split("/")
        if len(parts) >= 3 and parts[1] == "pods":
            pod_id = parts[2]
            if method == "DELETE":
                del self.pods[pod_id]
                return None
            if method == "POST" and parts[3] in ("start", "stop"):
                if parts[3] == "start" and self.start_error:
                    raise self.start_error
                self.pods[pod_id]["status"] = "RUNNING" if parts[3] == "start" else "EXITED"
                return None
            if method == "GET":
                return {
                    "desiredStatus": self.pods[pod_id]["status"],
                    "publicIp": "192.0.2.1",
                    "portMappings": {"22": 12345},
                }
        raise AssertionError((method, path))

    def graphql(self, key, query, variables=None):
        self.calls.append(("GRAPHQL", query, copy.deepcopy(variables)))
        if "gpuTypes" in query:
            return {"gpuTypes": [{"id": "NVIDIA A40", "securePrice": self.price}]}
        if "volumeMounts" in query:
            pod_id = variables["input"]["podId"]
            return {"pod": {"volumeMounts": copy.deepcopy(self.mounts[pod_id])}}
        if "activeMigrations" in query:
            return {"myself": {"activeMigrations": copy.deepcopy(self.active)}}
        if "podMigrationById" in query:
            return {"podMigrationById": copy.deepcopy(self.migration)}
        if "migratePod" in query:
            if self.migrate_error:
                raise self.migrate_error
            return {"migratePod": copy.deepcopy(self.migration)}
        if "podUnlock" in query:
            self.pods[variables["input"]["podId"]]["locked"] = False
            return {"podUnlock": {"id": variables["input"]["podId"]}}
        raise AssertionError(query)

    def connect(self, replace=False):
        with (
            patch.object(recovery, "request", side_effect=self.request),
            patch.object(recovery, "graphql", side_effect=self.graphql),
        ):
            action = recovery.replace_pod if replace else recovery.connection
            return action("private-owner-key", "starter")

    def completed(self):
        self.connect(replace=True)
        self.pods["target"] = copy.deepcopy(self.pods["source"])
        self.pods["target"].update(id="target", status="RUNNING")
        self.mounts["target"] = copy.deepcopy(self.mounts["source"])
        self.migration["status"] = "COMPLETED"

    def mutations(self):
        return [
            call
            for call in self.calls
            if call[0] in ("PATCH", "POST", "DELETE") or call[0] == "GRAPHQL" and call[1].startswith("mutation")
        ]

    def migrations_requested(self):
        return [call for call in self.calls if call[0] == "GRAPHQL" and "migratePod(input:" in call[1]]


class RecoveryTests(unittest.TestCase):
    def test_starts_existing_pod_without_migration(self):
        provider = Provider()
        provider.start_error = None
        self.assertEqual(provider.connect()["state"], "starting")
        result = provider.connect()
        self.assertEqual(
            result,
            {
                "state": "ready",
                "host": "192.0.2.1",
                "ssh_port": 12345,
                "comfy_port": 8188,
                "host_key": "ssh-ed25519 public-host",
            },
        )
        self.assertFalse(provider.migrations_requested())
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("RUNPOD_SECRET", json.dumps(result))

    def test_persists_intent_before_migration_and_recovers_after_cold_start(self):
        provider = Provider()
        provider.connect()
        patches = [call for call in provider.calls if call[0] == "PATCH"]
        intent = json.loads(patches[0][2]["env"]["NOTCH_RECOVERY"])
        self.assertEqual(intent["phase"], "requesting")
        self.assertNotIn("RUNPOD_SECRET_git", json.dumps(intent))
        self.assertLess(provider.calls.index(patches[0]), provider.calls.index(provider.migrations_requested()[0]))
        self.assertEqual(provider.connect()["state"], "starting")
        self.assertEqual(len(provider.migrations_requested()), 1)
        self.assertIn("0.5", provider.connect()["message"])

    def test_capacity_failure_is_backed_off_across_workers(self):
        provider = Provider()
        provider.migrate_error = recovery.CapacityUnavailable()
        with patch.object(recovery.time, "time", return_value=1000):
            self.assertIn("once a minute", provider.connect()["message"])
        mutations = len(provider.mutations())
        with patch.object(recovery.time, "time", return_value=1059):
            self.assertEqual(provider.connect()["state"], "starting")
        self.assertEqual(len(provider.mutations()), mutations)
        with patch.object(recovery.time, "time", return_value=1061):
            provider.connect()
        self.assertEqual(len(provider.migrations_requested()), 2)

    def test_ambiguous_request_never_repeats_without_provider_evidence(self):
        provider = Provider()
        provider.migrate_error = RuntimeError("timeout")
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            provider.connect()
        self.assertEqual(json.loads(provider.env["NOTCH_RECOVERY"])["phase"], "requesting")
        for _ in range(2):
            self.assertEqual(provider.connect()["state"], "unavailable")
        self.assertEqual(len(provider.migrations_requested()), 1)
        provider.active = [provider.migration]
        self.assertEqual(provider.connect()["state"], "starting")
        self.assertEqual(len(provider.migrations_requested()), 1)

    def test_ambiguous_multiple_migrations_require_owner_review(self):
        provider = Provider()
        provider.migrate_error = RuntimeError("timeout")
        with self.assertRaises(RuntimeError):
            provider.connect()
        provider.active = [provider.migration, {**provider.migration, "id": "second"}]
        self.assertEqual(provider.connect()["state"], "unavailable")
        self.assertEqual(len(provider.migrations_requested()), 1)

    def test_refuses_missing_multiple_or_concurrent_owned_pods(self):
        for count in (0, 2):
            provider = Provider()
            if count == 0:
                provider.pods.clear()
            else:
                provider.pods["second"] = {**provider.pods["source"], "id": "second"}
            with self.subTest(count=count), self.assertRaisesRegex(RuntimeError, "one hosted Pod"):
                provider.connect()
            self.assertFalse(provider.mutations())
        provider = Provider()
        provider.workers = 2
        with self.assertRaisesRegex(RuntimeError, "one starter worker"):
            provider.connect()
        self.assertFalse(provider.mutations())

    def test_higher_price_requires_owner_review_before_migration(self):
        provider = Provider()
        provider.price = 0.50
        self.assertEqual(provider.connect(replace=True)["state"], "unavailable")
        self.assertFalse(provider.mutations())

    def test_completed_migration_deletes_only_verified_source(self):
        provider = Provider()
        provider.completed()
        provider.pods["source"]["locked"] = True
        provider.pods["unrelated"] = {"id": "unrelated", "env": {"NOTCH_DEPLOYMENT_ID": "another"}}
        result = provider.connect()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(set(provider.pods), {"target", "unrelated"})
        self.assertEqual(provider.env["NOTCH_POD_ID"], "target")
        self.assertEqual(provider.env["NOTCH_RECOVERY"], "{}")
        self.assertEqual([call[1] for call in provider.calls if call[0] == "DELETE"], ["/pods/source"])
        self.assertEqual(len([call for call in provider.calls if "podUnlock" in call[1]]), 1)
        self.assertNotIn("private-owner-key", json.dumps(result))

    def test_source_is_stopped_before_deletion(self):
        provider = Provider()
        provider.completed()
        provider.pods["source"]["status"] = "RUNNING"
        self.assertEqual(provider.connect()["state"], "starting")
        self.assertIn("source", provider.pods)
        self.assertFalse([call for call in provider.calls if call[0] == "DELETE"])
        self.assertEqual(provider.connect()["state"], "ready")
        self.assertNotIn("source", provider.pods)

    def test_cleanup_resumes_when_source_was_already_deleted(self):
        provider = Provider()
        provider.completed()
        state = json.loads(provider.env["NOTCH_RECOVERY"])
        state.update(phase="cleanup", target_id="target")
        provider.env["NOTCH_RECOVERY"] = json.dumps(state)
        del provider.pods["source"]
        self.assertEqual(provider.connect()["state"], "ready")
        self.assertEqual(provider.env["NOTCH_POD_ID"], "target")
        self.assertFalse([call for call in provider.calls if call[0] == "DELETE"])

    def test_target_configuration_changes_stop_target_without_deleting_source(self):
        changes = (
            lambda pod: pod.update(cloud="COMMUNITY"),
            lambda pod: pod.update(cost=0.50),
            lambda pod: pod.update(image="other"),
            lambda pod: pod.update(args="different startup"),
            lambda pod: pod["gpu"].update(count=2),
            lambda pod: pod["gpu"].update(id="other"),
            lambda pod: pod["env"].update(PUBLIC_KEY="other-owner"),
            lambda pod: pod["env"].update(NOTCH_GIT_KEY_B64="other-secret"),
            lambda pod: pod.update(ports=["8188/http"]),
        )
        for index, change in enumerate(changes):
            provider = Provider()
            provider.completed()
            change(provider.pods["target"])
            with self.subTest(change=index):
                self.assertEqual(provider.connect()["state"], "unavailable")
                self.assertEqual(provider.pods["target"]["status"], "EXITED")
                self.assertIn("source", provider.pods)
                self.assertFalse([call for call in provider.calls if call[0] == "DELETE"])

    def test_global_network_and_local_mounts_are_checked(self):
        for kind in ("global", "network", "persistent"):
            provider = Provider()
            if kind == "network":
                provider.pods["source"]["mounts"] = {"network": [{"volumeId": "network", "path": "/workspace"}]}
            if kind == "persistent":
                provider.pods["source"]["mounts"] = {"persistent": {"size": 32, "path": "/workspace"}}
            provider.completed()
            if kind == "global":
                provider.mounts["target"][0]["volumeId"] = "other"
            else:
                provider.pods["target"]["mounts"] = {}
            with self.subTest(kind=kind):
                self.assertEqual(provider.connect()["state"], "unavailable")
                self.assertIn("source", provider.pods)

    def test_unrelated_migration_target_is_not_touched(self):
        provider = Provider()
        provider.completed()
        provider.pods["target"]["env"]["NOTCH_DEPLOYMENT_ID"] = "another"
        before = len(provider.mutations())
        with self.assertRaisesRegex(RuntimeError, "another workspace"):
            provider.connect()
        self.assertIn("source", provider.pods)
        self.assertEqual(provider.pods["target"]["status"], "RUNNING")
        self.assertFalse([call for call in provider.mutations()[before:] if call[0] in ("POST", "DELETE")])

    def test_failed_migration_does_not_retry_or_delete_original(self):
        provider = Provider()
        provider.connect()
        provider.migration["status"] = "FAILED"
        self.assertEqual(provider.connect()["state"], "unavailable")
        before = len(provider.mutations())
        self.assertEqual(provider.connect()["state"], "unavailable")
        self.assertEqual(len(provider.mutations()), before)
        self.assertEqual(set(provider.pods), {"source"})

    def test_wrong_migration_record_is_rejected(self):
        for field in ("id", "sourcePodId"):
            provider = Provider()
            provider.connect()
            provider.migration[field] = "other"
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "could not be verified"):
                provider.connect()
            self.assertIn("source", provider.pods)


class ErrorTests(unittest.TestCase):
    def test_capacity_error_variants_are_recognized(self):
        for message in (
            "not enough free GPUs",
            "There are no instances currently available",
            "There are no longer any instances available with the requested specifications",
        ):
            with (
                self.subTest(message=message),
                patch.object(recovery, "request", return_value={"errors": [{"message": message}]}),
            ):
                with self.assertRaises(recovery.CapacityUnavailable):
                    recovery.graphql("private-key", "mutation")

    def test_graphql_errors_do_not_expose_provider_details(self):
        with patch.object(recovery, "request", return_value={"errors": [{"message": "private-key private-value"}]}):
            with self.assertRaises(RuntimeError) as caught:
                recovery.graphql("private-key", "query")
        self.assertNotIn("private", str(caught.exception))

    def test_http_errors_do_not_expose_provider_details(self):
        error = urllib.error.HTTPError(
            "https://api.runpod.io/v2/pods", 400, "bad", {}, io.BytesIO(b"private-key private-value")
        )
        with patch.object(recovery.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                recovery.request("private-key", "GET", "/pods")
        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
