"""Report hosted workspace readiness through a restricted SSH command."""

import json
import urllib.request
from pathlib import Path

STARTUP_STAGES = frozenset({
    "checking_updates", "fetching_plugin", "starting_services", "updating_comfy",
    "installing_dependencies", "preparing_files", "starting_comfy",
})


def startup_status(path, config):
    try:
        with path.open("rb") as source:
            data = source.read(4097)
        if len(data) > 4096:
            return {}
        state = json.loads(data)
        if not isinstance(state, dict) or state.get("stage") not in STARTUP_STAGES:
            return {}
        if type(state.get("error", False)) is not bool:
            return {}
        if any(name in state and state[name] != config[name] for name in ("pod_id", "deployment_id")):
            return {}
        return {"stage": state["stage"], "error": state.get("error", False),
                "pod_id": config["pod_id"], "deployment_id": config["deployment_id"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def get_json(port, route):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{route}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Health response is too large")
        return json.loads(data)


def check(config_path=Path("/opt/notch-health.json"), marker_path=Path("/opt/notch-storage-restored"),
          stage_path=Path("/opt/notch-startup-stage")):
    pending = {"ready": False}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        port = config["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            return {"ready": False}
        if not all(isinstance(config[name], str) and config[name] for name in ("deployment_id", "pod_id", "store")):
            return {"ready": False}
        pending.update(startup_status(stage_path, config))
        if pending.get("error"):
            return pending
        if not marker_path.is_file() or not Path(config["store"]).is_mount():
            return pending
        stats = get_json(port, "/system_stats")
        version = stats["system"]["comfyui_version"]
        if not isinstance(version, str) or not version:
            return pending
        transports = get_json(port, "/features")["extension"]["notch"]["output_transports"]
        if not isinstance(transports, list) or "http" not in transports:
            return pending
        storage = get_json(port, "/hosted_comfyui/storage")
        if not isinstance(storage, dict) or storage.get("phase") not in ("idle", "downloading", "verifying", "error"):
            return pending
        return {"ready": True, "pod_id": config["pod_id"], "deployment_id": config["deployment_id"]}
    except (OSError, ValueError, KeyError, TypeError):
        return pending


if __name__ == "__main__":
    print(json.dumps(check(), separators=(",", ":")))
