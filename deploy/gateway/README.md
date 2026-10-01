# Enrollment gateway

This Worker consumes an invitation once, records the enrolled device, and forwards authenticated connection requests to one Runpod starter. One SQLite-backed Durable Object holds each deployed workspace's invitations, devices, job ownership and rate limits.

Deploy through the owner setup helper in `../runpod`. Its configuration supplies `WORKSPACE_NAME` and `RUNPOD_STARTER_ID`; its secrets are:

| Secret | Access |
| --- | --- |
| `GATEWAY_ADMIN_KEY` | Owner: issue/revoke invitations and revoke devices |
| `GATEWAY_SERVER_KEY` | Pod: read enabled SSH public keys |
| `RUNPOD_STARTER_KEY` | Gateway: invoke only the configured Runpod endpoint |

The gateway does not need a Runpod account control key. Secrets must be different, randomly generated values. Never put them in Wrangler configuration or a tester invitation. Observability is disabled to keep request data out of Worker logs.

`INVITATION_ORIGIN` is the exact HTTPS origin of the invitation page, derived by owner setup from the configured site URL. Only the read-only status endpoint permits requests from this browser origin; enrollment, device and owner endpoints still reject browser origins.

## Invitation status

The page posts `{invite_id,token_hash}` to `/v1/invitations/status` before offering installation. The hash is SHA-256 of the UTF-8 invitation token; the raw token is not sent. The response contains only `{state}`: `unused`, `redeemed`, `expired`, `revoked` or `invalid`. A wrong hash and an unknown ID both return `invalid`. This request cannot consume an invitation, register a device or start the Pod.

The page refreshes status every fifteen seconds while visible and before connection. A failed check offers retry instead of installation. Redeemed invitations still allow an installed connector to reopen access on the original computer. Revoking an invitation after enrollment does not revoke its device; status remains `redeemed` until the device itself is revoked.

## Tester requests

An invitation contains `{version:2,gateway,invite_id,token}`. The ID is 32 lowercase hexadecimal characters and the token is 64. The connector generates and saves its SSH key and independent 256-bit reconnect secret before submitting:

```
POST /v1/enroll
{invite_id,token,public_key,credential_hash,device_name}
```

`credential_hash` is the hexadecimal SHA-256 of the UTF-8, lowercase hexadecimal reconnect secret. Only Ed25519 SSH public keys are accepted. Names contain at most 80 characters and no control characters. The response is `{device_id,workspace_name}`. A retry with the same public key and credential hash returns the same receipt. Another binding receives HTTP 409; expired unused invitations receive 410; revoked invitations/devices receive 403.

Subsequent requests send `Authorization: Bearer <reconnect-secret>` and `X-Device-ID: <device_id>`:

| Request | Result |
| --- | --- |
| `POST /v1/connect` with `{}` | `{id,status:"IN_QUEUE"}`; poll the returned job even if the provider finished immediately |
| `GET /v1/jobs/<id>` | Sanitized Runpod job status and connection output |
| `POST /v1/jobs/<id>/cancel` with `{}` | Cancel only this device's recorded job |

Concurrent connection requests share a recorded in-flight job. HTTP 409 from `/v1/connect` means submission is still pending; retry later. Ambiguous provider submissions retain a two-minute reservation. Requests and provider replies are bounded; redirects are rejected. Each device is authenticated again after provider calls, so revocation blocks an in-flight response.

## Owner and Pod requests

Owner requests use `Authorization: Bearer <GATEWAY_ADMIN_KEY>`:

| Request | Result |
| --- | --- |
| `POST /v1/admin/invites` with `{label,expires_in_seconds}` | `{invite_id,token,expires_at,workspace_name}`; token returned only here |
| `GET /v1/admin/invites` | `{invites:[{invite_id,label,created_at,expires_at,state,device_id}]}` |
| `POST /v1/admin/invites/<id>/revoke` with `{}` | Revoke an invitation; already enrolled devices remain enabled |
| `GET /v1/admin/devices` | `{devices:[{device_id,device_name,created_at,revoked_at,invite_id}]}` |
| `POST /v1/admin/devices/<id>/revoke` with `{}` | Disable device reconnect and remove its public key from the registry |

Invitation lifetime defaults to seven days and accepts 60 seconds through 30 days. Times use Unix seconds. Up to 128 devices can be enabled; invitation/device history is capped at 4,096 records each. Job ownership expires after 24 hours.

`GET /v1/server/keys` uses `Authorization: Bearer <GATEWAY_SERVER_KEY>` and returns `{revision,lease_seconds:300,keys:[{device_id,public_key}]}`. The Pod must enforce the lease and refresh the authoritative registry. Revocation prevents subsequent SSH authentication once synchronized; existing tunnels remain until closed or the Pod parks.

Enrollment is independent of Pod startup. A GPU outage cannot consume a link without recording its reconnect identity. Used links can remain bookmarks on the enrolled computer, but an unused copied link is first-user-wins. Old v1 API keys and SSH credentials must be revoked when switching to v2.

## Local checks

```
npm ci
npm test
npm run check
```

Tests run real Miniflare SQLite Durable Objects with outbound requests mocked. They cover simultaneous redemption, retries, expiration, revocation during requests, durable restart, job ownership, resource bounds and secret filtering. The final command is a Wrangler deployment dry run; none of these checks changes cloud resources.
