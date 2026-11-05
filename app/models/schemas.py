"""Request and response models."""
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Source(BaseModel):
    id: int
    filename: str
    page: int | None = None
    document_id: str | None = None
    score: float | None = None            # 0 to 1, best passage on this page
    passages: list[str] = []              # retrieved text from this page
    chunk_ids: list[str | None] = []
    chunk_id: str | None = None           # older saved messages have one chunk and one text instead
    text: str | None = None


class Attachment(BaseModel):
    id: str
    filename: str


class ResponseMetrics(BaseModel):
    retrieval_ms: float | None = None
    generation_ms: float | None = None
    total_ms: float | None = None
    llm_calls: int | None = None
    passages: int | None = None
    tokens: int | None = None
    tokens_per_second: float | None = None


class MessageOut(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    sources: list[Source] = []
    follow_ups: list[str] = []
    grounded: bool | None = None
    attachments: list[Attachment] = []
    metrics: ResponseMetrics | None = None
    created_at: str


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=64)
    document_id: str | None = Field(default=None, max_length=64)
    # files attached to this message; the question is about these
    attachment_ids: list[str] | None = Field(default=None, max_length=10)

    @field_validator("question")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Question cannot be blank")
        return value


class ChatResponse(BaseModel):
    conversation_id: str
    route: str
    message: MessageOut


class FollowUpRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=64)


class ConversationOut(BaseModel):
    id: str
    title: str
    document_id: str | None = None
    created_at: str
    updated_at: str
    message_count: int | None = None


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class ConversationCreate(BaseModel):
    title: str = Field(default="New chat", min_length=1, max_length=80)
    document_id: str | None = Field(default=None, max_length=64)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    document_id: str | None = Field(default=None, max_length=64)
    clear_document: bool = False


class DocumentOut(BaseModel):
    id: str
    filename: str
    size_bytes: int
    status: Literal["processing", "ready", "failed"]
    error: str | None = None
    pages: int = 0
    chunks: int = 0
    created_at: str


class UploadResult(BaseModel):
    document: DocumentOut
    duplicate: bool = False


class UploadBatchResponse(BaseModel):
    results: list[UploadResult] = []
    errors: list[dict] = []


class SearchHit(BaseModel):
    document_id: str | None
    filename: str
    page: int | None
    score: float
    text: str


class SettingsUpdate(BaseModel):
    retrieval_top_k: int | None = Field(default=None, ge=1, le=20)
    retrieval_min_score: float | None = Field(default=None, ge=0, le=1)
    llm_temperature: float | None = Field(default=None, ge=0, le=1)
    chunk_size: int | None = Field(default=None, ge=200, le=4000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=1000)
    max_upload_size_mb: int | None = Field(default=None, ge=1, le=100)
