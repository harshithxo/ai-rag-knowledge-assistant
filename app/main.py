import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.conversations import router as conversations_router
from app.api.documents import router as documents_router
from app.api.system import router as system_router
from app.config import settings
from app.errors import AppError
from app.logging_config import configure_logging
from app.services import llm_service

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_directories()
    settings.validate_production()
    db.init_db()
    # load the chat model in the background so the first question doesn't have to wait
    threading.Thread(target=llm_service.warm_up, daemon=True).start()
    logger.info("Application started (LLM=%s, embeddings=%s)", settings.llm_model, settings.embedding_model)
    try:
        yield
    finally:
        db.close()
        logger.info("Application stopped")


app = FastAPI(title="DocMind", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception):
    logger.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"detail": "Something went wrong on the server."})


for router in (auth_router, system_router, chat_router, conversations_router, documents_router):
    app.include_router(router)

frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
