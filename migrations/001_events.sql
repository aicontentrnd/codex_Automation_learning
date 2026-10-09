-- Upgrade the original starter without dropping jobs or pending events.
ALTER TABLE event_outbox RENAME TO event_outbox_legacy;
CREATE TABLE event_outbox (
    event_id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
    job_id TEXT NOT NULL, name TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
);
INSERT INTO event_outbox SELECT * FROM event_outbox_legacy;
DROP TABLE event_outbox_legacy;
ALTER TABLE jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1;
ALTER TABLE jobs ADD COLUMN claim_id TEXT;
ALTER TABLE jobs ADD COLUMN claim_token TEXT;
ALTER TABLE jobs ADD COLUMN lease_until REAL;
ALTER TABLE jobs ADD COLUMN result_hash TEXT;
CREATE TABLE subscriptions (
    id TEXT PRIMARY KEY, principal TEXT NOT NULL, tenant TEXT NOT NULL,
    name TEXT NOT NULL, arguments TEXT NOT NULL, url TEXT NOT NULL,
    secret TEXT NOT NULL, old_secret TEXT, old_until REAL,
    expires_at REAL NOT NULL, active INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX subscriptions_tenant ON subscriptions(tenant, active, expires_at);
CREATE TABLE callback_verifications (
    principal TEXT NOT NULL, url TEXT NOT NULL, key_hash TEXT NOT NULL,
    verified_until REAL NOT NULL, PRIMARY KEY(principal, url, key_hash)
);
CREATE TABLE deliveries (
    event_id TEXT NOT NULL REFERENCES event_outbox(event_id),
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
    state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL, lease_until REAL, last_error TEXT, last_status INTEGER,
    PRIMARY KEY(event_id, subscription_id)
);
CREATE INDEX deliveries_due ON deliveries(state, available_at);
CREATE TABLE idempotency (
    tenant TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
    response TEXT NOT NULL, PRIMARY KEY(tenant, key)
);
