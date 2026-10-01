# Direct Comfy API integration goal and plan

Goal: a publisher deploys a pinned ComfyUI environment with Comfy Build/Deploy
and publishes remotely discoverable workflow definitions. A consumer with no
workflow enters only the hosted URL and an authorized API key in **Notch**.
Notch discovers workflows/controls and passes the key to its extension client
in memory. No manual bundle import, invitation link, Windows connector app,
SSH tunnel or local bridge is part of the consumer flow.

Research date: 2026-10-01. The supplied deploy overview and serverless quickstart,
linked v2 API/SDK docs, v2 OpenAPI spec and local plugin/client were inspected.
Smooth authentication, extension-client integration and Registry custom-node
support are acceptance requirements.

## Product flow

1. Publisher authors/tests workflows and deploys dependencies with Comfy Build.
   The publisher's compiled graphs and named bindings must be published as
   remotely discoverable metadata. The existing bundle compiler produces this
   metadata for development; its file is not a required consumer input.
2. Consumer opens Notch's connection settings, enters the hosted URL and an API
   key, and connects. They do not need a workflow, download or import step.
   Notch offers Save Key, Replace Key and Forget Key; store secrets in the OS
   credential store scoped to endpoint/account. Workflow/DFX stores a reference.
3. Client discovers the publisher's available workflows and input/output controls.
   Select the single/default workflow, or offer a chooser for several workflows.
   Fetch/cache the graph and bindings automatically, with a pinned identity.
4. The native v2 client uploads assets, submits the discovered/bound graph,
   polls/cancels durable jobs and downloads outputs. Host encodes/decodes media.
5. Host persists submission intent before sending and the job ID before polling.
   Resume known IDs after restart; report an ambiguous submit without a job ID
   for investigation, never automatically submit another paid job. Retry read-only
   polls with backoff and handle expiry. Clear authentication errors offer Replace
   Key; do not trigger a login loop or generation retry.

## Feasibility and architecture

Plausible through a direct SDK backend. This is not a URL substitution: the native
plugin uses `/notch/*` and `/ws`, while managed deployments document `/api/v2/assets`
and `/api/v2/jobs`. Registry installation does not prove custom route exposure.
The managed backend therefore skips native WebSocket handshakes and runtime
Registry updates; Build releases pin the worker environment.

### URL-only workflow discovery is a feasibility gate

Rechecked official docs after clarification: the deployment guide's FAQ says
Builds may include dependencies for several workflows, then clients submit each
API-format workflow. The SDK scope explicitly excludes saved-workflow management,
node introspection and named parameters. `GET /api/v2/jobs/{id}/workflow` retrieves
an already known job's graph; it cannot bootstrap a new consumer from a URL.

Therefore the current documented managed v2 API does not supply the whole desired
URL-only connection flow. Prove whether a deployed Notch custom node can expose
workflow/catalog metadata through the managed gateway, or whether an official
platform API provides that metadata. Neither capability is verified. If unavailable,
design an automatic metadata source associated with the entered deployment URL.
Do not invent managed routes, forward Comfy credentials to arbitrary metadata
hosts, or call manual consumer bundle import a solution to this requirement.
The native SDK execution client remains useful but is not proof of URL-only
workflow discovery. Do not claim the full migration is feasible until this gate
has an implemented and managed-tested path.

A publisher-generated, remotely fetched descriptor supplies the stateless graph
and bindings. Use native values,
LoadImage and SaveImage on workers rather than Notch cache references, disk paths,
CUDA IPC or shared-memory handles. Other Registry custom nodes stay in the graph.
Host binding must validate published types/ranges and workflow identity before
upload or submission. A content hash pins a bundle, not its publisher's identity.

The SDK transport owns HTTPS. API bearer headers stay on the selected origin;
asset redirects are explicit and drop bearer credentials on external storage.
Never persist signed storage URLs or put a key into shared workflows, DFX, logs
or exported diagnostics. The host stores only stable asset IDs and job IDs.
An API key is the documented baseline; browser OAuth is not assumed or invented.
Users need credentials authorized for the selected deployment. Access and billing
between separate Comfy accounts must be proven before claiming arbitrary sharing.
If Comfy's access model is insufficient, reconsider scope with the user rather
than quietly reintroduce an invitation gateway or publisher-key distribution.

## Implementation stages

1. **Research harness — implemented.** Bundle compilation, typed input validation,
   image conversion, one-shot submission, durable polling/recovery, cancellation,
   output download and delivery are exercised through a development-only Python
   bridge. This proves protocol behavior, not the final consumer architecture.
2. **Direct native SDK — implemented foundation.** Add a separate ComfyApiClient
   with endpoint/key options and a host-owned HTTPS transport. Support binary
   asset upload, one-shot idempotent-key submission, polling, cancellation and
   stable-ID output downloads with credential-safe redirects. Include it in the
   vendorable SDK. No local helper or managed `/notch/*` route dependency.
3. **Workflow discovery — unresolved feasibility gate.** Establish an actual
   managed-supported source of publisher graphs/bindings discoverable from the
   entered URL. Implement fetch, schema/identity validation and workflow selection.
   An endpoint that only supports arbitrary graph submission is insufficient.
4. **Notch host integration — required.** Add the connection settings/OS key store,
   automatic discovery and named controls, bind typed inputs, encode/decode images,
   persist job state, poll on a worker thread, deliver outputs and resume saved jobs.
   The host's source is outside these repositories; SDK methods alone do not finish
   the user experience. Ship via a normal Notch update, not a separate installer.
5. **Publisher UX — required.** Add remotely discoverable publication to the
   ComfyUI frontend. CLI bundle creation is only a development artifact. Validate
   dependencies against the pinned Build and display endpoint/bundle identity.
   Existing CLI publication is available for development; no invitation flow.
6. **Live managed/Registry proof — required.** Publish the node pack to Registry,
   install an exact version in a Build, test real text/image workflows, cold starts,
   cancellation, expiry and key rejection, and verify access/revocation/billing
   with separate accounts. No managed deployment or key is configured here.
7. **Broader parity — later.** Audio/video/3D, batching, previews, group control and
   live editor interaction need explicit design. Initial consumer scope is images.

Release acceptance: stages 3–6 must pass. On a clean Windows machine, a consumer
with no workflow connects using only URL/key, discovers a published workflow and
generates in Notch. No manual bundle import, bridge process, invitation link or
connector installation is allowed.
The research harness must not be presented as the completed consumer integration.

## Current evidence

The new native ComfyApiClient completed an encoded PNG upload, LoadImage/SaveImage
job, polling and 16×16 PNG download directly against the official token-protected
v2 proxy and Linux ComfyUI, without the development bridge. Its saved job was
resumed twice without a new submission. CTest validates direct-client auth and
redirect behavior along with the unchanged native protocol, and the vendored
single-header implementation compiles.

The actual existing C++ Client completed image generation through the development
bridge, both with an independent HTTP fake and real Linux ComfyUI through the
official v2 proxy. This validates the old native contract against the adapter;
it does not establish that Notch already calls v2 directly.

Linux ComfyUI 0.37.0 registered all five core Notch nodes. A v2 job executing
NotchSingleInput and NotchOutputNode produced a downloadable image. The HTTP output
needs `preview: true` for v2 to discover its native image reference. The included
`verify-plugin` command repeats this proof and resumes its recorded job ID.

Build docs/CLI support `customNodes` with `id` and `registryVersion`, Git sources
and uploaded packages. Registry `notch` returned 404 and its version list was empty
on the research date. Managed Registry installation and GPU execution remain
unverified. Keys and endpoint/account authorization cannot be proved by local tests.

## Operational constraints

- API is beta; pin/test the wire surface on upgrades.
- `min=0` scales to zero with cold starts. Workers incur startup/loading/execution
  and idle charges; model storage remains billable while paused. Discover current
  GPUs/regions/rates from Comfy instead of hardcoding prices.
- v2 idempotency keys reject reuse rather than replaying the first response.
- Worker state and WebSocket ownership cannot span autoscaled workers. Persist
  execution state in the host and use durable v2 job IDs.
- Source removal retires Runpod tooling but does not stop existing Pods, revoke
  credentials, unpublish an existing website or uninstall old connector apps.

## Sources

- https://docs.comfy.org/development/deploy/overview
- https://docs.comfy.org/development/serverless/quickstart
- https://docs.comfy.org/development/serverless/overview
- https://docs.comfy.org/development/run-workflows/overview
- https://docs.comfy.org/development/api-development/sdks
- https://docs.comfy.org/api-reference/v2/overview
- https://github.com/Comfy-Org/docs/blob/main/openapi-v2.yaml
- https://github.com/jKaarlehto/ComfyUI-Notch
