# ComfyUI Notch Connector

Run a private ComfyUI server on Runpod and share access through individual invitation links. The Windows connector starts the server and opens an authenticated SSH tunnel. Notch and the browser connect to ordinary local HTTP at `127.0.0.1:18188`.

If the stopped Pod's GPU is unavailable, the starter creates a replacement using the workspace's persistent storage and configured GPU fallbacks. It retires the old Pod after verifying the replacement, and existing invitation links continue to work. See [GPU recovery](deploy/runpod/README.md#gpu-recovery) for limits and owner controls.

- **Owners:** open [manage_hosted_comfyui.bat](deploy/runpod/manage_hosted_comfyui.bat) for setup, server controls, invitations and revocation. See the [setup and usage guide](deploy/runpod/README.md) for prerequisites and defaults.
- **Testers:** open the private invitation sent by the owner. The [connection page](https://jkaarlehto.github.io/comfyui-hosted-connector/) offers the connector installer when needed, then follows startup and model downloads. Once connected it shows the local address and an Open ComfyUI button. Keep the connector open while using ComfyUI.

After the owner configures the access gateway, new invitations contain a one-time enrollment token. Connector 1.1 generates its own SSH key and saves its enrolled device credential locally. The owner can revoke invitations and devices through the gateway without starting the GPU. Without gateway setup, `share` still issues the existing reusable invitations; those remain valid until explicitly revoked. Keep all links private. Owner credentials, private SSH keys and deployment state stay in Git-ignored `.env` and `.runpod/` files. The new flow requires deploying the gateway, applying the GPU template and publishing the 1.1 connector; source changes alone do not enable it for an existing deployment.

## Repository layout

| Path | Purpose |
| --- | --- |
| `deploy/runpod/` | Runpod deployment, startup, storage, parking, owner/tester helpers and tests |
| `deploy/runpod/connector/` | Windows connector source, tests and packaging |
| `deploy/runpod/site/` | Invitation page source |
| `deploy/gateway/` | Cloudflare invitation enrollment, device access and starter gateway |
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
