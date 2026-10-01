# Comfy API publishing and development harness

The native SDK now supports direct Comfy v2 execution, Builder/Deploy hooks,
workflow definition asset URLs and credential-free snapshot persistence for DFX.
The consumer enters a **Notch workflow URL** and authorized key; no workflow file,
manual import, helper app or bridge is required. A bare deployment URL identifies
an environment, not a graph. Managed definition retention and cross-account
access remain release gates. See [the goal and plan](PLAN.md) and the SDK's
[native publication contract](https://github.com/jKaarlehto/ComfyUI-Notch/blob/0251477473ee16701b58a162ae8c176fea251b1a/cpp/comfy_extension_client/PUBLISHING.md).

The SDK [backend boundaries](https://github.com/jKaarlehto/ComfyUI-Notch/blob/0251477473ee16701b58a162ae8c176fea251b1a/cpp/comfy_extension_client/BACKENDS.md) define feature ownership.
This managed feature covers image jobs. It does not expose the ComfyUI editor,
native plugin HTTP/WebSocket sessions or live previews. The environment is pinned,
but each job supplies its graph; frontend compilation happens before submission.
Future job SSE previews need a streaming implementation and managed verification,
including `501` handling. Full native remote behavior needs separate persistent
hosting with authenticated HTTPS/WSS; this harness does not provide that hosting.

This directory contains a workflow bundle compiler, a custom-node verification
command, and a development-only loopback bridge. The bridge exercises existing
Notch protocol behavior against Comfy's job API; it is not the consumer product.
The native SDK client and the remaining Notch host integration are tracked in the
plan. The CLI authentication below belongs to this development harness only.

## Install

Use Python **3.11 or newer** from this repository's root:

```shell
python -m pip install -r deploy/comfy_api/requirements.txt
```

## Publisher

Author and test a workflow in ComfyUI and export its compiled API JSON. A saved
Notch workflow containing `extra.notch.api_format` also works. Editor-only JSON
needs compilation before it can run on an endpoint.

Package the environment with `comfy build init`, `comfy build push`, and
`comfy build release create --target linux/nvidia --watch`. Review available GPUs,
regions, rates and worker bounds before `comfy deploy up`. These are Comfy's
commands; `compile` below only creates a local workflow bundle. Follow
[Comfy's quickstart](https://docs.comfy.org/development/serverless/quickstart).

One deployment can run several workflows whose dependencies are included in its
Build. Build/Deploy does not publish their named Notch controls. Our bundle pins
the workflow and supplies those controls separately.

Supported Notch nodes are primitive/IMAGE `NotchSingleInput` and `NotchMultiInput`,
direct `NotchImageFile` adapters, and single image `NotchOutputNode` outputs. The
compiler replaces these with native graph values, `LoadImage` and `SaveImage`, so
workers never depend on the bridge's media cache or connection state. Other
custom nodes remain in the graph and must be installed in the Build.

For a native workflow, specify widget bindings in JSON. Each input has a type,
node ID and input name, with optional label, bounds, default and COMBO options.
The example is a model-free solid image with three integer controls:

```shell
python -m deploy.comfy_api compile --workflow deploy/comfy_api/examples/solid-image.json --bindings deploy/comfy_api/examples/solid-bindings.json --name "Solid Image" --endpoint https://YOUR-DEPLOYMENT.run.comfy.app --output solid.notch-api.json
```

For modern autogrow Notch outputs, add an explicit output slot/type in the bindings
file. The API graph itself does not tell us an arbitrary upstream socket's type:

```json
{"outputs": {"42": {"type": "IMAGE", "slot": "outputs.output_0"}}}
```

For development, the compiler creates a descriptor file containing the endpoint
hint, original compiled
workflow, stateless worker graph and named input/output bindings. It contains no
connector API credential. Embedded credential fields are rejected. Its hash pins
its contents; it is not a signature proving who published it. Workflow edits
require a new descriptor identity. In the target product this descriptor is
published as native Comfy JSON assets and fetched automatically; a consumer does
not import it. The native SDK snapshot has its own schema containing a compiled
graph and bindings, distinct from the bridge's development bundle. Use the
bundle's worker graph and named inputs/outputs to construct that SDK snapshot.

## Development harness authentication and connection

Select the deployment URL explicitly. It must match the bundle; imported files
cannot silently redirect a saved credential to another host. Sign in once:

```shell
python -m deploy.comfy_api login --endpoint https://YOUR-DEPLOYMENT.run.comfy.app
python -m deploy.comfy_api serve --endpoint https://YOUR-DEPLOYMENT.run.comfy.app --bundle solid.notch-api.json
```

`login` prompts for a key without displaying it, checks authentication with a
read-only asset lookup, and stores it for that endpoint in Windows Credential
Manager, macOS Keychain, Secret Service or KWallet. Plaintext keyring backends are
refused. Later connections reuse the saved key. On a system without an accessible
OS store, set `COMFY_API_KEY` for the process; `COMFY_CLOUD_API_KEY` is also accepted
for compatibility with comfy-cli. Environment values override saved credentials.
Create a key under API Keys after signing in at
[Comfy Platform](https://platform.comfy.org/login).

Notch connects to `http://127.0.0.1:18188`, using the normal extension client.
Select the workflow under **ComfyAPI**, use **HTTP** inputs/outputs and **PNG**, and
keep the bridge running. Credentials stay in the bridge; the extension client
does not need to embed an API key in a workflow or DFX. Bearer headers are sent
only to the selected Comfy origin and removed on signed-storage redirects. Browser
Origin requests and non-loopback Host headers are rejected.

A deployment URL grants no access by itself. Consumers need a Comfy credential
authorized for that deployment. The published documentation does not establish
how another account gains deployment access or whose credits those calls consume.
Do not substitute an owner's account key for per-user access. Cross-account sharing is a release gate. If Comfy does not offer the required
access model, revisit the product scope; do not silently reinstate a custom
invitation service or distribute the publisher's key.

```shell
python -m deploy.comfy_api logout --endpoint https://YOUR-DEPLOYMENT.run.comfy.app
```

Restart an already running bridge after changing/removing its credential. A
credential rejected by Comfy produces a re-login instruction, rather than an
automatic login loop or another job submission. OS credential storage is tested
through its adapter; actual Notch OS credential storage remains part of host integration
validation.

## Registry and custom-node verification

Comfy Build's `customNodes` supports a Registry slug and pinned version:

```yaml
customNodes:
  - name: ComfyUI-Notch
    id: notch
    registryVersion: "<published-version>"
```

It also supports a pinned Git source or uploaded node package. A release must
import successfully on Linux and match the base ComfyUI version. Our plugin
declares ComfyUI `>=0.37.0`, Linux support and a Windows-only Spout dependency. Its
five core nodes register through the current ComfyExtension API. Do not require
the old `NODE_CLASS_MAPPINGS` export as the only evidence of a valid node pack.

The public `GET https://api.comfy.org/nodes/notch` returned 404 and its versions
endpoint returned `[]` on 2026-10-01. A published Registry version is still needed
before the Registry example can be deployed. Registry installation of node code
also does not imply that `/notch/*` or `/ws` will be exposed by the managed gateway.

After deployment, run the included proof against that endpoint:

```shell
python -m deploy.comfy_api verify-plugin --endpoint https://YOUR-DEPLOYMENT.run.comfy.app --output-dir .local-work/comfy-api-proof
```

It submits a 16×16 model-free workflow through v2 containing the actual
`NotchSingleInput` and `NotchOutputNode` classes, then downloads the output asset.
It records the submission key and job ID and resumes the existing job if rerun
with the same output directory. It does not test custom route exposure.

Notch's HTTP image node must have `preview: true` in its stamped output config
for v2 to discover its native image reference. With previews disabled, the node
can write its image successfully while v2 reports no assets. The proof enables
this flag; the bridge's compiled graphs use native SaveImage directly.
The flag enables output-asset discovery, not progressive previews or proof of
SSE delivery. The bridge's native WebSocket tests are a local adapter proof and
do not establish managed access to `/ws` or `/notch/*`.

## Recovery and scope

Job IDs, delivery streams, unacknowledged events and downloaded PNGs are saved
locally under `Notch/ComfyAPI` in the OS application-data directory. Use
`--state-dir` to choose another directory. Known unfinished jobs resume after
restart; reconnecting the same extension client replays pending output/terminal
events until acknowledged. Cancellation is scoped to the connection that started
the job. Completed images are also available through the extension client's
latest-output and HTTP resolve/attach methods.

```shell
python -m deploy.comfy_api status --endpoint https://YOUR-DEPLOYMENT.run.comfy.app --bundle solid.notch-api.json
```

v2 idempotency keys are single-use, not response-replay handles. A timeout or 5xx
before the job ID arrives may mean the job exists. The bridge records the key and
does not automatically resubmit. Find the job through Comfy and attach its ID:

```shell
python -m deploy.comfy_api recover --endpoint https://YOUR-DEPLOYMENT.run.comfy.app --bundle solid.notch-api.json --submission-key RECORDED-KEY --job-id EXISTING-JOB-ID
```

Then start the bridge to resume it. Raw input frames support `rgb`, `rgba`, `bgr`,
`bgra` and their `float32_` variants with row stride. Encoded images and base64 data
URIs are also accepted. Images are normalized to PNG, with 100 MiB/16 megapixel
limits. Batches, audio/video/3D, partner-node credentials, source metadata/replay,
group bypass, mutable workflows and live editor control remain outside this MVP.
The cache is retained until its directory is removed after the bridge is stopped;
check for unresolved jobs first. It is intended for prototype use, not an
unbounded unattended image service.

## Validation

The prototype was tested on 2026-10-01 with:

- An independent v2 HTTP fake covering authentication, asset upload, mounted links,
  single submission, cancellation, recovery, delivery replay and failure cases.
- The actual vendored C++ `Client` over HTTP and a relayed real WebSocket,
  including automatic plugin bootstrap, raw multipart input, parse/generate,
  PNG download, acknowledgements, latest-output discovery and HTTP resolution.
- The new native `ComfyApiClient` over HTTP directly to the official v2 proxy,
  without the development bridge: encoded PNG upload, LoadImage/SaveImage
  generation, polling and PNG download, then two resumptions of the same saved
  job ID without another submission. The proxy required a bearer token.
- Linux ComfyUI 0.37.0 + Notch 0.29.0 through official comfy-api-proxy: the full
  bridge image workflow driven by the actual C++ extension Client, plus actual
  Notch input/output custom-node execution and
  v2 asset download. This ran on CPU and did not provision a managed GPU.

Run the Python tests from the repository root:

```shell
python -m unittest deploy.comfy_api.test_bundle deploy.comfy_api.test_auth deploy.comfy_api.test_bridge
```

Build the extension-client test against a sibling plugin checkout, then set
`NOTCH_CLIENT_SMOKE_EXECUTABLE` to the built binary before running the tests:

```shell
cmake -S deploy/comfy_api/client_smoke -B .local-work/comfy-api-client-smoke -DNOTCH_CLIENT_DIR=/path/to/ComfyUI-Notch/cpp/comfy_extension_client
cmake --build .local-work/comfy-api-client-smoke --target extension_client_smoke
```

The native smoke uses libcurl; without the binary, that one test reports a skip.

The separate direct-backend smoke needs a checkout containing the new
`ComfyApiClient` and publication SDK hooks. It also uploads a native JSON
workflow asset, automatically loads it by URL, checks credential-free serialization
and saves its workflow snapshot/publication URL alongside the job state. Against an official v2 proxy or authorized deployment:

```shell
cmake --build .local-work/comfy-api-client-smoke --target direct_api_smoke
# Set COMFY_API_KEY in the process environment; do not pass it on the command line.
.local-work/comfy-api-client-smoke/direct_api_smoke http://127.0.0.1:8189 input.png .local-work/direct-proof
```

Use an existing state prefix to resume its saved job. Run only one instance per
prefix. An intent without a job ID stops the smoke rather than creating another
job. This executable is a development proof, not a consumer helper app.

Managed definition-asset retention/access, endpoint authorization, multi-account
billing, Registry release installation,
cold starts and Notch host integration still require live validation.
