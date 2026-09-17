"""Report hosted workspace readiness through a restricted SSH command."""

import json
import urllib.request
from pathlib import Path


def get_json(port, route):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{route}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Health response is too large")
        return json.loads(data)


def check(config_path=Path("/opt/notch-health.json"), marker_path=Path("/opt/notch-storage-restored")):
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        port = config["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            return {"ready": False}
        if not all(isinstance(config[name], str) and config[name] for name in ("deployment_id", "pod_id", "store")):
            return {"ready": False}
        if not marker_path.is_file() or not Path(config["store"]).is_mount():
            return {"ready": False}
        stats = get_json(port, "/system_stats")
        version = stats["system"]["comfyui_version"]
        if not isinstance(version, str) or not version:
            return {"ready": False}
        transports = get_json(port, "/features")["extension"]["notch"]["output_transports"]
        if not isinstance(transports, list) or "http" not in transports:
            return {"ready": False}
        storage = get_json(port, "/hosted_comfyui/storage")
        if not isinstance(storage, dict) or storage.get("phase") not in ("idle", "downloading", "verifying", "error"):
            return {"ready": False}
        return {"ready": True, "pod_id": config["pod_id"], "deployment_id": config["deployment_id"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {"ready": False}


if __name__ == "__main__":
    print(json.dumps(check(), separators=(",", ":")))
