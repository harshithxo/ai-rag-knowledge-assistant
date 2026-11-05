"""Small wrapper around ChatOllama: streaming, a reused client and readable errors."""
import logging
import time
from functools import lru_cache
from typing import Any, Iterator, Sequence

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_ollama import ChatOllama

from app.config import settings
from app.errors import LLMUnavailable
from app.services import settings_service

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _build_llm(temperature: float, max_tokens: int | None) -> Any:
    """Build and reuse either a local Ollama or OpenAI-compatible chat client."""
    if settings.ai_provider.lower() == "openai-compatible":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=settings.llm_model,
            base_url=settings.ai_base_url,
            api_key=settings.ai_api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=settings.llm_timeout_seconds,
        )
    return ChatOllama(
        model=settings.llm_model,
        base_url=settings.ollama_base_url,
        num_ctx=settings.llm_num_ctx,
        temperature=temperature,
        num_predict=max_tokens,
        keep_alive=settings.llm_keep_alive,
        client_kwargs={"timeout": settings.llm_timeout_seconds},
    )


def get_llm(temperature: float | None = None, max_tokens: int | None = None) -> Any:
    if temperature is None:
        temperature = settings_service.get_float("llm_temperature")
    return _build_llm(float(temperature), max_tokens)


def translate_error(exc: Exception) -> LLMUnavailable:
    text = str(exc).lower()
    if settings.ai_provider.lower() == "ollama":
        if "not found" in text and "model" in text:
            return LLMUnavailable(
                f"The model '{settings.llm_model}' is not installed. Run: ollama pull {settings.llm_model}")
        if any(word in text for word in ("connect", "refused", "unreachable", "timed out", "timeout")):
            return LLMUnavailable(
                f"Cannot reach Ollama at {settings.ollama_base_url}. Start it with 'ollama serve' and try again.")
    elif any(word in text for word in ("connect", "refused", "unreachable", "timed out", "timeout")):
        return LLMUnavailable("Cannot reach the configured AI API. Check AI_BASE_URL and provider availability.")
    return LLMUnavailable(f"The AI model request failed: {exc}")


def generate(messages: Sequence[BaseMessage], temperature: float | None = None,
             max_tokens: int | None = None) -> str:
    """Call the LLM and return the full reply as text. Used for short helper calls."""
    try:
        return str(get_llm(temperature, max_tokens).invoke(list(messages)).content).strip()
    except Exception as exc:  # noqa: BLE001 - ollama/httpx raise several unrelated types
        logger.warning("LLM call failed: %s", exc)
        raise translate_error(exc) from exc


def stream(messages: Sequence[BaseMessage], temperature: float | None = None,
           stats: dict | None = None) -> Iterator[str]:
    """Yield the reply piece by piece while Ollama writes it.

    If a `stats` dict is passed, it gets filled with 'tokens' and 'tokens_per_second'. Ollama sends
    exact numbers in its last chunk. If they are missing, we count chunks (about one token each)
    against the elapsed time instead."""
    stats = stats if stats is not None else {}
    started = time.perf_counter()
    chunks = 0
    try:
        for chunk in get_llm(temperature).stream(list(messages)):
            meta = getattr(chunk, "response_metadata", None) or {}
            if meta.get("eval_count"):
                stats["tokens"] = int(meta["eval_count"])
                if meta.get("eval_duration"):
                    stats["tokens_per_second"] = round(meta["eval_count"] / (meta["eval_duration"] / 1e9), 1)
            text = chunk.content if isinstance(chunk.content, str) else str(chunk.content or "")
            if text:
                chunks += 1
                yield text
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM streaming failed: %s", exc)
        raise translate_error(exc) from exc
    if "tokens" not in stats:
        stats["tokens"] = chunks
        elapsed = time.perf_counter() - started
        stats["tokens_per_second"] = round(chunks / elapsed, 1) if elapsed > 0 and chunks else None


def warm_up() -> None:
    """Load the chat model into memory at startup so the first real question isn't slow.
    Errors are ignored, since Ollama might just not be running yet."""
    try:
        get_llm(0.0, 1).invoke([HumanMessage(content="Hi")])
        logger.info("Chat model '%s' is loaded and ready", settings.llm_model)
    except Exception as exc:  # noqa: BLE001
        logger.info("Model warm-up skipped (%s)", exc)
