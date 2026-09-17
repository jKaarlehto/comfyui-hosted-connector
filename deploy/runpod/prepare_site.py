"""Prepare the public invitation page and explicit connector downloads."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

SITE_FILES = ("index.html", "connect.js", "style.css", "installed.html", "installed.js")
DOWNLOAD_FILES = ("HostedComfyUIConnector.exe", "HostedComfyUI.appinstaller", "HostedComfyUI.msix", "release.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--appinstaller", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--signtool", default="signtool.exe")
    args = parser.parse_args()
    if bool(args.appinstaller) != bool(args.package):
        parser.error("Provide both --appinstaller and --package for App Installer downloads")
    if args.package:
        subprocess.run([args.signtool, "verify", "/pa", str(args.package)], check=True)
    prepare_site(args.output, args.installer, args.appinstaller, args.package)


def prepare_site(output, installer=None, appinstaller=None, package=None):
    source = Path(__file__).with_name("site")
    if output.resolve() == source.resolve():
        raise RuntimeError("Choose a staging output folder outside the source site directory")
    allowed = set(SITE_FILES) | {"downloads/" + name for name in DOWNLOAD_FILES} | {".nojekyll"}
    if output.exists():
        for existing in output.rglob("*"):
            if existing.is_file() and existing.relative_to(output).as_posix() not in allowed:
                raise RuntimeError("The public staging folder contains an unexpected file; use a clean output folder")
    if bool(appinstaller) != bool(package):
        raise RuntimeError("App Installer metadata and package must be supplied together")
    artifacts = {
        "HostedComfyUIConnector.exe": installer,
        "HostedComfyUI.appinstaller": appinstaller,
        "HostedComfyUI.msix": package,
    }
    for artifact in artifacts.values():
        if artifact and not artifact.is_file():
            raise FileNotFoundError(artifact)
    site_files = {name: (source / name).read_bytes() for name in SITE_FILES}
    version_site_assets(site_files, installer.read_bytes() if installer else None)
    output.mkdir(parents=True, exist_ok=True)
    for name, content in site_files.items():
        (output / name).write_bytes(content)
    downloads = output / "downloads"
    downloads.mkdir(exist_ok=True)
    for name, artifact in artifacts.items():
        target = downloads / name
        if artifact:
            shutil.copyfile(artifact, target)
        else:
            target.unlink(missing_ok=True)
    release = {"installerAvailable": installer is not None, "appInstallerAvailable": appinstaller is not None}
    (downloads / "release.json").write_text(json.dumps(release) + "\n", encoding="utf-8")
    print("Public invitation site prepared:", output)


def version_site_assets(site_files, installer=None):
    asset_data = dict(site_files)
    index_assets = ["connect.js", "style.css"]
    if installer is not None:
        asset_data["downloads/HostedComfyUIConnector.exe"] = installer
        index_assets.append("downloads/HostedComfyUIConnector.exe")
    for page, assets in (
        ("index.html", index_assets),
        ("installed.html", ("installed.js", "style.css")),
    ):
        html = site_files[page].decode("utf-8")
        for asset in assets:
            digest = hashlib.sha256(asset_data[asset]).hexdigest()
            pattern = r"(\b(?:src|href)\s*=\s*)([\"'])" + re.escape(asset) + r"(?:\?[^\"']*)?\2"
            html, count = re.subn(pattern, lambda match: match[1] + match[2] + asset + "?v=" + digest + match[2], html)
            if not count:
                raise RuntimeError(f"{page} does not reference the expected local asset: {asset}")
        site_files[page] = html.encode("utf-8")


if __name__ == "__main__":
    main()
