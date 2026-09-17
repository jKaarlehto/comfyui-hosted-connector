"""Check demand loading without a GPU or cloud access."""

import importlib.util
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import model_cache


class DownloadTrackerTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_download_progress_and_completion(self):
        tracker = model_cache.DownloadTracker()
        tracker.track("job-1")
        tracker.track("../../unexpected")
        tracker.track(None)
        fetch = AsyncMock(return_value={"state": "running", "bytes_downloaded": 50, "bytes_total": 100, "models": [{"name": "new.safetensors", "state": "downloading"}]})
        status = await tracker.snapshot(fetch)
        self.assertEqual(status, {"phase": "downloading", "filename": "new.safetensors", "completed_bytes": 50, "total_bytes": 100})
        fetch.assert_awaited_once_with("job-1")
        fetch.return_value = {"state": "complete", "bytes_downloaded": 100, "bytes_total": 100}
        self.assertEqual((await tracker.snapshot(fetch))["phase"], "idle")
        self.assertIsNone(await tracker.snapshot(fetch))

    async def test_failed_download_and_lost_job(self):
        tracker = model_cache.DownloadTracker()
        tracker.track("gone")
        tracker.track("failed")
        fetch = AsyncMock(side_effect=[None, {"state": "failed", "models": [{"name": "bad.safetensors", "state": "error"}]}])
        status = await tracker.snapshot(fetch)
        self.assertEqual(status["phase"], "error")
        self.assertEqual(status["filename"], "bad.safetensors")
        self.assertIsNone(await tracker.snapshot(fetch))


class ModelCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import fcntl
        except ImportError:
            fcntl = types.SimpleNamespace(flock=lambda *args: None, LOCK_EX=2)
        spec = importlib.util.spec_from_file_location("model_store", Path(__file__).with_name("model_store.py"))
        cls.store_module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"fcntl": fcntl}):
            spec.loader.exec_module(cls.store_module)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.source = root / "source"
        self.local = root / "local"
        self.store = root / "store"
        for folder in (self.source / "models", self.store / "blobs", self.store / "files"):
            folder.mkdir(parents=True)
        probe = root / "link"
        try:
            probe.symlink_to(self.source)
            probe.unlink()
        except OSError:
            self.skipTest("Filesystem symlink support is required; run this suite on Linux")
        for name, content in (("first.safetensors", b"first model"), ("second.safetensors", b"second model")):
            (self.source / "models" / name).write_bytes(content)
        (self.source / "user/default/workflows").mkdir(parents=True)
        (self.source / "user/default/workflows/test.json").write_text("{}")
        self.store_module.sync(self.source, self.store, minimum_age=0)

    def prepare(self):
        self.store_module.restore(self.local, self.store, lazy_models=True)
        return model_cache.ModelCache(self.local, self.store, self.store_module.cache_model)

    def test_startup_reads_no_model_bytes_and_restores_workflow(self):
        original_hash = self.store_module.hash_file
        original_copy = self.store_module.shutil.copyfile
        model_digests = {self.store_module.hash_file(path) for path in (self.source / "models").iterdir()}

        def copy(source, destination):
            self.assertNotIn(Path(source).name, model_digests)
            return original_copy(source, destination)

        def hash_file(path):
            self.assertNotIn("models", Path(path).parts)
            return original_hash(path)

        with patch.object(self.store_module.shutil, "copyfile", side_effect=copy), patch.object(self.store_module, "hash_file", side_effect=hash_file):
            self.prepare()
        self.assertEqual((self.local / "user/default/workflows/test.json").read_text(), "{}")
        self.assertTrue((self.local / "models/first.safetensors").is_symlink())

    def test_first_load_copies_only_requested_model_and_reports_progress(self):
        cache = self.prepare()
        statuses = []
        progress = cache.progress

        def observe(*args):
            progress(*args)
            statuses.append(cache.snapshot())

        cache.progress = observe
        requested = self.local / "models/first.safetensors"
        result = cache.wrap_loader(lambda path, **kwargs: (Path(path).read_bytes(), kwargs))(str(requested), safe_load=True)
        self.assertEqual(result, (b"first model", {"safe_load": True}))
        self.assertFalse(requested.is_symlink())
        self.assertTrue((self.local / "models/second.safetensors").is_symlink())
        self.assertEqual([s["phase"] for s in statuses], ["downloading", "downloading", "verifying", "idle"])
        self.assertEqual(statuses[-1]["completed_bytes"], len(b"first model"))
        with patch.object(self.store_module, "hash_file", side_effect=AssertionError("Unchanged files must not be rehashed")):
            self.store_module.sync(self.local, self.store, minimum_age=0)
            self.assertEqual(Path(cache.load(str(requested))).read_bytes(), b"first model")

    def test_failed_transfer_leaves_link_and_retry_succeeds(self):
        cache = self.prepare()
        target = self.local / "models/first.safetensors"
        blob = target.readlink()
        blob.write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            cache.load(str(target))
        self.assertTrue(target.is_symlink())
        self.assertEqual(cache.snapshot()["phase"], "error")
        self.assertFalse(list(target.parent.glob("*.notch-restore-part")))
        blob.write_bytes(b"first model")
        self.assertEqual(Path(cache.load(str(target))).read_bytes(), b"first model")
        self.assertEqual(cache.snapshot()["phase"], "idle")

    def test_existing_local_files_survive_and_preparation_is_idempotent(self):
        (self.local / "models").mkdir(parents=True)
        target = self.local / "models/first.safetensors"
        target.write_bytes(b"local replacement")
        self.prepare()
        cache = self.prepare()
        self.assertEqual(Path(cache.load(str(target))).read_bytes(), b"local replacement")
        self.assertEqual(len(json.loads((self.local / ".notch-model-links.json").read_text())), 1)

    def test_resolved_blob_path_and_concurrent_requests_use_local_copy(self):
        cache = self.prepare()
        target = self.local / "models/first.safetensors"
        blob = str(target.resolve())
        results = []
        threads = [threading.Thread(target=lambda: results.append(cache.load(blob))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [str(target), str(target)])
        self.assertEqual(target.read_bytes(), b"first model")

    def test_missing_global_blob_blocks_readiness(self):
        next((self.store / "blobs").iterdir()).unlink()
        with self.assertRaises((RuntimeError, FileNotFoundError)):
            self.prepare()

    def test_core_imported_loader_aliases_also_use_cache(self):
        cache = self.prepare()
        def original(filename):
            return Path(filename).read_bytes()

        def custom(filename):
            return b"another extension"

        utils = types.SimpleNamespace(load_torch_file=original)
        vision = types.SimpleNamespace(load_torch_file=original)
        removal = types.SimpleNamespace(load_torch_file=custom)
        with patch.dict("sys.modules", {"comfy.clip_vision": vision, "comfy.bg_removal_model": removal}):
            cache.install_loader(utils)
        target = self.local / "models/first.safetensors"
        self.assertEqual(vision.load_torch_file(str(target)), b"first model")
        self.assertFalse(target.is_symlink())
        self.assertIs(removal.load_torch_file, custom)


if __name__ == "__main__":
    unittest.main()
