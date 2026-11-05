"""Settings that can be changed while the app runs. They are saved in SQLite and fall back to the .env defaults.

Editable from the Settings page: retrieval, temperature, chunking and the upload limit.
Only changeable in .env: the Ollama URL and model names."""
from app import db
from app.config import settings
from app.errors import ValidationFailed

# key -> (type, min, max)
TUNABLE = {
    "retrieval_top_k": (int, 1, 20),
    "retrieval_min_score": (float, 0.0, 1.0),
    "llm_temperature": (float, 0.0, 1.0),
    "chunk_size": (int, 200, 4000),
    "chunk_overlap": (int, 0, 1000),
    "max_upload_size_mb": (int, 1, 100),
}


def _default(key: str):
    return getattr(settings, key)


def _get_raw(key: str):
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def get_int(key: str) -> int:
    raw = _get_raw(key)
    return int(raw) if raw is not None else int(_default(key))


def get_float(key: str) -> float:
    raw = _get_raw(key)
    return float(raw) if raw is not None else float(_default(key))


def get_max_upload_bytes() -> int:
    return get_int("max_upload_size_mb") * 1024 * 1024


def get_all() -> dict:
    return {key: (get_int(key) if kind is int else get_float(key)) for key, (kind, _, _) in TUNABLE.items()}


def update(values: dict) -> dict:
    """Check every value first and only then save, so one bad value can't leave things half-updated."""
    clean = {}
    for key, value in values.items():
        if key not in TUNABLE:
            raise ValidationFailed(f"Unknown setting: {key}")
        kind, low, high = TUNABLE[key]
        try:
            value = kind(value)
        except (TypeError, ValueError):
            raise ValidationFailed(f"{key} must be a {kind.__name__}") from None
        if not low <= value <= high:
            raise ValidationFailed(f"{key} must be between {low} and {high}")
        clean[key] = value

    merged = {**get_all(), **clean}
    if merged["chunk_overlap"] >= merged["chunk_size"]:
        raise ValidationFailed("Chunk overlap must be smaller than the chunk size")

    with db.connect() as conn:
        for key, value in clean.items():
            conn.execute("INSERT INTO app_settings(key, value) VALUES(?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
    return get_all()
