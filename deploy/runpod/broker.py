"""Start one configured Pod and return its SSH endpoint to invited testers."""

import json
import os
import urllib.error
import urllib.request


class PodUnavailable(RuntimeError):
    pass


def handler(job):
    if job.get("input") != {"action": "connect"}:
        return {"error": "This endpoint only accepts a connect request"}
    try:
        return connection()
    except PodUnavailable:
        return {
            "state": "unavailable",
            "message": "The Pod's GPU is no longer available. Ask the owner to replace the Pod.",
        }


def connection():
    pod_id = os.environ["NOTCH_POD_ID"]
    print("Checking hosted Pod", flush=True)
    pod = request("GET", "/pods/" + pod_id)
    if pod["desiredStatus"] == "EXITED":
        request("POST", "/pods/" + pod_id + "/start", {})
        return {"state": "starting", "message": "Starting hosted ComfyUI"}
    if pod["desiredStatus"] != "RUNNING":
        return {"state": "starting", "message": "Waiting for the GPU Pod"}
    host = pod.get("publicIp")
    port = pod.get("portMappings", {}).get("22")
    if not host or not port:
        return {"state": "starting", "message": "Waiting for SSH"}
    return {
        "state": "ready",
        "host": host,
        "ssh_port": int(port),
        "comfy_port": int(os.environ.get("NOTCH_COMFY_PORT", "8188")),
        "host_key": os.environ["NOTCH_SSH_HOST_KEY"],
    }


def request(method, path, body=None):
    req = urllib.request.Request(
        "https://rest.runpod.io/v1" + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": "Bearer " + os.environ["NOTCH_CONTROL_KEY"],
            "Content-Type": "application/json",
            "User-Agent": "ComfyUI-Notch-Runpod/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        if "not enough free GPUs" in error.read(16384).decode("utf-8", errors="replace"):
            raise PodUnavailable() from None
        raise RuntimeError(f"Runpod is unavailable (HTTP {error.code}); retry shortly") from None


if __name__ == "__main__":
    import runpod

    runpod.serverless.start({"handler": handler})
