CREATE TABLE calls (
    call_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    runtime TEXT NOT NULL CHECK (runtime IN ('pipecat', 'livekit')),
    channel TEXT NOT NULL CHECK (channel IN ('phone', 'web')),
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    provider TEXT,
    provider_call_id TEXT,
    from_number TEXT,
    to_number TEXT,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'completed', 'failed')),
    webhook_status TEXT NOT NULL DEFAULT 'not_ready'
        CHECK (webhook_status IN ('not_ready', 'pending', 'delivered', 'dead_lettered')),
    started_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    terminal_reason TEXT,
    owner_id TEXT,
    generation BIGINT NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    delivery_endpoint TEXT,
    include_json JSONB NOT NULL DEFAULT '["transcript","data","recording","metrics"]',
    redact_json JSONB NOT NULL DEFAULT '[]',
    purge_after_days INTEGER NOT NULL DEFAULT 30,
    recording_id TEXT,
    outcome TEXT,
    results_json JSONB NOT NULL DEFAULT '{}',
    interruptions INTEGER NOT NULL DEFAULT 0 CHECK (interruptions >= 0),
    last_provider_state TEXT
);
CREATE INDEX calls_started_at_idx ON calls(started_at DESC);
CREATE INDEX calls_status_updated_idx ON calls(status, updated_at);

CREATE TABLE call_timeline (
    sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    details_json JSONB NOT NULL,
    relay_operation_id TEXT
);
CREATE INDEX call_timeline_call_idx ON call_timeline(call_id, sequence);
CREATE UNIQUE INDEX call_timeline_relay_operation_idx
    ON call_timeline(relay_operation_id) WHERE relay_operation_id IS NOT NULL;

CREATE TABLE call_transcript (
    sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    text TEXT NOT NULL,
    t_ms INTEGER NOT NULL CHECK (t_ms >= 0),
    relay_operation_id TEXT
);
CREATE INDEX call_transcript_call_idx ON call_transcript(call_id, sequence);
CREATE UNIQUE INDEX call_transcript_relay_operation_idx
    ON call_transcript(relay_operation_id) WHERE relay_operation_id IS NOT NULL;

CREATE TABLE call_tools (
    sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    invocation_id TEXT NOT NULL UNIQUE,
    tool_name TEXT NOT NULL,
    arguments_json JSONB NOT NULL,
    result_json JSONB,
    duration_ms DOUBLE PRECISION NOT NULL CHECK (duration_ms >= 0),
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed', 'timed_out')),
    occurred_at TIMESTAMPTZ NOT NULL,
    relay_operation_id TEXT
);
CREATE INDEX call_tools_call_idx ON call_tools(call_id, sequence);
CREATE UNIQUE INDEX call_tools_relay_operation_idx
    ON call_tools(relay_operation_id) WHERE relay_operation_id IS NOT NULL;

CREATE TABLE call_latency (
    sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL CHECK (turn_index >= 1),
    metric TEXT NOT NULL
        CHECK (metric IN ('stt_partial', 'stt_final', 'llm_ttft', 'tts_ttfb', 'e2e')),
    duration_ms DOUBLE PRECISION NOT NULL CHECK (duration_ms >= 0),
    observed_at TIMESTAMPTZ NOT NULL,
    relay_operation_id TEXT
);
CREATE INDEX call_latency_call_idx ON call_latency(call_id, sequence);
CREATE UNIQUE INDEX call_latency_relay_operation_idx
    ON call_latency(relay_operation_id) WHERE relay_operation_id IS NOT NULL;

CREATE TABLE call_events (
    event_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL
        CHECK (event_type IN (
            'call.started',
            'call.completed',
            'call.failed',
            'call.recording.ready'
        )),
    is_terminal BOOLEAN NOT NULL,
    body BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX one_terminal_event_per_call
    ON call_events(call_id) WHERE is_terminal;
CREATE UNIQUE INDEX one_recording_ready_event_per_call
    ON call_events(call_id) WHERE event_type = 'call.recording.ready';
CREATE INDEX call_events_call_idx ON call_events(call_id, created_at);

CREATE TABLE deliveries (
    event_id TEXT NOT NULL REFERENCES call_events(event_id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivering', 'delivered', 'dead_lettered')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    delivered_at TIMESTAMPTZ,
    PRIMARY KEY(event_id, endpoint)
);
CREATE INDEX deliveries_claim_idx
    ON deliveries(status, next_attempt_at, lease_expires_at);

CREATE TABLE recordings (
    recording_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL UNIQUE REFERENCES calls(call_id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'ready', 'failed')),
    access_url TEXT,
    storage_key TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    ready_at TIMESTAMPTZ
);

CREATE TABLE backups (
    backup_id TEXT PRIMARY KEY,
    storage_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE purge_queue (
    storage_key TEXT PRIMARY KEY,
    artifact_kind TEXT NOT NULL CHECK (artifact_kind IN ('recording', 'backup')),
    queued_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE relay_nonces (
    key_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(key_id, nonce)
);
CREATE INDEX relay_nonces_expiry_idx ON relay_nonces(expires_at);

CREATE TABLE relay_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    request_kind TEXT NOT NULL,
    call_id TEXT NOT NULL,
    response_body BYTEA,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE relay_streams (
    call_id TEXT PRIMARY KEY,
    last_sequence BIGINT NOT NULL DEFAULT 0 CHECK(last_sequence >= 0)
);

CREATE TABLE relay_updates (
    call_id TEXT NOT NULL,
    sequence BIGINT NOT NULL CHECK(sequence >= 1),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_body BYTEA,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(call_id, sequence),
    UNIQUE(call_id, idempotency_key)
);
