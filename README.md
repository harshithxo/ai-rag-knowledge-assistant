# DocMind

DocMind is a local AI document assistant. Create an account, upload PDFs, and ask questions about your documents. Each account has its own private document library and chat history.

## Features

- Sign up, sign in and sign out
- Email verification and forgot-password flow
- Account profile and password updates
- Separate documents and conversations for each user
- Upload and preview PDF files
- Ask questions using RAG, with streaming answers and source references
- LangGraph workflow for document questions and calculator requests
- Local AI models through Ollama

## Tech stack

Python, FastAPI, SQLite for local development, PostgreSQL with pgvector in production, LangChain Core, LangChain Ollama, LangChain OpenAI-compatible integrations, LangGraph, Ollama, HTML, CSS and JavaScript.

## Run locally (Windows)

1. Install Python 3.11+, Node.js (for the frontend test), and [Ollama](https://ollama.com/). PostgreSQL is not needed for local development.
2. Open PowerShell in the project folder and run:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
ollama pull llama3.2
ollama pull nomic-embed-text
uvicorn app.main:app --reload
```

3. Open http://localhost:8000 and create an account.
4. In local development, email verification and password-reset links are printed in the terminal running the backend. Open the link shown there, then sign in.

Ollama must be running locally. If it is not already running, start it in another terminal with `ollama serve`.

## Email configuration

The default `EMAIL_PROVIDER=console` is for local development only. It prints verification and password-reset links in the backend terminal instead of sending real emails. To send emails, set `EMAIL_PROVIDER=smtp` or `EMAIL_PROVIDER=resend` in `.env` and restart the backend after changing settings.

### Gmail SMTP

For a personal Gmail account, use `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587` and `SMTP_STARTTLS=true`. Set `SMTP_USERNAME` to the Gmail address and `EMAIL_FROM` to that same address. Gmail SMTP generally requires 2-Step Verification and a Google App Password; the normal Google account password will not authenticate. Put the App Password in `SMTP_PASSWORD` without spaces, keep it private, and check the backend terminal for the SMTP error if delivery fails. Google Workspace administrators may need to enable SMTP access. A successful SMTP handoff can still be filtered into spam or rejected by recipient policy.

For registration, the account is created before email delivery is attempted. If delivery fails, inspect the backend log, correct SMTP settings, then use **Resend verification** for that address; the endpoint deliberately returns a generic response for privacy.

## Main settings

| Variable | Purpose |
|---|---|
| `OLLAMA_BASE_URL` | Ollama server address |
| `AI_PROVIDER` | `ollama` locally or `openai-compatible` for hosted deployments |
| `AI_BASE_URL` / `AI_API_KEY` | OpenAI-compatible chat endpoint and secret key |
| `LLM_MODEL` | Chat model name accepted by the selected provider |
| `EMBEDDING_PROVIDER` | `ollama` locally or `openai-compatible` for hosted deployments |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` | Optional separate embedding endpoint/key; falls back to the chat endpoint/key |
| `EMBEDDING_MODEL` | Embedding model name; output must be 768 dimensions for the included schema |
| `DATABASE_URL` | Leave empty for local SQLite; set to a PostgreSQL URL for PostgreSQL |
| `DATABASE_PATH` | SQLite file location in local development |
| `VECTOR_DIMENSIONS` | Embedding length (fixed at 768 for `nomic-embed-text` and the PG schema) |
| `DATABASE_POOL_MAX` | PostgreSQL pool upper bound (default 5) |
| `DATABASE_POOL_TIMEOUT` | Seconds to wait for a pooled connection (default 15) |
| `DATABASE_SSLMODE` | PostgreSQL TLS verification mode (default `verify-full`) |
| `DATABASE_SSLROOTCERT` | Optional provider CA certificate path for PostgreSQL TLS |
| `UPLOAD_DIR` | Uploaded PDF storage |
| `UPLOAD_STORAGE` | `local` for development, `s3` for production object storage |
| `SECRET_KEY` | Long random secret; required for production |
| `EMAIL_PROVIDER` | `console`, `resend` or `smtp` |

## Project structure

```text
app/
  agents/       LangGraph workflow and tools
  api/          FastAPI routes, including authentication
  rag/          Embeddings, pgvector storage and retrieval
  services/     Authentication, chat, documents and ingestion
  db.py         PostgreSQL schema and connection handling
frontend/       HTML, CSS and JavaScript interface
 tests/         Backend and frontend tests
```

## Tests

```powershell
pytest
node --test tests/frontend/markdown.test.mjs
```

The backend tests use temporary databases and mocked AI components. They do not need a live Ollama model or a real email provider.

## PostgreSQL setup and explicit schema migrations

The application uses SQLite automatically when `DATABASE_URL` is empty and `APP_ENV` is not `production`. In production, `DATABASE_URL` must be set and the application refuses to start if the PostgreSQL schema is not at the expected version. It never creates or upgrades a PostgreSQL schema during startup.

For Supabase, use the **Session Pooler** connection string copied from the Supabase Connect panel (PostgreSQL URL, usually port `5432`, with the pooler host and `postgres.<project-ref>` username). Keep the full credential-bearing URL private. The runtime pool is bounded (one minimum, five maximum by default), uses `prepare_threshold=None` for pooler compatibility, and verifies TLS certificates with `verify-full`. If your Supabase project requires its downloaded CA bundle, mount it securely and set `DATABASE_SSLROOTCERT` to that file path. Do not set SSL mode to `disable` in production.

**Important database isolation:** DocMind creates and uses a dedicated PostgreSQL schema named `docmind`, with only `docmind,extensions` on its connection search path for pgvector. It does not use or alter the existing `public.conversations`, `public.documents`, `public.document_chunks`, or `public.messages` tables. It does not create or modify anything in Supabase-managed `auth`, `storage`, `realtime`, or `vault` schemas. The migration runner refuses to adopt a non-empty `docmind` schema if its migration ledger is missing.

After taking an independent provider backup and reviewing the target database, an operator can apply the checked-in versioned schema migrations explicitly with:

```powershell
$env:DATABASE_URL = "<private Supabase Session Pooler URL>"
$env:APP_ENV = "production"
$env:VECTOR_DIMENSIONS = "768"
python -m scripts.migrate_postgres
```

This command is not run by application startup. It applies all pending numbered SQL files and their version records in one transaction under an advisory lock. Verify the schema and restore procedure against a disposable Supabase project before using it on a production database. `python -m scripts.migrate_postgres` is separate from the legacy SQLite/Chroma data-copy script.

## Notes

Local development uses SQLite, local PDF files, and Ollama. If `DATABASE_URL` is set locally, vectors and application records use PostgreSQL + pgvector. Production requires PostgreSQL with the `vector` extension, `UPLOAD_STORAGE=s3`, a private S3-compatible bucket, `EMAIL_PROVIDER=resend`, exact `CORS_ORIGINS`, `FRONTEND_URL`, and a strong `SECRET_KEY`. The S3 client uses path-style addressing and does not send AWS server-side-encryption headers, which improves compatibility with Supabase Storage. Do not use ephemeral local disk for uploaded PDFs. Prefer serving the frontend from the same origin as the API, or use custom frontend/API domains under the same parent domain for secure cookie behavior. The current AI runtime uses Ollama; a hosted deployment must provide a reachable Ollama-compatible endpoint or a separately implemented hosted model provider. Do not expose development console email mode publicly.

### Existing local data migration

For a data migration rehearsal, stop the local app and use copies of the SQLite, Chroma, and upload directories with a disposable PostgreSQL database and disposable PDF storage. Apply the PostgreSQL schema migrations first. Install the temporary Chroma reader (`pip install chromadb`), then run `python scripts/migrate_sqlite_chroma.py --dry-run` to preflight the source, existing destination rows, PDF hashes, message order, ownership, and every nonempty Chroma collection without writing destination data. A normal run requires `--source-quiesced`, creates a timestamped source backup, and reads SQLite, Chroma, and PDFs from that snapshot. PostgreSQL row writes are validated and committed in one transaction. PDFs use conditional create operations and are never overwritten; a failure after creating PDFs can leave unreferenced new objects, which the command reports. Preserve the source files and take a destination backup before any real migration. Legacy unowned rows remain unowned.

## Production deployment (Render + Supabase)

A single Render web service is the simplest supported deployment because FastAPI serves both the API and the static frontend from the same origin. This avoids cross-site session-cookie problems. The included `render.yaml` is a starting blueprint; it intentionally leaves all credentials and account-specific URLs as manual environment variables.

1. Create a private Supabase Storage bucket for PDFs and create S3-compatible Storage credentials. The S3 endpoint is `https://<project-ref>.supabase.co/storage/v1/s3`; use the region shown for the project. Do not use the database password as an S3 secret.
2. In Supabase, ensure the `vector` extension is enabled in the `extensions` schema.
3. In Render, create the service from this repository using `render.yaml`, then set the `sync: false` variables in the Render dashboard. Set `DATABASE_URL` to the Session Pooler URI, `FRONTEND_URL` to the deployed Render URL, and `CORS_ORIGINS` to that exact origin. Use a new random `SECRET_KEY` of at least 32 characters.
4. Configure Resend and an OpenAI-compatible chat/embedding provider. Set `AI_BASE_URL`, `AI_API_KEY`, and `LLM_MODEL`. The embedding endpoint must support the configured model and return 768-dimensional vectors when `dimensions=768` is requested; set `EMBEDDING_BASE_URL`/`EMBEDDING_API_KEY` if it is a separate service. If your provider does not support that parameter, use an embedding model that natively returns 768 dimensions and adjust both the schema migration and `VECTOR_DIMENSIONS` together before the first migration.
5. Download/retain the Supabase CA certificate and confirm `certs/prod-ca-2021.crt` is present in the repository. TLS verification remains enabled.
6. Take a provider backup before the first migration. Run the migration command once from a trusted local terminal using the production environment variables. It creates only the isolated `docmind` schema; it does not migrate, rename, or drop the existing `public` tables. The app will refuse to start until schema version 2 and the required pgvector dimension are present.
7. Deploy/restart the Render service. Verify registration, email delivery, PDF upload, indexing, retrieval, deletion, and logout with a test account and non-sensitive test PDF.

Do not commit `.env`, API keys, database URLs, Storage secrets, SMTP passwords, or generated user data. Render Free services sleep when idle and do not provide durable local file storage; DocMind therefore requires the configured S3-compatible storage in production.
