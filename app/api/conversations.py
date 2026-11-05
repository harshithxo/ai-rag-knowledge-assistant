from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.models.schemas import (ConversationCreate, ConversationDetail, ConversationOut, ConversationUpdate)
from app.services import conversation_service as svc
from app.api.auth import get_user, csrf_guard

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=dict)
def list_conversations(user=Depends(get_user)):
    return {"conversations": svc.list_conversations(user["id"])}


@router.post("", response_model=ConversationOut, status_code=201)
def create(body: ConversationCreate, user=Depends(csrf_guard)):
    return svc.create_conversation(body.title, body.document_id, user["id"])


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get(conversation_id: str, user=Depends(get_user)):
    convo = svc.get_conversation(conversation_id, user["id"])
    return {**convo, "messages": svc.get_messages(conversation_id, user["id"])}


@router.patch("/{conversation_id}", response_model=ConversationOut)
def update(conversation_id: str, body: ConversationUpdate, user=Depends(csrf_guard)):
    convo = svc.get_conversation(conversation_id, user["id"])
    if body.title is not None:
        convo = svc.rename_conversation(conversation_id, body.title, user["id"])
    if body.clear_document:
        convo = svc.set_document_scope(conversation_id, None, user["id"])
    elif body.document_id is not None:
        convo = svc.set_document_scope(conversation_id, body.document_id, user["id"])
    return convo


@router.post("/{conversation_id}/clear", response_model=ConversationOut)
def clear(conversation_id: str, user=Depends(csrf_guard)):
    svc.clear_messages(conversation_id, user["id"])
    return svc.get_conversation(conversation_id, user["id"])


@router.get("/{conversation_id}/export", response_class=PlainTextResponse)
def export(conversation_id: str, user=Depends(get_user)):
    return PlainTextResponse(svc.export_markdown(conversation_id, user["id"]), media_type="text/markdown")


@router.delete("/{conversation_id}")
def delete(conversation_id: str, user=Depends(csrf_guard)):
    svc.delete_conversation(conversation_id, user["id"])
    return {"success": True}
