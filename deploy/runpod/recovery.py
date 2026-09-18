"""Reconcile one hosted workspace while retaining its global persistent storage."""

import base64
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

LOCK = threading.Lock()
DEADLINE = threading.local()
CAPACITY_ERRORS = (
    "not enough free gpus",
    "no instances currently available",
    "no longer any instances available",
)
FALLBACK_GPUS = [
    "NVIDIA L40S",
    "NVIDIA RTX 6000 Ada Generation",
    "NVIDIA RTX A6000",
    "NVIDIA A40",
]
STARTUP_MESSAGES = {
    "checking_updates": ("Checking for updates", "Could not check for updates; the owner must check GitHub access"),
    "fetching_plugin": ("Downloading Notch plugin", "Could not download the Notch plugin; the owner must check GitHub access and the deploy key"),
    "starting_services": ("Starting connection services", "Could not start connection services; contact the owner"),
    "updating_comfy": ("Updating ComfyUI", "Could not update ComfyUI; the owner must check GitHub access"),
    "installing_dependencies": ("Installing ComfyUI dependencies", "Could not install ComfyUI dependencies; contact the owner"),
    "preparing_files": ("Preparing saved files", "Could not prepare persistent files; the owner must check storage"),
    "starting_comfy": ("Starting ComfyUI", "ComfyUI failed to start; the owner must check the startup log"),
}


def connection(key, endpoint_id):
    with LOCK:
        DEADLINE.value = time.monotonic() + 48
        try:
            return Recovery(key, endpoint_id).connect()
        except UncertainRequest:
            return starting("Runpod is taking longer to respond; checking again shortly")
        finally:
            del DEADLINE.value


def replace_pod(key, endpoint_id):
    job = request(
        key,
        "POST",
        "/" + endpoint_id + "/run",
        {"input": {"action": "connect"}},
        "https://api.runpod.ai/v2",
    )
    return {
        "state": "starting",
        "message": "Recovery requested through the starter",
        "job_id": job["id"],
    }


class Recovery:
    def __init__(self, key, endpoint_id):
        self.key = key
        endpoint = request(key, "GET", "/serverless/" + endpoint_id)
        if endpoint["workers"]["max"] != 1:
            raise RuntimeError("Hosted recovery requires one starter worker")
        self.env = endpoint["env"]
        self.template_id = self.env["NOTCH_TEMPLATE_ID"]
        self.template = request(key, "GET", "/templates/" + self.template_id)
        if self.template["env"].get("NOTCH_DEPLOYMENT_ID") != self.env["NOTCH_DEPLOYMENT_ID"]:
            raise RuntimeError("The configured template belongs to another workspace")
        self.state = json.loads(self.template["env"].get("NOTCH_RECOVERY") or "{}")
        self.refresh()

    def refresh(self):
        inventory = request(self.key, "GET", "/pods")
        pagination = inventory.get("pagination") or {}
        if pagination.get("hasNextPage") or pagination.get("nextCursor"):
            raise UncertainRequest("Runpod returned a partial Pod inventory")
        pods = inventory["pods"]
        if not isinstance(pods, list) or any(not isinstance(pod, dict) or not pod.get("id") for pod in pods):
            raise RuntimeError("Runpod returned an incomplete Pod inventory")
        self.owned = {
            pod["id"]: pod
            for pod in pods
            if pod.get("env", {}).get("NOTCH_DEPLOYMENT_ID") == self.env["NOTCH_DEPLOYMENT_ID"]
        }

    def save(self, state):
        env = {
            **self.template["env"],
            "NOTCH_RECOVERY": json.dumps(state, separators=(",", ":")),
        }
        request(self.key, "PATCH", "/templates/" + self.template_id, {"env": env})
        self.template["env"] = env
        self.state = state

    def connect(self):
        phase = self.state.get("phase")
        if phase == "failed":
            return {"state": "unavailable", "message": self.state["message"]}
        if phase in ("requesting", "verifying", "cleanup"):
            return self.reconcile()
        attempts = self.state.get("attempts", [])
        if phase == "capacity" and any(
            pod["id"] != self.state.get("source_id") and pod.get("name") in attempts for pod in self.owned.values()
        ):
            self.save({**self.state, "phase": "requesting"})
            return self.reconcile()
        if phase == "complete" and self.owned and set(self.owned) != {self.state["pod_id"]}:
            self.save(
                {
                    **self.state,
                    "phase": "verifying",
                    "target_id": self.state["pod_id"],
                    "source_id": None,
                }
            )
            return self.reconcile()
        if len(self.owned) > 1:
            raise RuntimeError("Unexpected Pods in this workspace; owner review required")
        pod = next(iter(self.owned.values()), None)
        if pod and pod["status"] != "EXITED":
            return self.ready(pod["id"])
        if self.state.get("retry_after", 0) > time.time():
            return starting("Waiting for GPU capacity; retrying once a minute")
        if pod and phase != "capacity":
            if pod.get("args") != self.template.get("args"):
                if self.mounts(pod) != self.expected_mounts():
                    raise RuntimeError("The stopped Pod storage differs; refusing to change its configuration")
                request(self.key, "PATCH", "/pods/" + pod["id"], {"templateId": self.template_id})
                return starting("Applying updated startup settings before starting ComfyUI")
            try:
                self.action(pod["id"], "start")
                return starting("Starting hosted ComfyUI")
            except CapacityUnavailable:
                pass
        return self.allocate(pod)

    def allocate(self, source):
        if source and (source["status"] != "EXITED" or self.mounts(source) != self.expected_mounts()):
            raise RuntimeError("Replacement requires a stopped Pod using the configured global storage")
        tiers = json.loads(self.env["NOTCH_GPU_TIERS"])
        prices = graphql(self.key, "query { gpuTypes { id securePrice } }")["gpuTypes"]
        prices = {item["id"]: item.get("securePrice") for item in prices}
        index = int(self.state.get("gpu_index", 0))
        while index < len(tiers):
            price = prices.get(tiers[index])
            if price is not None and 0 < float(price) <= float(self.env["NOTCH_MAX_COST"]):
                break
            index += 1
        if index >= len(tiers):
            self.save(
                {
                    **self.state,
                    "phase": "capacity",
                    "gpu_index": 0,
                    "retry_after": time.time() + 60,
                }
            )
            return starting("Waiting for GPU capacity within the configured price limit")
        state = {
            "phase": "requesting",
            "source_id": source["id"] if source else None,
            "attempts": self.state.get("attempts", []) + [self.template["name"] + "-" + uuid.uuid4().hex],
            "gpu_id": tiers[index],
            "gpu_index": index,
            "requested_at": time.time(),
            "checks": 0,
            "last_check": 0,
        }
        self.save(state)
        current = request(self.key, "GET", "/templates/" + self.template_id)
        if json.loads(current["env"].get("NOTCH_RECOVERY") or "{}") != state:
            return starting("Another recovery request is being reconciled")
        specification = {
            "name": state["attempts"][-1],
            "templateId": self.template_id,
            "cloudType": "SECURE",
            "gpuTypeId": state["gpu_id"],
            "gpuCount": 1,
            "containerDiskInGb": self.template["disk"],
            "volumeInGb": 0,
            "startSsh": True,
            "supportPublicIp": True,
            "allowedCudaVersions": ["12.8", "13.0", "13.2"],
            "volumeMounts": self.expected_mounts()["global"],
        }
        minutes = int(self.template["env"].get("NOTCH_MAX_MINUTES", "240"))
        if minutes > 0:
            specification["stopAfter"] = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        try:
            target = graphql(
                self.key,
                "mutation ($input: PodFindAndDeployOnDemandInput!) { podFindAndDeployOnDemand(input: $input) { id } }",
                {"input": specification},
            )["podFindAndDeployOnDemand"]
        except CapacityUnavailable:
            self.save({**state, "phase": "capacity", "gpu_index": index + 1})
            return starting("That GPU is unavailable; checking the next configured GPU")
        except RejectedRequest:
            message = "Runpod rejected the replacement configuration; the owner must check the starter settings"
            self.save({**state, "phase": "failed", "message": message})
            return {"state": "unavailable", "message": message}
        except UncertainRequest:
            return starting("Checking whether Runpod accepted the replacement")
        if not isinstance(target, dict) or not target.get("id"):
            return starting("Checking whether Runpod accepted the replacement")
        self.save({**state, "phase": "verifying", "target_id": target["id"]})
        return starting("Preparing a replacement GPU and restoring persistent files")

    def reconcile(self):
        source_id = self.state.get("source_id")
        names = self.state.get("attempts", [])
        candidates = [pod for pod in self.owned.values() if pod["id"] != source_id and pod.get("name") in names]
        known = {pod["id"] for pod in candidates} | {
            source_id,
            self.state.get("target_id"),
        }
        if set(self.owned) - known:
            raise RuntimeError("Unexpected Pods during recovery; owner review required")
        target_id = self.state.get("target_id")
        target = self.owned.get(target_id) if target_id else None
        if target and target not in candidates:
            raise RuntimeError("The replacement does not match the recorded allocation")
        if not target and candidates:
            target = min(candidates, key=lambda pod: (names.index(pod["name"]), pod["id"]))
            self.save({**self.state, "phase": "verifying", "target_id": target["id"]})
        if not target:
            now = time.time()
            if now - self.state.get("last_check", 0) >= 20:
                self.save(
                    {
                        **self.state,
                        "checks": self.state.get("checks", 0) + 1,
                        "last_check": now,
                    }
                )
            if now - self.state["requested_at"] >= 120 and self.state.get("checks", 0) >= 3:
                self.save(
                    {
                        **self.state,
                        "phase": "capacity",
                        "gpu_index": self.state["gpu_index"] + 1,
                    }
                )
            return starting("Reconciling the replacement with Runpod before retrying")
        for candidate in candidates:
            if candidate["id"] != target["id"]:
                if not self.valid_target(candidate):
                    raise RuntimeError("Duplicate allocation configuration differs; owner review required")
                if candidate["status"] != "EXITED":
                    self.action(candidate["id"], "stop")
                    return starting("Stopping a duplicate recovery allocation")
                request(
                    self.key,
                    "DELETE",
                    "/pods/" + candidate["id"],
                    base="https://rest.runpod.io/v1",
                )
                return starting("Retiring a duplicate recovery allocation")
        if not self.valid_target(target):
            if target["status"] != "EXITED":
                self.action(target["id"], "stop")
            message = "The replacement configuration or price differs; it is stopped for owner review"
            self.save({**self.state, "phase": "failed", "message": message})
            return {"state": "unavailable", "message": message}
        if target["status"] == "EXITED":
            try:
                self.action(target["id"], "start")
                return starting("Starting the replacement GPU")
            except CapacityUnavailable:
                request(
                    self.key,
                    "DELETE",
                    "/pods/" + target["id"],
                    base="https://rest.runpod.io/v1",
                )
                self.save(
                    {
                        **self.state,
                        "phase": "capacity",
                        "gpu_index": self.state["gpu_index"] + 1,
                    }
                )
                return starting("The replacement GPU became unavailable; trying another")
        ready = self.ready(target["id"])
        if ready["state"] != "ready":
            return ready
        source = self.owned.get(source_id)
        if source:
            if source["status"] != "EXITED":
                self.action(source_id, "stop")
                return starting("Stopping the original Pod before completing recovery")
            if self.mounts(source) != self.expected_mounts():
                raise RuntimeError("Original Pod storage changed; refusing to delete it")
            self.save({**self.state, "phase": "cleanup"})
            request(
                self.key,
                "DELETE",
                "/pods/" + source_id,
                base="https://rest.runpod.io/v1",
            )
        self.save({"phase": "complete", "pod_id": target["id"], "attempts": names})
        return ready

    def valid_target(self, target):
        wanted = self.template
        return (
            target["cloud"] == "SECURE"
            and target["gpu"]["count"] == 1
            and target["gpu"]["id"] in json.loads(self.env["NOTCH_GPU_TIERS"])
            and 0 < float(target["cost"]) <= float(self.env["NOTCH_MAX_COST"])
            and all(target.get(name) == wanted.get(name) for name in ("image", "args", "disk"))
            and sorted(target.get("ports") or []) == sorted(wanted.get("ports") or [])
            and all(
                target.get("env", {}).get(name) == value
                for name, value in wanted["env"].items()
                if name != "NOTCH_RECOVERY"
            )
            and self.mounts(target) == self.expected_mounts()
        )

    def expected_mounts(self):
        if not self.env.get("NOTCH_VOLUME_ID"):
            raise RuntimeError("Automatic replacement requires global persistent storage")
        return {
            "local_network": {},
            "global": [
                {
                    "volumeId": self.env["NOTCH_VOLUME_ID"],
                    "volumeType": "OBJECT_STORE_VOLUME",
                    "mountPath": "/workspace-global",
                }
            ],
        }

    def mounts(self, pod):
        storage = graphql(
            self.key,
            "query ($input: PodFilter!) { pod(input: $input) { volumeMounts { volumeId volumeType mountPath } } }",
            {"input": {"podId": pod["id"]}},
        )["pod"]
        if storage is None:
            raise RuntimeError("The Pod storage could not be verified")
        return {
            "local_network": pod.get("mounts") or {},
            "global": sorted(
                storage["volumeMounts"] or [],
                key=lambda item: (item["mountPath"], item["volumeId"]),
            ),
        }

    def action(self, pod_id, action):
        return request(
            self.key,
            "POST",
            "/pods/" + pod_id + "/" + action,
            {},
            "https://rest.runpod.io/v1",
        )

    def ready(self, pod_id):
        pod = request(self.key, "GET", "/pods/" + pod_id, base="https://rest.runpod.io/v1")
        host, port = pod.get("publicIp"), (pod.get("portMappings") or {}).get("22")
        if pod["desiredStatus"] != "RUNNING" or not host or not port:
            return starting("Waiting for SSH")
        if getattr(DEADLINE, "value", float("inf")) - time.monotonic() < 20:
            return starting("Checking ComfyUI readiness on the next connection request")
        status = health_status(self.env, pod_id, host, int(port))
        if not status["ready"]:
            messages = STARTUP_MESSAGES.get(status.get("stage"))
            if messages:
                if status.get("error"):
                    return {"state": "unavailable", "message": messages[1]}
                return starting(messages[0])
            return starting("Waiting for the server's startup status")
        return {
            "state": "ready",
            "host": host,
            "ssh_port": int(port),
            "comfy_port": int(self.env["NOTCH_COMFY_PORT"]),
            "host_key": self.env["NOTCH_SSH_HOST_KEY"],
        }


def validate_health(result, env, pod_id):
    pending = {"ready": False}
    if not isinstance(result, dict):
        return pending
    identity = {"pod_id": pod_id, "deployment_id": env["NOTCH_DEPLOYMENT_ID"]}
    if result.get("ready") is True:
        return {"ready": True} if result == {"ready": True, **identity} else pending
    if result.get("ready") is not False or any(name in result and result[name] != value for name, value in identity.items()):
        return pending
    stage = result.get("stage")
    if not isinstance(stage, str) or stage not in STARTUP_MESSAGES or type(result.get("error", False)) is not bool:
        return pending
    return {"ready": False, "stage": stage, "error": result.get("error", False)}


def health_status(env, pod_id, host, port):
    import paramiko

    client = paramiko.SSHClient()
    try:
        public = env["NOTCH_SSH_HOST_KEY"].split()
        client.get_host_keys().add(
            f"[{host}]:{port}" if port != 22 else host,
            public[0],
            paramiko.Ed25519Key(data=base64.b64decode(public[1])),
        )
        private = base64.b64decode(os.environ["NOTCH_HEALTH_KEY_B64"]).decode()
        identity = paramiko.Ed25519Key.from_private_key(io.StringIO(private))
        client.connect(
            host,
            port=port,
            username="root",
            pkey=identity,
            timeout=5,
            auth_timeout=5,
            banner_timeout=5,
            allow_agent=False,
            look_for_keys=False,
        )
        _, output, _ = client.exec_command("health", timeout=8)
        data = output.read(16385)
        return validate_health(json.loads(data), env, pod_id) if len(data) <= 16384 else {"ready": False}
    except (OSError, ValueError, KeyError, paramiko.SSHException):
        return {"ready": False}
    finally:
        client.close()


def starting(message):
    return {"state": "starting", "message": message}


class CapacityUnavailable(RuntimeError):
    pass


class RejectedRequest(RuntimeError):
    pass


class UncertainRequest(RuntimeError):
    pass


def request(key, method, path, body=None, base="https://api.runpod.io/v2"):
    remaining = getattr(DEADLINE, "value", float("inf")) - time.monotonic()
    if remaining < 2:
        raise UncertainRequest("Continuing recovery on the next connection request")
    req = urllib.request.Request(
        base + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "ComfyUI-Notch-Runpod/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=min(8, remaining)) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read(16384).decode("utf-8", errors="replace")
        if any(text in detail.lower() for text in CAPACITY_ERRORS):
            raise CapacityUnavailable() from None
        if 400 <= error.code < 500 and error.code not in (408, 429):
            raise RejectedRequest(f"Runpod rejected the request (HTTP {error.code})") from None
        raise UncertainRequest("Runpod could not confirm the request") from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise UncertainRequest("Runpod could not confirm the request") from None


def graphql(key, query, variables=None):
    result = request(
        key,
        "POST",
        "/graphql",
        {"query": query, "variables": variables or {}},
        "https://api.runpod.io",
    )
    if result.get("errors"):
        messages = " ".join(str(error.get("message", "")) for error in result["errors"])
        if any(text in messages.lower() for text in CAPACITY_ERRORS):
            raise CapacityUnavailable() from None
        if result.get("data") and any(result["data"].values()):
            raise UncertainRequest("Runpod returned an incomplete result")
        codes = {error.get("extensions", {}).get("code") for error in result["errors"]}
        if codes and codes <= {
            "GRAPHQL_VALIDATION_FAILED",
            "BAD_USER_INPUT",
            "UNAUTHENTICATED",
            "FORBIDDEN",
        }:
            raise RejectedRequest("Runpod rejected the operation")
        raise UncertainRequest("Runpod could not confirm the operation")
    return result["data"]
