"""Runs the LangGraph agent and saves the conversation around it.

chat_events() does the real work and yields events as the answer is produced:
    {"type": "status", "text": "Searching your documents…"}
    {"type": "token",  "text": "Hello"}                       (many of these)
    {"type": "done",   "conversation_id": ..., "message": {...}, "route": ...}
chat() just waits for the "done" event, for callers that don't need streaming.
"""
import json
import logging
import re
import time
from typing import Iterator

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.graph import stream_agent
from app.errors import LLMUnavailable, ValidationFailed
from app.prompts.prompts import FOLLOWUP_SYSTEM_PROMPT
from app.services import conversation_service as conversations
from app.services import document_service, llm_service

logger = logging.getLogger(__name__)


def _title_from(question: str) -> str:
    text = " ".join(question.split())
    return text if len(text) <= 50 else text[:47].rstrip() + "…"


def _resolve_scope(conversation_document_id: str | None, attachment_ids: list[str] | None, user_id: str | None = None) -> list[str]:
    """Work out which documents this question may search. Only fully indexed ones count.
    Attached files come first, then the conversation's document, then everything."""
    ready = document_service.ready_document_ids(user_id)
    if attachment_ids:
        return [i for i in attachment_ids if i in ready]
    if conversation_document_id:
        return [conversation_document_id] if conversation_document_id in ready else []
    return ready


def _attachment_info(attachment_ids: list[str] | None, user_id: str | None = None) -> list[dict]:
    """Read the file names from the database instead of trusting the browser."""
    info = []
    for doc_id in attachment_ids or []:
        try:
            info.append({"id": doc_id, "filename": document_service.get_document(doc_id, user_id)["filename"]})
        except Exception:  # noqa: BLE001 - unknown id, skip it (the scope filter drops it as well)
            continue
    return info


def chat_events(question: str, conversation_id: str | None = None, document_id: str | None = None,
                attachment_ids: list[str] | None = None, user_id: str | None = None) -> Iterator[dict]:
    started = time.perf_counter()
    question = question.strip()
    if not question:
        raise ValidationFailed("Question cannot be empty")

    convo = None
    if conversation_id is None:
        if document_id:
            document_service.get_document(document_id, user_id)        # raises 404 early if it doesn't exist
    else:
        convo = conversations.get_conversation(conversation_id, user_id)
        if document_id and document_id != convo["document_id"]:
            convo = conversations.set_document_scope(convo["id"], document_id, user_id)

    # Reject foreign or missing IDs explicitly instead of silently ignoring them.
    if document_id:
        document_service.get_document(document_id, user_id)
    # Attachment IDs are hints from the client. Resolve only documents owned by
    # this user and silently omit missing/foreign IDs; the scoped ready list below
    # is authoritative and prevents an attachment ID from widening access.
    history = conversations.recent_history(convo["id"], user_id) if convo else []
    scope_doc = convo["document_id"] if convo else document_id
    scope = _resolve_scope(scope_doc, attachment_ids, user_id)
    attachments = _attachment_info(attachment_ids, user_id)

    def save_exchange(answer: str, sources: list, grounded: bool | None, metrics: dict | None) -> dict:
        """Save the question and answer together, so a failed request doesn't leave half a conversation."""
        nonlocal convo
        if convo is None:
            convo = conversations.create_conversation(document_id=document_id, user_id=user_id)
        conversations.add_message(convo["id"], "user", question, attachments=attachments)
        saved = conversations.add_message(convo["id"], "assistant", answer, sources, [], grounded, metrics=metrics)
        if not history and convo["title"] == "New chat":
            conversations.rename_conversation(convo["id"], _title_from(question), user_id)
        return saved

    streamed: list[str] = []
    final_state: dict = {}
    try:
        for kind, payload in stream_agent(question, history, document_ids=scope, user_id=user_id):
            if kind == "custom":
                if payload.get("type") == "token":
                    streamed.append(payload["text"])
                yield payload
            else:
                final_state = payload
    except GeneratorExit:
        # Stop button or closed tab: keep whatever was written so far
        if streamed:
            save_exchange("".join(streamed).strip() + "\n\n_(Response stopped)_", [], None, None)
        raise

    metrics = dict(final_state.get("metrics") or {})
    metrics.setdefault("llm_calls", 0)
    metrics["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    route = final_state.get("route", "document")
    message = save_exchange(final_state.get("answer") or "I could not generate an answer.",
                            final_state.get("sources", []), final_state.get("grounded"), metrics)
    logger.info("Answered in %.0f ms (retrieval %.0f ms, generation %.0f ms, %s LLM calls, %s passages)",
                metrics["total_ms"], metrics.get("retrieval_ms", 0), metrics.get("generation_ms", 0),
                metrics["llm_calls"], metrics.get("passages", 0))
    yield {"type": "done", "conversation_id": convo["id"], "message": message, "route": route}


def chat(question: str, conversation_id: str | None = None, document_id: str | None = None,
         attachment_ids: list[str] | None = None, user_id: str | None = None) -> dict:
    """Non-streaming version. Waits for the whole answer."""
    done = None
    for event in chat_events(question, conversation_id, document_id, attachment_ids, user_id):
        if event["type"] == "done":
            done = event
    return {"conversation_id": done["conversation_id"], "message": done["message"], "route": done["route"]}


def suggest_follow_ups(message_id: str, user_id: str | None = None) -> list[str]:
    """Suggest 3 next questions for an answer. This is its own small LLM call, made after the
    answer is already on screen so it never slows the answer down. The result is saved with the message."""
    item = conversations.get_answer_with_question(message_id, user_id)
    if item["follow_ups"]:
        return item["follow_ups"]
    if not item["sources"]:                       # only answers based on documents get suggestions
        return []
    try:
        raw = llm_service.generate([
            SystemMessage(content=FOLLOWUP_SYSTEM_PROMPT),
            HumanMessage(content=f"Question: {item['question']}\n\nAnswer: {item['answer'][:1000]}"),
        ], temperature=0.3, max_tokens=120)
        match = re.search(r"\[.*\]", raw, re.S)
        items = json.loads(match.group(0)) if match else []
    except (LLMUnavailable, ValueError):
        return []
    follow_ups = [str(i).strip() for i in items if isinstance(i, str) and i.strip()][:3]
    if follow_ups:
        conversations.save_follow_ups(message_id, follow_ups, user_id)
    return follow_ups
