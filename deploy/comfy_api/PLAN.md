# Native Comfy API integration goal and plan

Goal: a publisher packages an environment with Builder and hosts a release on
Comfy API. A consumer with no graph enters only a **Notch workflow URL** and an
authorized key in Notch. The SDK fetches the graph/controls and runs it directly.
DFX stores the complete snapshot and endpoint; keys stay in the host credential
store. No invitation, helper app, tunnel or manual consumer import.

The user confirmed Notch host source is unavailable and requested SDK hooks.
Implementation scope is the vendorable SDK and research harness, with an explicit
contract for the unavailable host UI/DFX integration.

The managed scope is **image jobs**, not full native live-connection parity.
Host UI/DFX must explicitly select that backend and expose only its implemented
features. Do not silently ignore unsupported live operations or fall back to
native routes after managed requests fail.

## Chosen flow

1. Resolve the author's workflow dependencies. Review unresolved classes, exact
   custom-node versions, model sources/digests/placement and ComfyUI version.
   Workflow import alone does not capture a full environment or model sources.
2. Select a compatible existing Build/release/deployment, or create/validate a
   Build, cut a Linux/NVIDIA release and wait for `deployable: true`.
3. Review current GPU/region/rates and worker bounds. Persist deployment intent
   and IDs, create/poll it, then take the ready record's `endpointUrl`. A new
   environment release requires a new deployment/URL; another graph on a
   compatible existing environment does not.
4. Compile stateless graph I/O and bindings. Upload a credential-free Notch
   snapshot as native JSON asset metadata. Copy its stable authenticated
   `/api/v2/assets/{id}/content` workflow URL and disclose its retention.
5. Consumer enters that workflow URL in Notch. Derive its origin before selecting
   a saved credential, fetch/validate the graph and controls, then cache the
   complete snapshot in DFX. No file download/import action or paid probe job.
6. Host optionally saves its publication reference beside the local authored
   JSON. DFX always embeds the graph and bindings as well as the endpoint, so an
   expired definition asset need not lose the saved workflow. Reopening uses the
   snapshot and asks the recipient for an authorized key if needed.
7. Bind validated controls and encoded image uploads. Save job intent, submit
   once, save its ID, poll/cancel/download and deliver outputs. Resume known IDs;
   an ambiguous submit without an ID must not silently become a second paid run.

Method mapping and the DFX contract are in the SDK's
[PUBLISHING.md](https://github.com/jKaarlehto/ComfyUI-Notch/blob/0251477473ee16701b58a162ae8c176fea251b1a/cpp/comfy_extension_client/PUBLISHING.md).
The SDK's [backend boundaries](https://github.com/jKaarlehto/ComfyUI-Notch/blob/0251477473ee16701b58a162ae8c176fea251b1a/cpp/comfy_extension_client/BACKENDS.md) define canonical interface/capability ownership;
host integration must follow it alongside this feature's release acceptance.

## Feasibility boundaries

Builder packages an environment; a release freezes it. Comfy API deploys that
release and accepts multiple caller-submitted graphs. A bare endpoint URL has
no saved graph, and the documented v2 scope excludes workflow catalogs/named
parameters. Build/Deploy does not rewrite the original workflow JSON with an
endpoint. Comfy Cloud and enterprise Managed Builds are separate products.

The implemented URL-only candidate is a **Notch definition asset URL**, using
native Comfy assets. It is not a Comfy saved-workflow feature or invitation.
Managed retention and authorization with another user's key remain release gates.
Local proxy tests prove the HTTP path, not managed cross-account sharing. If
native assets cannot meet these requirements, URL-only sharing remains blocked.
Do not distribute the owner's key or reinstate an invitation gateway.

The direct SDK skips `/notch/*`, WebSocket ownership and runtime plugin updates.
Registry installation does not prove custom routes are exposed. Worker graphs
use native values, LoadImage and SaveImage instead of caches or IPC handles.
Other custom nodes remain in the graph and must exist in the pinned Build.

Native publication hooks accept reviewed remote Build definitions with Registry
pins/public model sources. Local scanning, private-model/blob transfer and node
ZIP packaging remain native Builder wizard/comfy-cli publisher tasks. The host
owns its compiler, typed binding, media encoding and actual DFX resource writes.

### Frontend, native routes and previews

The managed public contract is a job/asset API, not a deployed ComfyUI editor or
our browser extension. Builder's UI configures the environment; Comfy Cloud's
hosted editor is a separate product. The environment release is frozen, but each
job supplies a graph. Notch's published snapshot pins graph/bindings separately.
Frontend compilation and output metadata stamping happen before submission in
the author's editor or Notch host, not in a worker's browser extension.

Existing `Client` HTTP/WebSocket methods remain the native backend. Full native
remote features need a persistent ComfyUI server exposing real plugin routes over
authenticated HTTPS/WSS; these PRs do not implement that hosting path. Installing
Registry code or keeping workers warm does not establish public route exposure.

The v2 contract documents job SSE progress/previews, but permits `501` when the
deployment lacks streaming. The current SDK has no SSE implementation. Future
support needs an incremental streaming transport/event parser and managed preview
tests, separate from native WebSocket handling. Polling remains authoritative and
reconnection must never submit another job. A requirement for live previews is
not met by silently continuing without them.

## Status and acceptance

1. **Research reviewed:** Builder page, all serverless/deploy guides, all six
   official Build/Deploy CLI references, v2/SDK/auth docs and actual API source.
   [Research coverage](RESEARCH.md) records the product distinctions and gaps.
2. **Direct execution SDK implemented:** HTTPS transport contract, endpoint/key
   validation, binary upload, one-shot submission, polling/cancellation and
   stable-ID downloads with safe redirects; vendored header generated/compiled.
3. **Publication/persistence SDK implemented:** dependency/model resolution,
   Build/release create/status, paginated selection/recovery, compute catalog
   and estimate, deployment create/status/pause/resume, JSON definition assets,
   workflow-URL loading and snapshot serialization. Live Build creation,
   validation and release requests passed; the release failed because Builder
   cannot fetch the private Git repository. No real deployment created.
4. **Research harness implemented:** stateless compiler, custom-node proof and
   development-only bridge. CLI `compile` creates a local bundle, not a deployment
   or cloud publication.
5. **Host wiring outside available source:** Publish/Connect UI, OS credential
   store, compiler/binding, image encoding, DFX resource serializer, async jobs
   and durable state. User accepted SDK hooks as the available implementation.
6. **Managed/Registry proof required:** publish `notch`, pin that exact version
   in a Build, and test managed node execution, JSON assets, retention, another
   account's authorization/billing, rejection/revocation, cold starts and expiry.
   An authorized key has passed live Builder/Deploy listing and Comfy Cloud
   publication/execution checks. The account has no existing serverless endpoint;
   one test Build/release was created, but Git assembly and subsequent private
   blob upload failed. No serverless deployment or GPU compute was created.
7. **Separate capability work:** optional job SSE previews require implementation
   and managed proof. Native live protocol/editor parity needs a proven persistent
   hosting path. Audio/video/3D, batches and groups remain outside image-job scope.

Release acceptance: a consumer on a clean Windows machine with Notch installed,
no graph, and a workflow URL/key connects and generates. A saved DFX reopens with
its embedded graph and the recipient's credential. No helper, manual import or
invitation. Host wiring and managed/Registry gates must pass before release.
Acceptance also requires explicit managed backend selection in UI/DFX and clear
unavailability of native live/editor operations. This accepts a managed image-job
mode, not full connector parity. If live preview/interactive behavior is required
for a complete replacement, add and pass its separate acceptance checks first.

## Evidence and constraints

Seven native CTest checks cover runtime/publication requests and auth, URL
validation, retention/deletion, credential rejection, snapshot round trips and
64-bit numeric preservation. The C++ direct smoke passed real JSON asset
upload/URL loading/serialization plus PNG upload, generation, polling and download
through the official token-protected local proxy. Saved jobs resume without a new
submit; expired jobs are reported. The old C++ Client also executed through the
development bridge against actual ComfyUI as a separate protocol proof.

Linux ComfyUI 0.37.0 registered all five core Notch nodes; a v2 job executing
NotchSingleInput and NotchOutputNode produced an image. HTTP output requires
`preview: true` for v2 image discovery. Registry `notch` lookup returned 404 and
its versions list was empty on 2026-10-01. Managed Registry execution is untested.
That output flag proves image-asset discovery, not progressive previews, SSE or
direct plugin/WebSocket access. Local bridge WebSocket proof is also not managed
route/streaming proof.

Both API surfaces are beta. Runtime uses bearer auth; workspace publication uses
`X-API-Key`. Credential headers stay on selected origins and external HTTPS asset
redirects drop bearer headers. Never persist signed storage URLs. Job idempotency
rejects key reuse, while deployment creation has its own semantics; preserve
intent/IDs and reconcile after lost responses. `min=0` has cold starts; model
storage can still bill while paused. Read current catalogs/rates.

Source retirement removes Runpod service/app/invitation tooling. It does not stop
Pods, revoke credentials, unpublish a website or uninstall an existing app.
