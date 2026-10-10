# Image request MCP Events server

A backend for your application and ChatGPT MCP connection. Python 3.12+, the
official MCP Python SDK 2.3.0, SQLite, and local image storage implement:

- MCP 2026-07-28 discovery, three image tools, and the documented OpenAI Events methods.
- Workspace-scoped jobs, persistent subscriptions, expiration/refresh,
  encrypted signing secrets, signed callback challenges, and key rotation.
- Transactional event creation, persistent delivery attempts, bounded exponential
  retries, stable event IDs, and fresh Standard Webhooks signatures on every attempt.
- Validated HTTPS destinations with DNS pinning, TLS hostname verification, no
  redirects, and blocking of private and other non-public addresses.
- Job claims, idempotent creation and results, safe retries, and decoded/re-encoded
  image storage. Your application retrieves the result through the job API.
- Anonymous MCP tools and REST endpoints in one shared workspace, with no login,
  OAuth provider, bearer token, or bundled frontend.

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

Run from the repository directory. The initializer creates `.env` with a persistent
subscription encryption key and refuses to overwrite existing configuration.
The server binds to loopback port 8000 by default. There is no browser frontend;
`/` returns 404. Your existing application uses the REST API:

| Endpoint | Purpose |
| --- | --- |
| `POST /jobs` | Create a request with prompt, aspect ratio, dimensions, HTTPS reference URLs and output preferences. |
| `GET /jobs/{id}` | Read status, delivery counts and the resulting image location. |
| `GET /jobs/{id}/image` | Retrieve the completed image bytes. |
| `POST /jobs/{id}/retry` | Create another attempt and invalidate stale results. |
| `GET /health` | Check server/database availability. |

No Authorization header is required. Send a unique `Idempotency-Key` header
(8–128 characters) for creation and retry; reuse it on network retries. Subscribe
before creating requests. Requests without a matching subscriber stay pending;
after subscribing, retry the request.

## Deploy and connect ChatGPT

1. Deploy one backend process with persistent `data/` storage. Install the locked
   Python dependencies and put an HTTPS reverse proxy in front of it;
   `deploy/nginx.conf` provides an example with body and rate limits.
   `PUBLIC_BASE_URL` must match the public origin and forwarded Host header.
2. Configure the variables in `.env.example`. Generate and retain a Fernet
   `SUBSCRIPTION_ENCRYPTION_KEY`; it encrypts webhook signing secrets, not user
   credentials. Back up the key separately and keep it stable across restarts.
3. In ChatGPT's custom MCP server setup, enter the public HTTPS `/mcp` endpoint
   and choose **No authentication**. All three tools advertise
   `securitySchemes: [{"type":"noauth"}]`; there is no OAuth discovery document
   or authentication challenge. If reconnecting a server previously configured
   for OAuth, update/recreate its connection with no authentication and rescan.
   ChatGPT may still show connection/tool confirmation prompts.
4. Start a Work chat and paste [the reusable task instructions](docs/chatgpt-task.md),
   using empty subscription arguments. Confirm subscription and callback
   verification, then submit a request from your application and retrieve its result.

All callers use `APP_WORKSPACE_ID` (default `local-demo`). It is a server-side
storage namespace, not a credential or a client-selected tenant. Anyone who can
reach the endpoints can access jobs in this shared workspace and call its tools.
Host/origin checks and webhook signature validation remain enabled.
Browser calls must use the same public origin; cross-origin CORS is not enabled.
Your application backend can call this API directly without browser CORS.

### Existing deployments

Keep the existing database, images and `SUBSCRIPTION_ENCRYPTION_KEY`. Set
`APP_WORKSPACE_ID` to the tenant ID previously used by your app; this preserves
its jobs, idempotency records and static-token subscription identity. The local
initializer previously used `local-demo`, which is still the default. Existing
OAuth subscriptions used user-specific identities: recreate those subscriptions
in ChatGPT, then deactivate the old subscriptions during migration to avoid
sending duplicate events. Do not change database schemas or delete saved jobs.

Remove the old `AUTH_*`, `APP_TOKENS_JSON` and `PUBLIC_DEMO_MODE` environment
entries and obsolete credential files from your deployment configuration; the
backend no longer reads them. The initializer preserves existing `.env` files,
so update an existing file manually while retaining its encryption key.

## Protocol and operation details

`POST /mcp` uses the SDK's Streamable HTTP transport. Requests use protocol metadata
and the matching `MCP-Protocol-Version`, `MCP-Method`, and (where required) `MCP-Name`
headers. Prefer an MCP client SDK instead of hand-writing these envelopes. The
Events extension is registered through the SDK's public handler and middleware
hooks, including the extra `events` discovery capability.

Subscriptions are keyed by principal, tenant, callback URL, event name and canonical
arguments JSON. Default and maximum lifetime is 24 hours. A requested shorter positive
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
are relative paths; access does not require a token.

Migrations run automatically, preserving the original starter's jobs and events.
Back up SQLite and images together while the service is stopped (or use SQLite's
backup API), and protect prompts, images and encryption keys. A filesystem
write followed by a database failure can leave an unreferenced image file; it cannot
be read through the job endpoint. Remove such files during maintenance.
This deployment intentionally supports one process; do not run multiple Uvicorn
workers/replicas against it. For horizontal scale, move to shared database/storage
and distributed subscription/delivery locking. Apply retention policies before
long-term use; rows and files are retained until operator maintenance.

## Verification and current limits

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q server.py config.py common.py events.py secure_http.py storage.py
```

Automated tests use a mocked ChatGPT callback and actual SDK HTTP requests. They
cover discovery, schemas, subscription identity/refresh, encryption, challenge and
signature validation, secret rotation, workspace scoping, unsubscription,
expiry/restart, retry statuses and limits, job claims, safe image ingestion,
idempotency, failures, retries and completed application updates. See
[verification checklist](docs/verification.md) for deployment checks.

**A real ChatGPT delivery and image generation have not been tested.** This repository
contains the integration; you must supply a hosting destination,
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
