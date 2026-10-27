"""Standalone RAG HTTP API, independent of the sentiment app server."""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.auth import authenticate_request, authorize_scope
from app.core import AppError, attach_diagnostic
from app.security_store import consume_rate_limit
from app.vector_store import save_record
from standalone_rag.core import MAX_BODY_BYTES, answer_question

app = FastAPI(
    title="Standalone Email RAG",
    description="Query a local persistent index of synthetic email records with source citations.",
    version="1.0.0",
)


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    principal, response = authenticate_request(request)
    if response is not None:
        return response
    request.state.owner_id = principal["user_id"]
    request.state.scopes = principal["scopes"]
    return await call_next(request)


def _rate_limit(client_ip: str) -> None:
    limit = max(1, int(os.environ.get("RAG_RATE_LIMIT_REQUESTS", "10")))
    window = max(120, int(os.environ.get("RAG_RATE_LIMIT_WINDOW_SECONDS", "120")))
    if not consume_rate_limit("standalone_rag_ip", client_ip, limit, window):
        raise AppError("RAG query rate limit exceeded. Try again shortly.", 429, stage="rag_rate_limit", category="rate_limited")


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    safe = attach_diagnostic(exc, operation="standalone_rag")
    return JSONResponse(status_code=safe.status_code, content={"error": safe.message, "diagnostic_id": safe.diagnostic_id})


@app.exception_handler(Exception)
async def unexpected_error_handler(_request: Request, exc: Exception):
    safe = attach_diagnostic(exc, operation="standalone_rag")
    return JSONResponse(status_code=safe.status_code, content={"error": safe.message, "diagnostic_id": safe.diagnostic_id})


@app.post("/rag/query")
async def post_query(request: Request):
    denied = authorize_scope(request, "rag:query")
    if denied:
        return denied
    client_ip = request.client.host if request.client else "unknown"
    _rate_limit(client_ip)
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise AppError("Request body exceeds the 20 KB raw size limit.", 413, stage="rag_input", category="input_too_large")
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"question"} or not isinstance(payload["question"], str):
            raise ValueError("invalid question payload")
        question = payload["question"]
    except (UnicodeError, ValueError) as exc:
        raise AppError("Provide JSON with one string field named question.", 422, stage="rag_input", category="invalid_payload") from exc
    result = await asyncio.to_thread(answer_question, question)
    # answer_question redacts the question before provider use. Re-run the same
    # deterministic sanitizer for persistence so no raw query is written to disk.
    from standalone_rag.core import _validate_question
    stored_question = await asyncio.to_thread(_validate_question, question)
    await asyncio.to_thread(save_record, request.state.owner_id, "standalone_rag", stored_question, result)
    return result
