import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.errors import AppError
from app.models.schemas import ChatRequest, ChatResponse, FollowUpRequest
from app.services import chat_service
from app.api.auth import get_user, csrf_guard

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
def chat_endpoint(request: ChatRequest, user=Depends(csrf_guard)):
    """Wait for the full answer. AppErrors are turned into JSON by the handler in main.py."""
    return chat_service.chat(request.question, request.conversation_id, request.document_id,
                             request.attachment_ids, user["id"])


@router.post("/stream")
def chat_stream_endpoint(request: ChatRequest, user=Depends(csrf_guard)):
    """Stream the answer as it is written, one JSON object per line (NDJSON).

    Event types are status, token, done and error."""
    def lines():
        try:
            for event in chat_service.chat_events(request.question, request.conversation_id,
                                                  request.document_id, request.attachment_ids, user["id"]):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except AppError as exc:
            yield json.dumps({"type": "error", "detail": exc.message}) + "\n"
        except Exception:  # noqa: BLE001 - the response has already started, so send the error as an event
            logger.exception("Chat stream failed")
            yield json.dumps({"type": "error", "detail": "Something went wrong on the server."}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/follow-ups", response_model=dict)
def follow_up_suggestions(request: FollowUpRequest, user=Depends(csrf_guard)):
    """Suggest next questions. The browser calls this after the answer is shown."""
    return {"follow_ups": chat_service.suggest_follow_ups(request.message_id, user["id"])}
