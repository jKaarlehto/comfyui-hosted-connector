"""Gateway setup and invitation boundaries without cloud mutations."""

import base64
import io
import json
import os
import struct
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import gateway_owner
import bootstrap
import hosted
import runpod
import server_keys


PUBLIC = (
    "ssh-ed25519 "
    + base64.b64encode(
        struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(32)
    ).decode()
)
ORIGIN = "https://workspace.account.workers.dev"


class RegistryTests(unittest.TestCase):
    def test_bootstrap_removes_old_lease_and_keeps_server_secret_out_of_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sshd_config").write_text("AllowTcpForwarding local\n")
            (root / "notch-gateway-keys.json").write_text("stale")

            def local_path(value):
                return root / Path(value).name

            with (
                patch.object(bootstrap, "Path", side_effect=local_path),
                patch.dict(
                    os.environ,
                    {"NOTCH_GATEWAY_URL": ORIGIN, "NOTCH_GATEWAY_SERVER_KEY": "a" * 64},
                ),
                patch.object(bootstrap.subprocess, "Popen") as process,
            ):
                bootstrap.configure_gateway(8188)
                self.assertNotIn("NOTCH_GATEWAY_SERVER_KEY", os.environ)
                self.assertNotIn("NOTCH_GATEWAY_URL", os.environ)
                self.assertFalse((root / "notch-gateway-keys.json").exists())
                self.assertIn("authorize %u %t %k", (root / "sshd_config").read_text())
                self.assertNotIn("a" * 64, repr(process.call_args))
            self.assertEqual(
                json.loads((root / "notch-gateway.json").read_text())["key"], "a" * 64
            )

    def test_verified_registry_grants_only_comfy_forwarding_until_lease_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory, "config.json")
            cache = Path(directory, "cache.json")
            config.write_text(
                json.dumps({"gateway": ORIGIN, "key": "a" * 64, "port": 8188})
            )
            response = {
                "revision": 4,
                "lease_seconds": 300,
                "keys": [{"device_id": "b" * 32, "public_key": PUBLIC}],
            }
            with (
                patch.object(server_keys.urllib.request, "build_opener") as opener,
                patch.object(server_keys.time, "monotonic", return_value=10),
            ):
                opener.return_value.open.return_value = io.BytesIO(
                    json.dumps(response).encode()
                )
                server_keys.refresh(config, cache)
            self.assertNotIn("a" * 64, cache.read_text())
            values = ["root"] + PUBLIC.split()
            with patch.object(server_keys.time, "monotonic", return_value=309):
                line = server_keys.authorize(cache, values)
                self.assertIn(
                    'restrict,port-forwarding,permitopen="127.0.0.1:8188",command="/bin/false"',
                    line,
                )
                self.assertEqual(
                    server_keys.authorize(cache, ["other"] + PUBLIC.split()), ""
                )
            with patch.object(server_keys.time, "monotonic", return_value=310):
                self.assertEqual(server_keys.authorize(cache, values), "")

    def test_bad_registry_does_not_replace_a_valid_cached_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory, "config.json")
            cache = Path(directory, "cache.json")
            config.write_text(
                json.dumps({"gateway": ORIGIN, "key": "a" * 64, "port": 8188})
            )
            cache.write_text("unchanged")
            for bad in (PUBLIC + " bad\ncommand=x", "ssh-ed25519 " + "A" * 68):
                data = {
                    "revision": 1,
                    "lease_seconds": 300,
                    "keys": [{"device_id": "b" * 32, "public_key": bad}],
                }
                with patch.object(server_keys.urllib.request, "build_opener") as opener:
                    opener.return_value.open.return_value = io.BytesIO(
                        json.dumps(data).encode()
                    )
                    with self.assertRaises(ValueError):
                        server_keys.refresh(config, cache)
                self.assertEqual(cache.read_text(), "unchanged")

    def test_gateway_origin_and_redirect_restrictions(self):
        self.assertEqual(server_keys.gateway_origin(ORIGIN), ORIGIN)
        for bad in (
            "http://workspace.account.workers.dev",
            ORIGIN + "/",
            ORIGIN + ":443",
            ORIGIN + "/x",
            "https://a@workspace.account.workers.dev",
            ORIGIN.upper(),
        ):
            with self.assertRaises(ValueError):
                server_keys.gateway_origin(bad)
        self.assertIsNone(
            server_keys.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://other.example"
            )
        )

    def test_unavailable_registry_expires_without_refresher(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory, "cache.json")
            cache.write_text(
                json.dumps({"expires": 300, "keys": {"b" * 32: PUBLIC}, "port": 8188})
            )
            with patch.object(server_keys.time, "monotonic", return_value=301):
                self.assertEqual(
                    server_keys.authorize(cache, ["root"] + PUBLIC.split()), ""
                )


class GatewayOwnerTests(unittest.TestCase):
    def test_setup_deploys_scoped_secrets_and_updates_template_without_pod_actions(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            args = types.SimpleNamespace(
                state_dir=Path(directory), env_file=Path(directory, ".env")
            )
            state = {
                "deployment_id": "a" * 32,
                "template_id": "template",
                "broker_id": "starter",
                "config": {"name": "ComfyUI Notch"},
            }
            values = {
                "CLOUDFLARE_ACCOUNT_ID": "b" * 32,
                "CLOUDFLARE_API_TOKEN": "cloud-private",
                "GATEWAY_ADMIN_KEY": "c" * 64,
                "GATEWAY_SERVER_KEY": "d" * 64,
                "RUNPOD_GATEWAY_STARTER_KEY": "starter-scoped",
            }
            with (
                patch.object(
                    gateway_owner,
                    "credential",
                    side_effect=lambda p, n, **kw: values[n],
                ),
                patch.object(runpod, "api_key", return_value="owner-private"),
                patch.object(gateway_owner.shutil, "which", side_effect=lambda n: n),
                patch.object(gateway_owner, "run") as commands,
                patch.object(
                    gateway_owner,
                    "http_json",
                    return_value={"success": True, "result": {"subdomain": "account"}},
                ),
                patch.object(runpod, "ensure_secret", return_value="secret"),
                patch.object(runpod, "save_state"),
                patch.object(gateway_owner.subprocess, "run") as process,
                patch.object(runpod, "request") as pods,
            ):
                gateway_owner.setup(args, state, Path(directory, "state.json"))
            pods.assert_not_called()
            self.assertEqual(process.call_count, 1)
            self.assertIn("setup", process.call_args.args[0])
            secret_upload = json.loads(commands.call_args.args[3])
            self.assertEqual(secret_upload["RUNPOD_STARTER_KEY"], "starter-scoped")
            self.assertNotIn("owner-private", json.dumps(secret_upload))
            self.assertNotIn("d" * 64, json.dumps(state))
            self.assertIn(
                "RUNPOD_SECRET_", state["hosted_env"]["NOTCH_GATEWAY_SERVER_KEY"]
            )
            for call in commands.call_args_list:
                self.assertNotIn("cloud-private", " ".join(call.args[0]))

    def test_existing_credentials_skip_prompt_and_generated_values_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory, ".env")
            env.write_text("OTHER=value\nGATEWAY_ADMIN_KEY=" + "a" * 64 + "\n")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(gateway_owner.getpass, "getpass") as prompt,
                patch.object(runpod, "private_file"),
            ):
                self.assertEqual(
                    gateway_owner.credential(env, "GATEWAY_ADMIN_KEY"), "a" * 64
                )
                value = gateway_owner.credential(
                    env, "GATEWAY_SERVER_KEY", generate=True
                )
                self.assertEqual(
                    runpod.read_env_value(env, "GATEWAY_SERVER_KEY"), value
                )
                self.assertIn("OTHER=value", env.read_text())
                prompt.assert_not_called()

    def test_scoped_starter_policy_has_no_pod_or_account_access(self):
        policy = gateway_owner.starter_policy("endpoint")
        statement = policy["Statement"][0]
        self.assertEqual(statement["Actions"], ["serverless:Read", "serverless:Write"])
        self.assertEqual(statement["Resources"], ["runpod/serverless/*/endpoint/*"])

    def test_v2_invitation_needs_no_pod_or_private_ssh_key(self):
        with tempfile.TemporaryDirectory() as directory:
            args = types.SimpleNamespace(
                guest="alice", invite_days=7, env_file=Path(directory, ".env")
            )
            state = {"gateway": ORIGIN}
            folder = Path(directory, "alice")
            result = {"invite_id": "b" * 32, "token": "c" * 64, "expires_at": 12345}
            with (
                patch.object(gateway_owner, "api", return_value=result),
                patch.object(runpod, "private_file"),
                patch.object(runpod, "save_state"),
                patch.object(runpod, "request") as cloud,
            ):
                gateway_owner.invite(args, state, Path(directory, "state.json"), folder)
            cloud.assert_not_called()
            data = json.loads(base64.b64decode((folder / "invitation.txt").read_text()))
            self.assertEqual(set(data), {"version", "gateway", "invite_id", "token"})
            self.assertEqual(data["version"], 2)
            self.assertFalse((folder / "id_ed25519").exists())
            self.assertNotIn("token", json.dumps(state))

    def test_invitation_revoke_does_not_revoke_device_or_discard_state_on_failure(self):
        args = types.SimpleNamespace(guest="alice")
        state = {"gateway_invites": {"alice": {"invite_id": "a" * 32}}}
        with (
            patch.object(gateway_owner, "api", side_effect=RuntimeError("unavailable")),
            patch.object(runpod, "save_state") as save,
        ):
            with self.assertRaises(RuntimeError):
                gateway_owner.revoke_invite(args, state, Path("state"))
            self.assertNotIn("revoked", state["gateway_invites"]["alice"])
            save.assert_not_called()
        with (
            patch.object(gateway_owner, "api") as api,
            patch.object(runpod, "save_state"),
        ):
            gateway_owner.revoke_invite(args, state, Path("state"))
            self.assertEqual(api.call_args.args[2], "invites/" + "a" * 32 + "/revoke")

    def test_v2_main_routes_before_runpod_credentials_or_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "state.json").write_text(json.dumps({"gateway": ORIGIN}))
            with (
                patch.object(gateway_owner, "invite"),
                patch.object(hosted, "copy_launchers"),
                patch.object(hosted, "write_invitation_link"),
                patch.object(runpod, "api_key") as key,
                patch.object(runpod, "resolve_pod") as resolve,
                patch.object(
                    hosted.sys,
                    "argv",
                    ["hosted.py", "share", "--state-dir", str(state_dir)],
                ),
            ):
                hosted.main()
            key.assert_not_called()
            resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
