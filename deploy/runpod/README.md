# ComfyUI Notch hosted workspace

The owner deploys one private GPU Pod. Testers receive a dedicated invitation that can wake that Pod and open a local SSH tunnel to ComfyUI. Notch connects to `127.0.0.1:18188` using HTTP transport. These tools live in the public `comfyui-hosted-connector` repository; the ComfyUI plugin remains a separate dependency.

## Access flow

```text
Public invitation page (static files; no login or backend)
    -> Windows connector redeems a one-time invitation with the access gateway
        -> device token: asks the gateway to wake the hosted workspace
        -> locally generated SSH key: opens the restricted tunnel to ComfyUI
            -> http://127.0.0.1:18188
                -> browser and Notch use ordinary local HTTP
```

After gateway setup, new links contain a one-time enrollment token. The connector keeps its enrolled device credential and SSH private key locally; Notch receives neither. The Cloudflare gateway holds a starter-only Runpod key. The owner's Runpod account key stays in the private owner environment/`.env` and the starter's server-side secret. The Pod's separate gateway server credential only fetches authorized public keys. A read-only Git deploy key fetches the private plugin on the Pod; it is not included in invitations or the public page. Without gateway setup, `share` still issues reusable invitations; existing reusable invitations keep their original access until explicitly revoked.

| Role | Helper | Purpose |
| --- | --- | --- |
| Owner | `manage_hosted_comfyui.bat` / `.ps1` | Double-click menu for setup, secrets, Pod controls, owner connection and tester invitations. |
| Owner | `runpod.py` | Creates the private template and storage, deploys and controls the Pod, and opens an owner tunnel. |
| Owner | `hosted.py` / `gateway_owner.py` | Configures the starter and access gateway, and issues or revokes invitations and enrolled devices. |
| Tester | `start_hosted_comfyui.bat` | Opens the launcher, accepts an invitation, starts the Pod, and keeps the connection window open. |
| Tester | `start_hosted_comfyui.ps1` | Implements progress, credential storage, SSH forwarding and the clickable WebUI link. The batch file launches it. |
| Pod | `bootstrap.py` | Fetches ComfyUI and the private plugin at each start, installs requirements, restores files and starts ComfyUI. |
| Pod | `model_store.py` | Prepares saved model links and mirrors completed models, workflows, inputs and outputs to the global volume. |
| Pod | `model_cache.py` | Copies a saved model locally when ComfyUI loads it and reports transfer progress to the connector. |
| Pod | `park.py` | Flushes files and stops the Pod after the configured idle or runtime limit. |
| Starter | `broker.py` | Accepts a connect request for the owner's workspace and returns its current SSH endpoint. |
| Starter | `recovery.py` | Starts or recreates the workspace, tries GPU fallbacks, verifies the replacement and retires the old Pod. |
| Pod | `health.py` | Checks restored storage, ComfyUI and the plugin through a restricted SSH command. |
| Pod | `server_keys.py` | Refreshes enrolled public keys and authorizes SSH connections only while its gateway lease is valid. |
| Publisher | `prepare_site.py` / `publish_site.py` | Stages and publishes an explicit allowlist of public page and connector files. |

## Owner setup

On Windows, double-click `manage_hosted_comfyui.bat`. Its menu stays open after success or failure. Closing it closes its owner tunnel. Choose **G** to sign in to GitHub, **A** to configure the invitation gateway, **8** to create an invitation, **L** to copy its private link, **I** to list invitations, **9** to revoke an invitation, **D** to list enrolled devices or **R** to revoke a device. Gateway invitation and device management work while the GPU Pod is stopped.

Install Python 3.10+, GitHub CLI and Windows OpenSSH. Authenticate GitHub CLI with access to the private plugin repository. Set `RUNPOD_API_KEY` in the environment or an owner-only `.env` file. The helper prompts for it when run interactively without either, saves the entered value to `.env` and skips that prompt on later runs. It updates only `RUNPOD_API_KEY` and preserves other settings.

The same operations are available from the connector repository root:

```powershell
python deploy/runpod/runpod.py setup
python deploy/runpod/runpod.py deploy
python deploy/runpod/runpod.py connect --background
python deploy/runpod/hosted.py setup
python deploy/runpod/hosted.py setup-gateway
python deploy/runpod/hosted.py share --guest alice
python deploy/runpod/hosted.py link --guest alice
python deploy/runpod/hosted.py devices
python deploy/runpod/hosted.py revoke-device --device DEVICE_ID
```

Gateway setup also requires Node.js/npm and Cloudflare account credentials. Set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` in the private owner `.env`; missing values are requested interactively and saved without replacing other settings. The Cloudflare token needs Workers Scripts edit access and account settings read access for the selected account. `setup-gateway` installs the pinned gateway dependencies, deploys its Worker/Durable Object, provisions a scoped starter key and stores its administrative and server keys privately. It updates the GPU template without restarting the Pod. Stop and reconnect the workspace once to apply the new SSH authorization helper before issuing device access.

Once configured, `share` creates a one-time invitation valid for seven days; `--invite-days 1..30` changes its expiry. Repeating `share` reuses an existing unused invitation. Send the private link, or the three launcher files and `invitation.txt` from `.runpod/shares/alice/`. The GPU does not need to be running. A leaked unused link can enroll one device and incur workspace GPU usage; expiry or invitation revocation prevents enrollment. Once redeemed, revoke the enrolled **device** to remove its access. Revoking only the invitation does not revoke its device.

The Pod fetches enrolled public keys every ten seconds. SSH checks the local five-minute lease on every new connection, including when the refresh process has failed. Device revocation blocks gateway wake requests immediately and new SSH connections within five minutes. Existing tunnels remain until closed or parked. These managed keys are never restored from persistent guest files. Owner, health and existing reusable tester keys remain separate.

`share` also writes `invitation-link.txt`; `link` regenerates it without waking the Pod. The default site is `https://jkaarlehto.github.io/comfyui-hosted-connector/`; pass `--site-url https://your-site.example/path/` to save another HTTPS site. Links and files are private credentials and must never be published.

Existing reusable invitations are listed separately and remain usable until explicitly revoked. Their revocation still requires the Pod to be running to remove its static SSH key and starter credential. Reissuing an existing reusable tester name through the gateway is refused: revoke it first or use another name. Successful revocation removes its locally generated invitation and SSH files; a failed remote update keeps the control record for retry. No existing access is automatically migrated.

`hosted.py setup` applies the hosted template to the existing Pod; Runpod may restart it. Wait for startup before sharing. Each setup uses its own state directory. Keep that directory, its private keys and `.env` out of source control and sharing packages.

The GitHub credential on the Pod is a read-only deploy key registered through the owner's GitHub CLI login and delivered through a Runpod secret. The gateway's server-only key is also delivered through a Runpod secret and removed from the ComfyUI process environment. Neither owner credential is included in invitations or public assets.

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

Plugin deployment uses a partial Git fetch and sparse checkout, excluding test media and the C++ client SDK. SSH starts before update downloads, allowing the connector to report the current startup stage or a failure. Plugin fetches retry using GitHub's pinned SSH endpoints on ports 443 and 22; credentials remain read-only deploy keys.

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

Replacement creates a new Pod ID and SSH address. The starter retains the stopped source until it verifies the replacement's ownership, GPU price, configuration, storage mounts and running backend. A dedicated SSH key can only execute the fixed health check; it cannot forward ports or open a shell. The check requires prepared storage, the model-cache status endpoint, ComfyUI and the plugin's HTTP transport. Successful recovery removes the source, leaving one Pod. Enrolled devices continue through the same gateway and starter; reusable invitations retain their restored guest keys.

Use `python deploy/runpod/runpod.py replace` to queue recovery through the same starter. It reuses a healthy Pod, starts a stopped Pod when possible, and replaces it when capacity is unavailable. Owner commands resolve the current Pod by deployment identity.

When startup code in the template changes, the starter applies it to a stopped Pod before resuming it. Running Pods are left alone; their configuration is updated on the next wake.

Recovery progress is stored in the private GPU template's `NOTCH_RECOVERY` environment entry. Updating progress does not redeploy the starter. After an uncertain allocation response, the starter checks for the recorded attempt for at least two minutes and three separated inventories before retrying. Delayed duplicate allocations are reconciled and retired. Retries after cold starts or interrupted cleanup resume from the saved state. Unexpected ownership, configuration or storage changes stop recovery for owner review.

`hosted.py setup --no-restart` updates the template and starter without restarting the Pod. Use it only when the running Pod already has the matching health command and key; normally setup applies those through a restart.

## Tester connection

For one-time invitations, the page checks status with the gateway before showing installation or Connect. Expired, revoked and invalid invitations ask for a new link. Redeemed invitations direct the recipient to their existing saved workspace, and still allow an installed connector to reconnect on the original computer. The check does not consume the invitation or start the Pod. Status refreshes every fifteen seconds while the page is visible and no connection has been launched; a five-second timeout offers **Check again**.

The following describes the 1.1 connector source. An existing deployment needs the updated connector and page published, the gateway configured and the GPU template applied before one-time invitations work.

The invitation page shows **Download and install** until the local connector answers, then **Connect**. Older connectors show **Update connector**. Open the downloaded installer from the browser's downloads menu, usually near the upper right, or from Downloads. The page checks every two seconds while visible and switches automatically after installation or removal. Click Connect and allow the browser to open the connector; it starts the Pod, establishes the local connection and opens the WebUI when ready.

Connector 1.1 reports progress to the invitation tab and shows **Saved workspaces** in its native window. A one-time invitation enrolls this computer and saves the workspace for later connections; select it and click Connect without reopening the invitation. The page shows server startup stages, model transfer and verification progress, errors and disconnection. Once connected, it provides **Open ComfyUI**, the local address and **Copy address**. Keep the connector open while working. If the browser does not open it, **Reconnect** appears after twenty seconds. Connection failures also offer Reconnect. Reloading the page requires clicking Connect again; the connector attaches the new page to an existing connection for the same invitation.

Saved device credentials and SSH private keys are protected with Windows DPAPI for the current Windows account on this computer. Copying the workspace files or bookmark to another computer does not grant access; request a separate invitation there. **Remove** deletes the selected workspace's local credentials and closes its active connection. It does not revoke the device on the server. Ask the owner to revoke a lost or unwanted device; a new invitation is needed to add a removed workspace again.

Installation includes a small background presence helper that starts at Windows sign-in. It reports connector availability and read-only connection status on `127.0.0.1:18187`; it cannot start a Pod or accept an invitation. Uninstall removes the startup entry and stops the helper. The browser may request permission to contact localhost. If permission is denied, the helper is stopped or that port is occupied, the page cannot detect it. Allow local access for the invitation site to enable detection. No browser setting or enterprise policy is changed by the installer.

Presence requests contain only a fresh random nonce. When Connect is clicked, the page creates a separate random session token and passes it through the URI alongside the invitation. Local status requests use that session token and a fresh nonce; responses contain only connection state, fixed progress messages, model progress and the loopback address. The helper checks the configured page origin, loopback Host header and protocol registration. The page never treats a cookie or a past installation as proof that the connector is available. Before connection, the page sends only the invitation ID and SHA-256 token hash to the gateway's read-only status endpoint. The raw token stays in the URL fragment until passed to the connector and is never sent to GitHub Pages, the presence helper or browser storage. The connector submits it only to the designated enrollment gateway. Keep invitation links private, including browser history and copied messages. Older reusable invitations remain reusable and revocable by the owner.

The script-only alternative remains available:

Double-click `start_hosted_comfyui.bat`, paste the invitation and wait for the progress steps. For a one-time invitation, the script stores DPAPI-protected credentials under `workspaces/` beside its `.env` file. `HOSTED_COMFYUI_ACCESS` contains only a bookmark with the gateway and invitation ID, with no enrollment token or device secret. Later launches open that saved workspace. For an older reusable invitation, `.env` still contains the reusable invitation and must remain private. The script establishes an SSH tunnel bound only to loopback. The final window shows the WebUI link and the local address/port for Notch. Keep the launcher open while using the connection.

For custom settings, run the PowerShell script with `-LocalPort` (default `18188`), `-StartupTimeoutMinutes` (default `15`) or `-EnvFile` (default `.env` beside the script).

The launcher needs Windows PowerShell and the Windows OpenSSH client. Python and Runpod account access are unnecessary for testers. Each invitation is private to its recipient. Closing the launcher closes the tunnel; the Pod stops later according to its idle policy.

## Publishing the invitation page

This repository contains the connector and Runpod source under `deploy/runpod/`, and the access gateway under `deploy/gateway/`. Edit the page in `deploy/runpod/site/`; the generated public page and downloads live under `docs/`. GitHub Pages serves static files from **main /docs**. The separately deployed Cloudflare Worker/Durable Object handles one-time enrollment, device authorization and forwarding wake requests to the existing Runpod starter. Publishing the page does not deploy that backend. The private plugin, `.env`, `.runpod`, invitation files and keys must never enter Git or the Pages payload.

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
