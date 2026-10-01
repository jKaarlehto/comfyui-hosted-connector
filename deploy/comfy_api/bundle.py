"""Compile published image workflows into stateless Comfy API jobs.

No ComfyUI, torch, editor session or publisher credentials are needed to consume
a bundle. The wire graph and exposed bindings are pinned by the bundle digest.
"""

import copy
import hashlib
import json
import math
import re
from urllib.parse import urlsplit

PROTOCOL = "0.12.0"
SCALARS = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_endpoint(value):
    url = urlsplit(value)
    local = url.hostname in {"127.0.0.1", "localhost", "::1"}
    managed = url.hostname == "cloud.comfy.org" or bool(
        re.fullmatch(r"[a-zA-Z0-9-]+\.run\.comfy\.app", url.hostname or "")
    )
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("Endpoint must not contain credentials, a query or a fragment")
    if not (
        (local and url.scheme in {"http", "https"}) or (managed and url.scheme == "https" and url.port in {None, 443})
    ):
        raise ValueError("Use an HTTPS Comfy deployment/Cloud URL or a loopback v2 proxy")
    return value.rstrip("/")


def api_prompt(value):
    if not isinstance(value, dict):
        raise ValueError("Workflow must be a JSON object")
    if "prompt" in value:
        value = value["prompt"]
    elif "nodes" in value:
        value = value.get("extra", {}).get("notch", {}).get("api_format")
    if (
        not isinstance(value, dict)
        or not value
        or any(
            not isinstance(node, dict)
            or not isinstance(node.get("class_type"), str)
            or not isinstance(node.get("inputs", {}), dict)
            for node in value.values()
        )
    ):
        raise ValueError("Export a compiled API workflow; editor-only graphs cannot run headlessly")
    return copy.deepcopy(value)


def is_link(value, graph):
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and value[0] in graph
        and type(value[1]) is int
    )


def typed_value(binding, value):
    kind = binding["type"]
    valid = (
        (kind == "INT" and type(value) is int)
        or (kind == "FLOAT" and type(value) in {int, float} and math.isfinite(value))
        or (kind in {"STRING", "COMBO"} and isinstance(value, str))
        or (kind == "BOOLEAN" and type(value) is bool)
    )
    if not valid:
        raise ValueError(f"Input {binding['name']!r} requires {kind}")
    for field, too_far in (("min", lambda bound: value < bound), ("max", lambda bound: value > bound)):
        if kind in {"INT", "FLOAT"} and field in binding and too_far(float(binding[field])):
            raise ValueError(f"Input {binding['name']!r} is outside its {field} bound")
    if kind == "COMBO" and value not in binding.get("options", []):
        raise ValueError(f"Input {binding['name']!r} is not one of its published options")
    return value


def compile_bundle(workflow, name, endpoint, bindings=None):
    """Convert common Notch nodes, or bind widgets in a native API graph.

    Modern multi-output nodes need explicit output selection with type IMAGE,
    since API graphs do not carry the socket type of an arbitrary upstream node.
    """
    source = api_prompt(workflow)
    for node in source.values():
        for field, value in node.get("inputs", {}).items():
            if (
                field.lower()
                in {"api_key", "api_key_comfy_org", "auth_token_comfy_org", "access_token", "authorization", "password"}
                and value
                and not is_link(value, source)
            ):
                raise ValueError("Remove embedded credentials from the workflow before sharing a bundle")
    graph = copy.deepcopy(source)
    exposed = {}
    output_specs = (bindings or {}).get("outputs", {})
    outputs = []
    refs = {}

    def add_input(key, entry, targets):
        if not key or key in exposed:
            raise ValueError(f"Empty or duplicate published input: {key!r}")
        kind = entry.get("type", "STRING")
        if kind not in SCALARS | {"IMAGE"}:
            raise ValueError(f"Input {key!r}: {kind} is not supported by the image MVP")
        binding = {
            k: copy.deepcopy(entry[k])
            for k in ("label", "default", "min", "max", "step", "options", "control_after_generate")
            if k in entry
        }
        binding.update(name=key, type=kind, targets=targets)
        if "default" in binding and kind != "IMAGE":
            typed_value(binding, binding["default"])
        if kind == "IMAGE" and "default" in binding:
            raise ValueError("Image defaults must be supplied by the consumer, not worker-local paths")
        exposed[key] = binding
        return binding

    for node_id, node in source.items():
        if node["class_type"] not in {"NotchSingleInput", "NotchMultiInput"}:
            continue
        entries = node.get("inputs", {}).get("inputs_json", "[]")
        entries = json.loads(entries) if isinstance(entries, str) else entries
        if not isinstance(entries, list):
            raise ValueError("Notch inputs_json must be an array")
        for index, entry in enumerate(entries):
            if (
                entry.get("derived_from")
                or entry.get("source_policy", "run_owner") != "run_owner"
                or "override:" + entry.get("key", "") in node.get("inputs", {})
            ):
                raise ValueError("Derived inputs, exposure ownership and Comfy overrides need explicit native mappings")
            refs[(node_id, index)] = add_input(entry.get("key"), entry, [])
        del graph[node_id]

    # Image tensor bindings use native LoadImage; filename adapters bind the
    # uploaded core/ASSET directly into their downstream native loader instead.
    image_nodes = {}
    for (node_id, index), binding in refs.items():
        if binding["type"] == "IMAGE":
            generated_id = f"notch_api_image_{node_id}_{index}"
            if generated_id in source:
                raise ValueError("Workflow node collides with a generated image adapter")
            graph[generated_id] = {"class_type": "LoadImage", "inputs": {"image": ""}}
            binding["targets"].append({"node_id": generated_id, "input": "image"})
            image_nodes[(node_id, index)] = generated_id

    adapters = {}
    for node_id, node in source.items():
        if node["class_type"] == "NotchImageFile":
            link = node.get("inputs", {}).get("image")
            binding = refs.get(tuple(link)) if is_link(link, source) else None
            if not binding or binding["type"] != "IMAGE":
                raise ValueError("Image to Filename must connect directly to a published IMAGE input")
            adapters[(node_id, 0)] = binding
            del graph[node_id]

    for node_id, node in graph.items():
        for slot, value in list(node.get("inputs", {}).items()):
            if not is_link(value, source):
                continue
            ref = tuple(value)
            binding = adapters.get(ref) or refs.get(ref)
            if binding:
                if binding["type"] == "IMAGE" and ref not in adapters:
                    node["inputs"][slot] = [image_nodes[ref], 0]
                else:
                    binding["targets"].append({"node_id": node_id, "input": slot})
                    node["inputs"][slot] = binding.get("default")
            elif value[0] not in graph:
                raise ValueError(f"Unsupported Notch output index in {node_id}/{slot}")

    for key, entry in (bindings or {}).get("inputs", {}).items():
        targets = entry.get("targets", [{"node_id": entry.get("node_id"), "input": entry.get("input")}])
        if not isinstance(targets, list) or not targets:
            raise ValueError(f"Native binding {key!r} needs at least one target")
        for target in targets:
            node = graph.get(target.get("node_id"), {})
            slot = target.get("input")
            if slot not in node.get("inputs", {}) or is_link(node["inputs"][slot], graph):
                raise ValueError(f"Native binding {key!r} must target an existing unconnected widget")
        first = graph[targets[0]["node_id"]]["inputs"][targets[0]["input"]]
        entry = copy.deepcopy(entry)
        if entry.get("type") != "IMAGE":
            entry.setdefault("default", first)
        add_input(key, entry, targets)

    for node_id, node in list(graph.items()):
        kind = node["class_type"]
        if kind == "NotchOutputNode":
            values = node.get("inputs", {})
            if is_link(values.get("image"), graph):
                link, output_id = values["image"], node_id
            else:
                spec = output_specs.get(node_id)
                if not spec or spec.get("type") != "IMAGE":
                    raise ValueError(f"Output {node_id}: specify its image slot in the bindings file")
                slot = spec.get("slot", "outputs.output_0")
                link, output_id = values.get(slot), f"{node_id}/{slot.split('.')[-1]}"
                if not is_link(link, graph):
                    raise ValueError(f"Output {node_id}: no connected image at {slot}")
                if any(is_link(v, graph) for k, v in values.items() if k != slot):
                    raise ValueError("Multi-slot Notch outputs need separate image output nodes in the MVP")
            graph[node_id] = {"class_type": "SaveImage", "inputs": {"images": link, "filename_prefix": "NotchAPI"}}
        elif kind in {"SaveImage", "PreviewImage"}:
            output_id = node_id
            if kind == "PreviewImage":
                graph[node_id] = {
                    "class_type": "SaveImage",
                    "inputs": {"images": node["inputs"]["images"], "filename_prefix": "NotchAPI"},
                }
        elif kind.startswith("Notch") or kind == "SpoutReceiver":
            raise ValueError(f"Worker-local node {kind} is not supported; export native bindings instead")
        else:
            continue
        outputs.append(
            {
                "name": output_id,
                "node_id": node_id,
                "label": node.get("inputs", {}).get("output_name")
                or node.get("_meta", {}).get("title", "Image " + node_id),
                "type": "IMAGE",
                "transports": ["http"],
            }
        )
    if not outputs:
        raise ValueError("The bundle needs at least one SaveImage, PreviewImage or supported Notch image output")
    if any(not b["targets"] for b in exposed.values()):
        raise ValueError("Remove unused Notch input declarations before publication")
    result = {
        "schema_version": 1,
        "protocol_version": PROTOCOL,
        "name": name,
        "endpoint": validate_endpoint(endpoint),
        "source_prompt": source,
        "prompt": graph,
        "inputs": exposed,
        "outputs": outputs,
    }
    result["bundle_id"] = digest(result)
    return result


def validate_bundle(bundle):
    if bundle.get("schema_version") != 1 or bundle.get("protocol_version") != PROTOCOL:
        raise ValueError("Unsupported published bundle or Notch protocol version")
    expected = digest({k: v for k, v in bundle.items() if k != "bundle_id"})
    if bundle.get("bundle_id") != expected:
        raise ValueError("Bundle contents changed; publish a new version")
    # Validate downloaded declarations before using any binding as a graph path.
    # A content hash detects edits; it is not a publisher signature.
    api_prompt(bundle["source_prompt"])
    graph = api_prompt(bundle["prompt"])
    validate_endpoint(bundle["endpoint"])
    for key, binding in bundle["inputs"].items():
        if binding.get("name") != key or binding.get("type") not in SCALARS | {"IMAGE"}:
            raise ValueError("Invalid published input binding")
        if not binding.get("targets"):
            raise ValueError("Published input has no targets")
        for target in binding["targets"]:
            if target["input"] not in graph.get(target["node_id"], {}).get("inputs", {}):
                raise ValueError("Published input target is missing")
    if not bundle.get("outputs") or any(o["node_id"] not in graph or o["type"] != "IMAGE" for o in bundle["outputs"]):
        raise ValueError("Invalid published image outputs")
    return bundle


def contract(bundle):
    declarations = []
    for binding in bundle["inputs"].values():
        item = {
            k: binding[k]
            for k in ("name", "type", "label", "min", "max", "step", "options", "control_after_generate")
            if k in binding
        }
        item.update(
            declaration_id=binding["name"],
            node_id=binding["targets"][0]["node_id"],
            output_index=0,
            consumed=True,
            source_policy="run_owner",
        )
        for field in ("min", "max", "step"):
            if field in item:
                item[field] = str(item[field])
        if "default" in binding:
            item["saved_value"] = binding["default"]
        declarations.append(item)
    return {
        "input_declarations": declarations,
        "outputs": bundle["outputs"],
        "native_controls": [],
        "groups": [],
        "resource_id": bundle["bundle_id"],
        "resolved_workflow": {"prompt": bundle["source_prompt"]},
        "workflow_info": {"models": [], "model_download_required": False},
        "notch": {"workflow_source": {"requested": "request", "selected": "request"}},
        "diagnostics": ["Published Comfy API image workflow; live editor and group control are unavailable."],
    }
