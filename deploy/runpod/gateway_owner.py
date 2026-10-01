"""Owner administration for one-time connector invitations."""

import base64
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import runpod as owner
from server_keys import NoRedirect, gateway_origin


def setup(args, state, state_file):
    if not state.get("broker_id") or not state.get("template_id"):
        raise RuntimeError("Set up the GPU template and hosted starter first")
    account = credential(args.env_file, "CLOUDFLARE_ACCOUNT_ID", private=False)
    token = credential(args.env_file, "CLOUDFLARE_API_TOKEN")
    if not re.fullmatch(r"[a-f0-9]{32}", account):
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID must be a 32-character account ID")
    admin = credential(args.env_file, "GATEWAY_ADMIN_KEY", generate=True)
    server = credential(args.env_file, "GATEWAY_SERVER_KEY", generate=True)
    for value in (admin, server):
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise RuntimeError(
                "Gateway keys must be 64 lowercase hexadecimal characters"
            )
    key = owner.api_key(args.env_file)
    starter = credential(args.env_file, "RUNPOD_GATEWAY_STARTER_KEY", optional=True)
    if not starter:
        created = owner.graphql(
            key,
            "mutation ($input: CreateApiKeyInput) { createApiKeyNew(input: $input) { id rawKey } }",
            {
                "input": {
                    "name": "ComfyUI Notch gateway",
                    "policies": [starter_policy(state["broker_id"])],
                }
            },
        )["createApiKeyNew"]
        starter = created["rawKey"]
        owner.save_env_value(args.env_file, "RUNPOD_GATEWAY_STARTER_KEY", starter)
        state["gateway_starter_key_id"] = created["id"]
        owner.save_state(state_file, state)
    worker = "comfyui-notch-" + state["deployment_id"][:12]
    environment = os.environ.copy()
    environment.update(CLOUDFLARE_ACCOUNT_ID=account, CLOUDFLARE_API_TOKEN=token)
    directory = Path(__file__).resolve().parents[1] / "gateway"
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    npx = shutil.which("npx.cmd" if os.name == "nt" else "npx")
    if not npm or not npx:
        raise RuntimeError("Install Node.js with npm before setting up the gateway")
    print("Installing gateway deployment dependencies...")
    run([npm, "ci"], directory, environment)
    print("Deploying the private invitation gateway...")
    run(
        [
            npx,
            "wrangler",
            "deploy",
            "--name",
            worker,
            "--var",
            "RUNPOD_STARTER_ID:" + state["broker_id"],
            "--var",
            "WORKSPACE_NAME:" + state["config"]["name"],
            "--var",
            "INVITATION_ORIGIN:" + invitation_origin(state),
        ],
        directory,
        environment,
    )
    run(
        [npx, "wrangler", "secret", "bulk", "--name", worker],
        directory,
        environment,
        json.dumps(
            {
                "GATEWAY_ADMIN_KEY": admin,
                "GATEWAY_SERVER_KEY": server,
                "RUNPOD_STARTER_KEY": starter,
            }
        ),
    )
    result = http_json(
        "https://api.cloudflare.com/client/v4/accounts/"
        + account
        + "/workers/subdomain",
        token,
    )
    if not result.get("success"):
        raise RuntimeError("Cloudflare did not return the Workers account subdomain")
    origin = gateway_origin(
        "https://" + worker + "." + result["result"]["subdomain"] + ".workers.dev"
    )
    state["gateway"] = origin
    secret_name = "notch_" + state["deployment_id"][:12] + "_gateway"
    tracked = state.setdefault("hosted_secrets", {})
    tracked[secret_name] = owner.ensure_secret(
        key,
        secret_name,
        server,
        "ComfyUI Notch device registry",
        tracked.get(secret_name),
    )
    state.setdefault("hosted_env", {}).update(
        {
            "NOTCH_GATEWAY_URL": origin,
            "NOTCH_GATEWAY_SERVER_KEY": "{{ RUNPOD_SECRET_" + secret_name + " }}",
        }
    )
    owner.save_state(state_file, state)
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("runpod.py")),
            "setup",
            "--state-dir",
            str(args.state_dir),
            "--env-file",
            str(args.env_file),
        ],
        check=True,
    )
    print("Gateway configured:", origin)
    print(
        "The GPU template is updated. Stop and reconnect the workspace to apply device authorization."
    )


def invitation_origin(state):
    site = urllib.parse.urlsplit(state.get("invitation_site", "https://jkaarlehto.github.io/comfyui-hosted-connector/"))
    if site.scheme != "https" or not site.hostname or site.username or site.password or site.query or site.fragment:
        raise RuntimeError("Set an HTTPS invitation site without credentials, query or fragment")
    return "https://" + site.netloc.lower()


def starter_policy(endpoint):
    return {
        "Version": "2024-09-01",
        "Statement": [
            {
                "Effect": "Allow",
                "Actions": ["serverless:Read", "serverless:Write"],
                "Resources": ["runpod/serverless/*/" + endpoint + "/*"],
            }
        ],
    }


def credential(path, name, private=True, generate=False, optional=False):
    value = os.environ.get(name) or owner.read_env_value(path, name)
    if value:
        return value
    if optional:
        return ""
    if generate:
        value = secrets.token_hex(32)
    else:
        if not sys.stdin.isatty():
            raise RuntimeError("Set " + name + " in the owner .env file")
        value = (
            getpass.getpass(name + ": ") if private else input(name + ": ")
        ).strip()
        if not value:
            raise RuntimeError(name + " is required")
    owner.save_env_value(path, name, value)
    return value


def run(command, directory, environment, payload=None):
    result = subprocess.run(
        command,
        cwd=directory,
        env=environment,
        input=payload,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(
            "Gateway deployment command failed: "
            + Path(command[0]).name
            + " "
            + command[1]
            + ". Check Cloudflare permissions and account settings."
        )


def http_json(url, key, method="GET", body=None):
    request = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "ComfyUI-Notch-Owner",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(
            request, timeout=30
        ) as response:
            data = response.read(262145)
        if len(data) > 262144:
            raise RuntimeError("Gateway response is too large")
        return json.loads(data)
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            "Gateway request failed (HTTP " + str(error.code) + ")"
        ) from None
    except (OSError, ValueError) as error:
        raise RuntimeError("Gateway request could not be completed") from error


def api(args, state, path, method="GET", body=None):
    return http_json(
        gateway_origin(state["gateway"]) + "/v1/admin/" + path,
        credential(args.env_file, "GATEWAY_ADMIN_KEY"),
        method,
        body,
    )


def invite(args, state, state_file, folder):
    if args.guest in state.get("guests", {}):
        raise RuntimeError(
            "This name has an existing reusable invitation. Revoke that access explicitly or use a new tester name."
        )
    previous = state.get("gateway_invites", {}).get(args.guest)
    if previous:
        current = api(args, state, "invites")["invites"]
        if any(
            item["invite_id"] == previous["invite_id"] and item["state"] == "unused"
            for item in current
        ):
            if (folder / "invitation.txt").is_file():
                print("Reusing this tester's unused invitation.")
                return
            raise RuntimeError(
                "The private invitation file is missing; revoke it before issuing another"
            )
    result = api(
        args,
        state,
        "invites",
        "POST",
        {"label": args.guest, "expires_in_seconds": args.invite_days * 86400},
    )
    if not re.fullmatch(
        r"[a-f0-9]{32}", result.get("invite_id", "")
    ) or not re.fullmatch(r"[a-f0-9]{64}", result.get("token", "")):
        raise RuntimeError("Gateway returned an invalid invitation")
    payload = {
        "version": 2,
        "gateway": gateway_origin(state["gateway"]),
        "invite_id": result["invite_id"],
        "token": result["token"],
    }
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "invitation.txt"
    path.touch(exist_ok=True)
    owner.private_file(path)
    path.write_text(base64.b64encode(json.dumps(payload).encode()).decode() + "\n")
    state.setdefault("gateway_invites", {})[args.guest] = {
        "invite_id": result["invite_id"],
        "expires_at": result["expires_at"],
    }
    owner.save_state(state_file, state)
    print("One-time invitation saved; it expires in", args.invite_days, "days.")


def revoke_invite(args, state, state_file):
    record = state.get("gateway_invites", {}).get(args.guest)
    if not record:
        raise RuntimeError("No one-time invitation recorded for this tester")
    api(args, state, "invites/" + record["invite_id"] + "/revoke", "POST", {})
    record["revoked"] = True
    owner.save_state(state_file, state)
    print(
        "Invitation revoked. An already enrolled device remains authorized; use devices and revoke-device to remove it."
    )


def list_access(args, state, devices=False):
    name = "devices" if devices else "invites"
    for record in api(args, state, name)[name]:
        if devices:
            print(
                record["device_id"],
                record["device_name"],
                "revoked" if record.get("revoked_at") else "active",
            )
        else:
            print(
                record["invite_id"],
                record.get("label", ""),
                record["state"],
                record.get("device_id") or "",
            )


def revoke_device(args, state):
    if not args.device or not re.fullmatch(r"[a-f0-9]{32}", args.device):
        raise RuntimeError("Set --device to an ID from hosted.py devices")
    api(args, state, "devices/" + args.device + "/revoke", "POST", {})
    print(
        "Device revoked. New SSH connections are denied within five minutes; existing tunnels remain until closed or parked."
    )
