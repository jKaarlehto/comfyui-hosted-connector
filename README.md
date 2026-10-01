# Notch workflows on Comfy API

Publish a workflow and its dependencies with Comfy Build/Deploy. A user imports
its workflow bundle into Notch, selects the deployment, and sets an authorized
Comfy API key in Notch. The extension client calls Comfy's v2 API directly.

The target consumer needs **Notch only**. There are no invitation links, device
enrollment, SSH tunnels, Windows connector app, or local bridge to install.

## Flow

1. **Publish:** export a compiled ComfyUI workflow, pin its named inputs/outputs in
   a versioned bundle, and deploy its models and custom nodes using Comfy Build.
2. **Share:** distribute the bundle as a file or ordinary download. It carries
   the workflow, input/output bindings and endpoint hint, without credentials.
3. **Connect in Notch:** import the bundle, explicitly select/confirm its endpoint,
   and enter an API key authorized for that deployment. Notch saves the key in
   the OS credential store; workflows and DFX projects contain only a connection
   reference. The key is supplied to the extension client in memory.
4. **Run:** Notch exposes the published controls. Its extension client uploads
   encoded media, submits a job once, polls it and downloads its output assets.
   A saved job ID permits recovery after reconnecting without another paid run.

Comfy supports including Registry custom nodes in Builds. The Notch nodes
registered and executed on Linux through the official v2 proxy in local testing;
a published `notch` Registry version and a real managed deployment are still
needed to verify the hosted path. Cross-account authorization and billing also
need verification. A deployment URL alone grants no access; do not share a
publisher's account key as a substitute for consumer authorization.

## Implementation status

[The goal and plan](deploy/comfy_api/PLAN.md) defines the direct client architecture
and acceptance criteria. The native v2 API client is implemented in the separate
[ComfyUI-Notch SDK repository](https://github.com/jKaarlehto/ComfyUI-Notch).
The Notch product's connection UI, credential storage, workflow binding and job
persistence integration are still required; its source is outside these repos.

This repository implements a bundle compiler, a managed custom-node verification
command and a **development-only** Python bridge for exercising the existing
extension-client protocol. That bridge is a test harness, not the new consumer
flow or a replacement Windows app. See [prototype usage](deploy/comfy_api/README.md).

The previous Runpod tools, invitation service/pages, Windows app source and
installer downloads have been removed from this repository. This source removal
does not stop existing Pods, revoke credentials, disable an already published
GitHub Pages site, or uninstall existing apps.

## Development

Python 3.11 or newer:

```shell
python -m pip install -r deploy/comfy_api/requirements.txt
python -m unittest deploy.comfy_api.test_bundle deploy.comfy_api.test_bridge deploy.comfy_api.test_auth
```

The [prototype guide](deploy/comfy_api/README.md) also describes the actual C++
extension-client smoke test and custom-node proof.
