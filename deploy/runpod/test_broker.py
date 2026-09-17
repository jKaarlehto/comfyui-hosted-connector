"""Tester requests cannot select Pods or issue owner operations."""

import os
import unittest
from unittest.mock import patch

import broker
import hosted


class StarterTests(unittest.TestCase):
    def test_rejects_overrides_without_contacting_runpod(self):
        for request in (
            None,
            {},
            {"action": "stop"},
            {"action": "connect", "pod_id": "other"},
            {"action": "connect", "command": "delete"},
        ):
            with self.subTest(request=request), patch.object(broker.recovery, "connection") as api:
                self.assertIn("error", broker.handler({"input": request}))
                api.assert_not_called()

    def test_connect_uses_only_server_side_credentials_and_endpoint(self):
        with (
            patch.dict(os.environ, {"NOTCH_CONTROL_KEY": "owner-key", "NOTCH_STARTER_ID": "fixed-starter"}),
            patch.object(broker.recovery, "connection", return_value={"state": "starting"}) as api,
        ):
            self.assertEqual(broker.handler({"input": {"action": "connect"}}), {"state": "starting"})
        api.assert_called_once_with("owner-key", "fixed-starter")

    def test_bootstrap_includes_recovery_module(self):
        bootstrap = hosted.starter_code()
        self.assertIn("/opt/recovery.py", bootstrap)
        self.assertIn("/opt/broker.py", bootstrap)


if __name__ == "__main__":
    unittest.main()
