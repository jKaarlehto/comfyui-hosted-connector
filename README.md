# Notch workflows on Comfy API

The goal is a native Notch flow: a publisher creates or selects a Comfy API
deployment and publishes a Notch workflow definition; a consumer enters its
**workflow URL** and an authorized API key in Notch. The SDK automatically loads
the graph and controls and executes it through Comfy v2. DFX stores the complete
snapshot and deployment reference; secrets stay in Notch's credential store.

The consumer needs **Notch only**. There are no invitation links, device
enrollment, SSH tunnels, Windows connector app or local bridge to install.

## Product flow and boundaries

1. **Publisher:** author/test a workflow; package models/custom nodes with Builder,
   cut a Linux/NVIDIA release, and deploy it on Comfy API. Reuse a compatible
   existing deployment for additional workflows.
2. **Workflow URL:** upload the stateless graph and named controls as a JSON asset
   through Comfy's native API. This is Notch metadata stored in Comfy, separate
   from the Build environment. Show its actual retention.
3. **Consumer:** enter that workflow URL and an authorized key in Notch. The SDK
   fetches/validates the graph and bindings automatically. No workflow file or
   manual import is needed.
4. **Save and run:** serialize the graph, bindings and endpoint together into DFX.
   Bind controls, upload images, submit a job and download outputs. Persist job
   intent/IDs to recover without submitting another paid job.

**Builder packages an environment; its deployment URL can execute many graphs.**
It does not bake in one graph, create a workflow catalog or automatically add an
endpoint to the original local workflow JSON. A bare deployment URL is therefore
insufficient for a consumer with no graph. The Notch workflow URL identifies its
native definition asset. Comfy Cloud and enterprise Managed Builds are separate
products; their capabilities must not be assumed for Comfy API deployments.

## Implementation and remaining proof

[The goal and plan](deploy/comfy_api/PLAN.md) and [research coverage](deploy/comfy_api/RESEARCH.md)
record the design and supporting sources. Native Builder/Deploy methods,
workflow-URL parsing/loading, snapshot serialization and direct v2 execution are
implemented in the [SDK PR](https://github.com/jKaarlehto/ComfyUI-Notch/pull/95).
The user confirmed Notch host source is unavailable; SDK hooks are the implemented
scope. Publish/Connect UI, OS credential storage and actual DFX resource wiring
remain host integration work.

Native JSON asset upload/loading and image execution passed against the official
local v2 proxy. Asset retention and access with a separate user's key must still
be tested on managed Comfy API before offering permanent links or arbitrary
sharing. A URL grants no access; do not distribute the owner's account key.

Build supports Registry custom nodes. All five core Notch nodes registered on
Linux; actual Notch input/output nodes executed through v2. Registry publication
and a managed Build containing the exact published version remain required.

This repository contains a bundle compiler, custom-node verification command and
**development-only** bridge for testing the existing extension protocol. The
bridge is a test harness. See [prototype usage](deploy/comfy_api/README.md).

The previous Runpod tools, invitation service/pages, Windows app source and
installer downloads have been removed from this repository. Source removal does
not stop existing Pods, revoke credentials, unpublish an existing website or
uninstall existing apps.

## Development

Python 3.11 or newer:

```shell
python -m pip install -r deploy/comfy_api/requirements.txt
python -m unittest deploy.comfy_api.test_bundle deploy.comfy_api.test_bridge deploy.comfy_api.test_auth
```

The [prototype guide](deploy/comfy_api/README.md) also describes the actual C++
extension-client smoke test and custom-node proof.
