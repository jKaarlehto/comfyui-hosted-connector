"""Create a private ComfyUI Pod template and connect through local SSH forwarding."""

import argparse
import base64
import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.runpod.io/v2"
IMAGE = "runpod/comfyui@sha256:7ed65713a24c7c836cea8e5ba338ee140b0257b0a086cc3835784e0473efe2f2"
DEFAULT_REF = "install-startup-requirements"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "setup",
            "status",
            "deploy",
            "replace",
            "connect",
            "start",
            "stop",
            "terminate",
            "catalog",
            "sync",
        ],
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--state-dir", type=Path, default=Path(".runpod"))
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument("--comfy-ref", default="stable")
    parser.add_argument("--pod")
    parser.add_argument("--name", default="ComfyUI-Notch")
    parser.add_argument("--instance-name", default="ComfyUI Notch (Runpod)")
    parser.add_argument("--plugin-repo", default="jKaarlehto/ComfyUI-Notch")
    parser.add_argument("--secret-name", help="Deploy-key secret name; defaults to this deployment ID")
    parser.add_argument("--storage", choices=("global", "pod", "network"), default="global")
    parser.add_argument("--volume-id")
    parser.add_argument("--disk-gb", type=int, default=32)
    parser.add_argument("--volume-gb", type=int, default=32)
    parser.add_argument("--comfy-port", type=int, default=8188)
    parser.add_argument("--gpu", default="NVIDIA A40")
    parser.add_argument("--datacenter", default="EU-SE-1")
    parser.add_argument("--local-port", type=int, default=18188)
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--idle-minutes", type=int, default=20)
    parser.add_argument("--minutes", type=int, default=240)
    initial = parser.parse_args()
    initial_state = initial.state_dir / "state.json"
    if initial_state.exists():
        parser.set_defaults(**json.loads(initial_state.read_text()).get("config", {}))
    args = parser.parse_args()
    if args.disk_gb < 10 or args.volume_gb < 10 or args.minutes < 0 or args.idle_minutes < 0:
        parser.error("Disk sizes must be at least 10 GB and time limits must be non-negative")
    if not all(1 <= port <= 65535 for port in (args.comfy_port, args.local_port)):
        parser.error("Ports must be between 1 and 65535")
    if args.pod and args.command in ("setup", "catalog", "deploy", "replace"):
        parser.error("--pod applies only to commands that act on an existing Pod")
    explicit_pod = args.pod is not None
    args.state_dir = args.state_dir.resolve()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    state_file = args.state_dir / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    if args.command == "setup":
        state.setdefault("deployment_id", uuid.uuid4().hex)
        save_state(state_file, state)
        args.secret_name = args.secret_name or "notch_" + state["deployment_id"][:12] + "_git"
    key = api_key(args.env_file)
    if not explicit_pod and args.command not in ("catalog", "replace"):
        args.pod = resolve_pod(key, state, state_file)
    if args.command == "setup":
        setup(args, key, state, state_file)
    elif args.command == "catalog":
        print(
            json.dumps(
                request(key, "GET", "/catalog/gpus?include=AVAILABILITY&product=POD&cloud=SECURE&minCudaVersion=12.8"),
                indent=2,
            )
        )
    elif args.command == "replace":
        if not state.get("broker_id"):
            raise RuntimeError("Run hosted.py setup before requesting Pod migration")
        import recovery

        print(json.dumps(recovery.replace_pod(key, state["broker_id"]), indent=2))
    elif args.command == "deploy":
        if not state.get("template_id"):
            raise RuntimeError("Run setup before deploying a Pod")
        if state.get("pod_id"):
            raise RuntimeError("This setup already has a Pod recorded; inspect it before deploying another")
        body = {
            "name": args.name,
            "templateId": state["template_id"],
            "cloud": "SECURE",
            "gpu": {"id": args.gpu, "count": 1, "minCudaVersion": "12.8"},
            "dataCenterIds": [args.datacenter],
            "disk": args.disk_gb,
        }
        if args.storage == "global":
            deployment = {
                "name": args.name,
                "templateId": state["template_id"],
                "cloudType": "SECURE",
                "gpuTypeId": args.gpu,
                "gpuCount": 1,
                "dataCenterId": args.datacenter,
                "containerDiskInGb": args.disk_gb,
                "volumeInGb": 0,
                "startSsh": True,
                "supportPublicIp": True,
                "allowedCudaVersions": ["12.8", "13.0", "13.2"],
                "volumeMounts": [
                    {
                        "volumeId": args.volume_id or state["global_volume_id"],
                        "volumeType": "OBJECT_STORE_VOLUME",
                        "mountPath": "/workspace-global",
                    }
                ],
            }
            if args.minutes:
                deployment["stopAfter"] = (datetime.now(timezone.utc) + timedelta(minutes=args.minutes)).isoformat()
            created = graphql(
                key,
                "mutation ($input: PodFindAndDeployOnDemandInput) { podFindAndDeployOnDemand(input: $input) { id } }",
                {"input": deployment},
            )["podFindAndDeployOnDemand"]
            pod = {"id": created["id"]}
        else:
            if args.storage == "network":
                if not args.volume_id:
                    raise RuntimeError("Network storage requires --volume-id")
                volume = request(key, "GET", "/network-volumes/" + args.volume_id)
                body["dataCenterIds"] = [volume["dataCenter"]]
                body["mounts"] = {"network": [{"volumeId": args.volume_id, "path": "/workspace"}]}
            pod = request(key, "POST", "/pods", body)
        state["pod_id"] = pod["id"]
        state["created_at"] = time.time()
        save_state(state_file, state)
        print(json.dumps(pod_summary(pod), indent=2))
    elif not args.pod:
        raise RuntimeError("No Pod selected; supply --pod or deploy one first")
    elif args.command == "status":
        print(json.dumps(pod_summary(get_pod(key, args.pod)), indent=2))
    elif args.command == "connect":
        connect(args, key, state)
    elif args.command == "start":
        pod_action(key, args.pod, "start")
        print("Started", args.pod)
    elif args.command == "sync":
        sync_storage(args, key)
    elif args.command == "stop":
        if args.storage == "global":
            sync_storage(args, key)
        pod_action(key, args.pod, "stop")
        print("Stopped", args.pod)
    elif args.command == "terminate":
        if args.storage == "global":
            sync_storage(args, key)
        request(key, "DELETE", "/pods/" + args.pod, base="https://rest.runpod.io/v1")
        if not explicit_pod and state.get("pod_id") == args.pod:
            state.pop("pod_id")
            save_state(state_file, state)
        print("Terminated", args.pod)


def resolve_pod(key, state, state_file):
    deployment_id = state.get("deployment_id")
    if not deployment_id:
        if state.get("pod_id"):
            raise RuntimeError("This setup has no deployment identity; inspect the saved configuration")
        return None
    pods = request(key, "GET", "/pods", base="https://rest.runpod.io/v1")
    if not isinstance(pods, list) or any(
        not isinstance(pod, dict)
        or not isinstance(pod.get("id"), str)
        or not pod["id"]
        or not isinstance(pod.get("env"), dict)
        for pod in pods
    ):
        raise RuntimeError("Runpod returned an invalid Pod list; saved Pod is unchanged")
    owned = [pod for pod in pods if pod["env"].get("NOTCH_DEPLOYMENT_ID") == deployment_id]
    if len(owned) > 1:
        raise RuntimeError("Multiple Pods belong to this setup; wait for migration or inspect the deployment")
    if not owned:
        return None
    pod_id = owned[0]["id"]
    if pod_id != state.get("pod_id"):
        save_state(state_file, {**state, "pod_id": pod_id})
        state["pod_id"] = pod_id
    return pod_id


def setup(args, key, state, state_file):
    pod_key = args.state_dir / "pod_ed25519"
    deploy_key = args.state_dir / "github_deploy_ed25519"
    create_key(pod_key, "notch-runpod-pod")
    create_key(deploy_key, "notch-runpod-repo-readonly")
    public = deploy_key.with_suffix(".pub").read_text().strip()
    repo_keys = "repos/" + args.plugin_repo + "/keys"
    existing = json.loads(subprocess.check_output(["gh", "api", repo_keys], text=True))
    registered = next((item for item in existing if item["key"].split()[:2] == public.split()[:2]), None)
    if not registered:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                repo_keys,
                "--input",
                "-",
            ],
            input=json.dumps({"title": "Runpod Notch plugin read-only", "key": public, "read_only": True}),
            text=True,
            check=True,
            capture_output=True,
        )
        registered = json.loads(result.stdout)
    if not registered["read_only"]:
        raise RuntimeError("The plugin deploy key must be read-only")
    state["github_deploy_key_id"] = registered["id"]
    save_state(state_file, state)
    state.setdefault("deployment_id", uuid.uuid4().hex)
    save_state(state_file, state)
    state["secret_id"] = ensure_secret(
        key,
        args.secret_name,
        base64.b64encode(deploy_key.read_bytes()).decode(),
        "Read-only plugin deploy key; setup " + state["deployment_id"],
        state.get("secret_id"),
    )
    save_state(state_file, state)
    if args.storage == "global":
        buckets = graphql(key, "query { myself { globalStoreBuckets { id name } } }")["myself"]["globalStoreBuckets"]
        selected = args.volume_id or state.get("global_volume_id")
        bucket = next((item for item in buckets if item["id"] == selected), None)
        if selected and not bucket:
            raise RuntimeError("Selected global volume does not exist in this account")
        if not bucket:
            bucket_name = args.name + "-models-" + state["deployment_id"][:8]
            bucket = next((item for item in buckets if item["name"] == bucket_name), None)
            if not bucket:
                try:
                    bucket = graphql(
                        key,
                        "mutation ($input: GlobalStoreBucketCreateInput!) { globalStoreBucketCreate(input: $input) { id name } }",
                        {"input": {"name": bucket_name}},
                    )["globalStoreBucketCreate"]
                except (RuntimeError, OSError):
                    buckets = graphql(key, "query { myself { globalStoreBuckets { id name } } }")["myself"][
                        "globalStoreBuckets"
                    ]
                    bucket = next((item for item in buckets if item["name"] == bucket_name), None)
                    if not bucket:
                        raise
        state["global_volume_id"] = bucket["id"]
        save_state(state_file, state)
    bootstrap = Path(__file__).with_name("bootstrap.py").read_text(encoding="utf-8")
    store_script = Path(__file__).with_name("model_store.py").read_text(encoding="utf-8")
    bootstrap = bootstrap.replace('_MODEL_STORE_SCRIPT = ""', "_MODEL_STORE_SCRIPT = " + repr(store_script))
    bootstrap = bootstrap.replace(
        '_PARK_SCRIPT = ""', "_PARK_SCRIPT = " + repr(Path(__file__).with_name("park.py").read_text(encoding="utf-8"))
    )
    template = {
        "name": args.name,
        "image": IMAGE,
        "category": "NVIDIA",
        "public": False,
        "serverless": False,
        "disk": args.disk_gb,
        "mounts": {"persistent": {"size": args.volume_gb, "path": "/workspace"}},
        "args": json.dumps({"entrypoint": ["python3", "-c"], "cmd": [bootstrap]}),
        "ports": ["22/tcp"],
        "startSsh": True,
        "startJupyter": False,
        "env": {
            **state.get("hosted_env", {}),
            "NOTCH_DEPLOYMENT_ID": state["deployment_id"],
            "PUBLIC_KEY": pod_key.with_suffix(".pub").read_text().strip(),
            "NOTCH_GIT_KEY_B64": "{{ RUNPOD_SECRET_" + args.secret_name + " }}",
            "NOTCH_PLUGIN_REF": args.ref,
            "NOTCH_COMFY_REF": args.comfy_ref,
            "NOTCH_PLUGIN_REPO": args.plugin_repo,
            "NOTCH_COMFY_PORT": str(args.comfy_port),
            "NOTCH_INSTANCE_NAME": args.instance_name,
            "NOTCH_IDLE_MINUTES": str(args.idle_minutes),
            "NOTCH_MAX_MINUTES": str(args.minutes),
            "NOTCH_GLOBAL_STORE": "/workspace-global" if args.storage == "global" else "",
        },
    }
    (args.state_dir / "template.json").write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    if state.get("template_id"):
        result = request(key, "PATCH", "/templates/" + state["template_id"], template)
    else:
        result = create_resource(key, "/templates", "templates", template, state["deployment_id"])
    state["template_id"] = result["id"]
    state["plugin_ref"] = args.ref
    state["config"] = {
        name: getattr(args, name)
        for name in (
            "name",
            "instance_name",
            "plugin_repo",
            "secret_name",
            "ref",
            "comfy_ref",
            "storage",
            "volume_id",
            "disk_gb",
            "volume_gb",
            "comfy_port",
            "local_port",
            "gpu",
            "datacenter",
            "minutes",
            "idle_minutes",
        )
    }
    save_state(state_file, state)
    print(
        json.dumps(
            {
                "template_id": result["id"],
                "deploy_url": "https://console.runpod.io/deploy?template=" + result["id"],
                "image": IMAGE,
                "plugin_ref": args.ref,
            },
            indent=2,
        )
    )


def connect(args, key, state):
    pod = get_pod(key, args.pod)
    if pod["status"] != "RUNNING":
        if not args.start:
            raise RuntimeError("Pod is stopped; use connect --start to start it and open the tunnel")
        pod_action(key, args.pod, "start")
        print("Starting Pod...", flush=True)
    deadline = time.monotonic() + args.timeout
    while True:
        pod = get_pod(key, args.pod)
        direct = pod.get("ssh", {}).get("direct")
        if pod["status"] == "RUNNING" and direct:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out waiting for the Pod's direct SSH endpoint")
        time.sleep(5)
    port = args.local_port
    command = [
        "ssh",
        "-N",
        "-T",
        "-i",
        str(args.state_dir / "pod_ed25519"),
        "-p",
        str(direct["port"]),
        "-L",
        f"127.0.0.1:{port}:127.0.0.1:{args.comfy_port}",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=" + str(args.state_dir / "known_hosts"),
        direct.get("username", "root") + "@" + direct["host"],
    ]
    with open(args.state_dir / "tunnel.log", "ab") as log:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags)
    try:
        while True:
            if process.poll() is not None:
                raise RuntimeError("SSH failed; see " + str(args.state_dir / "tunnel.log"))
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/system_stats", timeout=3) as response:
                    if "comfyui_version" in json.load(response).get("system", {}):
                        break
            except (OSError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for ComfyUI startup")
            time.sleep(2)
        print(f"Notch: Host 127.0.0.1, Port {port}, Transport HTTP", flush=True)
        print(f"ComfyUI: http://127.0.0.1:{port}", flush=True)
        if args.background:
            (args.state_dir / "tunnel-pid.txt").write_text(str(process.pid))
            print("Tunnel process:", process.pid)
        else:
            process.wait()
    finally:
        if not args.background or sys.exc_info()[0] is not None:
            process.terminate()


def sync_storage(args, key):
    pod = get_pod(key, args.pod)
    if pod["status"] != "RUNNING":
        return
    direct = pod.get("ssh", {}).get("direct")
    if not direct:
        raise RuntimeError("No SSH endpoint available to sync storage")
    command = [
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
        direct.get("username", "root") + "@" + direct["host"],
        "python3 /opt/notch-model-store.py --local /workspace/runpod-slim/ComfyUI --store /workspace-global/notch --mode sync",
    ]
    subprocess.run(command, check=True)


def api_key(env_file):
    value = os.environ.get("RUNPOD_API_KEY", "") or read_env_value(env_file, "RUNPOD_API_KEY")
    if not value and sys.stdin.isatty():
        value = getpass.getpass("Runpod API key: ").strip()
        if value:
            save_env_value(env_file, "RUNPOD_API_KEY", value)
    if not value:
        raise RuntimeError("Set RUNPOD_API_KEY in the environment or the specified .env file")
    return value


def read_env_value(path, name):
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"\s*" + re.escape(name) + r"\s*[:=]\s*(.+)", line)
            if match:
                return match[1].strip().strip("\"'")
    return ""


def save_env_value(path, name, value):
    if "\n" in value or "\r" in value:
        raise ValueError("Environment values must fit on one line")
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    result = []
    replaced = False
    for line in lines:
        if re.match(r"\s*" + re.escape(name) + r"\s*[:=]", line):
            if not replaced:
                result.append(name + "=" + value)
                replaced = True
        else:
            result.append(line)
    if not replaced:
        result.append(name + "=" + value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.touch(exist_ok=True)
    private_file(temporary)
    temporary.write_text("\n".join(result) + "\n", encoding="utf-8")
    temporary.replace(path)


def ensure_secret(key, name, value, description, tracked_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
        raise RuntimeError("Secret names must contain only letters, numbers, underscores or hyphens")

    def find():
        items = graphql(key, "query { myself { secrets { id name description } } }")["myself"]["secrets"]
        return next((item for item in items if item["name"] == name), None)

    found = find()
    if found:
        if found["id"] != tracked_id and found.get("description") != description:
            raise RuntimeError("A secret with this name already exists outside this setup: " + name)
        graphql(
            key,
            "mutation ($id: ID!, $value: String!) { secretValueUpdate(input: {id: $id, value: $value}) { id } }",
            {"id": found["id"], "value": value},
            retry=True,
        )
    else:
        try:
            found = graphql(
                key,
                "mutation ($name: String!, $value: String!, $description: String) { secretCreate(input: {name: $name, value: $value, description: $description}) { id } }",
                {"name": name, "value": value, "description": description},
            )["secretCreate"]
        except (RuntimeError, OSError):
            found = find()
            if not found or found.get("description") != description:
                raise
    return found["id"]


def create_resource(key, path, collection, body, deployment_id):
    found = find_resource(key, path, collection, deployment_id)
    if found:
        return request(key, "PATCH", path + "/" + found["id"], {k: v for k, v in body.items() if k != "type"})
    try:
        return request(key, "POST", path, body)
    except (RuntimeError, OSError):
        found = find_resource(key, path, collection, deployment_id)
        if found:
            return found
        raise


def find_resource(key, path, collection, deployment_id):
    items = request(key, "GET", path)[collection]
    owned = [item for item in items if item.get("env", {}).get("NOTCH_DEPLOYMENT_ID") == deployment_id]
    if len(owned) > 1:
        raise RuntimeError("Multiple resources belong to this setup; inspect " + path)
    return owned[0] if owned else None


def get_pod(key, pod_id):
    pod = request(key, "GET", "/pods/" + pod_id, base="https://rest.runpod.io/v1")
    direct = None
    port = pod.get("portMappings", {}).get("22")
    if pod.get("publicIp") and port:
        direct = {"host": pod["publicIp"], "port": port, "username": "root"}
    return {**pod, "status": pod.get("desiredStatus"), "ssh": {"direct": direct}}


def pod_action(key, pod_id, action):
    return request(key, "POST", "/pods/" + pod_id + "/" + action, {}, base="https://rest.runpod.io/v1")


def pod_summary(pod):
    fields = ("id", "name", "status", "costPerHr", "gpuCount", "dataCenterId", "ssh")
    return {name: pod[name] for name in fields if pod.get(name) is not None}


def create_key(path, comment):
    if not path.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)], check=True)
    private_file(path)


def private_file(path):
    if os.name == "nt":
        encoded_path = base64.b64encode(str(path.resolve()).encode("utf-8")).decode()
        script = "$ErrorActionPreference='Stop';"
        script += "$target=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded_path + "'));"
        script += "$identity=[Security.Principal.WindowsIdentity]::GetCurrent().User;"
        script += "$acl=[Security.AccessControl.FileSecurity]::new();"
        script += "$acl.SetAccessRuleProtection($true,$false);"
        script += "$rule=[Security.AccessControl.FileSystemAccessRule]::new($identity,'FullControl','Allow');"
        script += "$acl.AddAccessRule($rule);[IO.File]::SetAccessControl($target,$acl)"
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    else:
        path.chmod(0o600)


def save_state(path, state):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    private_file(temporary)
    temporary.replace(path)


def request(key, method, path, data=None, base=API):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "ComfyUI-Notch-Runpod/0.1",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if method in ("GET", "PATCH") and error.code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(2**attempt)
                continue
            detail = error.read().decode("utf-8", errors="replace")
            if "not enough free GPUs" in detail:
                raise RuntimeError(
                    "The stopped Pod's GPU is unavailable. Retry later or use replace to request native migration."
                ) from None
            raise RuntimeError(f"Runpod {method} {path}: HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError):
            if method in ("GET", "PATCH") and attempt < 3:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"Runpod {method} {path}: connection failed") from None


def graphql(key, query, variables=None, retry=False):
    req = urllib.request.Request(
        "https://api.runpod.io/graphql",
        data=json.dumps(
            {
                "query": query,
                "variables": variables or {},
            }
        ).encode(),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "ComfyUI-Notch-Runpod/0.1",
        },
    )
    operation = re.search(r"\{\s*(\w+)", query)
    label = operation[1] if operation else "operation"
    safe_retry = retry or query.lstrip().startswith(("query", "{"))
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.load(response)
            break
        except urllib.error.HTTPError as error:
            if safe_retry and error.code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"Runpod GraphQL {label}: HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError):
            if safe_retry and attempt < 3:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"Runpod GraphQL {label}: connection failed") from None
    if result.get("errors"):
        if label in ("secretCreate", "secretValueUpdate", "createApiKeyNew"):
            detail = "operation rejected"
        else:
            detail = "; ".join(str(error.get("message", "operation rejected")) for error in result["errors"])
            detail = detail.replace(key, "[redacted]")
        raise RuntimeError(f"Runpod GraphQL {label}: {detail}")
    return result["data"]


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, urllib.error.URLError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
