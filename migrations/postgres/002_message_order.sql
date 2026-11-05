-- Keep every unqualified object reference inside DocMind's isolated schema.
SET search_path TO docmind, extensions;

ALTER TABLE documents ADD COLUMN IF NOT EXISTS user_id TEXT REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS user_id TEXT REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS document_id TEXT REFERENCES documents(id) ON DELETE SET NULL;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS attachments TEXT;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS message_seq BIGINT NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS message_order BIGINT NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS document_chunks (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 user_id TEXT REFERENCES users(id) ON DELETE CASCADE, content TEXT NOT NULL,
 metadata JSONB NOT NULL, embedding vector(768) NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_id_user_unique ON documents(id, user_id);
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='documents_user_id_fkey' AND conrelid='documents'::regclass) THEN
  ALTER TABLE documents ADD CONSTRAINT documents_user_id_fkey FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE;
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='conversations_user_id_fkey' AND conrelid='conversations'::regclass) THEN
  ALTER TABLE conversations ADD CONSTRAINT conversations_user_id_fkey FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE;
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='conversations_document_id_fkey' AND conrelid='conversations'::regclass) THEN
  ALTER TABLE conversations ADD CONSTRAINT conversations_document_id_fkey FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE SET NULL;
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chunks_document_owner_fkey' AND conrelid='document_chunks'::regclass) THEN
  ALTER TABLE document_chunks ADD CONSTRAINT chunks_document_owner_fkey
   FOREIGN KEY(document_id,user_id) REFERENCES documents(id,user_id) ON DELETE CASCADE;
 END IF;
END $$;
WITH ordered AS (
 SELECT id, row_number() OVER (PARTITION BY conversation_id ORDER BY created_at, id) AS seq
 FROM messages
)
UPDATE messages AS m SET message_order=o.seq FROM ordered AS o
WHERE m.id=o.id AND m.message_order=0;
UPDATE conversations AS c SET message_seq=COALESCE(x.seq,0)
FROM (SELECT conversation_id, MAX(message_order) AS seq FROM messages GROUP BY conversation_id) AS x
WHERE c.id=x.conversation_id AND c.message_seq < x.seq;
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_conversation_order_unique ON messages(conversation_id, message_order);
CREATE INDEX IF NOT EXISTS idx_messages_conv_order ON messages(conversation_id, message_order);
CREATE INDEX IF NOT EXISTS idx_chunks_owner_document ON document_chunks(user_id, document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw ON document_chunks USING hnsw (embedding vector_cosine_ops);
DO $$ DECLARE embedding_type TEXT; BEGIN
 SELECT format_type(a.atttypid,a.atttypmod) INTO embedding_type
 FROM pg_attribute a WHERE a.attrelid='document_chunks'::regclass AND a.attname='embedding' AND NOT a.attisdropped;
 IF embedding_type IS DISTINCT FROM 'vector(768)' THEN
  RAISE EXCEPTION 'document_chunks.embedding must be vector(768), found %', embedding_type;
 END IF;
END $$;
