"""Health check and settings endpoints."""
import logging

import httpx
from fastapi import APIRouter, Depends

from app.config import settings
from app.models.schemas import SettingsUpdate
from app.services import settings_service
from app.api.auth import get_user, csrf_guard

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])


def _ollama_status() -> dict:
    """Ask Ollama which models are installed. The short timeout keeps /health quick."""
    try:
        data = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=2.0).json()
        installed = {m.get("name", "") for m in data.get("models", [])}
        names = installed | {n.split(":")[0] for n in installed}
        return {
            "reachable": True,
            "llm_installed": settings.llm_model in names or f"{settings.llm_model}:latest" in names,
            "embedding_installed": settings.embedding_model in names
            or f"{settings.embedding_model}:latest" in names,
        }
    except Exception:  # noqa: BLE001 - any failure just means Ollama can't be reached
        return {"reachable": False, "llm_installed": False, "embedding_installed": False}


@router.get("/health")
def health():
    if settings.ai_provider.lower() == "ollama" and settings.embedding_provider.lower() == "ollama":
        status = _ollama_status()
    else:
        # Configuration presence is not a live provider health check; the first request verifies the endpoint.
        status = {
            "reachable": bool(settings.ai_base_url and settings.ai_api_key),
            "llm_installed": bool(settings.llm_model),
            "embedding_installed": bool(settings.embedding_model and (settings.embedding_base_url or settings.ai_base_url)
                                         and (settings.embedding_api_key or settings.ai_api_key)),
            "configured_only": True,
        }
    return {"status": "ok", "ollama": status, "ai": status,
            "ai_provider": settings.ai_provider, "embedding_provider": settings.embedding_provider,
            "llm_model": settings.llm_model, "embedding_model": settings.embedding_model}


@router.get("/settings")
def get_settings(user=Depends(get_user)):
    """Editable values (saved in the database) plus the read-only ones from .env.
    Nothing secret is returned."""
    return {
        **settings_service.get_all(),
        "llm_model": settings.llm_model,
        "embedding_model": settings.embedding_model,
        "ollama_base_url": settings.ollama_base_url,
        "ai_provider": settings.ai_provider,
        "ai_base_url": settings.ai_base_url,
        "embedding_provider": settings.embedding_provider,
        "embedding_base_url": settings.embedding_base_url or settings.ai_base_url,
    }


@router.patch("/settings")
def update_settings(body: SettingsUpdate, user=Depends(csrf_guard)):
    return settings_service.update(body.model_dump(exclude_none=True))
