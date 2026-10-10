# Verification checklist

Automated/local:
- [x] Official SDK discovery and tool/event listing.
- [x] Persistent, tenant-scoped subscriptions.
- [x] Correct signed challenge; wrong challenge/key rejected.
- [x] Encrypted signing keys and refresh with dual-key rotation.
- [x] Expiration/revocation across a restart; idempotent unsubscription.
- [x] Filtered delivery with exact signed body and stable event ID.
- [x] Transient retries, terminal 410/413, bounded retry count.
- [x] Private/mixed DNS addresses rejected; tampering/stale signatures rejected.
- [x] Job creation, claims, image validation, duplicate submissions, failure/retry.
- [x] Mocked end-to-end event → tools → completed image retrieval.

Deployment/platform (requires your hosting and ChatGPT configuration):
- [ ] Public HTTPS and actual outbound callback DNS/TLS connectivity.
- [ ] OAuth provider discovery, PKCE/resource/client registration and user consent.
- [ ] Tenant ACL provisioned; account revocation wired to the ACL.
- [ ] Persisted database, images and encryption-key backup/restore.
- [ ] Plugin refresh shows tools and image.requested.
- [ ] Real ChatGPT callback verification, refresh, delivery and unsubscription.
- [ ] Actual Work task has compatible image generation and image-byte retrieval.
- [ ] Browser displays a real generated image and clear failure when unavailable.
- [ ] Burst behavior, batching preference and hosting capacity validated.

Mocked callback tests do not establish real ChatGPT connectivity or generation.
