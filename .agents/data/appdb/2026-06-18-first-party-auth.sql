-- Kaidera AI first-party console auth.
--
-- Operational app-DB only. Cortex memory stays out of the human-auth path.
-- Login is passwordless: email code/link by default, optional WebAuthn/passkeys.

CREATE TABLE IF NOT EXISTS auth_users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    email_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_auth_users_status ON auth_users (status);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    ip TEXT,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS ix_auth_sessions_user ON auth_sessions (user_id);
CREATE INDEX IF NOT EXISTS ix_auth_sessions_live
    ON auth_sessions (expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS auth_email_challenges (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    user_id TEXT REFERENCES auth_users(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL DEFAULT 'login' CHECK (purpose IN ('login', 'invite', 'verify')),
    code_hash TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    attempts INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    requested_ip TEXT,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS ix_auth_email_challenges_email
    ON auth_email_challenges (email, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_auth_email_challenges_live
    ON auth_email_challenges (email, expires_at)
    WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS auth_webauthn_challenges (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL CHECK (purpose IN ('passkey_register', 'passkey_login')),
    challenge TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_auth_webauthn_challenges_live
    ON auth_webauthn_challenges (user_id, purpose, expires_at)
    WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS auth_passkeys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    credential_id TEXT NOT NULL UNIQUE,
    public_key TEXT NOT NULL,
    sign_count BIGINT NOT NULL DEFAULT 0,
    transports TEXT[] NOT NULL DEFAULT '{}',
    aaguid TEXT,
    nickname TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_auth_passkeys_user ON auth_passkeys (user_id);

CREATE TABLE IF NOT EXISTS auth_audit_events (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT REFERENCES auth_users(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    email TEXT,
    ip TEXT,
    user_agent TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_auth_audit_events_created
    ON auth_audit_events (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_auth_audit_events_user
    ON auth_audit_events (user_id, created_at DESC);
