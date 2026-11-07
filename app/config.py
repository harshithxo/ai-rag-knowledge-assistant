
"""App settings. Values come from environment variables or the local .env file."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # AI providers
    # Local development defaults to Ollama. Production uses Gemini.
    ai_provider: str = "ollama"
    ai_base_url: str = ""
    ai_api_key: str = ""

    embedding_provider: str = "ollama"
    embedding_base_url: str = ""
    embedding_api_key: str = ""

    ollama_base_url: str = "http://localhost:11434"
    llm_model: str = "llama3.2"
    embedding_model: str = "nomic-embed-text"
    llm_temperature: float = 0.1
    llm_timeout_seconds: int = 180
    llm_num_ctx: int = 8192
    llm_keep_alive: str = "30m"

    # Storage
    database_url: str = ""
    vector_dimensions: int = 768
    database_pool_max: int = 5
    database_pool_timeout: float = 15
    database_sslmode: str = "verify-full"
    database_sslrootcert: str = ""
    upload_dir: str = "./data/uploads"
    database_path: str = "./data/app.db"
    upload_storage: str = "local"
    s3_bucket: str = ""
    s3_region: str = ""
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # Upload limits
    max_upload_size_mb: int = 20
    max_files_per_upload: int = 10

    # RAG tuning
    chunk_size: int = 1000
    chunk_overlap: int = 150
    retrieval_top_k: int = 5
    retrieval_min_score: float = 0.20
    max_history_messages: int = 10
    max_context_chars: int = 8000

    # Authentication
    app_env: str = "development"
    secret_key: str = ""
    frontend_url: str = "http://localhost:8000"
    session_expire_minutes: int = 10080

    # Email providers
    email_provider: str = "console"
    email_from: str = ""
    resend_api_key: str = ""
    brevo_api_key: str = ""

    # SMTP retained for local/other hosting compatibility.
    # Render Free cannot use outbound SMTP.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True

    email_verification_expire_minutes: int = 30
    password_reset_expire_minutes: int = 30

    # App
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:8000,http://127.0.0.1:8000"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]

    def validate_production(self) -> None:
        if self.app_env.lower() != "production":
            return

        # Database validation
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required in production")

        if not self.database_url.lower().startswith(
            ("postgresql://", "postgres://")
        ):
            raise RuntimeError("Production DATABASE_URL must use PostgreSQL")

        if self.database_sslmode.lower() != "verify-full":
            raise RuntimeError(
                "Production DATABASE_SSLMODE must be verify-full"
            )

        # Supabase Storage validation
        if self.upload_storage.lower() != "s3":
            raise RuntimeError(
                "Production requires UPLOAD_STORAGE=s3; "
                "hosted filesystems are not durable"
            )

        s3_required = {
            "S3_BUCKET": self.s3_bucket,
            "S3_REGION": self.s3_region,
            "S3_ENDPOINT_URL": self.s3_endpoint_url,
            "S3_ACCESS_KEY_ID": self.s3_access_key_id,
            "S3_SECRET_ACCESS_KEY": self.s3_secret_access_key,
        }

        missing_s3 = [
            key for key, value in s3_required.items() if not value
        ]

        if missing_s3:
            raise RuntimeError(
                "Missing required production S3 configuration: "
                + ", ".join(missing_s3)
            )

        # General production security
        if len(self.secret_key) < 32:
            raise RuntimeError(
                "SECRET_KEY must contain at least 32 characters in production"
            )

        if not self.frontend_url.startswith("https://"):
            raise RuntimeError(
                "FRONTEND_URL must be an HTTPS production URL"
            )

        if not self.cors_origin_list or "*" in self.cors_origin_list:
            raise RuntimeError(
                "CORS_ORIGINS must contain explicit origins and cannot use '*'"
            )

        if any(
            "localhost" in origin or "127.0.0.1" in origin
            for origin in self.cors_origin_list
        ):
            raise RuntimeError(
                "CORS_ORIGINS must not contain localhost origins in production"
            )

        required = {
            "SECRET_KEY": self.secret_key,
            "EMAIL_FROM": self.email_from,
            "FRONTEND_URL": self.frontend_url,
        }

        missing = [
            key for key, value in required.items() if not value
        ]

        if missing:
            raise RuntimeError(
                "Missing required production configuration: "
                + ", ".join(missing)
            )

        # Gemini chat validation
        ai_provider = self.ai_provider.lower()

        if ai_provider == "gemini":
            if not self.ai_api_key:
                raise RuntimeError(
                    "AI_API_KEY is required for Gemini chat"
                )
        elif ai_provider == "openai-compatible":
            if not self.ai_base_url or not self.ai_api_key:
                raise RuntimeError(
                    "AI_BASE_URL and AI_API_KEY are required"
                )
        else:
            raise RuntimeError(
                "Production AI_PROVIDER must be gemini or openai-compatible"
            )

        # Embedding provider validation
        embedding_provider = self.embedding_provider.lower()

        if embedding_provider == "gemini":
            if not self.embedding_api_key:
                raise RuntimeError(
                    "EMBEDDING_API_KEY is required for Gemini embeddings"
                )
        elif embedding_provider == "openai-compatible":
            if not (
                self.embedding_base_url or self.ai_base_url
            ):
                raise RuntimeError(
                    "EMBEDDING_BASE_URL or AI_BASE_URL is required"
                )

            if not (
                self.embedding_api_key or self.ai_api_key
            ):
                raise RuntimeError(
                    "EMBEDDING_API_KEY or AI_API_KEY is required"
                )
        else:
            raise RuntimeError(
                "Production EMBEDDING_PROVIDER must be gemini "
                "or openai-compatible"
            )

        # Email provider validation
        email_provider = self.email_provider.lower()

        if email_provider == "brevo":
            if not self.brevo_api_key:
                raise RuntimeError(
                    "BREVO_API_KEY is required when EMAIL_PROVIDER=brevo"
                )

        elif email_provider == "resend":
            if not self.resend_api_key:
                raise RuntimeError(
                    "RESEND_API_KEY is required when EMAIL_PROVIDER=resend"
                )

        elif email_provider == "smtp":
            raise RuntimeError(
                "SMTP is not supported on Render Free; "
                "use an HTTPS email API"
            )

        elif email_provider == "console":
            raise RuntimeError(
                "Console email provider is forbidden in production"
            )

        else:
            raise RuntimeError(
                "EMAIL_PROVIDER must be brevo, resend or console"
            )

    def ensure_directories(self) -> None:
        Path(self.upload_dir).mkdir(parents=True, exist_ok=True)
        Path(self.database_path).parent.mkdir(
            parents=True,
            exist_ok=True,
        )


settings = Settings()
settings.ensure_directories()
