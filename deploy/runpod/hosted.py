"""Owner-only setup and tester invitations for a hosted workspace."""

import argparse
import base64
import copy
import json
import re
import subprocess
import sys
import urllib.parse
import uuid
import zlib
from pathlib import Path

import runpod as owner
import recovery

BROKER_IMAGE = "python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
SDK_VERSION = "1.12.0"
DEFAULT_SITE = "https://jkaarlehto.github.io/comfyui-hosted-connector/"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "share", "link", "list", "revoke"))
    parser.add_argument("--state-dir", type=Path, default=Path(".runpod"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--guest", default="tester")
    parser.add_argument(
        "--starter-idle-seconds",
        type=int,
        help="Keep the CPU starter warm between polls; default 60",
    )
    parser.add_argument(
        "--secret-prefix",
        help="Prefix for starter secrets; defaults to this deployment ID",
    )
    parser.add_argument("--site-url", help="HTTPS invitation site; saved for future invitations")
    parser.add_argument("--gpu-fallbacks", help="Comma-separated GPU names after the configured GPU")
    parser.add_argument(
        "--max-hourly-cost",
        type=float,
        help="Maximum hourly GPU price; saved for future recovery",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Update templates and starter without restarting the existing Pod",
    )
    args = parser.parse_args()
    args.state_dir = args.state_dir.resolve()
    state_file = args.state_dir / "state.json"
    state = json.loads(state_file.read_text())
    if args.starter_idle_seconds is None:
        args.starter_idle_seconds = state.get("hosted_config", {}).get("starter_idle_seconds", 60)
    if not 1 <= args.starter_idle_seconds <= 3600:
        parser.error("Starter idle time must be between 1 and 3600 seconds")
    if args.command not in ("link", "list", "setup") and not state.get("pod_id"):
        raise RuntimeError("Deploy a Pod with runpod.py first")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", args.guest):
        raise RuntimeError("Guest names must contain only letters, numbers, underscores or hyphens")
    if args.site_url:
        state["invitation_site"] = invitation_site(args.site_url)
        owner.save_state(state_file, state)
    state.setdefault("invitation_site", DEFAULT_SITE)
    if args.command == "link":
        write_invitation_link(args, state)
        return
    if args.command == "list":
        names = sorted(state.get("guests", {}))
        print("Invited testers:" if names else "No tester invitations recorded.")
        for name in names:
            pending = " (revocation pending)" if state["guests"][name].get("revoking") else ""
            print(" ", name + pending)
        return
    key = owner.api_key(args.env_file)
    if args.command != "setup" and not owner.resolve_pod(key, state, state_file):
        raise RuntimeError("No hosted Pod found; deploy one first")
    if args.command == "setup":
        setup(args, key, state, state_file)
    elif args.command == "share":
        invite(args, key, state, state_file)
    else:
        revoke(args, key, state, state_file)


def starter_source():
    bootstrap = "import subprocess,sys\nfrom pathlib import Path\n"
    bootstrap += f"subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--no-cache-dir','runpod=={SDK_VERSION}'],check=True)\n"
    for name in ("broker.py", "recovery.py"):
        source = Path(__file__).with_name(name).read_text(encoding="utf-8")
        bootstrap += "Path('/opt/" + name + "').write_text(" + repr(source) + ")\n"
    bootstrap += "subprocess.run([sys.executable,'-u','/opt/broker.py'],check=True)\n"
    return bootstrap


def starter_code():
    encoded = base64.b85encode(zlib.compress(starter_source().encode(), 9)).decode()
    command = "import base64,zlib;exec(zlib.decompress(base64.b85decode(" + repr(encoded) + ")))"
    return json.dumps({"entrypoint": ["python3", "-u", "-c"], "cmd": [command]})


def setup(args, key, state, state_file):
    if state.get("config", {}).get("storage") != "global" or not state.get("global_volume_id"):
        raise RuntimeError("Hosted recovery requires a global persistent volume")
    if not state.get("template_id"):
        raise RuntimeError("Create the GPU template with runpod.py setup first")
    previous = state.get("hosted_config", {})
    tiers = list(
        dict.fromkeys(
            [state["config"]["gpu"]]
            + (
                [item.strip() for item in args.gpu_fallbacks.split(",") if item.strip()]
                if args.gpu_fallbacks is not None
                else previous.get("gpu_fallbacks", recovery.FALLBACK_GPUS)
            )
        )
    )
    maximum = args.max_hourly_cost if args.max_hourly_cost is not None else previous.get("max_hourly_cost")
    if maximum is None:
        prices = owner.graphql(key, "query { gpuTypes { id securePrice } }")["gpuTypes"]
        maximum = next((item["securePrice"] for item in prices if item["id"] == tiers[0]), None)
    if maximum is None or not 0 < float(maximum) < 100:
        raise RuntimeError("Set a positive --max-hourly-cost before enabling recovery")
    state.setdefault("deployment_id", uuid.uuid4().hex)
    owner.save_state(state_file, state)
    host_key = args.state_dir / "host_ed25519"
    owner.create_key(host_key, "notch-hosted-host")
    health_key = args.state_dir / "health_ed25519"
    owner.create_key(health_key, "notch-hosted-health")
    prefix = args.secret_prefix or state.get("hosted_secret_prefix") or "notch_" + state["deployment_id"][:12]
    state["hosted_secret_prefix"] = prefix
    owner.save_state(state_file, state)
    host_name = "notch_host_key" if "notch_host_key" in state.get("hosted_secrets", {}) else prefix + "_host_key"
    control_name = (
        "notch_start_control" if "notch_start_control" in state.get("hosted_secrets", {}) else prefix + "_control"
    )
    host_secret = secret(
        args,
        key,
        state,
        state_file,
        host_name,
        base64.b64encode(host_key.read_bytes()).decode(),
    )
    control_secret = secret(args, key, state, state_file, control_name, key)
    health_secret = secret(
        args,
        key,
        state,
        state_file,
        prefix + "_health",
        base64.b64encode(health_key.read_bytes()).decode(),
    )
    state["hosted_env"] = {
        "NOTCH_SSH_HOST_KEY_B64": "{{ RUNPOD_SECRET_" + host_secret + " }}",
        "NOTCH_HEALTH_PUBLIC_KEY": " ".join(health_key.with_suffix(".pub").read_text().split()[:2]),
    }
    owner.save_state(state_file, state)
    body = {
        "name": state["config"]["name"] + " starter",
        "type": "QUEUE",
        "image": BROKER_IMAGE,
        "args": starter_code(),
        "disk": 5,
        "cpu": [{"id": "cpu3c", "vcpuCount": 2}],
        "scaling": {"type": "QUEUE_DELAY", "queueDelay": 1},
        "workers": {"min": 0, "max": 1, "idleTimeout": args.starter_idle_seconds},
        "timeout": 60000,
        "env": {
            "NOTCH_DEPLOYMENT_ID": state["deployment_id"],
            "NOTCH_POD_ID": state.get("pod_id", ""),
            "NOTCH_TEMPLATE_ID": state["template_id"],
            "NOTCH_VOLUME_ID": state["global_volume_id"],
            "NOTCH_GPU_TIERS": json.dumps(tiers),
            "NOTCH_MAX_COST": str(maximum),
            "NOTCH_COMFY_PORT": str(state["config"]["comfy_port"]),
            "NOTCH_CONTROL_KEY": "{{ RUNPOD_SECRET_" + control_secret + " }}",
            "NOTCH_HEALTH_KEY_B64": "{{ RUNPOD_SECRET_" + health_secret + " }}",
            "NOTCH_SSH_HOST_KEY": " ".join(host_key.with_suffix(".pub").read_text().split()[:2]),
        },
    }
    if state.get("broker_id"):
        body.pop("type")
        body["env"]["NOTCH_STARTER_ID"] = state["broker_id"]
        endpoint = owner.request(key, "PATCH", "/serverless/" + state["broker_id"], body)
    else:
        endpoint = owner.create_resource(key, "/serverless", "endpoints", body, state["deployment_id"])
        body["env"]["NOTCH_STARTER_ID"] = endpoint["id"]
        owner.request(key, "PATCH", "/serverless/" + endpoint["id"], {"env": body["env"]})
    state["broker_id"] = endpoint["id"]
    state["hosted_config"] = {
        "starter_idle_seconds": args.starter_idle_seconds,
        "gpu_fallbacks": tiers[1:],
        "max_hourly_cost": maximum,
    }
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
    if not args.no_restart and owner.resolve_pod(key, state, state_file):
        owner.request(
            key,
            "PATCH",
            "/pods/" + state["pod_id"],
            {"templateId": state["template_id"]},
        )
    print("Starter:", endpoint["id"])
    print(
        "Hosted template and starter updated."
        if args.no_restart
        else "Applied the hosted template; Runpod may restart a running Pod."
    )


def invite(args, key, state, state_file):
    if not state.get("broker_id"):
        raise RuntimeError("Run hosted.py setup first")
    require_running(args, key, state)
    guests = state.setdefault("guests", {})
    if guests.get(args.guest, {}).get("revoking"):
        raise RuntimeError("This tester's revocation is pending; retry revoke before creating another invitation")
    folder = guest_folder(args)
    folder.mkdir(parents=True, exist_ok=True)
    identity = folder / "id_ed25519"
    if args.guest not in guests:
        clear_guest_credentials(args)
        owner.create_key(identity, "notch-guest-" + args.guest)
        created = owner.graphql(
            key,
            "mutation ($input: CreateApiKeyInput) { createApiKeyNew(input: $input) { id rawKey policies } }",
            {
                "input": {
                    "name": "ComfyUI tester " + args.guest,
                    "policies": [
                        {
                            "Version": "2024-09-01",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Actions": ["serverless:Read", "serverless:Write"],
                                    "Resources": ["runpod/serverless/*/" + state["broker_id"] + "/*"],
                                }
                            ],
                        }
                    ],
                },
            },
        )["createApiKeyNew"]
        invite_data = {
            "version": 1,
            "endpoint": state["broker_id"],
            "key": created["rawKey"],
            "ssh_key": identity.read_text(),
        }
        invitation = folder / "invitation.txt"
        invitation.touch(exist_ok=True)
        owner.private_file(invitation)
        invitation.write_text(base64.b64encode(json.dumps(invite_data).encode()).decode() + "\n")
        guests[args.guest] = {
            "api_key_id": created["id"],
            "public_key": " ".join(identity.with_suffix(".pub").read_text().split()[:2]),
        }
        owner.save_state(state_file, state)
    invitation = folder / "invitation.txt"
    if not invitation.is_file():
        raise RuntimeError("Invitation file is missing; revoke this tester and create a new invitation")
    owner.private_file(invitation)
    update_guests(args, key, state)
    for name in ("start_hosted_comfyui.bat", "start_hosted_comfyui.ps1"):
        (folder / name).write_bytes(Path(__file__).with_name(name).read_bytes())
    print("Tester launcher:", folder / "start_hosted_comfyui.bat")
    print("Send the two launcher files and invitation.txt privately to this tester.")
    print("The invitation contains a dedicated tunnel key and a starter-only API key.")
    if state.get("invitation_site"):
        write_invitation_link(args, state)


def write_invitation_link(args, state):
    if args.guest not in state.get("guests", {}):
        raise RuntimeError("Create this tester's invitation with share first")
    if state["guests"][args.guest].get("revoking"):
        raise RuntimeError("This tester's revocation is pending; retry revoke")
    site = invitation_site(state.get("invitation_site", ""))
    folder = guest_folder(args)
    invitation = folder / "invitation.txt"
    if not invitation.is_file():
        raise RuntimeError("Invitation file is missing; revoke this tester and create a new invitation")
    payload = base64.b64decode(invitation.read_text().strip(), validate=True)
    token = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    target = folder / "invitation-link.txt"
    target.touch(exist_ok=True)
    owner.private_file(target)
    target.write_text(site + "#" + token + "\n", encoding="utf-8")
    print("Private invitation link saved to:", target)
    print("Send its contents privately. The link grants the same access as invitation.txt.")


def invitation_site(value):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise RuntimeError("Set --site-url to the HTTPS invitation page without credentials, query or fragment")
    try:
        parsed.port
    except ValueError as error:
        raise RuntimeError("The invitation site's port is invalid") from error
    return value.rstrip("/") + "/"


def revoke(args, key, state, state_file):
    guest = state.get("guests", {}).get(args.guest)
    if not guest:
        raise RuntimeError("No invitation recorded for this tester")
    require_running(args, key, state)
    guest["revoking"] = True
    owner.save_state(state_file, state)
    revised = copy.deepcopy(state)
    del revised["guests"][args.guest]
    update_guests(args, key, revised)
    owner.graphql(
        key,
        "mutation($input: DeleteApiKeyInput!) { deleteApiKey(input:$input){count} }",
        {"input": {"id": guest["api_key_id"]}},
    )
    clear_guest_credentials(args)
    state["guests"] = revised["guests"]
    owner.save_state(state_file, state)
    print("Revoked starter access and new SSH connections for", args.guest)
    print("Existing tunnel sessions remain connected until closed or the Pod parks.")
    print("Removed this tester's locally generated invitation and SSH key files.")


def clear_guest_credentials(args):
    folder = guest_folder(args)
    paths = [
        folder / name
        for name in (
            "id_ed25519",
            "id_ed25519.pub",
            "invitation.txt",
            "invitation-link.txt",
        )
    ]
    if any(path.resolve().parent != folder for path in paths):
        raise RuntimeError("A tester credential path points outside its private folder")
    for path in paths:
        path.unlink(missing_ok=True)


def guest_folder(args):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", args.guest):
        raise RuntimeError("Invalid tester name")
    state_dir = args.state_dir.resolve()
    shares = (state_dir / "shares").resolve()
    folder = (shares / args.guest).resolve()
    if shares != state_dir / "shares" or folder != shares / args.guest:
        raise RuntimeError("Tester files must remain inside this deployment's private shares folder")
    return folder


def require_running(args, key, state):
    pod = owner.get_pod(key, state["pod_id"])
    direct = pod.get("ssh", {}).get("direct")
    if pod["status"] != "RUNNING" or not direct:
        raise RuntimeError("Start the Pod before updating tester access; no invitation changes were applied")
    return direct


def update_guests(args, key, state):
    direct = require_running(args, key, state)
    registry = {
        name: guest["public_key"] for name, guest in state.get("guests", {}).items() if not guest.get("revoking")
    }
    target = (
        "/workspace-global/notch/guests.json"
        if state["config"]["storage"] == "global"
        else "/workspace/.notch-hosted/guests.json"
    )
    port = state["config"]["comfy_port"]
    restriction = f'restrict,port-forwarding,permitopen="127.0.0.1:{port}",command="/bin/false" '
    records = [restriction + public + " notch-guest-" + name for name, public in registry.items()]
    script = "from pathlib import Path\nimport json\n"
    script += "p=Path(" + repr(target) + ");p.parent.mkdir(parents=True,exist_ok=True)\n"
    script += "p.write_text(" + repr(json.dumps(registry) + "\n") + ")\n"
    script += "p=Path('/root/.ssh/authorized_keys')\n"
    script += "lines=[line for line in p.read_text().splitlines() if 'notch-guest-' not in line]\n"
    script += "lines.extend(" + repr(records) + ")\np.write_text('\\n'.join(lines)+'\\n');p.chmod(0o600)\n"
    subprocess.run(
        [
            "ssh",
            "-T",
            "-i",
            str(args.state_dir / "pod_ed25519"),
            "-p",
            str(direct["port"]),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "UserKnownHostsFile=" + str(args.state_dir / "known_hosts"),
            "root@" + direct["host"],
            "python3 -",
        ],
        input=script,
        text=True,
        check=True,
    )


def secret(args, key, state, state_file, name, value):
    tracked = state.setdefault("hosted_secrets", {})
    tracked[name] = owner.ensure_secret(
        key,
        name,
        value,
        "ComfyUI hosted starter; setup " + state["deployment_id"],
        tracked.get(name),
    )
    owner.save_state(state_file, state)
    return name


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
