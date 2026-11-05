-- DocMind owns an isolated schema. Existing public tables and Supabase-managed schemas are untouched.
CREATE SCHEMA IF NOT EXISTS docmind;
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
SET search_path TO docmind, extensions;

CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, full_name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
 password_hash TEXT NOT NULL, is_verified SMALLINT NOT NULL DEFAULT 0,
 is_active SMALLINT NOT NULL DEFAULT 1, role TEXT NOT NULL DEFAULT 'user',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, filename TEXT NOT NULL, stored_name TEXT NOT NULL,
 size_bytes BIGINT NOT NULL, content_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('processing','ready','failed')),
 error TEXT, pages INTEGER DEFAULT 0, chunks INTEGER DEFAULT 0,
 created_at TEXT NOT NULL, user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
 UNIQUE(id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, created_at);
CREATE TABLE IF NOT EXISTS conversations (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, document_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 user_id TEXT REFERENCES users(id) ON DELETE CASCADE, message_seq BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
 content TEXT NOT NULL, sources TEXT, follow_ups TEXT, grounded SMALLINT,
 attachments TEXT, created_at TEXT NOT NULL, message_order BIGINT NOT NULL DEFAULT 0,
 UNIQUE(conversation_id, message_order)
);
CREATE INDEX IF NOT EXISTS idx_messages_conv_order ON messages(conversation_id, message_order);
CREATE TABLE IF NOT EXISTS response_metrics (
 message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
 retrieval_ms DOUBLE PRECISION, generation_ms DOUBLE PRECISION, total_ms DOUBLE PRECISION,
 llm_calls INTEGER, passages INTEGER, tokens INTEGER, tokens_per_second DOUBLE PRECISION,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS email_verification_tokens (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_reset_tokens (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 session_token_hash TEXT NOT NULL UNIQUE, csrf_token_hash TEXT NOT NULL,
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS auth_rate_limits (
 scope TEXT NOT NULL, subject_hash TEXT NOT NULL, window_started TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope, subject_hash)
);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS document_chunks (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 user_id TEXT REFERENCES users(id) ON DELETE CASCADE, content TEXT NOT NULL,
 page INTEGER, metadata JSONB NOT NULL, embedding vector(768) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_owner_document ON document_chunks(user_id, document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw ON document_chunks USING hnsw (embedding vector_cosine_ops);
