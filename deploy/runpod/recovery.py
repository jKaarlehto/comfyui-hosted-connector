"""Recover one hosted workspace through Runpod's native Pod migration."""

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request

LOCK = threading.Lock()
MIGRATION_FIELDS = "id sourcePodId targetPodId status progress"
CAPACITY_ERRORS = ("not enough free gpus", "no instances currently available", "no longer any instances available")


class CapacityUnavailable(RuntimeError):
    pass


def request(key, method, path, body=None, base="https://api.runpod.io/v2"):
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
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read(16384).decode("utf-8", errors="replace")
        if any(text in detail.lower() for text in CAPACITY_ERRORS):
            raise CapacityUnavailable() from None
        raise RuntimeError(f"Runpod request failed (HTTP {error.code}); retry shortly") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Runpod request timed out; retry shortly") from None


def graphql(key, query, variables=None):
    result = request(key, "POST", "/graphql", {"query": query, "variables": variables or {}}, "https://api.runpod.io")
    if result.get("errors"):
        messages = " ".join(str(error.get("message", "")) for error in result["errors"])
        if any(text in messages.lower() for text in CAPACITY_ERRORS):
            raise CapacityUnavailable() from None
        raise RuntimeError("Runpod could not complete recovery; the owner should check the Pod migration")
    return result["data"]


def connection(key, endpoint_id):
    with LOCK:
        return Recovery(key, endpoint_id).connect()


def replace_pod(key, endpoint_id):
    with LOCK:
        return Recovery(key, endpoint_id).connect(replace=True)


class Recovery:
    def __init__(self, key, endpoint_id):
        self.key = key
        self.endpoint_id = endpoint_id
        endpoint = request(key, "GET", "/serverless/" + endpoint_id)
        if endpoint["workers"]["max"] != 1:
            raise RuntimeError("Hosted recovery requires one starter worker")
        self.env = endpoint["env"]
        self.state = json.loads(self.env.get("NOTCH_RECOVERY") or "{}")
        self.pods = request(key, "GET", "/pods")["pods"]
        self.owned = {
            pod["id"]: pod
            for pod in self.pods
            if pod.get("env", {}).get("NOTCH_DEPLOYMENT_ID") == self.env["NOTCH_DEPLOYMENT_ID"]
        }

    def save(self, state, pod_id=None):
        self.env["NOTCH_RECOVERY"] = json.dumps(state, separators=(",", ":"))
        if pod_id:
            self.env["NOTCH_POD_ID"] = pod_id
        request(self.key, "PATCH", "/serverless/" + self.endpoint_id, {"env": self.env})
        self.state = state

    def connect(self, replace=False):
        if self.state.get("phase") == "failed":
            return {"state": "unavailable", "message": self.state["message"]}
        if self.state.get("phase") == "cleanup":
            return self.finish()
        if self.state.get("migration_id") or self.state.get("phase") == "requesting":
            return self.poll()
        if len(self.owned) != 1:
            raise RuntimeError("Expected one hosted Pod; the owner must inspect this workspace")
        pod = next(iter(self.owned.values()))
        if self.env["NOTCH_POD_ID"] != pod["id"]:
            self.save({}, pod["id"])
        if pod["status"] == "EXITED":
            if self.state.get("retry_after", 0) > time.time():
                return self.waiting_capacity()
            if not replace:
                try:
                    self.action(pod["id"], "start")
                    if self.state:
                        self.save({})
                    return self.starting("Starting hosted ComfyUI")
                except CapacityUnavailable:
                    pass
            return self.begin(pod)
        if replace:
            raise RuntimeError("Stop the Pod before requesting replacement")
        return self.ready(pod["id"])

    def begin(self, pod):
        gpu = pod["gpu"]
        prices = graphql(self.key, "query { gpuTypes { id securePrice } }")["gpuTypes"]
        price = next((item["securePrice"] for item in prices if item["id"] == gpu["id"]), None)
        if pod["cloud"] != "SECURE" or price is None or price * gpu["count"] > float(pod["cost"]) + 0.000001:
            return {"state": "unavailable", "message": "Recovery needs owner review: the GPU price has changed"}
        state = {
            "phase": "requesting",
            "source_id": pod["id"],
            "gpu_id": gpu["id"],
            "gpu_count": gpu["count"],
            "max_cost": pod["cost"],
            "configuration": self.configuration(pod),
            "mounts": self.mounts(pod),
        }
        self.save(state)
        try:
            migration = graphql(
                self.key,
                "mutation ($input: MigratePodInput!) { migratePod(input: $input) { " + MIGRATION_FIELDS + " } }",
                {"input": {"podId": pod["id"]}},
            )["migratePod"]
        except CapacityUnavailable:
            self.save({"phase": "capacity", "retry_after": time.time() + 60})
            return self.waiting_capacity()
        self.record(migration)
        return self.starting("Moving hosted ComfyUI to an available GPU")

    def record(self, migration):
        if migration["sourcePodId"] != self.state["source_id"]:
            raise RuntimeError("Runpod returned a different migration source; owner review required")
        self.save({**self.state, "phase": "migrating", "migration_id": migration["id"]})

    def poll(self):
        if not self.state.get("migration_id"):
            migrations = graphql(self.key, "query { myself { activeMigrations { " + MIGRATION_FIELDS + " } } }")[
                "myself"
            ]["activeMigrations"]
            matches = [item for item in migrations if item["sourcePodId"] == self.state["source_id"]]
            if len(matches) != 1:
                return {
                    "state": "unavailable",
                    "message": "Recovery could not be confirmed. The owner must check Runpod before retrying migration.",
                }
            self.record(matches[0])
        migration = graphql(
            self.key,
            "query ($id: String!) { podMigrationById(migrationId: $id) { " + MIGRATION_FIELDS + " } }",
            {"id": self.state["migration_id"]},
        )["podMigrationById"]
        if (
            not migration
            or migration["id"] != self.state["migration_id"]
            or migration["sourcePodId"] != self.state["source_id"]
        ):
            raise RuntimeError("Runpod migration could not be verified; owner review required")
        if migration["status"] in ("FAILED", "CANCELLED"):
            message = "Runpod migration did not complete. The original Pod is retained; ask the owner to inspect it."
            self.save({**self.state, "phase": "failed", "message": message})
            return {"state": "unavailable", "message": message}
        if migration["status"] != "COMPLETED":
            return self.starting(
                "Migrating hosted ComfyUI (Runpod progress: " + str(migration.get("progress") or 0) + ")"
            )
        self.save({**self.state, "phase": "cleanup", "target_id": migration["targetPodId"]})
        return self.finish()

    def finish(self):
        source_id, target_id = self.state["source_id"], self.state["target_id"]
        if not target_id or target_id == source_id or set(self.owned) - {source_id, target_id}:
            raise RuntimeError("Unexpected Pods in recovery; owner review required")
        target = self.owned.get(target_id)
        if not target:
            if any(pod["id"] == target_id for pod in self.pods):
                raise RuntimeError("The migration target belongs to another workspace; owner review required")
            return self.starting("Waiting for the migrated Pod")
        gpu = target["gpu"]
        if (
            target["cloud"] != "SECURE"
            or gpu["id"] != self.state["gpu_id"]
            or gpu["count"] != self.state["gpu_count"]
            or float(target["cost"]) > float(self.state["max_cost"]) + 0.000001
            or self.configuration(target) != self.state["configuration"]
            or self.mounts(target) != self.state["mounts"]
        ):
            if target["status"] == "RUNNING":
                self.action(target_id, "stop")
            message = (
                "The replacement differs from the original configuration. It is stopped; owner review is required."
            )
            self.save({**self.state, "phase": "failed", "message": message})
            return {"state": "unavailable", "message": message}
        if target["status"] != "RUNNING":
            if target["status"] == "EXITED":
                self.action(target_id, "start")
            return self.starting("Starting the migrated Pod")
        ready = self.ready(target_id)
        if ready["state"] != "ready":
            return ready
        source = self.owned.get(source_id)
        if source:
            if source.get("locked"):
                graphql(
                    self.key,
                    "mutation ($input: PodLockInput!) { podUnlock(input: $input) { id } }",
                    {"input": {"podId": source_id}},
                )
            if source["status"] != "EXITED":
                self.action(source_id, "stop")
                return self.starting("Finishing migration and retiring the old Pod")
            request(self.key, "DELETE", "/pods/" + source_id, base="https://rest.runpod.io/v1")
        self.save({}, target_id)
        return ready

    def mounts(self, pod):
        storage = graphql(
            self.key,
            "query ($input: PodFilter!) { pod(input: $input) { volumeMounts { volumeId volumeType mountPath } } }",
            {"input": {"podId": pod["id"]}},
        )["pod"]
        if storage is None:
            raise RuntimeError("The Pod storage could not be verified; owner review required")
        return {
            "local_network": pod.get("mounts") or {},
            "global": sorted(storage["volumeMounts"] or [], key=lambda item: (item["mountPath"], item["volumeId"])),
        }

    @staticmethod
    def configuration(pod):
        values = {name: pod.get(name) for name in ("image", "args", "disk", "registry", "globalNetworking")}
        values["ports"] = sorted(pod.get("ports") or [])
        values["env"] = {
            name: value
            for name, value in pod.get("env", {}).items()
            if name.startswith("NOTCH_") or name == "PUBLIC_KEY"
        }
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def action(self, pod_id, action):
        return request(self.key, "POST", "/pods/" + pod_id + "/" + action, {}, "https://rest.runpod.io/v1")

    def ready(self, pod_id):
        pod = request(self.key, "GET", "/pods/" + pod_id, base="https://rest.runpod.io/v1")
        host, port = pod.get("publicIp"), (pod.get("portMappings") or {}).get("22")
        if pod["desiredStatus"] != "RUNNING" or not host or not port:
            return self.starting("Waiting for SSH")
        return {
            "state": "ready",
            "host": host,
            "ssh_port": int(port),
            "comfy_port": int(self.env["NOTCH_COMFY_PORT"]),
            "host_key": self.env["NOTCH_SSH_HOST_KEY"],
        }

    @staticmethod
    def starting(message):
        return {"state": "starting", "message": message}

    def waiting_capacity(self):
        return self.starting("Waiting for an available GPU; retrying once a minute")
