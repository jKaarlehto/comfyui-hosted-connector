# Hosted ComfyUI for Notch

The owner deploys one private GPU Pod. Testers receive a dedicated invitation that can wake that Pod and open a local SSH tunnel to ComfyUI. Notch connects to `127.0.0.1:18188` using HTTP transport. These tools live in the public `comfyui-hosted-connector` repository; the ComfyUI plugin remains a separate dependency.

## Access flow

```text
Public invitation page (static files; no login or backend)
    -> Windows connector receives the private invitation
        -> starter key: asks Runpod to wake the hosted workspace
        -> SSH key: opens the restricted tunnel to ComfyUI
            -> http://127.0.0.1:18188
                -> browser and Notch use ordinary local HTTP
```

The link contains those two reusable tester keys. The connector handles authentication; Notch receives neither key. The owner's Runpod account key stays in the private owner environment/`.env` and the starter's server-side secret. A separate read-only Git deploy key fetches the private plugin on the Pod; it is not included in invitations or the public page.

| Role | Helper | Purpose |
| --- | --- | --- |
| Owner | `manage_hosted_comfyui.bat` / `.ps1` | Double-click menu for setup, secrets, Pod controls, owner connection and tester invitations. |
| Owner | `runpod.py` | Creates the private template and storage, deploys and controls the Pod, and opens an owner tunnel. |
| Owner | `hosted.py` | Creates the small serverless starter and issues or revokes individual tester invitations. |
| Tester | `start_hosted_comfyui.bat` | Opens the launcher, accepts an invitation, starts the Pod, and keeps the connection window open. |
| Tester | `start_hosted_comfyui.ps1` | Implements progress, credential storage, SSH forwarding and the clickable WebUI link. The batch file launches it. |
| Pod | `bootstrap.py` | Fetches ComfyUI and the private plugin at each start, installs requirements, restores files and starts ComfyUI. |
| Pod | `model_store.py` | Prepares saved model links and mirrors completed models, workflows, inputs and outputs to the global volume. |
| Pod | `model_cache.py` | Copies a saved model locally when ComfyUI loads it and reports transfer progress to the connector. |
| Pod | `park.py` | Flushes files and stops the Pod after the configured idle or runtime limit. |
| Starter | `broker.py` | Accepts a connect request for the owner's workspace and returns its current SSH endpoint. |
| Starter | `recovery.py` | Starts or recreates the workspace, tries GPU fallbacks, verifies the replacement and retires the old Pod. |
| Pod | `health.py` | Checks restored storage, ComfyUI and the plugin through a restricted SSH command. |
| Publisher | `prepare_site.py` / `publish_site.py` | Stages and publishes an explicit allowlist of public page and connector files. |

## Owner setup

On Windows, double-click `manage_hosted_comfyui.bat`. Its menu covers setup and secrets, deploy, status, start, stop, opening the WebUI, starter setup, invitation creation and revocation. It stays open after success or failure. Closing the menu closes its owner tunnel. Choose **G** to sign in to GitHub, **8** to create an invitation and open its folder, **L** to copy an existing tester's private invitation link, **I** to list testers, or **9** to revoke one. Listing testers and copying an existing link work while the Pod is stopped.

Install Python 3.10+, GitHub CLI and Windows OpenSSH. Authenticate GitHub CLI with access to the private plugin repository. Set `RUNPOD_API_KEY` in the environment or an owner-only `.env` file. The helper prompts for it when run interactively without either, saves the entered value to `.env` and skips that prompt on later runs. It updates only `RUNPOD_API_KEY` and preserves other settings.

The same operations are available from the connector repository root:

```powershell
python deploy/runpod/runpod.py setup
python deploy/runpod/runpod.py deploy
python deploy/runpod/runpod.py connect --background
python deploy/runpod/hosted.py setup
python deploy/runpod/hosted.py share --guest alice
python deploy/runpod/hosted.py link --guest alice
```

The Pod must be running when invitations are granted or revoked. Send Alice the two launcher files and her `invitation.txt` from `.runpod/shares/alice/` privately. Run `python deploy/runpod/hosted.py revoke --guest alice` to remove her starter credential and prevent new SSH connections. Existing tunnels remain connected until closed or the Pod stops.

Successful revocation also removes that tester's generated local invitation, link and SSH key files. Copies already delivered to the tester cannot be erased remotely, but their credentials are revoked. Reissuing the same tester name creates a fresh SSH key and starter credential, so an old link does not regain access. If a remote update fails, the helper marks the invitation's revocation pending and retains its control record for a retry. Sharing other invitations cannot restore a pending tester's SSH access. Use `python deploy/runpod/hosted.py list` to list recorded tester names and pending revocations without contacting Runpod.

`share` also writes `invitation-link.txt`. Send its contents privately for the installed connector flow. `link` regenerates it from the existing invitation without contacting Runpod or waking the Pod. The default invitation site is `https://jkaarlehto.github.io/comfyui-hosted-connector/`; pass `--site-url https://your-site.example/path/` to save a different HTTPS site for this deployment. Links contain the same reusable credentials as `invitation.txt`. Anyone with a leaked link can access the shared ComfyUI workspace and incur GPU usage until their invitation is revoked; links are not single-use and do not expire automatically.

`hosted.py setup` applies the hosted template to the existing Pod; Runpod may restart it. Wait for startup before sharing. Each setup uses its own state directory. Keep that directory, its private keys and `.env` out of source control and sharing packages.

The GitHub credential on the Pod is a read-only deploy key for the plugin repository. It is registered through the owner's existing GitHub CLI login and delivered through a Runpod secret. The owner's Runpod API key is held by the starter as a Runpod secret. Invitations contain a starter-only Runpod API key and an SSH key restricted to the ComfyUI port; they do not contain either owner credential.

## Configuration

Pass settings to `runpod.py setup`; they are saved for subsequent commands. Re-run setup and reapply the template to an existing Pod when changing its startup settings. Deploy uses the saved template and storage selection.

| Option | Default | Meaning |
| --- | --- | --- |
| `--storage` | `global` | Dedicated elastic global volume with a local disk cache; `pod` and an existing `network` volume are also available. |
| `--disk-gb` | `32` | Local container disk capacity. This must fit the base image and the active models. |
| `--volume-gb` | `32` | Pod volume capacity when using Pod storage. Global storage grows with usage. |
| `--volume-id` | Created during setup | Reuse a particular global or network volume. |
| `--ref` | `install-startup-requirements` | Plugin branch, tag or commit fetched at every start. Use a branch for automatic updates. |
| `--comfy-ref` | `stable` | Latest stable ComfyUI release, resolved at each start. Explicit branches, tags and commits are also supported. |
| `--instance-name` | `ComfyUI Notch (Runpod)` | Advertised ComfyUI instance label. |
| `--comfy-port` | `8188` | Remote ComfyUI port; only reachable through SSH. |
| `--local-port` | `18188` | Owner's forwarded local port. The tester script also accepts `-LocalPort`. |
| `--gpu` | `NVIDIA A40` | Requested GPU model. |
| `--datacenter` | `EU-SE-1` | Preferred GPU location. |
| `--idle-minutes` | `20` | Stop after no queued/running generation, new history or active model download. Zero disables it. |
| `--minutes` | `240` | Maximum runtime after ComfyUI startup. Zero disables it. |
| `--state-dir` | `.runpod` | Owner deployment state and private keys. |

`hosted.py setup --starter-idle-seconds 60` controls how long the CPU starter stays warm between requests. The default is 60 seconds, so polling during GPU startup does not repeatedly cold-start it. Its worker minimum is zero.

The starter manages one hosted workspace with global storage. It first tries the saved GPU, then L40S, RTX 6000 Ada, RTX A6000 and A40 across available regions, within a saved hourly price limit. Configure these with `hosted.py setup --gpu-fallbacks "NVIDIA L40S,NVIDIA A40" --max-hourly-cost 2.09`. The default limit is the configured GPU's current price at setup. Recovery replaces the workspace's Pod; it does not scale it.

Opening the WebUI or leaving an SSH tunnel connected does not prevent idle parking. Long-running generations are stopped if the maximum runtime is reached. Storage is flushed before parking; a failed flush is retried before stopping.

Every startup resolves `stable` through the official ComfyUI repository's latest-release API and fetches that exact release tag. Draft and prerelease releases are rejected. If the release cannot be resolved, startup fails instead of selecting a development branch. The plugin independently fetches its configured branch, tag or commit on every startup; its default remains `install-startup-requirements`. Logs show the resolved ComfyUI tag and both checked-out commits. Existing setups keep their saved ref; run `runpod.py setup --comfy-ref stable` and reapply the template to switch them.

The base image and its startup script are pinned and checked. This remote deployment uses HTTP transport and skips the plugin's `cuda-python` metapackage when installing its dependencies, preserving the base image's PyTorch-compatible CUDA bindings. The plugin's source requirements are left unchanged and its redundant startup installer is disabled in the deployment. Updates retain the image's pinned PyTorch/CUDA requirements. An incompatible upstream dependency update fails startup instead of silently replacing that stack.

## Storage

Global storage persists after Pod deletion and can restore the important files on another Pod. Startup restores workflows, inputs and outputs, then exposes saved models through local file links to the mounted global volume. It checks their presence and size without reading every model, so the WebUI can start before model transfers. Existing local models are reused.

The hosting extension copies a linked model to local disk when ComfyUI's standard model loader first requests it. It reports the filename, transferred bytes and verification state through the existing SSH connection; connector 1.0.3 or newer displays this progress while remaining connected. It also follows downloads started through the plugin's model-download API. The copy is checked against its saved checksum before atomically replacing the link. Failed transfers leave the saved model intact and can be retried by generating again. Custom loaders which bypass ComfyUI's standard loader can read the global link directly, without local-cache progress.

New files and downloads remain on a normal local filesystem. Completed files are copied to checksum-addressed blobs and verified before publishing their manifest entries. Model links and unfinished transfers are excluded from synchronization. A global object volume does not provide normal filesystem locking and rename semantics.

`models/`, `user/default/workflows/`, `input/` and `output/` are mirrored. Custom node installations, virtual environments and private SSH keys are not mirrored. File deletions are not propagated, and old blobs are retained. This is a single-writer store for this Pod, not a live shared filesystem for multiple Pods. Global storage still incurs storage charges while the Pod is stopped.

Pod storage survives a stop/start but is deleted with the Pod. A network volume is tied to its region and persists independently. No helper deletes a persistent volume when terminating a Pod.

Use `python deploy/runpod/runpod.py stop` to flush global storage and stop the Pod, or `python deploy/runpod/runpod.py terminate` to flush and remove it. `python deploy/runpod/runpod.py sync` requests an explicit flush while it is running.

## GPU recovery

A stopped Pod does not reserve its GPU. During connection, the starter first tries to start the existing Pod. If that host has no free GPU, or the Pod is missing, it allocates a replacement using the existing template and global volume. Each request tries at most one GPU tier. After all eligible tiers are exhausted, it waits a minute before trying again. The connector's startup timeout still applies.

Replacement creates a new Pod ID and SSH address. The starter retains the stopped source until it verifies the replacement's ownership, GPU price, configuration, storage mounts and running backend. A dedicated SSH key can only execute the fixed health check; it cannot forward ports or open a shell. The check requires prepared storage, the model-cache status endpoint, ComfyUI and the plugin's HTTP transport. Successful recovery removes the source, leaving one Pod. Existing invitations continue to use the same starter and restored guest keys.

Use `python deploy/runpod/runpod.py replace` to queue recovery through the same starter. It reuses a healthy Pod, starts a stopped Pod when possible, and replaces it when capacity is unavailable. Owner commands resolve the current Pod by deployment identity.

When startup code in the template changes, the starter applies it to a stopped Pod before resuming it. Running Pods are left alone; their configuration is updated on the next wake.

Recovery progress is stored in the private GPU template's `NOTCH_RECOVERY` environment entry. Updating progress does not redeploy the starter. After an uncertain allocation response, the starter checks for the recorded attempt for at least two minutes and three separated inventories before retrying. Delayed duplicate allocations are reconciled and retired. Retries after cold starts or interrupted cleanup resume from the saved state. Unexpected ownership, configuration or storage changes stop recovery for owner review.

`hosted.py setup --no-restart` updates the template and starter without restarting the Pod. Use it only when the running Pod already has the matching health command and key; normally setup applies those through a restart.

## Tester connection

The invitation page has two states: **Download and install** until the local connector answers, then **Connect**. Open the downloaded installer from the browser's downloads menu, usually near the upper right, or from Downloads. The page checks every two seconds while visible and switches automatically after installation or removal. Click Connect and allow the browser to open the connector; it starts the Pod, establishes the local connection and opens the WebUI when ready.

Installation includes a small background presence helper that starts at Windows sign-in. It only reports connector availability on `127.0.0.1:18187`; it cannot start a Pod or accept an invitation. Uninstall removes the startup entry and stops the helper. The browser may request permission to contact localhost. If permission is denied, the helper is stopped or that port is occupied, the page cannot detect it and keeps showing Download and install. Allow local access for the invitation site to enable detection. No browser setting or enterprise policy is changed by the installer.

Presence requests contain only a fresh random nonce. The helper checks the page origin, loopback Host header and protocol registration, and returns its application identity, protocol version and nonce. The page never treats a cookie or a past installation as proof that the connector is available. Invitation data stays in the URL fragment until the user clicks Connect; it is never sent to the hosting server, presence helper or browser storage. Keep invitation links private, including browser history and copied messages.

The script-only alternative remains available:

Double-click `start_hosted_comfyui.bat`, paste the invitation and wait for the progress steps. The script saves the invitation as `HOSTED_COMFYUI_ACCESS` in a protected `.env` file beside the launcher and reuses it on later launches. Keep that file private. It connects through the starter and establishes an SSH tunnel bound only to loopback. The final window shows the WebUI link and the local address/port for Notch. Keep the launcher open while using the connection.

For custom settings, run the PowerShell script with `-LocalPort` (default `18188`), `-StartupTimeoutMinutes` (default `15`) or `-EnvFile` (default `.env` beside the script).

The launcher needs Windows PowerShell and the Windows OpenSSH client. Python and Runpod account access are unnecessary for testers. Each invitation is private to its recipient. Closing the launcher closes the tunnel; the Pod stops later according to its idle policy.

## Publishing the invitation page

This repository contains the connector and deployment source under `deploy/runpod/`. Edit the page in `deploy/runpod/site/`; the generated public page and downloads live under `docs/`. GitHub Pages serves **main /docs**, while the existing Runpod starter handles wake requests. There is no new web backend. The private plugin, `.env`, `.runpod`, invitation files and keys must never enter Git or the Pages payload.

After building the connector, stage explicit files, inspect the publication manifest, then publish:

```powershell
python deploy/runpod/prepare_site.py --output .runpod/public-site --installer PATH/HostedComfyUIConnector.exe
python deploy/runpod/publish_site.py check --site .runpod/public-site
python deploy/runpod/publish_site.py publish --site .runpod/public-site
```

The staging helper rejects unexpected existing files, versions script and stylesheet URLs by their content hashes, and allows the generated `.nojekyll` marker when refreshing `docs/` locally. The publisher uses an isolated checkout, checks the payload allowlist, artifact metadata and executable header, and stages only `docs/`. It preserves source files and other repository content. An unexpected file under `docs/` or a different Pages source stops publication for review. App Installer packaging remains available for separately signed distribution; the invitation page uses the executable installer.

## Verification

Run `uv run python -m unittest discover -s deploy/runpod -p "test_*.py"` for mutation-free tests of ownership checks, failed-request reconciliation, revocation ordering, starter request restrictions and storage integrity. On Windows the storage tests stub only the Linux file lock; actual locking needs Linux verification. `smoke.py` exercises a model-free remote HTTP image round trip after connecting; its optional test dependencies are Pillow, requests and websocket-client. Run it with `uv run --with pillow --with requests --with websocket-client python deploy/runpod/smoke.py`.

`powershell.exe -NoProfile -File deploy/runpod/test_owner_menu.ps1` checks menu command construction, paths with spaces, process failures and navigation using local stubs. `node deploy/runpod/test_site.js` checks invitation parsing, invalid input and page navigation without opening a connection.
