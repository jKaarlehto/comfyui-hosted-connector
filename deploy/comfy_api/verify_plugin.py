"""Execute actual Notch custom nodes through v2 on a chosen deployment.

Checks worker execution and artifact capture, not exposure of /notch routes.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

import aiohttp

from .client import RemoteError, V2Client
from .images import png


def smoke_graph():
    return {
        "1": {
            "class_type": "NotchSingleInput",
            "inputs": {"inputs_json": '[{"key":"width","type":"INT"}]', "remote_values_json": '{"width":16}'},
        },
        "2": {
            "class_type": "EmptyImage",
            "inputs": {"width": ["1", 0], "height": 16, "batch_size": 1, "color": 16711680},
        },
        "3": {
            "class_type": "NotchOutputNode",
            "inputs": {
                "image": ["2", 0],
                "notch_output_config": {
                    "mode": "http",
                    "type": "image",
                    "extension": "png",
                    "consumer_id": "v2-smoke",
                    "workflow_output_id": "3",
                    "preview": True,
                },
            },
        },
    }


async def verify(endpoint, api_key, directory, timeout=300):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    report_path = directory / "verify-plugin.json"
    existing = report_path.exists()
    report = (
        json.loads(report_path.read_text()) if existing else {"endpoint": endpoint, "submission_key": str(uuid.uuid4())}
    )
    if report["endpoint"] != endpoint:
        raise ValueError("This verification directory belongs to a different endpoint")

    def save():
        temporary = report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(report_path)

    async with aiohttp.ClientSession(trust_env=True) as session:
        client = V2Client(endpoint, api_key, session)
        if "job_id" not in report:
            if existing and not report.get("submission_rejected"):
                raise ValueError(
                    "Previous verification submission outcome is unknown. Investigate its recorded key before starting another."
                )
            save()
            report.pop("submission_rejected", None)
            save()
            try:
                job = await client.submit(smoke_graph(), report["submission_key"])
            except RemoteError as exc:
                if exc.status < 500 and exc.code != "idempotency_key_reuse":
                    report["submission_rejected"] = True
                    save()
                raise
            report.update(job_id=job["id"], urls=job["urls"])
            save()
        print("Verifying Notch custom nodes through v2; job " + report["job_id"], flush=True)
        async with asyncio.timeout(timeout):
            while True:
                job = await client.json("GET", report["urls"]["self"])
                if job["status"] in {"succeeded", "failed", "canceled", "expired"}:
                    break
                await asyncio.sleep(1)
        report["status"] = job["status"]
        outputs = [o for o in job.get("outputs", []) if o["node_id"] == "3" and o["type"] == "image"]
        report["image_outputs"] = len(outputs)
        save()
        if job["status"] != "succeeded" or not outputs:
            raise ValueError(
                "Custom-node verification failed or produced no v2 image asset. Check Build imports and enable preview on Notch HTTP image outputs."
            )
        image = await client.download(outputs[0]["url"])
        _, width, height = await asyncio.to_thread(png, image)
        if (width, height) != (16, 16):
            raise ValueError("Custom-node verification returned an unexpected image size")
        target = directory / "notch-custom-node.png"
        target.write_bytes(image)
        os.chmod(target, 0o600)
        print("NotchSingleInput and NotchOutputNode executed; image downloaded to " + str(target.resolve()), flush=True)
        return report
