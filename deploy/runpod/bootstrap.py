"""Add ComfyUI-Notch to the pinned official Runpod ComfyUI image."""

import base64
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

START_SHA256 = "a265753f3bf3f54b38a7badabe1656da414a5d50fa9abb4efe2fd2542e309084"
GITHUB_HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
_MODEL_STORE_SCRIPT = ""
_MODEL_CACHE_SCRIPT = ""
_PARK_SCRIPT = ""
_HEALTH_SCRIPT = ""
COMFY_UPDATE_SCRIPT = """
echo "[Notch Runpod] Updating ComfyUI"
git -C "$COMFYUI_DIR" fetch --quiet --depth=1 https://github.com/Comfy-Org/ComfyUI.git "$NOTCH_COMFY_REF" || exit $?
git -C "$COMFYUI_DIR" checkout --quiet --detach FETCH_HEAD || exit $?
echo "[Notch Runpod] ComfyUI revision $(git -C "$COMFYUI_DIR" rev-parse HEAD) ($NOTCH_COMFY_REF)"
"""
STATUS_SCRIPT = """
NOTCH_BOOT_STAGE=starting_services
notch_stage() {
    NOTCH_BOOT_STAGE="$1"
    printf '{"stage":"%s"}\\n' "$NOTCH_BOOT_STAGE" > /opt/notch-startup-stage.tmp
    mv /opt/notch-startup-stage.tmp /opt/notch-startup-stage
}
notch_failed() {
    printf '{"stage":"%s","error":true}\\n' "$NOTCH_BOOT_STAGE" > /opt/notch-startup-stage.tmp
    mv /opt/notch-startup-stage.tmp /opt/notch-startup-stage
    sleep 30
}
trap 'if [ $? -ne 0 ]; then notch_failed; fi' EXIT
"""


def set_stage(stage, error=False):
    path = Path("/opt/notch-startup-stage")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"stage": stage, "error": error}))
    temporary.replace(path)


def start_ssh():
    folder = Path("/root/.ssh")
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    keys = folder / "authorized_keys"
    keys.write_text(os.environ["PUBLIC_KEY"].strip() + "\n")
    keys.chmod(0o600)
    Path("/run/sshd").mkdir(exist_ok=True)
    subprocess.run(["ssh-keygen", "-A", "-q"], check=True)
    subprocess.run(["/usr/sbin/sshd", "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no"], check=True)


def main():
    set_stage("starting_services")
    startup = Path("/start.sh").read_bytes()
    if hashlib.sha256(startup).hexdigest() != START_SHA256:
        raise RuntimeError("The base image changed; review its startup script before updating the image pin")
    store = os.environ.get("NOTCH_GLOBAL_STORE", "")
    if store:
        if not Path(store).is_mount():
            raise RuntimeError("The global volume is not mounted at " + store)
        Path("/opt/notch-model-store.py").write_text(_MODEL_STORE_SCRIPT, encoding="utf-8")
        Path("/opt/notch-model-cache.py").write_text(_MODEL_CACHE_SCRIPT, encoding="utf-8")
    host_key = os.environ.pop("NOTCH_SSH_HOST_KEY_B64", "")
    if host_key:
        host_path = Path("/etc/ssh/ssh_host_ed25519_key")
        host_path.write_bytes(base64.b64decode(host_key, validate=True))
        host_path.chmod(0o600)
        public = subprocess.check_output(["ssh-keygen", "-y", "-f", str(host_path)])
        host_path.with_suffix(".pub").write_bytes(public)
    configure_ssh(Path("/etc/ssh/sshd_config"))
    port = int(os.environ.get("NOTCH_COMFY_PORT", "8188"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid ComfyUI port")
    health_key = os.environ.get("NOTCH_HEALTH_PUBLIC_KEY", "")
    if health_key:
        if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+", health_key):
            raise ValueError("Invalid health public key")
        Path("/opt/notch-health.py").write_text(_HEALTH_SCRIPT, encoding="utf-8")
        Path("/opt/notch-health.json").write_text(
            json.dumps(
                {
                    "deployment_id": os.environ["NOTCH_DEPLOYMENT_ID"],
                    "pod_id": os.environ["RUNPOD_POD_ID"],
                    "port": port,
                    "store": store,
                }
            )
        )
        os.environ["PUBLIC_KEY"] = (
            os.environ.get("PUBLIC_KEY", "")
            + '\nrestrict,command="/usr/bin/python3 /opt/notch-health.py" '
            + health_key
            + " notch-health"
        )
    Path("/opt/notch-storage-restored").unlink(missing_ok=True)
    guests_path = Path(store, "notch/guests.json") if store else Path("/workspace/.notch-hosted/guests.json")
    if guests_path.is_file():
        guests = json.loads(guests_path.read_text())
        keys = [os.environ.get("PUBLIC_KEY", "")]
        for name, public in guests.items():
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", name) or not re.fullmatch(
                r"ssh-ed25519 [A-Za-z0-9+/=]+", public
            ):
                raise ValueError("Invalid guest public key")
            keys.append(
                f'restrict,port-forwarding,permitopen="127.0.0.1:{port}",command="/bin/false" '
                + public
                + " notch-guest-"
                + name
            )
        os.environ["PUBLIC_KEY"] = "\n".join(keys)
    Path("/opt/notch-park.py").write_text(_PARK_SCRIPT, encoding="utf-8")
    encoded_key = os.environ.pop("NOTCH_GIT_KEY_B64")
    start_ssh()
    set_stage("checking_updates")
    os.environ["NOTCH_COMFY_REF"] = resolve_comfy_ref(os.environ.get("NOTCH_COMFY_REF", "stable"))
    set_stage("fetching_plugin")
    revision = os.environ["NOTCH_PLUGIN_REF"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", revision):
        raise ValueError("NOTCH_PLUGIN_REF must be a branch, tag or commit SHA")
    if "RUNPOD_SECRET" in encoded_key:
        raise RuntimeError("Runpod did not resolve the plugin deploy-key secret")
    repository = os.environ.get("NOTCH_PLUGIN_REPO", "jKaarlehto/ComfyUI-Notch")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("NOTCH_PLUGIN_REPO must be a GitHub owner/repository")
    checkout = Path("/opt/notch-plugin")
    checkout.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="notch-git-", dir="/dev/shm") as directory:
        key = Path(directory, "key")
        key.write_bytes(base64.b64decode(encoded_key, validate=True))
        key.chmod(0o600)
        known = Path(directory, "known_hosts")
        known.write_text("github.com " + GITHUB_HOST_KEY + "\n[ssh.github.com]:443 " + GITHUB_HOST_KEY + "\n")
        git_env = os.environ.copy()
        git_env["GIT_TERMINAL_PROMPT"] = "0"
        git_env["GIT_SSH_COMMAND"] = shlex.join(
            [
                "ssh",
                "-i",
                str(key),
                "-p",
                "443",
                "-o",
                "Hostname=ssh.github.com",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=20",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=" + str(known),
            ]
        )
        actual = checkout_plugin(checkout, repository, revision, git_env)
    print("[Notch Runpod] Plugin revision " + actual + " (" + revision + ")", flush=True)
    prepare_http_requirements(checkout / "requirements.txt", Path("/opt/notch-http-requirements.txt"))

    set_stage("starting_services")
    hook = (
        "notch_stage updating_comfy\n" + COMFY_UPDATE_SCRIPT
        + """
notch_stage installing_dependencies
python -m pip install --disable-pip-version-check --no-input --prefer-binary \
    --constraint /opt/comfyui-runtime-constraints.txt -r "$COMFYUI_DIR/requirements.txt" || exit $?
NOTCH_TARGET="$COMFYUI_DIR/custom_nodes/ComfyUI-Notch"
if [ -e "$NOTCH_TARGET" ] && [ ! -f "$NOTCH_TARGET/.runpod-managed" ]; then
    echo "Refusing to replace an unmanaged ComfyUI-Notch installation" >&2
    exit 1
fi
mkdir -p "$NOTCH_TARGET"
rsync -a --delete --exclude=.git /opt/notch-plugin/ "$NOTCH_TARGET/"
touch "$NOTCH_TARGET/.runpod-managed"
notch_stage installing_dependencies
python -m pip install --disable-pip-version-check --no-input --prefer-binary \
    --constraint /opt/comfyui-runtime-constraints.txt -r /opt/notch-http-requirements.txt || exit $?
export NOTCH_AUTO_INSTALL=0
if [ -n "${NOTCH_GLOBAL_STORE:-}" ]; then
    notch_stage preparing_files
    python /opt/notch-model-store.py --local "$COMFYUI_DIR" --store "$NOTCH_GLOBAL_STORE/notch" --mode prepare || exit $?
    mkdir -p "$COMFYUI_DIR/custom_nodes/hosted_model_cache"
    cp /opt/notch-model-cache.py "$COMFYUI_DIR/custom_nodes/hosted_model_cache/__init__.py" || exit $?
    touch /opt/notch-storage-restored
    python /opt/notch-model-store.py --local "$COMFYUI_DIR" --store "$NOTCH_GLOBAL_STORE/notch" --mode watch &
fi
echo "[Notch Runpod] Plugin ready; starting ComfyUI"
python /opt/notch-park.py &
notch_stage starting_comfy
(python main.py $FIXED_ARGS || notch_failed) &
"""
    )
    source = startup.decode("utf-8").replace("--port 8188", "--port " + str(port))
    if source.count("\nsetup_ssh\n") != 1:
        raise RuntimeError("Expected one SSH startup command")
    source = STATUS_SCRIPT + source.replace("\nsetup_ssh\n", "\n")
    marker = "python main.py $FIXED_ARGS &"
    if source.count(marker) != 1:
        raise RuntimeError("Expected one ComfyUI startup command")
    patched = Path("/opt/notch-start.sh")
    patched.write_text(source.replace(marker, hook), encoding="utf-8")
    os.execv("/bin/bash", ["/bin/bash", str(patched)])


def resolve_comfy_ref(revision):
    if revision != "stable":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", revision):
            raise ValueError("NOTCH_COMFY_REF must be stable, a branch, tag or commit SHA")
        return revision
    request = urllib.request.Request(
        "https://api.github.com/repos/Comfy-Org/ComfyUI/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Hosted-ComfyUI-Connector",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError):
        raise RuntimeError(
            "Could not resolve the latest stable ComfyUI release; retry startup or set --comfy-ref"
        ) from None
    if (
        not isinstance(release, dict)
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or not isinstance(release.get("tag_name"), str)
        or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", release["tag_name"])
    ):
        raise RuntimeError("GitHub did not return a valid stable ComfyUI release")
    return "refs/tags/" + release["tag_name"]


def checkout_plugin(checkout, repository, revision, git_env):
    for attempt in range(3):
        with tempfile.TemporaryDirectory(prefix="notch-checkout-", dir=checkout.parent) as directory:
            stage = Path(directory, "plugin")
            subprocess.run(["git", "init", "-q", str(stage)], check=True)
            subprocess.run(["git", "-C", str(stage), "remote", "add", "origin", "git@github.com:" + repository + ".git"], check=True)
            subprocess.run(["git", "-C", str(stage), "config", "remote.origin.promisor", "true"], check=True)
            subprocess.run(["git", "-C", str(stage), "config", "remote.origin.partialclonefilter", "blob:none"], check=True)
            subprocess.run(["git", "-C", str(stage), "sparse-checkout", "set", "--no-cone", "--stdin"], input="/*\n!/tests/\n!/cpp/\n", text=True, check=True)
            attempt_env = git_env.copy()
            if attempt == 1 and "GIT_SSH_COMMAND" in attempt_env:
                arguments = shlex.split(attempt_env["GIT_SSH_COMMAND"])
                arguments = ["Hostname=github.com" if item == "Hostname=ssh.github.com" else item for item in arguments]
                for index, value in enumerate(arguments[:-1]):
                    if value == "-p":
                        arguments[index + 1] = "22"
                attempt_env["GIT_SSH_COMMAND"] = shlex.join(arguments)
            try:
                fetch_plugin(stage, repository, revision, attempt_env)
                run_git(["git", "-C", str(stage), "checkout", "--quiet", "--detach", "FETCH_HEAD"], attempt_env)
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                if attempt == 2:
                    raise
                print("[Notch Runpod] Plugin fetch failed; retrying", flush=True)
                continue
            actual = subprocess.check_output(["git", "-C", str(stage), "rev-parse", "HEAD"], text=True).strip()
            if re.fullmatch(r"[0-9a-f]{40}", revision) and actual != revision:
                raise RuntimeError("Plugin revision verification failed")
            if checkout.exists():
                checkout.replace(Path(directory, "previous"))
            stage.replace(checkout)
            return actual


def fetch_plugin(checkout, repository, revision, git_env):
    command = ["git", "-C", str(checkout), "fetch", "--quiet", "--filter=blob:none", "--depth=1", "origin", revision]
    run_git(command, git_env)


def run_git(command, git_env):
    with subprocess.Popen(command, env=git_env, start_new_session=os.name == "posix") as process:
        try:
            result = process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
            raise
        if result:
            raise subprocess.CalledProcessError(result, command)


def prepare_http_requirements(source, target):
    lines = source.read_text(encoding="utf-8").splitlines()
    lines = [line for line in lines if not re.match(r"\s*cuda[-_]python(?:[<=>!~;\s\[]|$)", line, re.IGNORECASE)]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def configure_ssh(path):
    settings = "AllowTcpForwarding local\nAllowStreamLocalForwarding no\n"
    current = path.read_text(encoding="utf-8")
    if not current.startswith(settings):
        path.write_text(settings + current, encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        stage_path = Path("/opt/notch-startup-stage")
        if stage_path.exists():
            stage = json.loads(stage_path.read_text())["stage"]
            set_stage(stage, error=True)
        time.sleep(30)
        raise
