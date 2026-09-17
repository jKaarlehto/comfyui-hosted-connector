"""Exercise allocation recovery across lost responses, cold workers and GPU shortages."""

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
            "NOTCH_TEMPLATE_ID": "template",
            "NOTCH_VOLUME_ID": "volume",
            "NOTCH_GPU_TIERS": json.dumps(["NVIDIA PRO", "NVIDIA A40"]),
            "NOTCH_MAX_COST": "2.09",
            "NOTCH_COMFY_PORT": "8188",
            "NOTCH_SSH_HOST_KEY": "ssh-ed25519 public",
        }
        self.template = {
            "name": "Comfy",
            "image": "image@sha256:fixed",
            "args": "startup",
            "disk": 32,
            "ports": ["22/tcp"],
            "env": {
                "NOTCH_DEPLOYMENT_ID": "workspace",
                "NOTCH_MAX_MINUTES": "240",
                "PUBLIC_KEY": "owner",
                "NOTCH_GIT_KEY_B64": "{{ RUNPOD_SECRET_git }}",
            },
        }
        self.pods = {}
        self.add("source", "Comfy", status="EXITED")
        self.calls = []
        self.creates = []
        self.now = 1000
        self.start_error = recovery.CapacityUnavailable()
        self.create_error = None
        self.commit_before_error = False
        self.delete_error = False
        self.update_error = False
        self.backend_ready = True
        self.workers = 1
        self.pagination = {"hasNextPage": False, "nextCursor": None}

    @property
    def state(self):
        return json.loads(self.template["env"].get("NOTCH_RECOVERY", "{}"))

    def add(self, pod_id, name, status="RUNNING", gpu="NVIDIA PRO"):
        self.pods[pod_id] = {
            **copy.deepcopy(self.template),
            "id": pod_id,
            "name": name,
            "status": status,
            "cloud": "SECURE",
            "cost": 2.09 if gpu == "NVIDIA PRO" else 0.49,
            "gpu": {"id": gpu, "count": 1},
            "mounts": {},
            "global_mounts": [
                {
                    "volumeId": "volume",
                    "volumeType": "OBJECT_STORE_VOLUME",
                    "mountPath": "/workspace-global",
                }
            ],
        }

    def request(self, key, method, path, body=None, base=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/serverless/starter" and method == "GET":
            return {"workers": {"max": self.workers}, "env": copy.deepcopy(self.env)}
        if path == "/templates/template":
            if method == "GET":
                return copy.deepcopy(self.template)
            if method == "PATCH":
                self.template["env"] = copy.deepcopy(body["env"])
                return {"id": "template"}
        if path == "/pods" and method == "GET":
            return {"pods": copy.deepcopy(list(self.pods.values())), "pagination": copy.deepcopy(self.pagination)}
        parts = path.split("/")
        if len(parts) >= 3 and parts[1] == "pods":
            pod_id = parts[2]
            if method == "DELETE":
                del self.pods[pod_id]
                if self.delete_error:
                    raise recovery.UncertainRequest()
                return None
            pod = self.pods[pod_id]
            if method == "PATCH":
                pod["args"] = self.template["args"]
                if self.update_error:
                    raise recovery.UncertainRequest()
                return {}
            if method == "GET":
                return {
                    "desiredStatus": pod["status"],
                    "publicIp": "192.0.2.1",
                    "portMappings": {"22": 1234},
                }
            if parts[-1] == "start":
                if self.start_error:
                    raise self.start_error
                pod["status"] = "RUNNING"
                return {}
            if parts[-1] == "stop":
                pod["status"] = "EXITED"
                return {}
        raise AssertionError((method, path))

    def graphql(self, key, query, variables=None):
        if "gpuTypes" in query:
            return {
                "gpuTypes": [
                    {"id": "NVIDIA PRO", "securePrice": 2.09},
                    {"id": "NVIDIA A40", "securePrice": 0.49},
                ]
            }
        if "volumeMounts" in query:
            return {"pod": {"volumeMounts": copy.deepcopy(self.pods[variables["input"]["podId"]]["global_mounts"])}}
        if "podFindAndDeployOnDemand" in query:
            spec = copy.deepcopy(variables["input"])
            self.creates.append(spec)
            pod_id = "target" + str(len(self.creates))
            if not self.create_error or self.commit_before_error:
                self.add(pod_id, spec["name"], gpu=spec["gpuTypeId"])
            if self.create_error:
                raise self.create_error
            return {"podFindAndDeployOnDemand": {"id": pod_id}}
        raise AssertionError(query)

    def connect(self):
        with (
            patch.object(recovery, "request", side_effect=self.request),
            patch.object(recovery, "graphql", side_effect=self.graphql),
            patch.object(recovery, "healthy", return_value=self.backend_ready),
            patch.object(recovery.time, "time", return_value=self.now),
        ):
            return recovery.connection("key", "starter")

    def mutations(self):
        return [call for call in self.calls if call[0] != "GET"]


class RecoveryTests(unittest.TestCase):
    def test_stopped_pod_adopts_updated_startup_before_resuming(self):
        p = Provider()
        p.start_error = None
        p.template["args"] = "updated startup"
        self.assertEqual(p.connect()["state"], "starting")
        self.assertEqual(p.pods["source"]["status"], "EXITED")
        self.assertEqual(p.mutations(), [("PATCH", "/pods/source", {"templateId": "template"})])
        self.assertEqual(p.connect()["state"], "starting")
        self.assertEqual(p.connect()["state"], "ready")
        self.assertFalse(p.creates)

    def test_lost_template_update_response_is_reconciled_without_duplicate_update(self):
        p = Provider()
        p.start_error = None
        p.update_error = True
        p.template["args"] = "updated startup"
        self.assertEqual(p.connect()["state"], "starting")
        self.assertEqual(p.connect()["state"], "starting")
        self.assertEqual(len([call for call in p.calls if call[0] == "PATCH"]), 1)
        self.assertEqual(p.pods["source"]["status"], "RUNNING")

    def test_running_pod_is_not_reset_when_template_changes(self):
        p = Provider()
        p.pods["source"]["status"] = "RUNNING"
        p.template["args"] = "updated startup"
        self.assertEqual(p.connect()["state"], "ready")
        self.assertFalse(p.mutations())

    def test_stopped_template_update_refuses_unexpected_storage(self):
        p = Provider()
        p.template["args"] = "updated startup"
        p.pods["source"]["global_mounts"] = []
        with self.assertRaisesRegex(RuntimeError, "storage differs"):
            p.connect()
        self.assertFalse(p.mutations())

    def test_partial_inventory_retries_without_allocating_or_mutating(self):
        for pagination in (
            {"hasNextPage": True, "nextCursor": None},
            {"hasNextPage": False, "nextCursor": "more"},
        ):
            for empty in (False, True):
                with self.subTest(pagination=pagination, empty=empty):
                    p = Provider()
                    p.pagination = pagination
                    if empty:
                        p.pods.clear()
                    self.assertEqual(p.connect()["state"], "starting")
                    self.assertFalse(p.mutations())
                    self.assertFalse(p.creates)

    def test_existing_pod_starts_without_allocation(self):
        p = Provider()
        p.start_error = None
        self.assertEqual(p.connect()["state"], "starting")
        self.assertEqual(p.connect()["state"], "ready")
        self.assertFalse(p.creates)

    def test_missing_pod_is_created_from_template_and_global_volume(self):
        p = Provider()
        p.pods.clear()
        self.assertEqual(p.connect()["state"], "starting")
        self.assertEqual(p.connect()["state"], "ready")
        self.assertEqual(set(p.pods), {"target1"})
        spec = p.creates[0]
        self.assertNotIn("dataCenterId", spec)
        self.assertEqual(spec["volumeMounts"][0]["volumeId"], "volume")
        self.assertEqual(spec["gpuCount"], 1)
        self.assertIn("stopAfter", spec)

    def test_state_is_saved_to_gpu_template_not_starter(self):
        p = Provider()
        p.connect()
        self.assertEqual(p.state["phase"], "verifying")
        self.assertTrue(any(c[1] == "/templates/template" for c in p.mutations()))
        self.assertFalse(any(c[1] == "/serverless/starter" for c in p.mutations()))

    def test_only_backend_ready_replacement_retires_original(self):
        p = Provider()
        p.connect()
        p.backend_ready = False
        self.assertEqual(p.connect()["state"], "starting")
        self.assertIn("source", p.pods)
        p.backend_ready = True
        self.assertEqual(p.connect()["state"], "ready")
        self.assertEqual(set(p.pods), {"target1"})
        self.assertEqual(p.connect()["state"], "ready")
        self.assertEqual(len(p.creates), 1)

    def test_capacity_falls_back_one_gpu_per_request(self):
        p = Provider()
        p.create_error = recovery.CapacityUnavailable()
        self.assertEqual(p.connect()["state"], "starting")
        self.assertEqual(len(p.creates), 1)
        p.create_error = None
        p.connect()
        self.assertEqual(p.creates[1]["gpuTypeId"], "NVIDIA A40")
        self.assertEqual(p.connect()["state"], "ready")

    def test_exhausted_capacity_retries_after_minute(self):
        p = Provider()
        p.create_error = recovery.CapacityUnavailable()
        p.connect()
        p.connect()
        p.connect()
        self.assertEqual(p.state["retry_after"], 1060)
        p.connect()
        self.assertEqual(len(p.creates), 2)
        p.now += 61
        p.connect()
        self.assertEqual(len(p.creates), 3)

    def test_more_expensive_gpu_is_skipped(self):
        p = Provider()
        p.env["NOTCH_MAX_COST"] = ".49"
        p.connect()
        self.assertEqual(p.creates[0]["gpuTypeId"], "NVIDIA A40")

    def test_timeout_after_committed_create_is_adopted(self):
        p = Provider()
        p.create_error = recovery.UncertainRequest()
        p.commit_before_error = True
        p.connect()
        self.assertEqual(p.state["phase"], "requesting")
        self.assertEqual(p.connect()["state"], "ready")
        self.assertEqual(len(p.creates), 1)

    def test_timeout_before_create_retries_after_bounded_inventory_checks(self):
        p = Provider()
        p.create_error = recovery.UncertainRequest()
        p.connect()
        for delta in (20, 40, 60):
            p.now = 1000 + delta
            self.assertEqual(p.connect()["state"], "starting")
        self.assertEqual(len(p.creates), 1)
        p.now = 1121
        p.connect()
        self.assertEqual(p.state["phase"], "capacity")
        p.create_error = None
        p.connect()
        self.assertEqual(len(p.creates), 2)
        self.assertEqual(p.connect()["state"], "ready")

    def test_late_duplicate_is_stopped_then_removed_even_after_completion(self):
        p = Provider()
        p.create_error = recovery.UncertainRequest()
        p.connect()
        old_name = p.creates[0]["name"]
        for delta in (20, 40, 121):
            p.now = 1000 + delta
            p.connect()
        p.create_error = None
        p.connect()
        p.connect()
        p.add("late", old_name)
        self.assertEqual(p.connect()["state"], "starting")
        self.assertEqual(p.pods["late"]["status"], "EXITED")
        p.connect()
        self.assertNotIn("late", p.pods)
        self.assertEqual(p.connect()["state"], "ready")
        self.assertEqual(set(p.pods), {"target2"})

    def test_late_candidate_during_capacity_is_adopted_before_allocating(self):
        p = Provider()
        p.create_error = recovery.UncertainRequest()
        p.connect()
        for delta in (20, 40, 121):
            p.now = 1000 + delta
            p.connect()
        p.add("late", p.creates[0]["name"])
        self.assertEqual(p.connect()["state"], "ready")
        self.assertEqual(len(p.creates), 1)

    def test_delete_response_lost_recovers_with_source_absent(self):
        p = Provider()
        p.connect()
        p.delete_error = True
        self.assertEqual(p.connect()["state"], "starting")
        self.assertNotIn("source", p.pods)
        self.assertEqual(p.connect()["state"], "ready")
        self.assertEqual(len(p.creates), 1)

    def test_missing_target_reconciles_then_retries(self):
        p = Provider()
        p.connect()
        del p.pods["target1"]
        for delta in (20, 40, 121):
            p.now = 1000 + delta
            p.connect()
        p.connect()
        self.assertEqual(len(p.creates), 2)

    def test_source_disappearing_during_startup_is_safe(self):
        p = Provider()
        p.connect()
        del p.pods["source"]
        self.assertEqual(p.connect()["state"], "ready")

    def test_unknown_multiple_pods_and_other_workspaces_are_not_touched(self):
        p = Provider()
        p.add("unknown", "unrecorded")
        with self.assertRaisesRegex(RuntimeError, "Unexpected"):
            p.connect()
        self.assertFalse(p.mutations())
        p = Provider()
        p.add("unrelated", "other")
        p.pods["unrelated"]["env"]["NOTCH_DEPLOYMENT_ID"] = "other"
        p.connect()
        p.connect()
        self.assertIn("unrelated", p.pods)

    def test_configuration_rejection_does_not_loop_gpu_tiers(self):
        p = Provider()
        p.create_error = recovery.RejectedRequest()
        self.assertEqual(p.connect()["state"], "unavailable")
        self.assertEqual(p.connect()["state"], "unavailable")
        self.assertEqual(len(p.creates), 1)
        self.assertIn("source", p.pods)

    def test_changed_target_is_stopped_without_deleting_source(self):
        for change in (
            lambda p: p.update(cost=3),
            lambda p: p.update(image="other"),
            lambda p: p["env"].update(PUBLIC_KEY="other"),
            lambda p: p["global_mounts"][0].update(volumeId="other"),
            lambda p: p["gpu"].update(count=2),
            lambda p: p.update(ports=["8188/http"]),
        ):
            p = Provider()
            p.connect()
            change(p.pods["target1"])
            self.assertEqual(p.connect()["state"], "unavailable")
            self.assertIn("source", p.pods)
            self.assertEqual(p.pods["target1"]["status"], "EXITED")

    def test_provider_extra_environment_is_ignored_but_secret_refs_are_checked(self):
        p = Provider()
        p.connect()
        p.pods["target1"]["env"]["RUNPOD_EXTRA"] = "provider"
        self.assertEqual(p.connect()["state"], "ready")

    def test_local_disk_mount_is_not_discarded(self):
        p = Provider()
        p.pods["source"]["mounts"] = {"persistent": {"size": 32, "path": "/workspace"}}
        with self.assertRaisesRegex(RuntimeError, "global storage"):
            p.connect()
        self.assertFalse(p.creates)

    def test_second_recovery_retains_previously_served_source_until_ready(self):
        p = Provider()
        p.connect()
        p.connect()
        p.pods["target1"]["status"] = "EXITED"
        p.create_error = recovery.CapacityUnavailable()
        p.connect()
        p.create_error = None
        p.connect()
        p.backend_ready = False
        p.connect()
        self.assertIn("target1", p.pods)
        self.assertEqual(p.pods["target1"]["status"], "EXITED")
        p.backend_ready = True
        self.assertEqual(p.connect()["state"], "ready")
        self.assertEqual(set(p.pods), {"target3"})

    def test_late_candidate_is_verified_when_completed_target_disappears(self):
        p = Provider()
        p.connect()
        name = p.creates[0]["name"]
        p.connect()
        del p.pods["target1"]
        p.add("late", name)
        p.pods["late"]["cost"] = 3
        self.assertEqual(p.connect()["state"], "unavailable")
        self.assertEqual(p.pods["late"]["status"], "EXITED")

    def test_owner_replacement_runs_through_the_same_starter_worker(self):
        with patch.object(recovery, "request", return_value={"id": "job"}) as request:
            recovery.replace_pod("owner", "starter")
        request.assert_called_once_with(
            "owner",
            "POST",
            "/starter/run",
            {"input": {"action": "connect"}},
            "https://api.runpod.ai/v2",
        )


class ErrorTests(unittest.TestCase):
    def test_capacity_messages_are_safe_rejections(self):
        for message in recovery.CAPACITY_ERRORS:
            with patch.object(recovery, "request", return_value={"errors": [{"message": message}]}):
                with self.assertRaises(recovery.CapacityUnavailable):
                    recovery.graphql("key", "mutation")

    def test_unclassified_graphql_errors_stay_uncertain(self):
        with patch.object(recovery, "request", return_value={"errors": [{"message": "private value"}]}):
            with self.assertRaises(recovery.UncertainRequest) as caught:
                recovery.graphql("key", "mutation")
        self.assertNotIn("private", str(caught.exception))

    def test_schema_rejection_is_definitive(self):
        with patch.object(
            recovery,
            "request",
            return_value={"errors": [{"extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}]},
        ):
            with self.assertRaises(recovery.RejectedRequest):
                recovery.graphql("key", "mutation")

    def test_http_timeout_and_rejection_are_distinct_and_private(self):
        for code, kind in (
            (400, recovery.RejectedRequest),
            (401, recovery.RejectedRequest),
            (500, recovery.UncertainRequest),
        ):
            error = urllib.error.HTTPError("https://api.runpod.io", code, "bad", {}, io.BytesIO(b"private key"))
            with patch.object(recovery.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(kind) as caught:
                    recovery.request("key", "POST", "/pods")
                self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
