"""Publisher and consumer commands; execute from the repository root."""

import argparse
import asyncio
import getpass
import json
import os
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

from .auth import load_key, remove_key, store_key
from .bridge import Bridge, State
from .bundle import compile_bundle, digest, validate_bundle, validate_endpoint
from .client import RemoteError, V2Client
from .verify_plugin import verify


def state_directory(bundle, endpoint, override):
    if override:
        return Path(override)
    root = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / "Notch" / "ComfyAPI" / digest({"bundle": bundle["bundle_id"], "endpoint": endpoint})[:24]


async def probe_key(endpoint, key):
    async with aiohttp.ClientSession(trust_env=True) as session:
        client = V2Client(endpoint, key, session)
        try:
            await client.json("GET", endpoint + "/api/v2/assets/" + str(uuid.uuid4()))
        except RemoteError as exc:
            if exc.status != 404 or exc.code != "not_found":
                raise


async def recover(state, endpoint, key, submission_key, job_id):
    record = state.data["jobs"].get(submission_key)
    if record is None or record.get("id"):
        raise ValueError("Select an ambiguous submission record that has no recorded job ID")
    async with aiohttp.ClientSession(trust_env=True) as session:
        client = V2Client(endpoint, key, session)
        remote = await client.json("GET", endpoint + "/api/v2/jobs/" + str(uuid.UUID(job_id)))
        for link in remote["urls"].values():
            client.link(link)
        record.update(id=remote["id"], urls=remote["urls"], finished=False)
        record.pop("attention", None)
        state.save()


def main():
    parser = argparse.ArgumentParser(description="Compile workflow metadata and exercise Comfy API for development")
    sub = parser.add_subparsers(dest="command", required=True)
    compile_command = sub.add_parser("compile", help="Compile a local workflow bundle; does not publish or deploy")
    compile_command.add_argument("--workflow", type=Path, required=True)
    compile_command.add_argument("--bindings", type=Path)
    compile_command.add_argument("--name", required=True)
    compile_command.add_argument("--output", type=Path, required=True)
    compile_command.add_argument("--endpoint", required=True)
    for command in ("login", "logout", "serve", "status", "recover", "verify-plugin"):
        action = sub.add_parser(command)
        action.add_argument(
            "--endpoint", required=True, help="Explicitly chosen Comfy deployment or loopback v2 proxy URL"
        )
        if command in {"serve", "status", "recover"}:
            action.add_argument("--bundle", type=Path, required=True)
            action.add_argument("--state-dir", type=Path)
        if command == "serve":
            action.add_argument("--port", type=int, default=18188)
        if command == "recover":
            action.add_argument("--submission-key", required=True)
            action.add_argument(
                "--job-id", required=True, help="Existing job ID found in Comfy; never submits another job"
            )
        if command == "verify-plugin":
            action.add_argument("--output-dir", type=Path, required=True)
            action.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    try:
        endpoint = validate_endpoint(args.endpoint)
        if args.command == "compile":
            workflow = json.loads(args.workflow.read_text(encoding="utf-8-sig"))
            bindings = json.loads(args.bindings.read_text(encoding="utf-8-sig")) if args.bindings else None
            bundle = compile_bundle(workflow, args.name, endpoint, bindings)
            args.output.write_text(json.dumps(bundle, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            print(f"Compiled {bundle['name']}: {len(bundle['inputs'])} inputs, {len(bundle['outputs'])} image outputs")
            print(f"Bundle: {args.output.resolve()}\nVersion: {bundle['bundle_id']}")
            return
        if args.command == "logout":
            remove_key(endpoint)
            print("Removed this endpoint's saved credential. Restart a running bridge to discard its in-memory key.")
            return
        if args.command == "login":
            print("Create an authorized API key under API Keys at https://platform.comfy.org/login")
            key = getpass.getpass("Comfy API key (stored in your OS credential store): ")
            asyncio.run(probe_key(endpoint, key))
            store_key(endpoint, key)
            print("Credential checked and saved for this endpoint. Shared bundles contain no credentials.")
            return
        if args.command == "verify-plugin":
            if args.timeout < 1:
                raise ValueError("Verification timeout must be positive")
            key = load_key(endpoint)
            if not key and ".comfy." in endpoint:
                raise ValueError("Sign in once with the login command, or set COMFY_API_KEY for this process")
            asyncio.run(verify(endpoint, key, args.output_dir, args.timeout))
            return
        bundle = validate_bundle(json.loads(args.bundle.read_text(encoding="utf-8-sig")))
        if bundle["endpoint"] != endpoint:
            raise ValueError("Selected endpoint differs from the compiled bundle. Recompile for the intended endpoint.")
        directory = state_directory(bundle, endpoint, args.state_dir)
        if args.command == "status":
            state = State(directory, bundle, endpoint)
            print(
                json.dumps(
                    {
                        "state_directory": str(directory),
                        "jobs": [
                            {k: j[k] for k in ("key", "id", "finished", "attention") if k in j}
                            for j in state.data["jobs"].values()
                        ],
                    },
                    indent=2,
                )
            )
            return
        key = load_key(endpoint)
        if not key and ".comfy." in endpoint:
            raise ValueError("Sign in once with the login command, or set COMFY_API_KEY for this process")
        if args.command == "recover":
            asyncio.run(recover(State(directory, bundle, endpoint), endpoint, key, args.submission_key, args.job_id))
            print("Recorded the existing job. Start the bridge to resume polling it without submitting again.")
            return
        if not 1 <= args.port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        bridge = Bridge(bundle, endpoint, key, directory)
        print(f"Connect Notch to http://127.0.0.1:{args.port}; select {bridge.workflow_path}")
        print("Select HTTP input/output and PNG; keep this bridge open while generating.")
        print(f"Job state and downloaded images: {directory}")
        web.run_app(bridge.app, host="127.0.0.1", port=args.port, access_log=None)
    except (ValueError, KeyError, OSError, RemoteError, aiohttp.ClientError, TimeoutError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
