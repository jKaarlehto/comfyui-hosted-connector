"""Endpoint-scoped OS credential storage; no keys in bundles or bridge state."""

import os

from .bundle import digest, validate_endpoint

SERVICE = "Notch.ComfyAPI"


def credential_store():
    import keyring

    selected = keyring.get_keyring()
    candidates = getattr(selected, "backends", [selected])
    for backend in candidates:
        if type(backend).__module__ in {
            "keyring.backends.Windows",
            "keyring.backends.macOS",
            "keyring.backends.SecretService",
            "keyring.backends.kwallet",
        }:
            return backend
    raise ValueError("No supported OS credential store is available")


def account(endpoint):
    return digest(validate_endpoint(endpoint))


def store_key(endpoint, key):
    if not key or any(c.isspace() for c in key):
        raise ValueError("Supply a nonempty Comfy API key without whitespace")
    try:
        credential_store().set_password(SERVICE, account(endpoint), key)
    except Exception as exc:
        raise ValueError(
            "OS credential storage is unavailable. Use COMFY_API_KEY for this process instead; no plaintext key was saved."
        ) from exc


def load_key(endpoint):
    # Environment values are an explicit override, suitable for CI or a session.
    value = os.environ.get("COMFY_API_KEY") or os.environ.get("COMFY_CLOUD_API_KEY")
    if value:
        return value
    try:
        return credential_store().get_password(SERVICE, account(endpoint)) or ""
    except Exception:
        return ""


def remove_key(endpoint):
    try:
        backend = credential_store()
        if backend.get_password(SERVICE, account(endpoint)):
            backend.delete_password(SERVICE, account(endpoint))
    except Exception as exc:
        raise ValueError("OS credential storage could not be accessed") from exc
