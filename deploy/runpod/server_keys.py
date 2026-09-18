"""Leased SSH authorization for enrolled connector devices."""

import argparse
import base64
import json
import re
import struct
import time
import urllib.request
from pathlib import Path

CONFIG = Path("/opt/notch-gateway.json")
CACHE = Path("/opt/notch-gateway-keys.json")
MAX_LEASE = 300


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("watch", "authorize"))
    parser.add_argument("values", nargs="*")
    args = parser.parse_args()
    if args.command == "authorize":
        try:
            line = authorize(CACHE, args.values)
            if line:
                print(line)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return
    while True:
        try:
            refresh(CONFIG, CACHE)
        except (OSError, ValueError, KeyError, TypeError):
            print(
                "[ComfyUI Notch] Device authorization refresh unavailable", flush=True
            )
        time.sleep(10)


def gateway_origin(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.workers\.dev",
        value,
    ):
        raise ValueError("Invalid gateway origin")
    return value


def public_key(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"ssh-ed25519 [A-Za-z0-9+/]{68}", value
    ):
        raise ValueError("Invalid device public key")
    blob = base64.b64decode(value.split()[1], validate=True)
    if len(blob) != 51 or blob[:19] != struct.pack(
        ">I", 11
    ) + b"ssh-ed25519" + struct.pack(">I", 32):
        raise ValueError("Invalid Ed25519 key")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def refresh(config_path, cache_path):
    config = json.loads(config_path.read_text())
    origin = gateway_origin(config["gateway"])
    key = config["key"]
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("Invalid gateway credential")
    request = urllib.request.Request(
        origin + "/v1/server/keys", headers={"Authorization": "Bearer " + key}
    )
    started = time.monotonic()
    with urllib.request.build_opener(NoRedirect).open(request, timeout=8) as response:
        payload = response.read(65537)
    if len(payload) > 65536:
        raise ValueError("Oversized device registry")
    data = json.loads(payload)
    lease = data["lease_seconds"]
    revision = data["revision"]
    if (
        type(lease) is not int
        or not 1 <= lease <= MAX_LEASE
        or type(revision) is not int
        or revision < 0
    ):
        raise ValueError("Invalid registry lease")
    records = data["keys"]
    if not isinstance(records, list) or len(records) > 128:
        raise ValueError("Invalid device registry")
    keys = {}
    for record in records:
        identity = record["device_id"]
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[a-f0-9]{32}", identity)
            or identity in keys
        ):
            raise ValueError("Invalid device identity")
        keys[identity] = public_key(record["public_key"])
    port = config["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid ComfyUI port")
    cached = {
        "expires": started + lease,
        "revision": revision,
        "keys": keys,
        "port": port,
    }
    temporary = cache_path.with_suffix(".tmp")
    temporary.touch(mode=0o600, exist_ok=True)
    temporary.chmod(0o600)
    temporary.write_text(json.dumps(cached))
    temporary.replace(cache_path)


def authorize(cache_path, values):
    if len(values) != 3 or values[0] != "root" or values[1] != "ssh-ed25519":
        return ""
    requested = public_key(values[1] + " " + values[2])
    cached = json.loads(cache_path.read_text())
    remaining = cached["expires"] - time.monotonic()
    if not 0 < remaining <= MAX_LEASE:
        return ""
    port = cached["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        return ""
    for identity, key in cached["keys"].items():
        if key == requested and re.fullmatch(r"[a-f0-9]{32}", identity):
            return f'restrict,port-forwarding,permitopen="127.0.0.1:{port}",command="/bin/false" {key} notch-device-{identity}'
    return ""


if __name__ == "__main__":
    main()
