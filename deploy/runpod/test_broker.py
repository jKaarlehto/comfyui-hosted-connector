"""Verify that tester requests can only wake the configured Pod."""

import json
import os
import unittest
from unittest.mock import patch

import broker


class StarterTests(unittest.TestCase):
    def test_rejects_pod_override_and_command_without_contacting_runpod(self):
        for request in (
            {},
            {"action": "stop"},
            {"action": "connect", "pod_id": "other"},
            {"action": "connect", "command": "delete"},
        ):
            with self.subTest(request=request), patch.object(broker, "request") as api:
                self.assertIn("error", broker.handler({"input": request}))
                api.assert_not_called()

    def test_starts_only_the_fixed_stopped_pod(self):
        with (
            patch.dict(os.environ, {"NOTCH_POD_ID": "owned-pod"}),
            patch.object(broker, "request", side_effect=[{"desiredStatus": "EXITED"}, {}]) as api,
        ):
            result = broker.handler({"input": {"action": "connect"}})
        self.assertEqual(result["state"], "starting")
        self.assertEqual(api.call_args_list[1].args, ("POST", "/pods/owned-pod/start", {}))

    def test_capacity_failure_has_actionable_tester_state(self):
        with (
            patch.dict(os.environ, {"NOTCH_POD_ID": "owned-pod"}),
            patch.object(broker, "request", side_effect=broker.PodUnavailable),
        ):
            result = broker.handler({"input": {"action": "connect"}})
        self.assertEqual(result["state"], "unavailable")
        self.assertIn("owner", result["message"])

    def test_ready_response_contains_connection_data_not_owner_credentials(self):
        values = {
            "NOTCH_POD_ID": "owned-pod",
            "NOTCH_CONTROL_KEY": "private-control",
            "NOTCH_COMFY_PORT": "8188",
            "NOTCH_SSH_HOST_KEY": "ssh-ed25519 public-key",
        }
        pod = {
            "desiredStatus": "RUNNING",
            "publicIp": "192.0.2.1",
            "portMappings": {"22": 12345},
            "env": {"credential": "private-value"},
        }
        with patch.dict(os.environ, values), patch.object(broker, "request", return_value=pod):
            result = broker.handler({"input": {"action": "connect"}})
        self.assertEqual(
            result,
            {
                "state": "ready",
                "host": "192.0.2.1",
                "ssh_port": 12345,
                "comfy_port": 8188,
                "host_key": "ssh-ed25519 public-key",
            },
        )
        self.assertNotIn("private", json.dumps(result))

    def test_waits_for_public_ssh_mapping(self):
        with (
            patch.dict(os.environ, {"NOTCH_POD_ID": "owned-pod"}),
            patch.object(broker, "request", return_value={"desiredStatus": "RUNNING"}),
        ):
            result = broker.handler({"input": {"action": "connect"}})
        self.assertEqual(result["state"], "starting")


if __name__ == "__main__":
    unittest.main()
