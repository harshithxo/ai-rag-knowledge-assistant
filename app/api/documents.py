import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, Response

from app.config import settings
from app.errors import PayloadTooLarge
from app.models.schemas import DocumentOut, SearchHit, UploadBatchResponse, UploadResult
from app.services import document_service, ingestion_service, settings_service
from app.api.auth import get_user, csrf_guard

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/upload", response_model=UploadBatchResponse)
async def upload_documents(background: BackgroundTasks,
                           files: Annotated[list[UploadFile], File(description="One or more PDFs")], user=Depends(csrf_guard)):
    """Accept one or more PDFs. Each file is checked and saved right away, indexing runs in the
    background, and the client polls GET /documents to see the status."""
    response = UploadBatchResponse()
    for upload in files[: settings.max_files_per_upload]:
        name = upload.filename or "document.pdf"
        try:
            # read one byte past the limit so a huge file is rejected without loading all of it
            limit = settings_service.get_max_upload_bytes()
            content = await upload.read(limit + 1)
            if len(content) > limit:
                raise PayloadTooLarge(f"File exceeds the {settings_service.get_int('max_upload_size_mb')} MB upload limit")
            row, duplicate = ingestion_service.register_upload(name, content, user["id"])
            if not duplicate:
                background.add_task(ingestion_service.process_document, row["id"], user["id"])
            doc = document_service.get_document(row["id"], user["id"])
            response.results.append(UploadResult(document=DocumentOut(**doc), duplicate=duplicate))
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't stop the rest
            if not hasattr(exc, "message"):
                logger.exception("Unexpected upload failure for %s", name)
            response.errors.append({"filename": ingestion_service.sanitize_filename(name),
                                    "detail": getattr(exc, "message", "Upload failed")})
    for skipped in files[settings.max_files_per_upload:]:
        response.errors.append({"filename": ingestion_service.sanitize_filename(skipped.filename),
                                "detail": f"Only {settings.max_files_per_upload} files can be uploaded at once"})
    return response


@router.get("", response_model=dict)
def list_documents(user=Depends(get_user)):
    return {"documents": document_service.list_documents(user["id"])}


@router.get("/search", response_model=dict)
def search(q: Annotated[str, Query(min_length=2, max_length=300)], user=Depends(get_user)):
    hits = document_service.search_documents(q.strip(), user_id=user["id"])
    return {"query": q, "results": [SearchHit(**h).model_dump() for h in hits]}


@router.get("/{document_id}", response_model=DocumentOut)
def get_document(document_id: str, user=Depends(get_user)):
    return document_service.get_document(document_id, user["id"])


@router.get("/{document_id}/file")
def preview_file(document_id: str, user=Depends(get_user)):
    """Send the original PDF so the browser can show it inline."""
    doc = document_service.get_document(document_id, user["id"])
    if settings.upload_storage.lower() == "s3":
        return Response(document_service.get_file_content(document_id, user["id"]), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{doc["filename"]}"',
                                 "X-Content-Type-Options": "nosniff"})
    path = document_service.get_file_path(document_id, user["id"])
    return FileResponse(path, media_type="application/pdf", filename=doc["filename"],
                        content_disposition_type="inline",
                        headers={"X-Content-Type-Options": "nosniff"})


@router.post("/{document_id}/reprocess", response_model=DocumentOut)
def reprocess(document_id: str, background: BackgroundTasks, user=Depends(csrf_guard)):
    document_service.get_document(document_id, user["id"])
    background.add_task(ingestion_service.reprocess_document, document_id, user["id"])
    return {**document_service.get_document(document_id, user["id"]), "status": "processing", "error": None}


@router.delete("/{document_id}")
def remove_document(document_id: str, user=Depends(csrf_guard)):
    document_service.delete_document(document_id, user["id"])
    return {"success": True, "document_id": document_id}
