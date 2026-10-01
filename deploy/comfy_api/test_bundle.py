import io
import unittest

import numpy as np
from PIL import Image

from .bundle import (
    compile_bundle,
    contract,
    typed_value,
    validate_bundle,
    validate_endpoint,
)
from .images import png


def workflow():
    return {
        "1": {
            "class_type": "NotchSingleInput",
            "inputs": {"inputs_json": '[{"key":"width","type":"INT","default":16}]'},
        },
        "2": {"class_type": "NotchSingleInput", "inputs": {"inputs_json": '[{"key":"image","type":"IMAGE"}]'}},
        "3": {
            "class_type": "ImageScale",
            "inputs": {
                "image": ["2", 0],
                "width": ["1", 0],
                "height": 16,
                "upscale_method": "nearest-exact",
                "crop": "disabled",
            },
        },
        "4": {"class_type": "NotchOutputNode", "inputs": {"image": ["3", 0], "output_name": "Beauty"}},
    }


class BundleTests(unittest.TestCase):
    def test_stateless_worker_graph_and_default_contract(self):
        source = workflow()
        bundle = compile_bundle(source, "Resize", "https://example.run.comfy.app")
        self.assertEqual(source, workflow())
        self.assertEqual(bundle["prompt"]["4"]["class_type"], "SaveImage")
        self.assertNotIn("1", bundle["prompt"])
        self.assertNotIn("2", bundle["prompt"])
        self.assertEqual(bundle["prompt"]["3"]["inputs"]["width"], 16)
        self.assertEqual(bundle["prompt"]["notch_api_image_2_0"]["class_type"], "LoadImage")
        self.assertEqual(contract(bundle)["input_declarations"][0]["saved_value"], 16)
        validate_bundle(bundle)

    def test_native_widget_bindings(self):
        graph = {
            "1": {"class_type": "EmptyImage", "inputs": {"width": 16, "height": 16, "batch_size": 1, "color": 0}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "Notch"}},
        }
        bundle = compile_bundle(
            graph,
            "Solid",
            "http://127.0.0.1:8189",
            {"inputs": {"width": {"node_id": "1", "input": "width", "type": "INT", "min": 1, "max": 4096}}},
        )
        self.assertEqual(bundle["inputs"]["width"]["default"], 16)
        self.assertEqual(contract(bundle)["input_declarations"][0]["min"], "1")

    def test_image_filename_adapter_binds_asset_not_cache(self):
        source = workflow()
        source["5"] = {"class_type": "NotchImageFile", "inputs": {"image": ["2", 0]}}
        source["6"] = {"class_type": "LoadImage", "inputs": {"image": ["5", 0]}}
        source["3"]["inputs"]["image"] = ["6", 0]
        bundle = compile_bundle(source, "Resize", "http://127.0.0.1:8189")
        self.assertNotIn("5", bundle["prompt"])
        self.assertIn({"node_id": "6", "input": "image"}, bundle["inputs"]["image"]["targets"])

    def test_modern_output_requires_explicit_type_and_slot(self):
        source = workflow()
        source["4"]["inputs"] = {"outputs.output_0": ["3", 0]}
        with self.assertRaisesRegex(ValueError, "specify its image slot"):
            compile_bundle(source, "Resize", "http://localhost:8189")
        bundle = compile_bundle(source, "Resize", "http://localhost:8189", {"outputs": {"4": {"type": "IMAGE"}}})
        self.assertEqual(bundle["outputs"][0]["name"], "4/output_0")

    def test_edits_require_republication(self):
        bundle = compile_bundle(workflow(), "Resize", "http://localhost:8189")
        bundle["prompt"]["3"]["inputs"]["height"] = 128
        with self.assertRaisesRegex(ValueError, "changed"):
            validate_bundle(bundle)

    def test_editor_only_export_and_worker_state_rejected(self):
        with self.assertRaisesRegex(ValueError, "compiled API"):
            compile_bundle({"nodes": []}, "Editor", "http://localhost:8189")
        source = workflow()
        source["5"] = {"class_type": "SpoutReceiver", "inputs": {}}
        with self.assertRaisesRegex(ValueError, "Worker-local"):
            compile_bundle(source, "Resize", "http://localhost:8189")

    def test_bool_is_not_integer_and_numeric_bounds_are_enforced(self):
        binding = {"name": "seed", "type": "INT", "min": 0, "max": 10}
        for value in (True, -1, 11, "5"):
            with self.assertRaises(ValueError):
                typed_value(binding, value)
        self.assertEqual(typed_value(binding, 5), 5)

    def test_endpoint_credentials_cannot_be_sent_to_bundle_controlled_hosts(self):
        for url in (
            "https://evil.example",
            "https://cloud.comfy.org.evil.com",
            "http://example.run.comfy.app",
            "https://user:secret@cloud.comfy.org",
            "https://cloud.comfy.org?token=secret",
        ):
            with self.assertRaises(ValueError):
                validate_endpoint(url)

    def test_raw_buffer_matches_extension_client_format_and_padded_stride(self):
        content, width, height = png(
            bytes([0, 0, 255, 255, 0, 0, 0, 0]), {"format": "bgra", "width": 1, "height": 1, "stride": 8}
        )
        self.assertEqual((width, height), (1, 1))
        self.assertEqual(Image.open(io.BytesIO(content)).getpixel((0, 0)), (255, 0, 0, 255))
        content, _, _ = png(
            np.array([1, 0, 0, 1], dtype="<f4").tobytes(), {"format": "float32_rgba", "width": 1, "height": 1}
        )
        self.assertEqual(Image.open(io.BytesIO(content)).getpixel((0, 0)), (255, 0, 0, 255))

    def test_raw_buffer_length_validation(self):
        with self.assertRaisesRegex(ValueError, "length or stride"):
            png(b"\0", {"format": "rgba", "width": 1, "height": 1})

    def test_embedded_credentials_are_not_published(self):
        source = workflow()
        source["3"]["inputs"]["api_key"] = "private-value"
        with self.assertRaisesRegex(ValueError, "embedded credentials"):
            compile_bundle(source, "Resize", "http://localhost:8189")


if __name__ == "__main__":
    unittest.main()
