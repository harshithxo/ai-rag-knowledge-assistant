# DocMind

**DocMind** is a document question-answering web application built with
Retrieval-Augmented Generation (RAG). Users can create an account,
upload documents, and ask questions about their content. DocMind
retrieves relevant passages and uses an AI model to generate answers
grounded in the uploaded material.

-   **Live demo:** https://docmind-faze.onrender.com
-   **GitHub:** https://github.com/harshithxo/ai-rag-knowledge-assistant

------------------------------------------------------------------------

## Features

-   User registration and login
-   Email verification and password reset
-   User-specific documents and conversations
-   PDF and TXT document upload
-   Text extraction and chunking
-   Vector-based retrieval of relevant passages
-   Gemini-powered question answering
-   Streaming chat responses
-   Source-aware answers based on retrieved document content
-   Configurable retrieval and answer-generation settings

## Production technology stack

  -------------------------------------------------------------------------
  Layer                   Technology / Service      Purpose
  ----------------------- ------------------------- -----------------------
  Hosting                 Render Web Service (Free) Runs FastAPI and serves
                                                    the frontend

  Backend                 Python, FastAPI, Uvicorn  API, authentication,
                                                    ingestion and chat

  Frontend                HTML, CSS, JavaScript     Browser interface

  Chat model              Google Gemini             Generates answers
                          `gemini-3.5-flash-lite`   

  Embeddings              Google Gemini             Creates document and
                          `gemini-embedding-001`    query vectors

  RAG orchestration       LangChain, LangGraph      Coordinates AI and
                                                    retrieval workflows

  Database                Supabase PostgreSQL       Stores application and
                                                    user data

  Vector search           PostgreSQL with pgvector  Stores and searches
                                                    embeddings

  File storage            Supabase Storage          Stores uploaded
                          (S3-compatible API)       documents

  Transactional email     Brevo HTTPS API           Sends verification and
                                                    password-reset emails

  Deployment              GitHub + Render           Deploys updates from
                          Auto-Deploy               `main`
  -------------------------------------------------------------------------

The frontend and backend are served from the **same Render service**.
The browser uses same-origin API paths and cookies; a separate frontend
hosting service is not required.

## How RAG works

1.  A user uploads a document through the web interface.
2.  FastAPI validates the upload and extracts its text.
3.  DocMind splits the text into smaller chunks.
4.  Gemini generates an embedding for each chunk.
5.  The document data and vectors are stored in Supabase PostgreSQL. The
    uploaded file is stored in Supabase Storage.
6.  When the user asks a question, DocMind searches for relevant chunks
    using vector similarity.
7.  The retrieved context is sent to Gemini to generate an answer.
8.  The response is returned to the browser.

## Current production settings

  Setting                            Deployed value
  ----------------------- -------------------------
  AI provider                                Gemini
  Chat model                `gemini-3.5-flash-lite`
  Embedding provider                         Gemini
  Embedding model            `gemini-embedding-001`
  Vector dimensions                           `768`
  Passages per question                         `5`
  Relevance threshold                        `0.20`
  Temperature                                 `0.1`
  Chunk size                      `1000` characters
  Chunk overlap                    `150` characters
  Maximum upload size                       `20 MB`
  Database pool maximum                         `5`

These values describe the current deployment. Settings that are
configurable in the application can be adjusted there.

------------------------------------------------------------------------

## Run locally

The local development configuration is separate from production. By
default, local development uses **SQLite, local file storage and
Ollama**. It does not require the production Supabase, Gemini or Brevo
credentials.

### Requirements

-   Python 3.11 or newer
-   [Ollama](https://ollama.com/)
-   Git
-   Node.js only if you want to run frontend JavaScript tests

### 1. Clone the repository

``` bash
git clone https://github.com/harshithxo/ai-rag-knowledge-assistant.git
cd ai-rag-knowledge-assistant
```

### 2. Create a virtual environment

**Windows PowerShell**

``` powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux**

``` bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

``` bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Create the local environment file

**Windows PowerShell**

``` powershell
Copy-Item .env.example .env
```

**macOS / Linux**

``` bash
cp .env.example .env
```

The supplied `.env.example` contains local-development defaults. Review
it before starting the application. Keep real credentials in `.env` and
never commit that file.

### 5. Download the local Ollama models

``` bash
ollama pull llama3.2
ollama pull nomic-embed-text
```

Make sure Ollama is running. If it is not already running, start it in a
separate terminal:

``` bash
ollama serve
```

Local defaults:

-   Chat model: `llama3.2`
-   Embedding model: `nomic-embed-text`
-   Ollama URL: `http://localhost:11434`

### 6. Start the application

``` bash
uvicorn app.main:app --reload
```

Open:

-   Web app: http://localhost:8000
-   API documentation: http://localhost:8000/docs
-   Health endpoint: http://localhost:8000/health

With the local console email provider, verification and password-reset
links are printed in the backend terminal instead of being sent as real
emails.

------------------------------------------------------------------------

## Production environment variables

The hosted Render service is configured through Render's Environment
settings. The table below lists the variable names and non-secret values
used by the deployment. **Never add real credentials or secret values to
this README.**

  Variable                 Production configuration
  ------------------------ -------------------------------------------------
  `APP_ENV`                `production`
  `PYTHON_VERSION`         `3.12.10`
  `DATABASE_URL`           Supabase PostgreSQL connection string
  `DATABASE_SSLMODE`       `verify-full`
  `DATABASE_SSLROOTCERT`   `certs/prod-ca-2021.crt`
  `DATABASE_POOL_MAX`      `5`
  `VECTOR_DIMENSIONS`      `768`
  `UPLOAD_STORAGE`         `s3`
  `S3_BUCKET`              `documents`
  `S3_REGION`              `eu-north-1`
  `S3_ENDPOINT_URL`        Supabase S3-compatible endpoint
  `S3_ACCESS_KEY_ID`       Supabase Storage S3 access key
  `S3_SECRET_ACCESS_KEY`   Supabase Storage S3 secret key
  `AI_PROVIDER`            `gemini`
  `LLM_MODEL`              `gemini-3.5-flash-lite`
  `AI_API_KEY`             Gemini chat API key
  `EMBEDDING_PROVIDER`     `gemini`
  `EMBEDDING_MODEL`        `gemini-embedding-001`
  `EMBEDDING_API_KEY`      Gemini embedding API key
  `SECRET_KEY`             Render-generated application secret
  `FRONTEND_URL`           `https://docmind-faze.onrender.com`
  `CORS_ORIGINS`           `https://docmind-faze.onrender.com`
  `EMAIL_PROVIDER`         `brevo`
  `EMAIL_FROM`             Sender address configured and verified in Brevo
  `BREVO_API_KEY`          Brevo API key

The application uses a dedicated `docmind` PostgreSQL schema and the
`extensions` schema for the vector extension. Keep the database
connection string and all service credentials private.

### Email delivery

Production transactional email is sent through the Brevo HTTPS API. The
sender must be configured and verified in Brevo. Sender display and
delivery behavior are subject to Brevo account and sender-domain rules.

## Deployment

The repository contains `render.yaml`, which defines the Render web
service, Python build command, Uvicorn start command, health-check path
and environment-variable configuration.

The service is configured for automatic deployment from the `main`
branch:

1.  Commit and push changes to `main`.
2.  Render detects the GitHub update.
3.  Render installs dependencies from `requirements.txt`.
4.  Render starts the FastAPI application.
5.  Render checks the `/health` endpoint.

**Live application:** https://docmind-faze.onrender.com

## Project structure

``` text
ai-rag-knowledge-assistant/
├── app/
│   ├── agents/       # AI workflow and agent logic
│   ├── api/          # FastAPI route handlers
│   ├── models/       # Data and API schemas
│   ├── prompts/      # AI prompts
│   ├── rag/          # Embeddings, retrieval and vector search
│   ├── services/     # Authentication, chat, documents and ingestion
│   ├── config.py     # Application configuration
│   ├── db.py         # Database connection and setup
│   └── main.py       # FastAPI application entry point
├── certs/            # PostgreSQL CA certificate
├── frontend/         # HTML, CSS and JavaScript
├── migrations/       # Versioned PostgreSQL migrations
├── scripts/          # Database migration utilities
├── tests/            # Automated tests
├── .env.example      # Local example environment variables
├── render.yaml       # Render deployment configuration
├── requirements.txt  # Python dependencies
└── README.md
```

## Testing

Run the backend tests from the project root:

``` bash
pytest
```

If the frontend test file is present and Node.js is installed, run:

``` bash
node --test tests/frontend/markdown.test.mjs
```

Some database integration tests may require a separately configured test
database. Check the test output for prerequisites and skipped tests.

## Free-tier notes

The application currently uses free-tier hosting and service plans.
Limits and policies can change.

-   **Render Free:** The web service can spin down after inactivity. The
    first request after inactivity may be delayed while the service
    starts.
-   **Supabase Free:** Database and storage are subject to plan quotas,
    usage limits and inactivity policies.
-   **Gemini API:** Requests are subject to model availability, rate
    limits and API quotas.
-   **Brevo:** Email sending is subject to account verification, sending
    limits and anti-abuse policies.

This setup is suitable for a portfolio demo and testing. It should not
be treated as an unlimited or guaranteed-uptime production service.

## Security notes

-   Never commit `.env`, API keys, database passwords, storage secrets
    or private certificates.
-   Store production credentials in Render's environment settings.
-   Use HTTPS for the deployed application.
-   Keep uploaded documents private and enforce user-level authorization
    when accessing documents and conversations.
-   Rotate any credential that is accidentally exposed.

## License

No license has been specified in this repository. Unless a license is
added, the project should be treated as all rights reserved.
