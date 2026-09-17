"""Load saved hosted models on demand and report transfer progress."""

import functools
import importlib.util
import json
import os
import re
import sys
import threading
from collections import deque
from pathlib import Path

NODE_CLASS_MAPPINGS = {}


class DownloadTracker:
    def __init__(self):
        self.jobs = deque(maxlen=16)

    def track(self, job_id):
        if isinstance(job_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id) and job_id not in self.jobs:
            self.jobs.append(job_id)

    async def snapshot(self, fetch):
        for job_id in list(self.jobs):
            job = await fetch(job_id)
            if not isinstance(job, dict):
                self.jobs.remove(job_id)
                continue
            models = job.get("models", [])
            active = next((item for item in models if item.get("state") == "downloading"), None)
            if active is None:
                active = next((item for item in models if item.get("state") in ("checking", "pending", "error")), {})
            state = job.get("state")
            phase = "downloading" if active.get("state") == "downloading" else "verifying"
            if state in ("complete", "failed"):
                self.jobs.remove(job_id)
                phase = "idle" if state == "complete" else "error"
            status = {
                "phase": phase,
                "filename": active.get("name") or "Workflow models",
                "completed_bytes": job.get("bytes_downloaded") or 0,
                "total_bytes": job.get("bytes_total") or 0,
            }
            if state == "failed":
                status["error"] = "Model download failed. See ComfyUI for details."
            return status
        return None


class ModelCache:
    def __init__(self, local, store, cache_model):
        self.local = local
        self.store = store
        self.cache_model = cache_model
        self.transfer_lock = threading.Lock()
        self.status_lock = threading.Lock()
        self.status = {"phase": "idle", "filename": "", "completed_bytes": 0, "total_bytes": 0}
        self.paths = {}
        for relative, record in json.loads((local / ".notch-model-links.json").read_text()).items():
            self.paths[str((local / relative).absolute())] = record
            self.paths[str((store / "blobs" / record["sha256"]).absolute())] = record

    def progress(self, phase, filename, completed, total):
        with self.status_lock:
            self.status = {"phase": phase, "filename": filename, "completed_bytes": completed, "total_bytes": total}

    def snapshot(self):
        with self.status_lock:
            return dict(self.status)

    def load(self, filename):
        if not isinstance(filename, (str, os.PathLike)):
            return filename
        record = self.paths.get(str(Path(filename).absolute()))
        if record is None:
            return filename
        with self.transfer_lock:
            try:
                result = self.cache_model(self.local, self.store, record, self.progress)
                if self.snapshot()["phase"] == "error":
                    self.progress("idle", record["path"], record["size"], record["size"])
                return str(result)
            except (OSError, ValueError, RuntimeError):
                with self.status_lock:
                    self.status["phase"] = "error"
                    self.status["error"] = "Model transfer failed. Retry generation to try again."
                raise

    def wrap_loader(self, loader):
        @functools.wraps(loader)
        def load_torch_file(ckpt, *args, **kwargs):
            return loader(self.load(ckpt), *args, **kwargs)

        return load_torch_file

    def install_loader(self, utils):
        original = utils.load_torch_file
        wrapper = self.wrap_loader(original)
        utils.load_torch_file = wrapper
        for name in ("comfy.clip_vision", "comfy.bg_removal_model"):
            module = sys.modules.get(name)
            if getattr(module, "load_torch_file", None) is original:
                module.load_torch_file = wrapper


def install():
    import comfy.utils
    import folder_paths
    from aiohttp import ClientError, ClientSession, ClientTimeout, web
    from server import PromptServer

    spec = importlib.util.spec_from_file_location("hosted_model_store", "/opt/notch-model-store.py")
    model_store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(model_store)
    cache = ModelCache(Path(folder_paths.base_path), Path(os.environ["NOTCH_GLOBAL_STORE"], "notch"), model_store.cache_model)
    cache.install_loader(comfy.utils)
    downloads = DownloadTracker()
    port = int(os.environ.get("NOTCH_COMFY_PORT", "8188"))

    @web.middleware
    async def track_downloads(request, handler):
        response = await handler(request)
        if request.method == "POST" and request.path in ("/notch/models/download", "/api/notch/models/download") and response.status == 202:
            try:
                downloads.track(json.loads(response.body).get("job_id"))
            except (AttributeError, TypeError, ValueError):
                pass
        return response

    PromptServer.instance.app.middlewares.append(track_downloads)

    async def fetch_download(job_id):
        async with ClientSession(timeout=ClientTimeout(total=2), trust_env=False) as session:
            async with session.get(f"http://127.0.0.1:{port}/notch/models/download/{job_id}", allow_redirects=False) as response:
                if response.status == 404:
                    return None
                response.raise_for_status()
                return await response.json()

    @PromptServer.instance.routes.get("/hosted_comfyui/storage")
    async def storage_status(request):
        status = cache.snapshot()
        if status["phase"] not in ("downloading", "verifying"):
            try:
                status = await downloads.snapshot(fetch_download) or status
            except (ClientError, TimeoutError, ValueError):
                pass
        return web.json_response(status, headers={"Cache-Control": "no-store"})


if os.environ.get("NOTCH_GLOBAL_STORE"):
    install()
