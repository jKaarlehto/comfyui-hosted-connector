"""Check HTTP image input, remote execution, and HTTP image output."""

import argparse
import base64
import io
import json
import time
import uuid
from pathlib import Path

import requests
import websocket
from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18188")
    parser.add_argument("--output", type=Path, default=Path(".runpod/smoke"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    url = args.url.rstrip("/")
    client_id = "runpod-smoke-" + uuid.uuid4().hex
    consumer_id = client_id + "-output"
    workflow = {
        "1": {"class_type": "NotchSingleInput", "inputs": {"key": "image", "type": "IMAGE"}},
        "2": {"class_type": "ImageInvert", "inputs": {"image": ["1", 0]}},
        "3": {"class_type": "NotchOutputNode", "inputs": {"image": ["2", 0], "output_name": "Inverted"}},
    }
    original = Image.new("RGB", (128, 96), (32, 96, 160))
    image = io.BytesIO()
    original.save(image, format="PNG")
    (args.output / "input.png").write_bytes(image.getvalue())
    parse = requests.post(url + "/notch/parse", json={"prompt": workflow}, timeout=30)
    parse.raise_for_status()
    (args.output / "parse.json").write_text(json.dumps(parse.json(), indent=2))
    payload = {
        "prompt": workflow,
        "inputs": {
            "image": {"type": "IMAGE", "value": "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()}
        },
        "config": {
            "session": {"client_id": client_id, "consumer_id": client_id},
            "outputs": [
                {
                    "workflow_output_id": "3",
                    "consumer_id": consumer_id,
                    "transport": "http",
                    "http": {"type": "image", "extension": "png"},
                }
            ],
        },
    }
    (args.output / "request.json").write_text(json.dumps(payload))
    features = requests.get(url + "/features", timeout=10).json()["extension"]["notch"]
    assert "http" in features["output_transports"]
    hello = {
        "type": "feature_flags",
        "data": {
            "is_notch_client": True,
            "extension": {
                "notch_client": {
                    "protocol_version": "0.10.0",
                    "supports_protocol": ">=0.10.0,<0.11.0",
                    "cpp_client_version": "0.23.0",
                    "cpp_api_version": "0.25.0",
                    "capabilities": ["output-ack"],
                    "handled_notch_websocket_events": features["websocket_events"],
                    "delivery": {"delivery_stream_id": client_id, "applied_through_seq": 0},
                }
            },
        },
    }
    (args.output / "hello.json").write_text(json.dumps(hello))
    ws = websocket.create_connection(
        url.replace("https://", "wss://").replace("http://", "ws://") + "/ws?clientId=" + client_id, timeout=20
    )
    try:
        ws.send(json.dumps(hello))
        response = requests.post(url + "/notch/inject?execute=true", json=payload, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(response.text)
        result = response.json()
        (args.output / "response.json").write_text(json.dumps(result, indent=2))
        prompt_id = result["prompt_id"]
        output = None
        terminal = False
        events = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (output and terminal):
            raw = ws.recv()
            if not isinstance(raw, str):
                continue
            event = json.loads(raw)
            events.append(event)
            kind, data = event.get("type"), event.get("data", {})
            if kind == "notch-output-ready" and data.get("prompt_id") == prompt_id:
                output = data
            if kind == "notch-execution-terminal" and data.get("prompt_id") == prompt_id:
                if data.get("status") != "success":
                    raise RuntimeError(json.dumps(data))
                terminal = True
        (args.output / "events.json").write_text(json.dumps(events, indent=2))
        assert output and terminal, "No successful output event"
        assert output["transport"] == "http" and output.get("url") and not output.get("path")
        artifact = requests.get(url + output["url"], timeout=20)
        artifact.raise_for_status()
        (args.output / "output.png").write_bytes(artifact.content)
        decoded = Image.open(io.BytesIO(artifact.content)).convert("RGB")
        assert decoded.size == original.size
        assert set(decoded.getdata()) == {(223, 159, 94)} or set(decoded.getdata()) == {(223, 159, 95)}
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "prompt_id": prompt_id,
                    "transport": "http",
                    "size": decoded.size,
                    "pixel": decoded.getpixel((0, 0)),
                    "bytes": len(artifact.content),
                    "output_url": output["url"],
                }
            )
        )
    finally:
        ws.close()


if __name__ == "__main__":
    main()
