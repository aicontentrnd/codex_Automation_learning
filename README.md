# Image request MCP Events server

A standalone application for the initially empty repository. Python 3.12+, the
official MCP Python SDK 2.3.0, SQLite, and local image storage implement:

- MCP 2026-07-28 discovery, three image tools, and the documented OpenAI Events methods.
- Tenant-scoped jobs, persistent subscriptions, expiration/refresh,
  encrypted signing secrets, signed callback challenges, and key rotation.
- Transactional event creation, persistent delivery attempts, bounded exponential
  retries, stable event IDs, and fresh Standard Webhooks signatures on every attempt.
- Validated HTTPS destinations with DNS pinning, TLS hostname verification, no
  redirects, and blocking of private and other non-public addresses.
- Job claims, idempotent creation and results, safe retries, and decoded/re-encoded
  image storage. The browser displays the result from an authenticated endpoint.
- OAuth JWT verification and resource metadata for public deployments; private
  bearer-token mode for local development. Accounts within one tenant share jobs.

## Run locally

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python scripts/init_local.py
set -a
. ./.env
set +a
.venv/bin/python server.py
```

Run from the repository directory. The initializer creates private local-only
credentials in `.env` and `data/local-tokens.json` and refuses to overwrite them.
The local initializer enables the browser test page without a token. Existing
local `.env` files need `PUBLIC_DEMO_MODE=true` added. No services or paid
generation APIs are automatically connected. The server binds to loopback port
8000 by default.

Subscribe before creating requests. Requests without a matching subscriber stay
pending; after subscribing, use **Retry request**. The UI accepts a prompt and aspect
ratio. `POST /jobs` also accepts dimensions,
HTTPS reference image URLs and output preferences. The test page needs
`PUBLIC_DEMO_MODE=true` and no token. API clients outside demo mode send a bearer
token. Send a unique `Idempotency-Key` header (8–128 characters) for creation,
reusing it on network retries.
`GET /jobs/{id}` shows status and delivery counts. `POST /jobs/{id}/retry` with a
new idempotency key creates a new attempt and invalidates stale results.

## Deploy and connect ChatGPT

1. Deploy one application process with persistent `data/` storage. Install the
   locked Python dependencies on the host. Put an HTTPS reverse proxy in front of it;
   `deploy/nginx.conf` provides an example with body and rate limits.
   `PUBLIC_BASE_URL` must match the browser/ChatGPT origin and forwarded Host header.
2. Set the production variables in `.env.example` through your secret manager.
   Generate and retain a Fernet `SUBSCRIPTION_ENCRYPTION_KEY`; it encrypts callback
   secrets. Back up that key separately and keep it stable across restarts.
   For a temporary public test page, set `PUBLIC_DEMO_MODE=true`. It uses the
   sole active tenant from `AUTH_SUBJECTS_FILE` for browser requests. Anyone
   with the page URL can create and view demo jobs; turn the flag off after
   testing. The MCP endpoint still requires OAuth.
3. Configure an OAuth 2.1 identity provider supporting PKCE, the MCP resource
   parameter, and a supported ChatGPT client registration method (CIMD, DCR, or a
   predefined client). Configure its issuer and JWKS URLs. The access token audience
   must be `PUBLIC_BASE_URL/mcp`, with scopes `images:read images:write events:subscribe`.
   This app is the OAuth resource server; it does not invent an identity provider.
4. Provision `AUTH_SUBJECTS_FILE` as a private JSON mapping:
   `{"issuer-subject-id":{"tenant":"tenant-id","enabled":true}}`.
   This server-controlled map determines tenant access; no user-supplied tenant
   claim is trusted. Atomically replace the file to change access. Disabling/removing
   a subject blocks new requests and future deliveries. Connect your account
   disconnection/revocation process to this ACL; IdP revocation alone is not a
   webhook account-disconnection notification. Never commit this file.
5. In **ChatGPT Plugins → + → Add custom MCP server**, enter the public HTTPS
   `/mcp` endpoint, configure OAuth, and select **Create as a plugin**. Review its
   tools and `image.requested` event. Refresh/rescan after changing metadata.
6. Start a **Work** chat on ChatGPT web, or choose **Work and Cloud** in the desktop
   app. Paste [the reusable task instructions](docs/chatgpt-task.md). Confirm
   subscription and callback verification.
7. Submit a prompt in the app. Check delivery progress, the task run in ChatGPT,
   and the resulting image or clear failure reason in the app.

Public deployment requires OAuth mode for the MCP endpoint. The token-free
browser page works when `PUBLIC_DEMO_MODE=true` and the account file contains
exactly one active tenant.

## Protocol and operation details

`POST /mcp` uses the SDK's Streamable HTTP transport. Requests use protocol metadata
and the matching `MCP-Protocol-Version`, `MCP-Method`, and (where required) `MCP-Name`
headers. Prefer an MCP client SDK instead of hand-writing these envelopes. The
Events extension is registered through the SDK's public handler and middleware
hooks, including the extra `events` discovery capability.

Subscriptions are keyed by principal, tenant, callback URL and event name. Subscription
arguments must be empty. Default and maximum lifetime is 24 hours. A requested shorter positive
`ttlMs` is honored; null grants a finite day. Cursors are null because protocol replay
is not supported. Deliveries already queued for active subscriptions survive process
restarts; events missed while unsubscribed/expired are not replayed. Refresh secrets
are encrypted and old/new signatures overlap for five minutes. Verification is
cached for five minutes per principal, URL and signing key. One event is sent per
request with a 256 KiB limit and a 10-second connection/response budget. System DNS
lookup is also subject to the deployment resolver's timeout; configure it accordingly.

Retryable network failures and HTTP 408, 425, 429 and 5xx receive at most eight
attempts, with exponential backoff and jitter capped around five minutes. Other
HTTP failures, redirects, 410 and 413 are terminal; 410 also disables the subscription.
Status `event_delivered` means a callback accepted the event, not image completion.
A 30-minute processing claim prevents competing task executions. Identical final
submissions return the saved result; conflicting or stale attempts fail. Image URLs
are relative paths; access requires a bearer token unless demo mode is enabled.

Migrations run automatically, preserving the original starter's jobs and events.
Back up SQLite and images together while the service is stopped (or use SQLite's
backup API), and protect prompts, images, ACL files and encryption keys. A filesystem
write followed by a database failure can leave an unreferenced image file; it cannot
be read through the authenticated job endpoint. Remove such files during maintenance.
This deployment intentionally supports one process; do not run multiple Uvicorn
workers/replicas against it. For horizontal scale, move to shared database/storage
and distributed subscription/delivery locking. Apply retention policies before
long-term use; rows and files are retained until operator maintenance.

## Verification and current limits

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q server.py auth.py common.py events.py secure_http.py storage.py
node --check app.js
```

Automated tests use a mocked ChatGPT callback and actual SDK HTTP requests. They
cover discovery, schemas, subscription identity/refresh, encryption, challenge and
signature validation, secret rotation, tenant isolation, unsubscription,
expiry/restart/revocation, retry statuses and limits, job claims, safe image ingestion,
idempotency, failures, retries and completed application updates. See
[verification checklist](docs/verification.md) for deployment checks.

**A real ChatGPT delivery and image generation have not been tested.** This repository
contains the integration; you must supply a hosting destination and OAuth provider,
connect the plugin and subscribe in ChatGPT. Whether a Work event-triggered task has
an image-generation tool, and can retrieve its image bytes, depends on that execution
environment. The task reports failure when either capability is unavailable.

An optional fallback is an explicitly authorized image-generation API worker that
claims the same jobs and submits real results. Add provider credentials and a budget
only after choosing that paid integration. No paid API calls are enabled here.

Official references checked during implementation:
- [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)
- [Build an MCP server](https://developers.openai.com/plugins/build/mcp-server)
- [Authentication](https://developers.openai.com/plugins/build/auth)
- [Connect and test](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [Events design sketch](https://github.com/modelcontextprotocol/experimental-ext-triggers-events/blob/main/docs/design-sketch-proposal.md)
