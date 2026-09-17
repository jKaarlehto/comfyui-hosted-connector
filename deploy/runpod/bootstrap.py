"""Add ComfyUI-Notch to the pinned official Runpod ComfyUI image."""

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

START_SHA256 = "a265753f3bf3f54b38a7badabe1656da414a5d50fa9abb4efe2fd2542e309084"
GITHUB_HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
_MODEL_STORE_SCRIPT = ""
_PARK_SCRIPT = ""


def main():
    startup = Path("/start.sh").read_bytes()
    if hashlib.sha256(startup).hexdigest() != START_SHA256:
        raise RuntimeError("The base image changed; review its startup script before updating the image pin")
    revision = os.environ["NOTCH_PLUGIN_REF"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", revision):
        raise ValueError("NOTCH_PLUGIN_REF must be a branch, tag or commit SHA")
    comfy_revision = os.environ.get("NOTCH_COMFY_REF", "master")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", comfy_revision):
        raise ValueError("NOTCH_COMFY_REF must be a branch, tag or commit SHA")
    os.environ["NOTCH_COMFY_REF"] = comfy_revision
    encoded_key = os.environ.pop("NOTCH_GIT_KEY_B64")
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
        known.write_text("[ssh.github.com]:443 " + GITHUB_HOST_KEY + "\n")
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
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "fetch",
                "--quiet",
                "--depth=1",
                "git@github.com:" + repository + ".git",
                revision,
            ],
            env=git_env,
            check=True,
            timeout=180,
        )
        subprocess.run(["git", "-C", str(checkout), "checkout", "--quiet", "--detach", "FETCH_HEAD"], check=True)
    actual = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    if re.fullmatch(r"[0-9a-f]{40}", revision) and actual != revision:
        raise RuntimeError("Plugin revision verification failed")
    print("[Notch Runpod] Plugin revision " + actual + " (" + revision + ")", flush=True)
    prepare_http_requirements(checkout / "requirements.txt", Path("/opt/notch-http-requirements.txt"))

    store = os.environ.get("NOTCH_GLOBAL_STORE", "")
    if store:
        if not Path(store).is_mount():
            raise RuntimeError("The global volume is not mounted at " + store)
        Path("/opt/notch-model-store.py").write_text(_MODEL_STORE_SCRIPT, encoding="utf-8")
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
    hook = """
echo "[Notch Runpod] Updating ComfyUI"
git -C "$COMFYUI_DIR" fetch --quiet --depth=1 https://github.com/Comfy-Org/ComfyUI.git "$NOTCH_COMFY_REF"
git -C "$COMFYUI_DIR" checkout --quiet --detach FETCH_HEAD
echo "[Notch Runpod] ComfyUI revision $(git -C "$COMFYUI_DIR" rev-parse HEAD)"
python -m pip install --disable-pip-version-check --no-input --prefer-binary \
    --constraint /opt/comfyui-runtime-constraints.txt -r "$COMFYUI_DIR/requirements.txt"
NOTCH_TARGET="$COMFYUI_DIR/custom_nodes/ComfyUI-Notch"
if [ -e "$NOTCH_TARGET" ] && [ ! -f "$NOTCH_TARGET/.runpod-managed" ]; then
    echo "Refusing to replace an unmanaged ComfyUI-Notch installation" >&2
    exit 1
fi
mkdir -p "$NOTCH_TARGET"
rsync -a --delete --exclude=.git /opt/notch-plugin/ "$NOTCH_TARGET/"
touch "$NOTCH_TARGET/.runpod-managed"
python -m pip install --disable-pip-version-check --no-input --prefer-binary \
    --constraint /opt/comfyui-runtime-constraints.txt -r /opt/notch-http-requirements.txt
export NOTCH_AUTO_INSTALL=0
if [ -n "${NOTCH_GLOBAL_STORE:-}" ]; then
    python /opt/notch-model-store.py --local "$COMFYUI_DIR" --store "$NOTCH_GLOBAL_STORE/notch" --mode restore
    python /opt/notch-model-store.py --local "$COMFYUI_DIR" --store "$NOTCH_GLOBAL_STORE/notch" --mode watch &
fi
echo "[Notch Runpod] Plugin ready; starting ComfyUI"
python /opt/notch-park.py &
python main.py $FIXED_ARGS &
"""
    source = startup.decode("utf-8").replace("--port 8188", "--port " + str(port))
    marker = "python main.py $FIXED_ARGS &"
    if source.count(marker) != 1:
        raise RuntimeError("Expected one ComfyUI startup command")
    patched = Path("/opt/notch-start.sh")
    patched.write_text(source.replace(marker, hook), encoding="utf-8")
    os.execv("/bin/bash", ["/bin/bash", str(patched)])


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
    main()
