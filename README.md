# Notch workflows on Comfy API

Publish a workflow and its dependencies with Comfy Build/Deploy. A user enters
the hosted endpoint URL and an authorized API key in Notch. Notch discovers the
publisher's workflows and controls, then the extension client runs them through
Comfy's v2 API directly. This URL-only discovery is a required, unresolved part
of the integration, not a feature supplied by the current SDK client.

The target consumer needs **Notch only**. There are no invitation links, device
enrollment, SSH tunnels, Windows connector app, or local bridge to install.

## Flow

1. **Publish:** the owner authors workflows and deploys their models/custom nodes
   with Comfy Build. The owner also publishes the workflow definitions and named
   input/output metadata for automatic client discovery.
2. **Connect in Notch:** the consumer enters the hosted endpoint URL and API key,
   then connects. They need no workflow file and do not manually import a bundle.
   Notch saves the key in its OS credential store and passes it to the SDK in memory.
3. **Discover:** Notch retrieves available workflow definitions and controls,
   selecting the single/default workflow automatically or offering a chooser.
4. **Run:** the client binds controls/uploads to the discovered graph, submits it,
   polls the job and downloads outputs. Saved job IDs allow recovery without a
   second paid run.

**Platform gap:** Comfy Build/Deploy publishes an execution environment, not a
saved-workflow catalog. The documented v2 API requires a graph for each submitted
job and does not provide saved-workflow management or named workflow parameters.
We must prove how Notch can discover the publisher's definitions from the entered
URL. Managed exposure of custom `/notch/*` routes is unverified. If those routes
are unavailable, another automatic metadata source is required; a bare deployment
URL alone is insufficient under the currently documented API. Do not hide this
limitation behind a manual consumer import step.

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
Workflow discovery and the Notch product's connection UI, credential storage,
workflow binding and job persistence integration are still required; its source
is outside these repos.

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
