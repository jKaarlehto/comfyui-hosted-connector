"""Verify setup produces recovery configuration without requiring or restarting a Pod."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import hosted
import runpod


class SetupTests(unittest.TestCase):
    def test_hosted_setup_without_pod_creates_restricted_health_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(
                state_dir=root,
                env_file=root / ".env",
                gpu_fallbacks=None,
                max_hourly_cost=2.09,
                secret_prefix=None,
                starter_idle_seconds=60,
                no_restart=True,
            )
            state = {
                "deployment_id": "workspace",
                "template_id": "template",
                "global_volume_id": "volume",
                "broker_id": "starter",
                "hosted_env": {"NOTCH_GATEWAY_URL": "https://test.account.workers.dev", "NOTCH_GATEWAY_SERVER_KEY": "{{ RUNPOD_SECRET_gateway }}"},
                "config": {"storage": "global", "gpu": "NVIDIA PRO", "name": "Comfy", "comfy_port": 8188},
            }

            def create_key(path, comment):
                path.write_text("private-" + comment)
                path.with_suffix(".pub").write_text("ssh-ed25519 cHVibGlj " + comment)

            with (
                patch.object(runpod, "create_key", side_effect=create_key),
                patch.object(runpod, "save_state"),
                patch.object(hosted, "secret", side_effect=lambda a, k, s, f, n, v: n) as secrets,
                patch.object(runpod, "request", return_value={"id": "starter"}) as api,
                patch.object(runpod, "resolve_pod") as resolve,
                patch.object(hosted.subprocess, "run") as process,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                hosted.setup(args, "owner-private", state, root / "state.json")
            self.assertEqual(api.call_count, 1)
            body = api.call_args.args[3]
            self.assertEqual(body["env"]["NOTCH_TEMPLATE_ID"], "template")
            self.assertEqual(body["env"]["NOTCH_VOLUME_ID"], "volume")
            self.assertEqual(body["env"]["NOTCH_MAX_COST"], "2.09")
            self.assertNotIn("NOTCH_RECOVERY", body["env"])
            self.assertTrue(body["env"]["NOTCH_HEALTH_KEY_B64"].startswith("{{ RUNPOD_SECRET_"))
            self.assertNotIn("owner-private", json.dumps(body))
            self.assertIn("NOTCH_HEALTH_PUBLIC_KEY", state["hosted_env"])
            self.assertEqual(state["hosted_env"]["NOTCH_GATEWAY_SERVER_KEY"], "{{ RUNPOD_SECRET_gateway }}")
            self.assertNotIn("NOTCH_HEALTH_KEY_B64", state["hosted_env"])
            self.assertEqual(secrets.call_count, 3)
            resolve.assert_not_called()
            process.assert_called_once()
            self.assertEqual(body["args"], hosted.starter_code())
            self.assertIn("runpod==" + hosted.SDK_VERSION, hosted.starter_source())

    def test_template_setup_preserves_durable_recovery_and_embeds_health(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {"deployment_id": "workspace", "template_id": "template", "global_volume_id": "volume"}
            (root / "state.json").write_text(json.dumps(state))

            def create_key(path, comment):
                path.write_text("private-key")
                path.with_suffix(".pub").write_text("ssh-ed25519 cHVibGlj")

            def request(key, method, path, body=None):
                if method == "GET":
                    return {"env": {"NOTCH_RECOVERY": '{"phase":"requesting","attempt":"owned"}'}}
                return {"id": "template"}

            with (
                patch("sys.argv", ["runpod.py", "setup", "--state-dir", directory]),
                patch.object(runpod, "api_key", return_value="key"),
                patch.object(runpod, "private_file"),
                patch.object(runpod, "create_key", side_effect=create_key),
                patch.object(
                    runpod.subprocess,
                    "check_output",
                    return_value='[{"key":"ssh-ed25519 cHVibGlj","id":"git","read_only":true}]',
                ),
                patch.object(runpod, "ensure_secret", return_value="secret"),
                patch.object(
                    runpod,
                    "graphql",
                    return_value={"myself": {"globalStoreBuckets": [{"id": "volume", "name": "models"}]}},
                ),
                patch.object(runpod, "request", side_effect=request) as api,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                runpod.main()
            payload = api.call_args.args[3]
            self.assertEqual(json.loads(payload["env"]["NOTCH_RECOVERY"])["attempt"], "owned")
            self.assertIn("/opt/notch-health.py", payload["args"])
            self.assertIn("Report hosted workspace readiness", payload["args"])
            self.assertIn("--mode prepare || exit $?", payload["args"])
            self.assertIn("/hosted_comfyui/storage", payload["args"])
            self.assertIn("Leased SSH authorization", payload["args"])


if __name__ == "__main__":
    unittest.main()
