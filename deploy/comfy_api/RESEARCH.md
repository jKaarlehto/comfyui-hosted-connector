# Builder / Comfy API research coverage

Reviewed on 2026-10-01. This inventory distinguishes documents read, implemented
HTTP contracts and local evidence from behavior that needs an authenticated
managed test. It does not claim access to private platform documentation or UI.

## Product distinctions and consequences

| Concept | What the sources describe | Consequence for Notch |
| --- | --- | --- |
| Builder / Build | Editable environment definition: ComfyUI, custom nodes, models, Python dependencies | Package dependencies; it is not a saved graph |
| Release | Immutable definition cut with artifacts for selected OS/GPU targets | Require a ready Linux/NVIDIA artifact and `deployable: true` |
| Comfy API deployment | Hosted, autoscaling endpoint running one release, accepting submitted API graphs | One environment can run multiple workflows; each job needs its graph |
| Comfy Cloud | Managed editor/environment available without Build/Deploy | Separate execution target and subscription/credit rules |
| Managed Builds | Enterprise team sharing/governance on the Builder marketing page | Does not establish consumer access to serverless jobs/assets |
| Desktop snapshot | ComfyUI/custom-node/pip configuration, not model files or a workflow publication | Useful environment input; missing models still need sources/uploads |
| Notch workflow definition | Proposed graph/bindings/reference JSON stored through native assets | Supplies URL-only graph loading; not Comfy's saved-workflow feature |

The deployment guide explicitly says one Build can include dependencies from
multiple workflows and clients then submit each API-format graph. The quickstart
also passes `workflow_api.json` separately to `comfy deploy run`. Neither says
that release/deploy mutates the original graph or embeds its endpoint in local
workflow JSON. Notch must write its own optional publication reference and DFX
snapshot through host integration.

## Documentation reviewed

Builder product page:

- [Builder](https://comfy.org/platform/builder/): local Build packaging versus
  deployment on Comfy API, custom nodes and dependency resolution, and the
  enterprise boundary on Managed Builds team sharing/governance.
- [Platform home](https://platform.comfy.org/profile/home) and the user-provided
  marketing screenshot: **Deploy your workflow as an API**, with install scan,
  Linux/NVIDIA release creation and `deploy up` returning a Build endpoint.
  **Upload a workflow** is explained by the deployment guide as preselection of
  environment dependencies; clients still submit API graphs. The screenshot
  establishes neither public editor/plugin routes nor bare-URL graph discovery.
  The public page shell was inspected; an authenticated wizard was not tested.

All English pages in the source documentation's serverless section:

- [Quickstart](https://docs.comfy.org/development/serverless/quickstart)
- [Deployment Guide](https://docs.comfy.org/development/serverless/overview)
- [Build Sources](https://docs.comfy.org/development/serverless/build-sources)

All English pages in the source documentation's deploy section:

- [Overview](https://docs.comfy.org/development/deploy/overview)
- [Comfy Cloud](https://docs.comfy.org/development/deploy/cloud)
- [Cloud usage/limits](https://docs.comfy.org/development/deploy/cloud-usage)
- [Self-hosting](https://docs.comfy.org/development/deploy/self-hosting)

The official comfy-cli references that the quickstart instructs readers to use:

- [Build](https://github.com/Comfy-Org/comfy-cli/blob/main/comfy_cli/skills/comfy-build/SKILL.md)
- [Build authoring](https://github.com/Comfy-Org/comfy-cli/blob/main/comfy_cli/skills/comfy-build-authoring/SKILL.md)
- [Build dependency pins](https://github.com/Comfy-Org/comfy-cli/blob/main/comfy_cli/skills/comfy-build-pins/SKILL.md)
- [Build failures](https://github.com/Comfy-Org/comfy-cli/blob/main/comfy_cli/skills/comfy-build-failures/SKILL.md)
- [Deploy](https://github.com/Comfy-Org/comfy-cli/blob/main/comfy_cli/skills/comfy-deploy/SKILL.md)
- [Deploy failures](https://github.com/Comfy-Org/comfy-cli/blob/main/comfy_cli/skills/comfy-deploy-failures/SKILL.md)

Related runtime/authentication/import documentation:

- [Running workflows](https://docs.comfy.org/development/run-workflows/overview)
- [SDKs](https://docs.comfy.org/development/api-development/sdks)
- [SDK design/scope](https://docs.comfy.org/development/api-development/sdks-design)
- [v2 overview](https://docs.comfy.org/api-reference/v2/overview) and
  [OpenAPI schema](https://github.com/Comfy-Org/docs/blob/main/openapi-v2.yaml)
- [API keys](https://docs.comfy.org/development/api-development/getting-an-api-key)
- [Desktop snapshots](https://docs.comfy.org/installation/desktop/usage/snapshots)
- [Official local API proxy](https://docs.comfy.org/development/comfyui-server/api-proxy)

The generated `llms.txt` index did not enumerate these developer Build/Deploy
pages. Coverage was checked against the public docs repository tree instead.
Localized copies were not separately reviewed.

## Contracts checked in official source

[comfy-cli](https://github.com/Comfy-Org/comfy-cli): `builder_api.py`,
`deploy_api.py`, current Build commands/spec/imports and Deploy
up/runtime/run/type/progress commands. These establish:

- Builder uses `https://platformapi.comfy.org/builder/v1/`; Deploy uses
  `https://platformapi.comfy.org/deploy/v1/`. Workspace keys use `X-API-Key`,
  while a stored CLI login uses a bearer session. Runtime v2 uses bearer keys.
- Workflow resolution is read-only. Models are reported by filename, not added
  as download sources; use model resolution/review or upload blobs. Node
  versions chosen by import must be reviewed and fixed before release.
- `customNodes` supports Registry slug/version, pinned Git source or uploaded
  package. Requirements must import on the actual Linux base environment.
- Releases can be complete without being deployable. Ready Linux/NVIDIA
  artifacts, not the aggregate terminal status, determine deployability.
- The ready deployment's `endpointUrl` is independent from each submitted graph.
  Creation supports an idempotency key; jobs reject reused keys rather than
  replaying a lost submission response.
- Builder/deployment listings are paginated. Environment updates create a new
  deployment/URL; the old deployment remains separately billable until stopped.
- Native v2 assets accept a declared MIME type including JSON, have stable IDs
  and an authenticated content route, and can have retention deadlines. Their
  signed download URL's expiry is distinct from the asset's retention.
- Runtime scope excludes saved-workflow management, node introspection and named
  parameters. A known job's workflow route cannot bootstrap a new consumer.
- The managed public contract is the job/asset surface, without a documented
  deployed browser editor, native `/ws` or `/notch/*` forwarding. Worker node
  installation does not establish public access to those interfaces.
- `GET /api/v2/jobs/{id}/events` documents job SSE progress/previews/output hints
  and a `501 not_implemented` response where streaming is unavailable. The
  current native SDK has no SSE transport/parser. Its finite HTTP transport
  must not be mistaken for an incremental stream. Native WebSocket protocol
  tests and final output assets do not prove managed SSE.

## Local proof and open questions

Implemented native SDK hooks use those control-plane requests and the v2 wire
schema. Contract tests prove request construction, key isolation, URL validation,
snapshot integrity and failures; they do not prove account availability or a
managed deployment. Native JSON asset upload, URL-only loading and credential-free
snapshot persistence passed through the actual official local v2 proxy. Image
generation/polling/download and Notch custom-node execution also passed locally.

## Authenticated platform checks, 2026-10-01

An authorized user key was tested without including it in source or this report.
The actual C++ `ComfyPublishClient` successfully listed Builds and deployments;
both lists were empty. Raw platform checks also confirmed compute catalog access
and workflow dependency resolution. Builder reported `NotchOutputNode` as missing
and unresolved, so an automatic Registry-based import cannot yet package it.

The actual C++ `ComfyApiClient` passed against **Comfy Cloud**, a separate target:
JSON definition upload, URL-only snapshot loading, credential-free serialization,
PNG upload, LoadImage/SaveImage execution, polling and output download. The output
is 16×16 and matches the uploaded pixels. A saved-job rerun uses reads without a
new submission. Invalid credentials receive 401; an authenticated unknown asset
returns 404, consistent with treating that probe as inconclusive.

The live receiver initially rejected SDK uploads with `422 invalid_body` because
`content_type` followed the file field. Metadata now precedes the file in the SDK
multipart body; the regenerated header and native regression checks pass. Native
JSON assets are accepted on Cloud, but its responses omit retention, so the SDK
reports unknown retention rather than a permanent publication.

There was no existing serverless endpoint to test. After approval, the C++ hooks
created and remotely validated a model-free Build using ComfyUI `v0.37.0` and
plugin commit `3bc6fdc56dab97d95671a100990f315311e5e25f`, then cut one Linux/NVIDIA
release. It failed at assembly because the GitHub repository is private and the
builder could not fetch it. The repository remains private; no Registry release
was published.

The approved commit was archived from tracked files, excluding `.env` and local
state, for private node-blob packaging through official comfy-cli `1.22.0`.
That upload was rejected by GCS with `400 MalformedSecurityHeader`; no second
release was cut. A tiny upload probe reproduced the rejection independently of
node package size: storage reports a signed header missing from the request.
No serverless deployment or GPU compute was created. Actual
Linux/NVIDIA custom-node execution, serverless asset retention and cross-account
access remain gates. Private blob packaging also would not prove Registry-version
installation.

Test resources retained for diagnosis (one failed release, no deployment):

- Build: `27031e14-dad3-4e3c-bd08-9446a9fd9ede`
- Release: `584d7c87-25b2-474c-8da8-d463cb2b0274`

## Remaining managed validation

Managed validation must answer:

1. Can a different authorized user's key read the publisher's definition asset
   and run jobs on that deployment? What grants access, revokes it, and whose
   account is billed? The OpenAPI describes account-scoped keys/resources; it
   does not establish arbitrary sharing. Never distribute the owner's key.
2. What default asset retention applies on managed endpoints? Is durable metadata
   supported, and what expiry is actually returned? Do not offer permanent links
   based on a local proxy's retention. DFX embeds a snapshot to avoid depending
   on a definition link for saved graphs.
3. Does the exact published Notch Registry archive import and execute on managed
   Linux/NVIDIA? The local five-node registration/execution proof does not answer
   that. Registry `notch` had no public version at review time.
4. Are the control-plane hooks enabled for the intended account? Build/Deploy is
   limited beta. Its CLI-backed surface needs a live integration check.
5. For a separate future managed preview feature, does the actual target stream
   SSE and do its workflows produce previews? Test `501`, reconnects and
   authoritative polling. Full native editor/plugin sessions require a different
   persistent hosting/interface path, not inference from a successful image job.

Platform/Cloud key access has been verified. No serverless endpoint or Notch host
source is available in this workspace.
The SDK implementation and source retirement are reviewable draft PRs; complete
consumer shipping remains gated on these managed tests and host wiring.
