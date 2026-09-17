# Hosted ComfyUI connector

Run a private ComfyUI server on Runpod and share access through individual invitation links. The Windows connector starts the server and opens an authenticated SSH tunnel. Notch and the browser connect to ordinary local HTTP at `127.0.0.1:18188`.

If the stopped Pod's GPU is unavailable, the starter tries Runpod's native migration. It retires the old Pod after verifying the replacement, and existing invitation links continue to work. See [GPU recovery](deploy/runpod/README.md#gpu-recovery) for limits and owner controls.

- **Owners:** open [manage_hosted_comfyui.bat](deploy/runpod/manage_hosted_comfyui.bat) for setup, server controls, invitations and revocation. See the [setup and usage guide](deploy/runpod/README.md) for prerequisites and defaults.
- **Testers:** open the private invitation sent by the owner. The [connection page](https://jkaarlehto.github.io/comfyui-hosted-connector/) offers the connector installer when needed. Keep the connector open while using ComfyUI.

Invitations contain reusable tester credentials. Keep links private. Owner credentials, private SSH keys and deployment state stay in local, Git-ignored `.env` and `.runpod/` files. Creating invitations requires the owner's Runpod account; installing the private plugin also requires GitHub access to that repository.

## Repository layout

| Path | Purpose |
| --- | --- |
| `deploy/runpod/` | Runpod deployment, startup, storage, parking, owner/tester helpers and tests |
| `deploy/runpod/connector/` | Windows connector source, tests and packaging |
| `deploy/runpod/site/` | Invitation page source |
| `docs/` | Published page and connector downloads; the only GitHub Pages source |

The ComfyUI plugin is maintained separately in [ComfyUI-Notch](https://github.com/jKaarlehto/ComfyUI-Notch). This repository has no Notch engine source dependency.

## Development

The Python helpers use the standard library and require Python 3.10 or newer. Windows owner tools also need GitHub CLI and OpenSSH. Page tests use Node.js; connector builds use the .NET Framework compiler included with Windows. MSIX packaging additionally needs the Windows SDK and a trusted signing certificate for distribution.

From the repository root:

```powershell
python -m unittest discover -s deploy/runpod -p "test_*.py"
node deploy/runpod/test_site.js
powershell -NoProfile -File deploy/runpod/test_owner_menu.ps1
powershell -NoProfile -File deploy/runpod/connector/build.ps1 -OutputDirectory .local-work/connector -SiteUrl https://jkaarlehto.github.io/comfyui-hosted-connector/ -Test
```

See the [publication instructions](deploy/runpod/README.md#publishing-the-invitation-page) before updating the website or downloads.
