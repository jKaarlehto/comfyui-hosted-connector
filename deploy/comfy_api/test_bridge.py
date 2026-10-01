import asyncio
import base64
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from .bridge import Bridge
from .bundle import compile_bundle
from .client import RemoteError
from .test_bundle import workflow
from .verify_plugin import verify


def image_bytes():
    target = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(target, format="PNG")
    return target.getvalue()


class FakeComfy:
    """Independent v2 HTTP service: mounted links, bearer auth and assets/jobs."""

    def __init__(self):
        self.submissions, self.uploads, self.keys, self.download_headers = [], [], set(), []
        self.status, self.submit_status, self.poll_failures, self.job_count = "succeeded", 201, 0, 0
        self.return_outputs = True
        self.output_node = "4"
        self.content = image_bytes()
        self.app = web.Application(middlewares=[self.auth])
        self.app.router.add_post("/deployment/demo/api/v2/assets", self.upload)
        self.app.router.add_post("/deployment/demo/api/v2/jobs", self.submit)
        self.app.router.add_get("/deployment/demo/api/v2/jobs/{id}", self.poll)
        self.app.router.add_post("/deployment/demo/api/v2/jobs/{id}/cancel", self.cancel)
        self.app.router.add_get("/deployment/demo/api/v2/assets/output/content", self.content_route)

    @web.middleware
    async def auth(self, request, handler):
        if request.headers.get("Authorization") != "Bearer test-key":
            return web.json_response({"error": {"code": "unauthorized", "message": "sensitive-detail"}}, status=401)
        return await handler(request)

    def job(self):
        root = "/deployment/demo/api/v2/jobs/job-" + str(self.job_count)
        return {
            "id": "job-" + str(self.job_count),
            "status": self.status,
            "urls": {"self": root, "events": root + "/events", "cancel": root + "/cancel"},
            "progress": {"value": 1, "current_node": "4"},
            "outputs": [
                {
                    "node_id": self.output_node,
                    "name": "output.png",
                    "type": "image",
                    "id": "output",
                    "url": "/deployment/demo/api/v2/assets/output/content",
                }
            ]
            if self.return_outputs
            else [],
        }

    async def upload(self, request):
        fields = {}
        async for part in await request.multipart():
            fields[part.name] = bytes(await part.read())
        if set(fields) != {"file", "content_type", "file_path"}:
            return web.json_response({"error": {"code": "invalid_asset"}}, status=422)
        self.uploads.append(fields)
        return web.json_response({"id": "uploaded-image"}, status=201)

    async def submit(self, request):
        key = request.headers.get("Idempotency-Key")
        if not key or key in self.keys:
            return web.json_response({"error": {"code": "idempotency_key_reuse"}}, status=422)
        self.keys.add(key)
        self.submissions.append(await request.json())
        self.job_count += 1
        if self.submit_status != 201:
            return web.json_response({"error": {"code": "upstream_error"}}, status=self.submit_status)
        return web.json_response(self.job(), status=201)

    async def poll(self, request):
        if self.poll_failures:
            self.poll_failures -= 1
            return web.json_response({"error": {"code": "upstream_error"}}, status=503)
        return web.json_response(self.job())

    async def cancel(self, request):
        self.status = "canceled"
        return web.json_response(self.job())

    async def content_route(self, request):
        self.download_headers.append(request.headers.get("Authorization"))
        return web.Response(body=self.content, content_type="image/png")


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.fake = FakeComfy()
        self.provider = TestServer(self.fake.app)
        await self.provider.start_server()
        self.endpoint = str(self.provider.make_url("/deployment/demo"))
        self.bundle = compile_bundle(workflow(), "Resize", self.endpoint)
        await self.open_bridge()

    async def open_bridge(self, key="test-key"):
        self.bridge = Bridge(self.bundle, self.endpoint, key, self.directory.name, poll_interval=0.01)
        self.client = TestClient(TestServer(self.bridge.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.provider.close()
        self.directory.cleanup()

    async def connect(self, client_id="test-client", resume=None):
        socket = await self.client.ws_connect("/ws?clientId=" + client_id)
        await socket.receive_json()
        await socket.send_json(
            {
                "type": "feature_flags",
                "data": {"extension": {"notch_client": {"protocol_version": "0.12.0", "delivery": resume or {}}}},
            }
        )
        flags = await socket.receive_json()
        self.assertEqual(flags["type"], "feature_flags")
        return socket, flags["data"]["extension"]["notch"]["delivery"]

    def payload(self, client_id="test-client"):
        return {
            "prompt": self.bundle["source_prompt"],
            "inputs": {
                "width": {"value": 16, "type": "INT"},
                "image": {
                    "type": "IMAGE",
                    "value": "data:image/png;base64," + base64.b64encode(image_bytes()).decode(),
                },
            },
            "config": {
                "session": {"client_id": client_id},
                "outputs": [
                    {
                        "workflow_output_id": "4",
                        "consumer_id": "beauty",
                        "transport": "http",
                        "http": {"type": "image", "extension": "png"},
                    }
                ],
            },
        }

    async def terminal(self, socket):
        outputs = []
        async with asyncio.timeout(5):
            while True:
                message = await socket.receive_json()
                if message["type"] == "notch-output-ready":
                    outputs.append(message)
                if message["type"] == "notch-execution-terminal":
                    return outputs, message

    async def test_upload_submit_download_and_extension_contract(self):
        socket, stream = await self.connect()
        response = await self.client.post("/notch/parse", json={"prompt": self.bundle["source_prompt"]})
        self.assertEqual(response.status, 200)
        response = await self.client.post("/notch/inject?execute=true", json=self.payload())
        self.assertEqual(response.status, 200, await response.text())
        queued = await response.json()
        self.assertTrue(queued["queued"])
        outputs, terminal = await self.terminal(socket)
        self.assertEqual(terminal["data"]["status"], "success")
        self.assertEqual(len(outputs), 1)
        self.assertEqual(self.fake.submissions[0]["workflow"]["3"]["inputs"]["width"], 16)
        self.assertEqual(
            self.fake.submissions[0]["workflow"]["notch_api_image_2_0"]["inputs"]["image"],
            {"__type": "core/ASSET", "info": {"id": "uploaded-image"}},
        )
        downloaded = await self.client.get(outputs[0]["data"]["url"])
        self.assertEqual(await downloaded.read(), image_bytes())
        self.assertEqual(self.fake.download_headers, ["Bearer test-key"])
        self.assertNotIn("test-key", self.bridge.state.path.read_text())
        self.assertNotIn("Authorization", json.dumps(self.bundle))
        latest = await (await self.client.get("/notch/outputs/latest")).json()
        self.assertEqual(len(latest["outputs"]), 1)
        resolved = await self.client.post(
            "/notch/outputs/attach",
            json={
                "prompt_id": queued["prompt_id"],
                "workflow_output_id": "4",
                "consumer_id": "other-loader",
                "client_id": "test-client",
                "transport": "http",
                "resolve_only": True,
            },
        )
        self.assertEqual(resolved.status, 200)
        url = (await resolved.json())["output"]["url"]
        self.assertEqual(await (await self.client.get(url)).read(), image_bytes())

    async def test_reconnect_and_ack_replays_only_undelivered_events(self):
        socket, stream = await self.connect()
        await self.client.post("/notch/inject?execute=true", json=self.payload())
        outputs, terminal = await self.terminal(socket)
        await socket.send_json(
            {
                "type": "notch-delivery-ack",
                "data": {
                    "delivery_stream_id": stream["delivery_stream_id"],
                    "applied_through_seq": outputs[0]["data"]["seq"],
                },
            }
        )
        await socket.close()
        socket, delivery = await self.connect(
            resume={
                "delivery_stream_id": stream["delivery_stream_id"],
                "applied_through_seq": outputs[0]["data"]["seq"],
            }
        )
        replayed = await socket.receive_json()
        self.assertEqual(replayed, terminal)
        self.assertEqual(delivery["resume_status"], "ok")
        self.assertEqual(len(self.fake.submissions), 1)

    async def test_restart_recovers_known_job_without_new_submission(self):
        self.fake.status = "running"
        socket, stream = await self.connect()
        response = await self.client.post("/notch/inject?execute=true", json=self.payload())
        self.assertEqual(response.status, 200)
        await socket.close()
        # Submission persisted its ID before its follow-up links. Recover that
        # crash window with a job lookup, then resume through the returned URLs.
        next(iter(self.bridge.state.data["jobs"].values())).pop("urls")
        self.bridge.state.save()
        await self.client.close()
        self.fake.status = "succeeded"
        await self.open_bridge()
        socket, delivery = await self.connect()
        outputs, terminal = await self.terminal(socket)
        self.assertEqual(terminal["data"]["status"], "success")
        self.assertEqual(len(self.fake.submissions), 1)
        self.assertEqual(len(outputs), 1)

    async def test_missing_output_is_not_success(self):
        self.fake.return_outputs = False
        socket, _ = await self.connect()
        await self.client.post("/notch/inject?execute=true", json=self.payload())
        _, terminal = await self.terminal(socket)
        self.assertEqual(terminal["data"]["status"], "error")
        self.assertIn("produced no image", terminal["data"]["exception_message"])

    async def test_cancel_has_owner_check_and_uses_embedded_link(self):
        self.fake.status = "running"
        socket, _ = await self.connect()
        response = await self.client.post("/notch/inject?execute=true", json=self.payload())
        queued = await response.json()
        denied = await self.client.post(
            "/notch/cancel", json={"prompt_id": queued["prompt_id"], "client_id": "other-client"}
        )
        self.assertEqual(denied.status, 403)
        cancelled = await self.client.post(
            "/notch/cancel", json={"prompt_id": queued["prompt_id"], "client_id": "test-client"}
        )
        self.assertEqual(cancelled.status, 200)
        _, terminal = await self.terminal(socket)
        self.assertEqual(terminal["data"]["status"], "cancelled")

    async def test_ambiguous_submit_is_saved_and_never_retried(self):
        self.fake.submit_status = 503
        await self.connect()
        response = await self.client.post("/notch/inject?execute=true", json=self.payload())
        self.assertEqual(response.status, 503)
        self.assertEqual(len(self.fake.submissions), 1)
        record = next(iter(self.bridge.state.data["jobs"].values()))
        self.assertFalse(record["finished"])
        self.assertNotIn("id", record)
        await self.client.close()
        await self.open_bridge()
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.fake.submissions), 1)

    async def test_poll_transient_failure_does_not_resubmit(self):
        self.fake.poll_failures = 1
        socket, _ = await self.connect()
        await self.client.post("/notch/inject?execute=true", json=self.payload())
        _, terminal = await self.terminal(socket)
        self.assertEqual(terminal["data"]["status"], "success")
        self.assertEqual(len(self.fake.submissions), 1)

    async def test_wrong_credential_reports_reauthentication_without_leaking_provider_message(self):
        await self.client.close()
        await self.open_bridge("wrong-key")
        await self.connect()
        response = await self.client.post("/notch/inject?execute=true", json=self.payload())
        self.assertEqual(response.status, 401)
        error = await response.text()
        self.assertIn("login", error)
        self.assertNotIn("sensitive-detail", error)
        self.assertNotIn("wrong-key", error)
        self.assertEqual(len(self.fake.submissions), 0)

    async def test_origin_dns_rebinding_and_altered_workflow_are_rejected(self):
        for headers in ({"Origin": "https://example.com"}, {"Host": "attacker.example"}):
            response = await self.client.get("/features", headers=headers)
            self.assertEqual(response.status, 403)
        await self.connect()
        payload = self.payload()
        payload["prompt"] = copy.deepcopy(payload["prompt"])
        payload["prompt"]["3"]["inputs"]["height"] = 256
        response = await self.client.post("/notch/inject?execute=true", json=payload)
        self.assertEqual(response.status, 400)
        self.assertEqual(len(self.fake.submissions), 0)

    async def test_type_failure_precedes_upload_and_submission(self):
        await self.connect()
        payload = self.payload()
        payload["inputs"]["width"]["value"] = True
        response = await self.client.post("/notch/inject?execute=true", json=payload)
        self.assertEqual(response.status, 400)
        self.assertEqual(len(self.fake.uploads), 0)
        self.assertEqual(len(self.fake.submissions), 0)

    async def test_v2_links_resolve_against_origin_and_reject_foreign_hosts(self):
        client = self.bridge.remote
        self.assertEqual(client.link("/deployment/demo/api/v2/jobs/test"), self.endpoint + "/api/v2/jobs/test")
        with self.assertRaises(ValueError):
            client.link("https://other.run.comfy.app/api/v2/jobs/test")

    async def test_custom_node_proof_can_resume_after_reauthentication_without_a_second_job(self):
        self.fake.output_node = "3"
        directory = Path(self.directory.name) / "custom-node-proof"
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RemoteError):
                await verify(self.endpoint, "wrong-key", directory, timeout=5)
            report = await verify(self.endpoint, "test-key", directory, timeout=5)
            resumed = await verify(self.endpoint, "test-key", directory, timeout=5)
        self.assertEqual(report["job_id"], resumed["job_id"])
        self.assertEqual(len(self.fake.submissions), 1)
        self.assertNotIn("test-key", (directory / "verify-plugin.json").read_text())

    async def test_extension_client_native_http_and_websocket_integration(self):
        executable = os.environ.get("NOTCH_CLIENT_SMOKE_EXECUTABLE")
        if not executable:
            self.skipTest("Build extension_client_smoke.cpp and set NOTCH_CLIENT_SMOKE_EXECUTABLE")
        socket = await self.client.ws_connect("/ws?clientId=cpp-client")
        await socket.receive_json()
        process = await asyncio.create_subprocess_exec(
            executable,
            str(self.client.make_url("/")).rstrip("/"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(15):
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    event = json.loads(line)
                    if "send" in event:
                        await socket.send_json(event["send"])
                        if event["send"]["type"] == "feature_flags":
                            reply = await socket.receive_json()
                            process.stdin.write((json.dumps(reply) + "\n").encode())
                            await process.stdin.drain()
                    elif event.get("queued"):
                        while True:
                            reply = await socket.receive_json()
                            process.stdin.write((json.dumps(reply) + "\n").encode())
                            await process.stdin.drain()
                            if reply["type"] == "notch-execution-terminal":
                                break
                    elif event.get("complete"):
                        break
                await process.wait()
            self.assertEqual(process.returncode, 0, (await process.stderr.read()).decode())
            self.assertEqual(len(self.fake.submissions), 1)
            self.assertEqual(len(self.fake.uploads), 1)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


if __name__ == "__main__":
    unittest.main()
