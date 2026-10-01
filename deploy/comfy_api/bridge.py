"""Loopback-only Notch HTTP/WebSocket facade over durable Comfy v2 jobs."""

import asyncio
import base64
import copy
import json
import os
import uuid
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web

from .bundle import PROTOCOL, api_prompt, contract, typed_value, validate_bundle
from .client import MAX_BYTES, RemoteError, V2Client
from .images import png


class State:
    def __init__(self, directory, bundle, endpoint):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "state.json"
        identity = {"bundle_id": bundle["bundle_id"], "endpoint": endpoint}
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {**identity, "jobs": {}, "streams": {}}
        if any(self.data.get(k) != v for k, v in identity.items()):
            raise ValueError("This state directory belongs to another bundle/endpoint; select a separate directory")
        self.save()

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, allow_nan=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    def stream(self, client_id):
        streams = self.data["streams"]
        if client_id not in streams:
            if len(streams) >= 64:
                raise ValueError("This bridge has reached its 64-client limit; archive its state to start fresh")
            streams[client_id] = {"id": str(uuid.uuid4()), "seq": 0, "ack": 0, "events": []}
            self.save()
        return streams[client_id]

    def event(self, client_id, event_type, data):
        stream = self.stream(client_id)
        stream["seq"] += 1
        event = {"type": event_type, "data": {**data, "seq": stream["seq"], "delivery_stream_id": stream["id"]}}
        stream["events"].append(event)
        self.save()
        return event

    def ack(self, client_id, payload):
        stream = self.stream(client_id)
        seq = payload.get("applied_through_seq", 0)
        if (
            payload.get("delivery_stream_id") == stream["id"]
            and type(seq) is int
            and stream["ack"] <= seq <= stream["seq"]
        ):
            stream["ack"] = seq
            stream["events"] = [e for e in stream["events"] if e["data"]["seq"] > seq]
            self.save()


class Bridge:
    def __init__(self, bundle, endpoint, api_key, directory, poll_interval=1):
        self.bundle = validate_bundle(bundle)
        self.remote = V2Client(endpoint, api_key)
        self.state = State(directory, bundle, self.remote.endpoint)
        self.poll_interval = poll_interval
        self.sockets = {}
        self.tasks = set()
        self.compatible = set()
        self.workflow_path = "ComfyAPI/" + bundle["bundle_id"][:12] + ".json"
        self.app = web.Application(client_max_size=MAX_BYTES, middlewares=[self.guard])
        self.app.add_routes(
            [
                web.get("/features", self.features),
                web.get("/ws", self.websocket),
                web.get("/userdata", self.list_workflows),
                web.get("/userdata/{path:.*}", self.workflow),
                web.post("/notch/parse", self.parse),
                web.post("/notch/inject", self.inject),
                web.post("/notch/cancel", self.cancel),
                web.post("/notch/transport/probe", self.probe),
                web.post("/notch/plugin-update", self.plugin_update),
                web.post("/notch/working-copy/adopt", self.adopt),
                web.post("/notch/working-copy/status", self.working_copy),
                web.get("/notch/outputs/latest", self.latest),
                web.post("/notch/outputs/attach", self.attach),
                web.get("/notch/output/{consumer_id}/{prompt_id}", self.output),
                web.get("/notch/diagnostics", self.diagnostics),
            ]
        )
        self.app.cleanup_ctx.append(self.lifecycle)

    @web.middleware
    async def guard(self, request, handler):
        # Native Notch transports do not send Origin. A web page must not be
        # able to spend a locally stored key through the loopback bridge.
        if request.headers.get("Origin") or request.host.split(":")[0] not in {"127.0.0.1", "localhost"}:
            return web.json_response({"error": "Only native loopback clients may use this bridge"}, status=403)
        try:
            return await handler(request)
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except RemoteError as exc:
            return web.json_response({"error": str(exc), "code": exc.code}, status=exc.status)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return web.json_response(
                {"error": "Comfy could not be reached; inspect bridge diagnostics before retrying submission"},
                status=502,
            )

    async def lifecycle(self, app):
        async with aiohttp.ClientSession(trust_env=True) as session:
            self.remote.session = session
            for job in self.state.data["jobs"].values():
                if job.get("id") and not job.get("finished"):
                    job.pop("attention", None)
                    self.start(self.watch(job))
            self.start(self.replay_loop())
            yield
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def facts(self):
        return {
            "extension": {
                "notch": {
                    "protocol_version": PROTOCOL,
                    "supports_protocol": ">=0.12.0,<0.13.0",
                    "minimum_client_protocol": PROTOCOL,
                    "instance_name": self.bundle["name"] + " (Comfy API)",
                    "plugin_capabilities": ["http-output", "output-ack", "cancel-generation"],
                    "output_transports": ["http"],
                    "cuda_device_index": -1,
                    "disk_transport_probe": 1,
                    "plugin_update": {"bootstrap_version": 1, "endpoint": "/notch/plugin-update"},
                }
            }
        }

    async def features(self, request):
        return web.json_response(self.facts())

    async def probe(self, request):
        return web.json_response({"disk": False})

    async def plugin_update(self, request):
        data = await request.json()
        ready = data.get("client_id") in self.compatible
        return web.json_response(
            {
                "bootstrap_version": 1,
                "state": "ready" if ready else "incompatible",
                "message": "Local Comfy API bridge; reconnect a protocol 0.12.0 extension client",
            }
        )

    async def list_workflows(self, request):
        return web.json_response([self.workflow_path])

    async def workflow(self, request):
        if request.match_info["path"] != "workflows/" + self.workflow_path:
            raise web.HTTPNotFound()
        return web.json_response({"prompt": self.bundle["source_prompt"]})

    def check_workflow(self, data):
        if api_prompt(data) != self.bundle["source_prompt"]:
            raise ValueError("This endpoint bridge runs only its published workflow; republish changes as a new bundle")
        execution = data.get("config", {}).get("execution", {})
        if execution.get("workflow_source", "request") not in {"request", "live_editor_snapshot_if_available"}:
            raise ValueError("Published workflows have no live editor; use the Request workflow source")
        if execution.get("disabled_groups") or data.get("native_controls") or data.get("replay"):
            raise ValueError("Group bypass, native controls and artifact replay are not supported by the image MVP")

    async def parse(self, request):
        self.check_workflow(await request.json())
        return web.json_response(contract(self.bundle))

    async def adopt(self, request):
        data = await request.json()
        workflow = data.get("workflow", {})
        self.check_workflow(workflow)
        return web.json_response(
            {
                "status": "existing",
                "resource_id": self.bundle["bundle_id"],
                "user_data_path": "workflows/" + self.workflow_path,
            }
        )

    async def working_copy(self, request):
        return web.json_response(
            {
                "exists": True,
                "resource_id": self.bundle["bundle_id"],
                "user_data_path": "workflows/" + self.workflow_path,
                "resource_version": self.bundle["bundle_id"],
            }
        )

    async def payload(self, request):
        if request.content_type != "multipart/form-data":
            return await request.json(), {}, {}
        reader, data, files, metadata, total = await request.multipart(), {}, {}, {}, 0
        async for part in reader:
            raw = await part.read()
            total += len(raw)
            if total > MAX_BYTES:
                raise ValueError("Multipart input exceeds 100 MiB")
            if part.name == "workflow":
                data = json.loads(raw)
            elif part.name == "metadata":
                metadata = json.loads(raw)
            elif part.name == "types":
                types = json.loads(raw)
                if any(self.bundle["inputs"].get(k, {}).get("type") != v for k, v in types.items()):
                    raise ValueError("Uploaded input type differs from its published binding")
            elif part.filename:
                if part.name in files:
                    raise ValueError("Duplicate image upload")
                files[part.name] = (bytes(raw), part.filename)
        return data, files, metadata

    def routes(self, data):
        routes = data.get("config", {}).get("outputs", [])
        known = {o["name"]: o for o in self.bundle["outputs"]}
        result = []
        seen = set()
        for route in routes:
            output_id = route["workflow_output_id"]
            if output_id not in known or output_id in seen or not route.get("consumer_id"):
                raise ValueError("Select each published output once with a nonempty consumer_id")
            seen.add(output_id)
            config = route.get("http", {})
            if (
                route.get("transport") != "http"
                or config.get("type", "image").upper() != "IMAGE"
                or config.get("extension", "png").lower() != "png"
            ):
                raise ValueError("The image MVP delivers HTTP PNG outputs; select HTTP and PNG in Notch")
            accepted = route.get("accepted_formats", [])
            if accepted and not any(x.lower() in {"png", "image/png"} for x in accepted):
                raise ValueError("The selected Notch output must accept PNG")
            result.append(
                {
                    "workflow_output_id": output_id,
                    "node_id": known[output_id]["node_id"],
                    "consumer_id": route["consumer_id"],
                }
            )
        if not result:
            raise ValueError("Select at least one published image output")
        return result

    async def inject(self, request):
        data, files, metadata = await self.payload(request)
        self.check_workflow(data)
        config = data.get("config", {})
        client_id = config.get("session", {}).get("client_id")
        if client_id not in self.compatible:
            raise ValueError("Connect a compatible Notch extension client WebSocket before generating")
        if config.get("input", {}).get("transport", "http") != "http":
            raise ValueError("Comfy API inputs must use HTTP; CUDA/shared paths cannot reach its workers")
        routes = self.routes(data)
        inputs = data.get("inputs", {})
        unknown = (set(inputs) | set(files)) - set(self.bundle["inputs"])
        if unknown:
            raise ValueError("Unpublished inputs: " + ", ".join(sorted(unknown)))
        values, images = {}, {}
        # Validate the whole run before any paid submission or asset upload.
        for name, binding in self.bundle["inputs"].items():
            entry = inputs.get(name, {})
            if "type" in entry and entry["type"] != binding["type"]:
                raise ValueError("Input type differs from its published binding")
            if binding["type"] == "IMAGE":
                if name in files:
                    raw, filename = files[name]
                    descriptor = metadata.get(filename)
                else:
                    value = entry.get("value")
                    if not isinstance(value, str) or not value.startswith("data:image/") or ";base64," not in value:
                        raise ValueError(
                            f"Image {name!r} needs an upload or base64 image data; local/remote paths are not accepted"
                        )
                    raw = base64.b64decode(value.split(",", 1)[1], validate=True)
                    descriptor = None
                images[name] = await asyncio.to_thread(png, raw, descriptor)
            else:
                if "value" not in entry and "default" not in binding:
                    raise ValueError(f"Required published input {name!r} is missing")
                values[name] = typed_value(binding, entry.get("value", binding.get("default")))
        if request.query.get("execute") != "true":
            return web.json_response(
                {"queued": False, "diagnostics": ["Inputs validated locally; remote validation requires submission"]}
            )
        if sum(not j.get("finished") for j in self.state.data["jobs"].values()) >= 32:
            raise ValueError(
                "This bridge already has 32 unfinished jobs; resolve pending submissions before generating"
            )
        for name, (content, _, _) in images.items():
            values[name] = await self.remote.upload(content, str(uuid.uuid4()) + ".png")
        graph = copy.deepcopy(self.bundle["prompt"])
        for name, binding in self.bundle["inputs"].items():
            for target in binding["targets"]:
                graph[target["node_id"]]["inputs"][target["input"]] = values[name]
        key = str(uuid.uuid4())
        job = {
            "key": key,
            "client_id": client_id,
            "routes": routes,
            "outputs": [],
            "finished": False,
            "attention": "Submitting; if interrupted before the job ID is recorded, investigate this key before submitting again",
        }
        self.state.data["jobs"][key] = job
        self.state.save()
        try:
            remote = await self.remote.submit(graph, key)
        except RemoteError as exc:
            # 5xx and idempotency reuse are ambiguous. Definitive rejections
            # consume no job, and can be shown without an unfinished record.
            job["attention"] = str(exc)
            job["finished"] = exc.status < 500 and exc.code != "idempotency_key_reuse"
            self.state.save()
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError):
            job["attention"] = (
                "Submission outcome unknown; do not resubmit. Investigate Comfy using the recorded submission key."
            )
            self.state.save()
            raise
        job["id"] = remote["id"]
        self.state.save()
        job["urls"] = remote["urls"]
        job.pop("attention", None)
        self.state.save()
        self.start(self.watch(job))
        return web.json_response(
            {
                "queued": True,
                "prompt_id": job["id"],
                "prompt": {},
                "notch": {"workflow_source": {"requested": "request", "selected": "request"}},
            }
        )

    async def send(self, client_id, event):
        socket = self.sockets.get(client_id)
        if socket and not socket.closed:
            try:
                await socket.send_json(event)
            except ConnectionError:
                pass

    async def emit(self, job, event_type, data):
        payload = {"prompt_id": job["id"], "resource_id": self.bundle["bundle_id"], **data}
        await self.send(job["client_id"], self.state.event(job["client_id"], event_type, payload))

    async def watch(self, job):
        delay = self.poll_interval
        try:
            if not job.get("urls"):
                snapshot = await self.remote.json(
                    "GET", self.remote.endpoint + "/api/v2/jobs/" + quote(job["id"], safe="")
                )
                job["urls"] = snapshot["urls"]
                self.state.save()
            while True:
                try:
                    remote = await self.remote.json("GET", job["urls"]["self"])
                except (aiohttp.ClientError, asyncio.TimeoutError, RemoteError) as exc:
                    if isinstance(exc, RemoteError) and exc.status in {401, 403, 404, 410}:
                        raise
                    job["attention"] = (
                        "Comfy is temporarily unavailable; polling this existing job without resubmission"
                    )
                    self.state.save()
                    await asyncio.sleep(delay)
                    delay = min(30, max(delay * 2, 1))
                    continue
                job.pop("attention", None)
                delay = self.poll_interval
                progress = remote.get("progress")
                if progress:
                    await self.send(
                        job["client_id"],
                        {
                            "type": "progress",
                            "data": {
                                "prompt_id": job["id"],
                                "node": progress.get("current_node"),
                                "value": round(progress["value"] * 1000),
                                "max": 1000,
                            },
                        },
                    )
                if remote["status"] in {"succeeded", "failed", "canceled", "expired"}:
                    break
                await asyncio.sleep(delay)
            if remote["status"] == "succeeded":
                await self.deliver(job, remote.get("outputs", []))
                status, message = "success", ""
            else:
                status = "cancelled" if remote["status"] == "canceled" else "error"
                message = "Comfy job " + remote["status"]
            await self.emit(job, "notch-execution-terminal", {"status": status, "exception_message": message})
            job["finished"] = True
        except (RemoteError, ValueError, aiohttp.ClientError, asyncio.TimeoutError, KeyError) as exc:
            # Keep the remote ID for recovery; never hide the job or submit anew.
            message = (
                str(exc)
                if isinstance(exc, (RemoteError, ValueError))
                else "Comfy result retrieval failed; restart the bridge to resume this recorded job"
            )
            job["attention"] = message
            if isinstance(exc, ValueError) or isinstance(exc, RemoteError) and exc.status in {404, 410}:
                job["finished"] = True
            await self.emit(job, "notch-execution-terminal", {"status": "error", "exception_message": message})
        finally:
            self.state.save()

    async def deliver(self, job, outputs):
        for route in job["routes"]:
            if any(o["workflow_output_id"] == route["workflow_output_id"] for o in job["outputs"]):
                continue
            matches = [o for o in outputs if o.get("node_id") == route["node_id"] and o.get("type") == "image"]
            if len(matches) != 1:
                raise ValueError(
                    "Selected output "
                    + route["workflow_output_id"]
                    + (
                        " produced no image"
                        if not matches
                        else " produced an image batch; the image MVP requires one image per output"
                    )
                )
            raw = await self.remote.download(matches[0]["url"])
            content, width, height = await asyncio.to_thread(png, raw)
            file_name = str(uuid.uuid4()) + ".png"
            path = self.state.directory / file_name
            path.write_bytes(content)
            os.chmod(path, 0o600)
            output = {
                "prompt_id": job["id"],
                "workflow_output_id": route["workflow_output_id"],
                "consumer_id": route["consumer_id"],
                "transport": "http",
                "type": "image",
                "format": "png",
                "width": width,
                "height": height,
                "url": "/notch/output/"
                + quote(route["consumer_id"], safe="")
                + "/"
                + quote(job["id"], safe="")
                + "?workflow_output_id="
                + quote(route["workflow_output_id"], safe=""),
            }
            # Persist the local output and its outbox event in one state write,
            # so a restart cannot silently lose the notification.
            job["outputs"].append({**output, "file": file_name})
            await self.emit(job, "notch-output-ready", output)

    async def cancel(self, request):
        data = await request.json()
        job = next((j for j in self.state.data["jobs"].values() if j.get("id") == data.get("prompt_id")), None)
        if not job:
            raise web.HTTPNotFound()
        if job["client_id"] != data.get("client_id"):
            raise web.HTTPForbidden()
        await self.remote.json("POST", job["urls"]["cancel"])
        return web.json_response({"cancelled": True})

    async def latest(self, request):
        client_id = request.query.get("client_id")
        outputs = {}
        for job in self.state.data["jobs"].values():
            if client_id and job["client_id"] != client_id:
                continue
            for output in job["outputs"]:
                outputs[output["workflow_output_id"]] = self.public_output(output)
        return web.json_response({"outputs": list(outputs.values())})

    async def attach(self, request):
        data = await request.json()
        if data.get("transport") != "http" or not data.get("consumer_id"):
            raise ValueError("Published image outputs can only be resolved through HTTP")
        for job in self.state.data["jobs"].values():
            if job.get("id") != data.get("prompt_id"):
                continue
            source = next(
                (o for o in job["outputs"] if o["workflow_output_id"] == data.get("workflow_output_id")), None
            )
            if source:
                output = {**source, "consumer_id": data["consumer_id"]}
                output["url"] = (
                    "/notch/output/"
                    + quote(output["consumer_id"], safe="")
                    + "/"
                    + quote(job["id"], safe="")
                    + "?workflow_output_id="
                    + quote(output["workflow_output_id"], safe="")
                )
                if not any(
                    o["consumer_id"] == output["consumer_id"]
                    and o["workflow_output_id"] == output["workflow_output_id"]
                    for o in job["outputs"]
                ):
                    job["outputs"].append(output)
                    self.state.save()
                if not data.get("resolve_only"):
                    await self.send(
                        data.get("client_id"), {"type": "notch-output-ready", "data": self.public_output(output)}
                    )
                return web.json_response({"attached": True, "output": self.public_output(output)})
        raise web.HTTPNotFound()

    @staticmethod
    def public_output(output):
        return {k: v for k, v in output.items() if k != "file"}

    async def output(self, request):
        for job in self.state.data["jobs"].values():
            for output in job["outputs"]:
                if (
                    output["prompt_id"] == request.match_info["prompt_id"]
                    and output["consumer_id"] == request.match_info["consumer_id"]
                    and output["workflow_output_id"] == request.query.get("workflow_output_id")
                ):
                    return web.FileResponse(
                        self.state.directory / output["file"], headers={"Content-Type": "image/png"}
                    )
        raise web.HTTPNotFound()

    async def diagnostics(self, request):
        return web.json_response(
            {
                "backend": "comfy-api-v2",
                "bundle_id": self.bundle["bundle_id"],
                "jobs": [
                    {k: j[k] for k in ("key", "id", "finished", "attention") if k in j}
                    for j in self.state.data["jobs"].values()
                ],
            }
        )

    async def replay_loop(self):
        while True:
            await asyncio.sleep(2)
            for client_id in list(self.sockets):
                for event in self.state.stream(client_id)["events"]:
                    await self.send(client_id, event)

    async def websocket(self, request):
        client_id = request.query.get("clientId")
        if not client_id or len(client_id) > 200:
            raise ValueError("WebSocket requires a clientId of at most 200 characters")
        stream = self.state.stream(client_id)
        socket = web.WebSocketResponse(heartbeat=30, max_msg_size=1024 * 1024)
        await socket.prepare(request)
        previous = self.sockets.get(client_id)
        if previous:
            await previous.close()
        self.sockets[client_id] = socket
        await socket.send_json(
            {"type": "status", "data": {"sid": client_id, "status": {"exec_info": {"queue_remaining": 0}}}}
        )
        try:
            async for message in socket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                data = payload.get("data", {})
                if payload.get("type") == "feature_flags":
                    facts = data.get("extension", {}).get("notch_client", {})
                    if facts.get("protocol_version") != PROTOCOL:
                        await socket.close(code=1008, message=b"Notch protocol mismatch; use protocol 0.12.0")
                        break
                    self.compatible.add(client_id)
                    resume = facts.get("delivery", {})
                    self.state.ack(client_id, resume)
                    reply = self.facts()
                    reply["extension"]["notch"]["delivery"] = {
                        "delivery_stream_id": stream["id"],
                        "resume_status": "ok" if resume.get("delivery_stream_id") == stream["id"] else "stream_reset",
                        "first_available_seq": stream["ack"] + 1,
                    }
                    await socket.send_json({"type": "feature_flags", "data": reply})
                    for event in stream["events"]:
                        await socket.send_json(event)
                elif payload.get("type") == "notch-delivery-ack":
                    self.state.ack(client_id, data)
        finally:
            if self.sockets.get(client_id) is socket:
                self.sockets.pop(client_id, None)
                self.compatible.discard(client_id)
        return socket
