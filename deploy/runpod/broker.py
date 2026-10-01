"""Connect invited testers to one hosted workspace."""

import os

import recovery


def handler(job):
    if not isinstance(job, dict) or job.get("input") != {"action": "connect"}:
        return {"error": "This endpoint only accepts a connect request"}
    return recovery.connection(os.environ["NOTCH_CONTROL_KEY"], os.environ["NOTCH_STARTER_ID"])


if __name__ == "__main__":
    import runpod

    runpod.serverless.start({"handler": handler})
