# DocMind

DocMind is a document question-answering app. You can create an account, upload PDF files, and ask questions about the documents. It uses retrieval-augmented generation (RAG) to find relevant parts of your documents before sending the question to an AI model.

## Features

- Create an account, sign in, and manage your profile.
- Verify an email address and reset a forgotten password.
- Upload and preview PDF documents.
- Keep documents and chat conversations separate for each account.
- Ask questions about uploaded documents and receive streamed answers.
- View source references used to prepare an answer.
- Use a LangGraph workflow for document questions and basic calculations.
- Run AI models locally with Ollama.

## Technologies

- **Backend:** Python, FastAPI
- **AI and RAG:** LangChain, LangGraph, Ollama
- **Embeddings and search:** Ollama embeddings; SQLite for local development and PostgreSQL with pgvector when configured
- **Frontend:** HTML, CSS, JavaScript
- **PDF processing:** pypdf
- **Testing:** pytest and Node.js built-in test runner

## How it works

1. The user uploads a PDF.
2. DocMind extracts the text and splits it into smaller sections.
3. The app creates an embedding for each section and stores it for search.
4. When the user asks a question, DocMind searches for relevant sections.
5. The retrieved text and question are sent to the configured chat model.
6. The answer is streamed back to the browser with source information.

The local setup uses SQLite, local PDF storage, and Ollama. PostgreSQL/pgvector and S3-compatible storage are optional configuration paths for a hosted setup.

## Project structure

```text
ai-rag-knowledge-assistant/
├── app/
│   ├── agents/       # LangGraph workflow and calculation tool
│   ├── api/          # FastAPI endpoints
│   ├── models/       # Request and response schemas
│   ├── prompts/      # Prompts used by the AI workflow
│   ├── rag/          # Embeddings, retrieval, and vector search
│   ├── services/     # Authentication, chat, documents, and ingestion
│   ├── config.py     # Application settings
│   ├── db.py         # Database connections and setup
│   └── main.py       # FastAPI application entry point
├── certs/            # CA certificate used for PostgreSQL TLS
├── frontend/         # HTML, CSS, and JavaScript interface
├── migrations/       # Versioned PostgreSQL schema migrations
├── scripts/          # Database migration utilities
├── tests/            # Backend and frontend tests
├── .env.example      # Example environment configuration
├── requirements.txt  # Application dependencies
└── run.ps1           # Windows startup helper
```

## Requirements

For local development, install:

- Python 3.11 or newer
- [Ollama](https://ollama.com/)
- Git (optional, for cloning and version control)
- Node.js only if you want to run the frontend JavaScript test

PostgreSQL is not required for the default local setup.

## Run locally on Windows

Open PowerShell in the project folder and run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Download the two default Ollama models:

```powershell
ollama pull llama3.2
ollama pull nomic-embed-text
```

Make sure Ollama is running. If it is not already running, open another terminal and start it:

```powershell
ollama serve
```

Start the application:

```powershell
uvicorn app.main:app --reload
```

Open **http://localhost:8000** in your browser. The API documentation is available at **http://localhost:8000/docs**.

On first start, the app creates its local database and required data directories. For local development, verification and password-reset links are printed in the backend terminal because the default email provider is `console`.

## Environment settings

The `.env.example` file contains the available settings. Copy it to `.env` and keep `.env` private.

Common settings:

| Variable | Purpose |
|---|---|
| `AI_PROVIDER` | Chat model provider. Defaults to `ollama`. |
| `OLLAMA_BASE_URL` | Address of the local Ollama service. |
| `LLM_MODEL` | Chat model name. Defaults to `llama3.2`. |
| `EMBEDDING_MODEL` | Embedding model. Defaults to `nomic-embed-text`. |
| `DATABASE_URL` | Leave empty to use local SQLite. |
| `DATABASE_PATH` | Location of the local SQLite database. |
| `UPLOAD_DIR` | Folder used for uploaded PDFs. |
| `SECRET_KEY` | Secret used by the application. Set a strong private value for deployment. |
| `EMAIL_PROVIDER` | Defaults to `console` for local development. |

Other options for hosted AI providers, PostgreSQL, S3-compatible file storage, and email delivery are documented as comments in `.env.example`. Those services need their own accounts and credentials. Do not commit real credentials to GitHub.

## Tests

Install the development dependencies:

```powershell
pip install -r requirements-dev.txt
```

Run the backend tests:

```powershell
pytest
```

Run the frontend Markdown test (requires Node.js):

```powershell
node --test tests/frontend/markdown.test.mjs
```

Some PostgreSQL integration or migration checks may require a test database or additional environment setup. Check the test output for skipped tests and prerequisites.

## Current limitations

- The default local setup depends on Ollama being installed and running.
- The default email provider prints account verification and password-reset links in the terminal. It does not send real emails.
- A hosted setup needs separately configured database, AI, email, and file-storage services.
- A successful local test run does not by itself verify a production deployment.

## Future improvements

- Add more automated tests for complete user workflows.
- Improve deployment documentation with environment-specific examples.
- Add additional document formats if they are needed.
