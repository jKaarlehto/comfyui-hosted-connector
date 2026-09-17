"""Readiness requires restored storage and the running ComfyUI HTTP plugin."""

import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import health


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.config = root / "health.json"
        self.marker = root / "restored"
        self.marker.touch()
        self.routes = {
            "/system_stats": {"system": {"comfyui_version": "0.36.0"}},
            "/features": {"extension": {"notch": {"output_transports": ["disk", "http"]}}},
        }
        routes = self.routes

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = routes.get(self.path)
                self.send_response(200 if data is not None else 503)
                self.end_headers()
                self.wfile.write(data if isinstance(data, bytes) else json.dumps(data).encode())

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.values = {
            "deployment_id": "workspace",
            "pod_id": "replacement",
            "port": self.server.server_port,
            "store": str(root / "global"),
        }

    def close_server(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def check(self, mounted=True):
        self.config.write_text(json.dumps(self.values), encoding="utf-8")
        with patch.object(Path, "is_mount", return_value=mounted):
            return health.check(self.config, self.marker)

    def test_ready_requires_comfy_plugin_and_restored_mounted_storage(self):
        self.assertEqual(self.check(), {"ready": True, "pod_id": "replacement", "deployment_id": "workspace"})
        self.assertEqual(self.check(mounted=False), {"ready": False})
        self.marker.unlink()
        self.assertEqual(self.check(), {"ready": False})

    def test_unavailable_or_invalid_backend_is_not_ready(self):
        for reply in (None, b"not json", [], {}, {"system": {}}, {"system": {"comfyui_version": ""}}):
            with self.subTest(reply=reply):
                self.routes["/system_stats"] = reply
                self.assertEqual(self.check(), {"ready": False})

    def test_stock_comfy_and_missing_http_transport_are_not_ready(self):
        for reply in ({}, {"extension": {}}, {"extension": {"notch": {"output_transports": ["disk"]}}},
                      {"extension": {"notch": {"output_transports": "http"}}}):
            with self.subTest(reply=reply):
                self.routes["/features"] = reply
                self.assertEqual(self.check(), {"ready": False})

    def test_bad_config_is_not_ready_and_cannot_choose_remote_host(self):
        for name, value in (("port", 0), ("port", 65536), ("port", True), ("port", "8188"),
                            ("deployment_id", ""), ("pod_id", None), ("store", [])):
            with self.subTest(name=name, value=value), patch.dict(self.values, {name: value}):
                self.assertEqual(self.check(), {"ready": False})
        self.config.write_text("invalid", encoding="utf-8")
        self.assertEqual(health.check(self.config, self.marker), {"ready": False})

    def test_external_proxy_is_not_used_for_loopback_health(self):
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""}):
            self.assertTrue(self.check()["ready"])


if __name__ == "__main__":
    unittest.main()
