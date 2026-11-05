"""Explicit, versioned PostgreSQL schema migrations. Never run during application startup."""
import hashlib
from pathlib import Path

from app.config import settings


def migrations_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "migrations" / "postgres"


def apply_migrations(connect_factory=None) -> list[int]:
    """Apply each unapplied SQL migration transactionally; intended for operator invocation."""
    if not settings.database_url:
        raise RuntimeError("Set DATABASE_URL to the target PostgreSQL database before running migrations")
    if int(settings.vector_dimensions) != 768:
        raise RuntimeError("This schema is fixed to vector(768); set VECTOR_DIMENSIONS=768")
    if settings.database_sslmode.lower() != "verify-full":
        raise RuntimeError("PostgreSQL schema migrations require DATABASE_SSLMODE=verify-full")
    if connect_factory is None:
        import psycopg
        from psycopg.rows import dict_row
        kwargs = {"row_factory": dict_row, "prepare_threshold": None,
                  "sslmode": settings.database_sslmode,
                  "options": "-c search_path=docmind,extensions"}
        if settings.database_sslrootcert:
            kwargs["sslrootcert"] = settings.database_sslrootcert
        connect_factory = lambda: psycopg.connect(settings.database_url, **kwargs)

    applied = []
    with connect_factory() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7083412901)")
        # Isolate DocMind from the pre-existing public tables and all Supabase-managed schemas.
        # Refuse to adopt a manually-created/partial docmind schema without its migration ledger.
        schema_exists = conn.execute("SELECT to_regnamespace('docmind') AS schema").fetchone()["schema"]
        ledger_exists = conn.execute("SELECT to_regclass('docmind.schema_migrations') AS ledger").fetchone()["ledger"]
        if schema_exists and not ledger_exists:
            existing = conn.execute("""SELECT tablename FROM pg_tables
                WHERE schemaname='docmind' ORDER BY tablename""").fetchall()
            if existing:
                names = ", ".join(row["tablename"] for row in existing)
                raise RuntimeError("Found existing objects in schema docmind without a migration ledger; refusing to adopt or modify them: " + names)
        conn.execute("CREATE SCHEMA IF NOT EXISTS docmind")
        conn.execute("SET search_path TO docmind, extensions")
        conn.execute("""CREATE TABLE IF NOT EXISTS docmind.schema_migrations (
            version INTEGER PRIMARY KEY, filename TEXT NOT NULL, checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        ledger_columns = {r["column_name"] for r in conn.execute("""SELECT column_name
            FROM information_schema.columns WHERE table_schema='docmind' AND table_name='schema_migrations'""").fetchall()}
        if not {"version", "filename", "checksum", "applied_at"}.issubset(ledger_columns):
            raise RuntimeError("DocMind migration ledger has an incompatible structure; refusing to continue")

        paths = sorted(migrations_dir().glob("[0-9][0-9][0-9]_*.sql"))
        by_version = {int(path.name.split("_", 1)[0]): path for path in paths}
        records = conn.execute("SELECT version, filename, checksum FROM docmind.schema_migrations ORDER BY version").fetchall()
        done = set()
        for record in records:
            version = record["version"]
            path = by_version.get(version)
            if path is None or path.name != record["filename"]:
                raise RuntimeError(f"Unknown or renamed migration ledger entry for version {version}; refusing to continue")
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            if checksum != record["checksum"]:
                raise RuntimeError(f"Migration {path.name} changed after it was applied; refusing to continue")
            done.add(version)
        if done and done != set(range(1, max(done) + 1)):
            raise RuntimeError("DocMind migration ledger has a version gap; refusing to continue")

        for path in paths:
            version = int(path.name.split("_", 1)[0])
            if version in done:
                continue
            if version != len(done) + 1:
                raise RuntimeError(f"Migration sequence gap before {path.name}; refusing to continue")
            sql = path.read_text(encoding="utf-8-sig")
            # No values are interpolated into migration SQL; disable prepare for multi-statement files.
            conn.execute(sql, prepare=False)
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            conn.execute("INSERT INTO docmind.schema_migrations(version, filename, checksum) VALUES (%s, %s, %s)",
                         (version, path.name, checksum))
            done.add(version)
            applied.append(version)
    return applied


if __name__ == "__main__":
    versions = apply_migrations()
    print("Applied PostgreSQL schema versions:", ", ".join(map(str, versions)) if versions else "none; already current")
