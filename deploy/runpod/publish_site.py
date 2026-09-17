"""Publish reviewed connector files under docs/ without changing repository sources."""

import argparse
import hashlib
import json
import re
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_REPOSITORY = "jKaarlehto/comfyui-hosted-connector"
SITE_FILES = frozenset({"index.html", "connect.js", "style.css", "installed.html", "installed.js"})
DOWNLOAD_FILES = frozenset(
    {
        "downloads/release.json",
        "downloads/HostedComfyUIConnector.exe",
        "downloads/HostedComfyUI.appinstaller",
        "downloads/HostedComfyUI.msix",
    }
)
PUBLIC_FILES = SITE_FILES | DOWNLOAD_FILES | {".nojekyll"}
MAX_FILE_BYTES = 95 * 1024 * 1024


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "publish", "status"))
    parser.add_argument("--site", type=Path, help="Prepared public files from prepare_site.py")
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY)
    parser.add_argument("--wait", type=int, default=180, help="Seconds to wait for public files after publishing")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("Repository must be OWNER/NAME")
    if args.command == "status":
        print(json.dumps(_github("GET", f"repos/{args.repo}/pages"), indent=2))
        return
    if not args.site:
        parser.error("--site is required")
    payload = read_public_payload(args.site)
    for name, data in sorted(payload.items()):
        print(f"{hashlib.sha256(data).hexdigest()}  {len(data):>9}  {name}")
    if args.command == "publish":
        publish(payload, args.repo, args.wait)


def read_public_payload(source):
    source = Path(source).absolute()
    if not source.is_dir() or _is_link(source):
        raise RuntimeError("Public source must be a regular directory")
    payload = {}
    for path in source.rglob("*"):
        name = path.relative_to(source).as_posix()
        if _is_link(path):
            raise RuntimeError(f"Linked public path is not allowed: {name}")
        if path.is_dir():
            if name != "downloads":
                raise RuntimeError(f"Unexpected public directory: {name}")
            continue
        if not path.is_file() or name not in PUBLIC_FILES:
            raise RuntimeError(f"Unexpected public file: {name}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise RuntimeError(f"Public file exceeds GitHub's ordinary Git size limit: {name}")
        payload[name] = path.read_bytes()
    required = SITE_FILES | {"downloads/release.json"}
    if missing := required - payload.keys():
        raise RuntimeError("Missing public files: " + ", ".join(sorted(missing)))
    release = json.loads(payload["downloads/release.json"])
    if set(release) != {"installerAvailable", "appInstallerAvailable"} or any(
        type(value) is not bool for value in release.values()
    ):
        raise RuntimeError("Invalid public release manifest")
    for available, names in (
        (release["installerAvailable"], ("downloads/HostedComfyUIConnector.exe",)),
        (release["appInstallerAvailable"], ("downloads/HostedComfyUI.appinstaller", "downloads/HostedComfyUI.msix")),
    ):
        if any((name in payload) != available for name in names):
            raise RuntimeError("Release manifest does not match the public downloads")
    installer = payload.get("downloads/HostedComfyUIConnector.exe")
    if installer is not None and not installer.startswith(b"MZ"):
        raise RuntimeError("Connector installer is not a Windows executable")
    payload[".nojekyll"] = b""
    return payload


def publish(payload, repository, wait_seconds=180):
    details = _github("GET", f"repos/{repository}")
    if details.get("private") is not False:
        raise RuntimeError("Use the public connector repository")
    page = _github("GET", f"repos/{repository}/pages", allow_missing=True)
    if page is not None and page.get("source") != {"branch": "main", "path": "/docs"}:
        raise RuntimeError("Existing Pages site must use main /docs; review its configuration before publishing")
    account = _github("GET", "user")
    with tempfile.TemporaryDirectory(prefix="hosted-comfyui-pages-") as directory:
        checkout = Path(directory)
        _git(checkout, "init", "--initial-branch=main")
        _git(checkout, "config", "core.autocrlf", "false")
        _git(checkout, "remote", "add", "origin", f"https://github.com/{repository}.git")
        existing = _git(checkout, "ls-remote", "--heads", "origin", "refs/heads/main")
        if existing.strip():
            _git(checkout, "fetch", "--depth=1", "origin", "main")
            _git(checkout, "checkout", "-B", "main", "FETCH_HEAD")
        stage_public_payload(checkout, payload)
        if _git(checkout, "diff", "--cached", "--name-only").strip():
            _git(
                checkout,
                "-c",
                f"user.name={account['login']}",
                "-c",
                f"user.email={account['id']}+{account['login']}@users.noreply.github.com",
                "commit",
                "-m",
                "update hosted connector downloads",
            )
            _git(checkout, "push", "origin", "main")
    if page is None:
        page = _github("POST", f"repos/{repository}/pages", {"source": {"branch": "main", "path": "/docs"}})
    origin = page["html_url"]
    print("GitHub Pages:", origin)
    if wait_seconds:
        verify_public_files(origin, payload, wait_seconds)


def stage_public_payload(checkout, payload):
    if set(payload) - PUBLIC_FILES:
        raise RuntimeError("Payload contains files outside the public allowlist")
    docs = checkout / "docs"
    if docs.exists() or docs.is_symlink():
        read_public_payload(docs)
    tracked = set(_git(checkout, "ls-files", "--", "docs").splitlines())
    expected = {"docs/" + name for name in payload}
    allowed = {"docs/" + name for name in PUBLIC_FILES}
    if tracked - allowed:
        raise RuntimeError("The docs folder contains tracked files outside the public allowlist")
    if set(_git(checkout, "diff", "--cached", "--name-only").splitlines()) - allowed:
        raise RuntimeError("Unrelated changes are already staged; publish from a clean checkout")
    for name in tracked - expected:
        (checkout / name).unlink()
    for name, data in payload.items():
        target = docs / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if read_public_payload(docs) != payload:
        raise RuntimeError("Written public files differ from the reviewed payload")
    _git(checkout, "add", "--all", "--", "docs")
    staged = set(_git(checkout, "ls-files", "--", "docs").splitlines())
    changed = set(_git(checkout, "diff", "--cached", "--name-only").splitlines())
    if staged != expected or changed - allowed:
        raise RuntimeError("Staged changes differ from the reviewed docs payload")


def verify_public_files(origin, payload, timeout):
    deadline = time.monotonic() + timeout
    pending = set(payload) - {".nojekyll"}
    while pending:
        for name in sorted(pending):
            try:
                request = urllib.request.Request(origin + name, headers={"User-Agent": "Hosted-ComfyUI-Publisher"})
                with urllib.request.urlopen(request, timeout=15) as response:
                    data = response.read(MAX_FILE_BYTES + 1)
                if data == payload[name]:
                    pending.remove(name)
            except (urllib.error.URLError, TimeoutError):
                pass
        if not pending:
            print("Public HTTPS files verified:", len(payload) - 1)
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Pages deployment is still pending: " + ", ".join(sorted(pending)))
        time.sleep(5)


def _is_link(path):
    metadata = path.lstat()
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _github(method, endpoint, data=None, allow_missing=False):
    command = ["gh", "api", "--method", method, endpoint]
    if data is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(
        command, input=json.dumps(data) if data is not None else None, capture_output=True, text=True
    )
    if allow_missing and result.returncode and "HTTP 404" in result.stderr:
        return None
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def _git(directory, *arguments):
    result = subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


if __name__ == "__main__":
    main()
