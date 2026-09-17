"""Mirror completed Comfy files to a global volume using verified immutable blobs."""

import argparse
import fcntl
import hashlib
import json
import re
import shutil
import time
from pathlib import Path, PurePosixPath

ROOTS = ("models", "user/default/workflows", "input", "output")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--mode", choices=("restore", "sync", "watch"), required=True)
    args = parser.parse_args()
    for name in ("blobs", "files"):
        (args.store / name).mkdir(parents=True, exist_ok=True)
    if args.mode == "restore":
        restore(args.local, args.store)
    elif args.mode == "sync":
        sync(args.local, args.store, minimum_age=0)
    else:
        while True:
            try:
                sync(args.local, args.store, minimum_age=30)
            except (OSError, RuntimeError) as error:
                print(f"[Notch storage] Sync will retry: {error}", flush=True)
            time.sleep(30)


def restore(local, store):
    for pointer in (store / "files").glob("*.json"):
        record = json.loads(pointer.read_text())
        relative = PurePosixPath(record["path"])
        digest = record["sha256"]
        if relative.is_absolute() or ".." in relative.parts or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid global model-store record")
        if not any(relative.is_relative_to(root) for root in ROOTS):
            raise ValueError("Global model-store record is outside the persisted directories")
        target = local / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".notch-restore-part")
        shutil.copyfile(store / "blobs" / digest, temporary)
        if temporary.stat().st_size != record["size"] or hash_file(temporary) != digest:
            temporary.unlink()
            raise RuntimeError(f"Global model-store integrity check failed: {relative}")
        temporary.replace(target)
        print(f"[Notch storage] Restored {relative}", flush=True)


def sync(local, store, minimum_age):
    with (local / ".notch-store.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = local / ".notch-store-state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        for root in ROOTS:
            for source in (local / root).rglob("*"):
                if not source.is_file() or source.is_symlink():
                    continue
                relative = source.relative_to(local).as_posix()
                if any(part.startswith(".") for part in source.relative_to(local).parts):
                    continue
                if source.suffix in (".part", ".tmp", ".notch-restore-part") or source.name.startswith("put_"):
                    continue
                before = source.stat()
                stamp = [before.st_size, before.st_mtime_ns]
                if (minimum_age > 0 and time.time() - before.st_mtime < minimum_age) or state.get(relative) == stamp:
                    continue
                digest = hash_file(source)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    continue
                blob = store / "blobs" / digest
                if not blob.exists() or blob.stat().st_size != before.st_size or hash_file(blob) != digest:
                    shutil.copyfile(source, blob)
                if hash_file(blob) != digest:
                    raise RuntimeError(f"Global model-store write verification failed: {relative}")
                record = {"path": relative, "sha256": digest, "size": before.st_size}
                pointer = store / "files" / (hashlib.sha256(relative.encode()).hexdigest() + ".json")
                pointer.write_text(json.dumps(record) + "\n")
                state[relative] = stamp
                temporary = state_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(state) + "\n")
                temporary.replace(state_path)
                print(f"[Notch storage] Saved {relative}", flush=True)


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
