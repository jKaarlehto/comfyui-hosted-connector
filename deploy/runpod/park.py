"""Stop this Pod after ComfyUI is idle, using Runpod's own Pod-scoped credential."""

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path


def main():
    idle_seconds = float(os.environ.get("NOTCH_IDLE_MINUTES", "20")) * 60
    maximum_seconds = float(os.environ.get("NOTCH_MAX_MINUTES", "240")) * 60
    port = int(os.environ.get("NOTCH_COMFY_PORT", "8188"))
    started = last_busy = time.monotonic()
    last_history = None
    ready = False
    while True:
        now = time.monotonic()
        try:
            queue = get_json(f"http://127.0.0.1:{port}/queue")
            history = get_json(f"http://127.0.0.1:{port}/history?max_items=1")
            current_history = tuple(history)
            if not ready or queue.get("queue_running") or queue.get("queue_pending") or current_history != last_history:
                last_busy = now
            if model_download_active():
                last_busy = now
            last_history = current_history
            ready = True
        except (OSError, ValueError):
            ready = False
            last_busy = now
        idle = ready and idle_seconds > 0 and now - last_busy >= idle_seconds
        expired = maximum_seconds > 0 and now - started >= maximum_seconds
        if idle or expired:
            print(
                "[Notch park] " + ("Idle limit" if idle else "Runtime limit") + "; saving files and stopping Pod",
                flush=True,
            )
            store = os.environ.get("NOTCH_GLOBAL_STORE")
            if store:
                result = subprocess.run(
                    [
                        "python3",
                        "/opt/notch-model-store.py",
                        "--local",
                        "/workspace/runpod-slim/ComfyUI",
                        "--store",
                        store + "/notch",
                        "--mode",
                        "sync",
                    ]
                )
                if result.returncode:
                    print("[Notch park] Storage sync failed; retrying before stopping", flush=True)
                    time.sleep(30)
                    continue
            try:
                stop_pod()
                return
            except (OSError, ValueError, RuntimeError) as error:
                print(f"[Notch park] Stop failed; retrying: {error}", flush=True)
        time.sleep(15)


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def model_download_active():
    root = Path("/workspace/runpod-slim/ComfyUI/models")
    return any(time.time() - path.stat().st_mtime < 120 for path in root.rglob("*.part") if path.is_file())


def stop_pod():
    request = urllib.request.Request(
        "https://api.runpod.io/graphql",
        data=json.dumps(
            {
                "query": "mutation ($input: PodStopInput!) { podStop(input: $input) { id } }",
                "variables": {"input": {"podId": os.environ["RUNPOD_POD_ID"]}},
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ["RUNPOD_API_KEY"],
            "User-Agent": "ComfyUI-Notch-Runpod/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get("errors"):
        raise RuntimeError("; ".join(error["message"] for error in result["errors"]))


if __name__ == "__main__":
    main()
