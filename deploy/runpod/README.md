# Hosted ComfyUI for Notch

The owner deploys one private GPU Pod. Testers receive a dedicated invitation that can wake that Pod and open a local SSH tunnel to ComfyUI. Notch connects to `127.0.0.1:18188` using HTTP transport. These tools live in the public `comfyui-hosted-connector` repository; the ComfyUI plugin remains a separate dependency.

## Access flow

```text
Public invitation page (static files; no login or backend)
    -> Windows connector receives the private invitation
        -> starter key: asks Runpod to wake the fixed Pod
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
| Pod | `model_store.py` | Mirrors completed models, workflows, inputs and outputs between local disk and the global volume. |
| Pod | `park.py` | Flushes files and stops the Pod after the configured idle or runtime limit. |
| Starter | `broker.py` | Accepts a connect request for the owner's fixed Pod, starts it if necessary and returns its SSH endpoint. |
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
| `--comfy-ref` | `master` | ComfyUI branch, tag or commit fetched at every start. |
| `--instance-name` | `ComfyUI Notch (Runpod)` | Advertised ComfyUI instance label. |
| `--comfy-port` | `8188` | Remote ComfyUI port; only reachable through SSH. |
| `--local-port` | `18188` | Owner's forwarded local port. The tester script also accepts `-LocalPort`. |
| `--gpu` | `NVIDIA A40` | Requested GPU model. |
| `--datacenter` | `EU-SE-1` | Preferred GPU location. |
| `--idle-minutes` | `20` | Stop after no queued/running generation, new history or active model download. Zero disables it. |
| `--minutes` | `240` | Maximum runtime after ComfyUI startup. Zero disables it. |
| `--state-dir` | `.runpod` | Owner deployment state and private keys. |

`hosted.py setup --starter-idle-seconds 60` controls how long the CPU starter stays warm between requests. The default is 60 seconds, so polling during GPU startup does not repeatedly cold-start it. Its worker minimum is zero.

Opening the WebUI or leaving an SSH tunnel connected does not prevent idle parking. Long-running generations are stopped if the maximum runtime is reached. Storage is flushed before parking; a failed flush is retried before stopping.

The base image and its startup script are pinned and checked. This remote deployment uses HTTP transport and skips the plugin's `cuda-python` metapackage when installing its dependencies, preserving the base image's PyTorch-compatible CUDA bindings. The plugin's source requirements are left unchanged and its redundant startup installer is disabled in the deployment. Updates retain the image's pinned PyTorch/CUDA requirements. An incompatible upstream dependency update fails startup instead of silently replacing that stack. Startup logs show the actual ComfyUI and plugin commits.

## Storage

Global storage persists after Pod deletion and can restore the important files on another Pod. The helper keeps a normal local filesystem for ComfyUI because a global object volume does not provide normal filesystem locking and rename semantics. Completed files are copied to checksum-addressed blobs and verified before publishing their manifest entries.

`models/`, `user/default/workflows/`, `input/` and `output/` are mirrored. Custom node installations, virtual environments and private SSH keys are not mirrored. File deletions are not propagated, and old blobs are retained. This is a single-writer store for this Pod, not a live shared filesystem for multiple Pods. Global storage still incurs storage charges while the Pod is stopped.

Pod storage survives a stop/start but is deleted with the Pod. A network volume is tied to its region and persists independently. No helper deletes a persistent volume when terminating a Pod.

Use `python deploy/runpod/runpod.py stop` to flush global storage and stop the Pod, or `python deploy/runpod/runpod.py terminate` to flush and remove it. `python deploy/runpod/runpod.py sync` requests an explicit flush while it is running.

A stopped Pod does not reserve its GPU. If Runpod cannot start it because that host has no free GPU, retry later or use `python deploy/runpod/runpod.py replace` with global/network storage. Replacement keeps the old stopped Pod, deploys a new one using the same storage, and records the old ID in owner state. Run `python deploy/runpod/hosted.py setup` afterward to point the existing starter at the replacement; tester invitations remain tied to that starter. Validate the replacement before removing the old Pod with `python deploy/runpod/runpod.py terminate --pod OLD_ID`. Pod-local storage cannot be moved this way.

## Tester connection

Open the private invitation link and click **Connect**. If the connector is not installed, expand **First time, or nothing opened?**, download and open the installer. Browser download controls are usually near the upper right; the file is also in Downloads. After installation, the invitation tab that started the latest download tries once to open the connector automatically. Browsers may require another click or their usual external-application prompt, so **Connect** remains available. If the invitation is in another browser/profile, return to it and click **Connect**. Browsers cannot reliably inspect registered URI handlers, so the page never polls the protocol or silently downloads an installer.

The page keeps invitation data in the URL fragment; it does not send it to the hosting server or store it in browser storage. Installation notifications carry only a random request ID; a short-lived ID and timestamp identify the requesting tab without storing its invitation. The completion page reports whether that tab acknowledged the handoff, not whether the server connected. The connector performs the actual connection checks and opens the local WebUI when ready. Keep invitation links private, including browser history and copied messages.

The script-only alternative remains available:

Double-click `start_hosted_comfyui.bat`, paste the invitation and wait for the progress steps. The script saves the invitation as `HOSTED_COMFYUI_ACCESS` in a protected `.env` file beside the launcher and reuses it on later launches. Keep that file private. It starts the fixed Pod through the starter and establishes an SSH tunnel bound only to loopback. The final window shows the WebUI link and the local address/port for Notch. Keep the launcher open while using the connection.

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

The staging helper rejects unexpected existing files and allows the generated `.nojekyll` marker when refreshing `docs/` locally. The publisher uses an isolated checkout, checks the payload allowlist, artifact metadata and executable header, and stages only `docs/`. It preserves source files and other repository content. An unexpected file under `docs/` or a different Pages source stops publication for review. App Installer is optional: supply both `--appinstaller` and `--package` to the staging helper; `signtool verify /pa` must trust the MSIX signature before the page advertises it. Direct `.appinstaller` download/open is supported; enabling the disabled `ms-appinstaller:` browser protocol is unnecessary.

## Verification

Run `uv run python -m unittest discover -s deploy/runpod -p "test_*.py"` for mutation-free tests of ownership checks, failed-request reconciliation, revocation ordering, starter request restrictions and storage integrity. On Windows the storage tests stub only the Linux file lock; actual locking needs Linux verification. `smoke.py` exercises a model-free remote HTTP image round trip after connecting; its optional test dependencies are Pillow, requests and websocket-client. Run it with `uv run --with pillow --with requests --with websocket-client python deploy/runpod/smoke.py`.

`powershell.exe -NoProfile -File deploy/runpod/test_owner_menu.ps1` checks menu command construction, paths with spaces, process failures and navigation using local stubs. `node deploy/runpod/test_site.js` checks invitation parsing, invalid input and page navigation without opening a connection.
