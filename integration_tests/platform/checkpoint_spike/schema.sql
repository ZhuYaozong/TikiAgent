CREATE TABLE spike_leases (
    thread_id text PRIMARY KEY,
    owner_id text NOT NULL,
    epoch bigint NOT NULL,
    expires_at timestamptz NOT NULL
);
