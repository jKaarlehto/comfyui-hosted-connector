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
    parser.add_argument("--mode", choices=("prepare", "restore", "sync", "watch"), required=True)
    args = parser.parse_args()
    for name in ("blobs", "files"):
        (args.store / name).mkdir(parents=True, exist_ok=True)
    if args.mode in ("prepare", "restore"):
        restore(args.local, args.store, lazy_models=args.mode == "prepare")
    elif args.mode == "sync":
        sync(args.local, args.store, minimum_age=0)
    else:
        while True:
            try:
                sync(args.local, args.store, minimum_age=30)
            except (OSError, RuntimeError) as error:
                print(f"[Notch storage] Sync will retry: {error}", flush=True)
            time.sleep(30)


def restore(local, store, lazy_models=False):
    links = {}
    for pointer in (store / "files").glob("*.json"):
        record = json.loads(pointer.read_text())
        relative = PurePosixPath(record["path"])
        digest = record["sha256"]
        if relative.is_absolute() or ".." in relative.parts or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid global model-store record")
        if not any(relative.is_relative_to(root) for root in ROOTS):
            raise ValueError("Global model-store record is outside the persisted directories")
        if type(record["size"]) is not int or record["size"] < 0:
            raise ValueError("Invalid global model-store size")
        target = local / relative
        blob = (store / "blobs" / digest).absolute()
        if lazy_models and relative.is_relative_to("models"):
            if target.is_symlink() and target.readlink() == blob:
                pass
            elif target.exists() or target.is_symlink():
                continue
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(blob)
            if not blob.is_file() or blob.stat().st_size != record["size"]:
                raise RuntimeError(f"Global model-store file is missing or incomplete: {relative}")
            links[relative.as_posix()] = record
            continue
        if target.exists() and not target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".notch-restore-part")
        shutil.copyfile(store / "blobs" / digest, temporary)
        verified = temporary.stat()
        stamp = [verified.st_size, verified.st_mtime_ns]
        if verified.st_size != record["size"] or hash_file(temporary) != digest:
            temporary.unlink()
            raise RuntimeError(f"Global model-store integrity check failed: {relative}")
        temporary.replace(target)
        with (local / ".notch-store.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state_path = local / ".notch-store-state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            state[relative.as_posix()] = stamp
            save_state(state_path, state)
        print(f"[Notch storage] Restored {relative}", flush=True)
    if lazy_models:
        local.mkdir(parents=True, exist_ok=True)
        save_state(local / ".notch-model-links.json", links)
        print(f"[Notch storage] {len(links)} models available on demand", flush=True)


def cache_model(local, store, record, progress):
    relative = record["path"]
    target = local / relative
    blob = (store / "blobs" / record["sha256"]).absolute()
    if not target.is_symlink() or target.readlink() != blob:
        return target
    temporary = target.with_name(target.name + ".notch-restore-part")
    digest = hashlib.sha256()
    completed = 0
    progress("downloading", relative, completed, record["size"])
    try:
        with blob.open("rb") as source, temporary.open("wb") as destination:
            while chunk := source.read(8 * 1024 * 1024):
                destination.write(chunk)
                digest.update(chunk)
                completed += len(chunk)
                progress("downloading", relative, completed, record["size"])
        progress("verifying", relative, completed, record["size"])
        if completed != record["size"] or digest.hexdigest() != record["sha256"]:
            raise RuntimeError("Saved model failed its integrity check")
        with (local / ".notch-store.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if target.is_symlink() and target.readlink() == blob:
                verified = temporary.stat()
                temporary.replace(target)
                state_path = local / ".notch-store-state.json"
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                state[relative] = [verified.st_size, verified.st_mtime_ns]
                save_state(state_path, state)
        progress("idle", relative, completed, record["size"])
        return target
    finally:
        temporary.unlink(missing_ok=True)


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
                save_state(state_path, state)
                print(f"[Notch storage] Saved {relative}", flush=True)


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state) + "\n")
    temporary.replace(path)


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
