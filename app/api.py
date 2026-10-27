"""Authenticated sentiment, integrated RAG, and redacted-history API."""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.auth import authenticate_request, authorize_scope
from app.core import (
    AnalysisRequest,
    AppError,
    MAX_RAW_BYTES,
    analyze_email,
    attach_diagnostic,
    list_emails,
    prepare_email,
    resolve_input,
)
from app.rag import query_rag, sanitize_question
from app.security_store import consume_rate_limit
from app.vector_store import list_records, save_record, search_records

app = FastAPI(
    title="Email Sentiment Analyzer",
    description="Authenticated email sentiment, integrated RAG, and redacted local history.",
    version="1.0.0",
)


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Require a valid per-user token before processing any protected route."""
    principal, response = authenticate_request(request)
    if response is not None:
        return response
    request.state.owner_id = principal["user_id"]
    request.state.scopes = principal["scopes"]
    return await call_next(request)

def _enforce_rate_limit(key: str) -> None:
    limit = max(1, int(os.environ.get("RATE_LIMIT_REQUESTS", "10")))
    window = max(120, int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "120")))
    if not consume_rate_limit("sentiment", key, limit, window):
        raise AppError("Analysis rate limit exceeded. Try again shortly.", 429, stage="rate_limit", category="rate_limited")


def _enforce_rag_rate_limit(key: str) -> None:
    limit = max(1, int(os.environ.get("RAG_RATE_LIMIT_REQUESTS", "10")))
    window = max(120, int(os.environ.get("RAG_RATE_LIMIT_WINDOW_SECONDS", "120")))
    if not consume_rate_limit("integrated_rag", key, limit, window):
        raise AppError("RAG query rate limit exceeded. Try again shortly.", 429, stage="rag_rate_limit", category="rate_limited")


@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError):
    safe = attach_diagnostic(exc, operation="http", source_type=getattr(_request.state, "source_type", None))
    return JSONResponse(status_code=safe.status_code, content={"error": safe.message, "diagnostic_id": safe.diagnostic_id})


@app.exception_handler(Exception)
async def unexpected_error_handler(_request: Request, exc: Exception):
    safe = attach_diagnostic(exc, operation="http")
    return JSONResponse(status_code=safe.status_code, content={"error": safe.message, "diagnostic_id": safe.diagnostic_id})


@app.get("/emails")
async def get_emails(request: Request):
    denied = authorize_scope(request, "emails:read")
    if denied:
        return denied
    emails = await asyncio.to_thread(list_emails)
    return {"count": len(emails), "emails": [email.model_dump(by_alias=True) for email in emails]}


@app.post(
    "/analyze",
    description="Pass source=payload with data.subject/body, or source=file with data.path (filename under emails/).",
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": AnalysisRequest.model_json_schema()}}}},
)
async def post_analyze(request: Request):
    denied = authorize_scope(request, "sentiment:analyze")
    if denied:
        return denied
    client_key = request.state.owner_id
    _enforce_rate_limit(client_key)
    # Read in chunks so large unknown-length bodies can be rejected without buffering them all.
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_RAW_BYTES:
            raise AppError("Request body exceeds the 100 KB raw size limit.", 413)
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        parsed = AnalysisRequest.model_validate(payload)
    except (UnicodeError, ValueError, ValidationError) as exc:
        raise AppError("Invalid JSON input: provide source and data in the documented format.", 422) from exc
    if parsed.source == "text":
        raise AppError("Use source=payload for HTTP requests; source=text is for the CLI.", 422)
    if parsed.source == "payload" and (
        not {"subject", "body"}.issubset(parsed.data.model_fields_set)
        or parsed.data.subject is None
        or parsed.data.body is None
    ):
        raise AppError("For source=payload, provide text subject and body fields.", 422)
    request.state.source_type = parsed.source
    email = await asyncio.to_thread(resolve_input, parsed.source, parsed.data.model_dump())
    result = await asyncio.to_thread(analyze_email, email, parsed.source)
    safe_subject, safe_body, _truncated, _pii = await asyncio.to_thread(prepare_email, email)
    await asyncio.to_thread(
        save_record,
        request.state.owner_id,
        "sentiment",
        json.dumps({"subject": safe_subject, "body": safe_body}, ensure_ascii=False),
        result.model_dump(),
    )
    return result.model_dump()


@app.post("/rag/query")
async def post_rag_query(request: Request):
    denied = authorize_scope(request, "rag:query")
    if denied:
        return denied
    _enforce_rag_rate_limit(request.state.owner_id)
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > 20_000:
            raise AppError("Request body exceeds the 20 KB limit.", 413)
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"question"} or not isinstance(payload["question"], str):
            raise ValueError("Invalid question payload")
        question = payload["question"].strip()
        if not question or len(question) > 1000:
            raise ValueError("Empty or too long question")
    except (UnicodeError, ValueError, KeyError) as exc:
        raise AppError("Provide JSON with a nonempty question of at most 1,000 characters.", 422) from exc
    safe_question = await asyncio.to_thread(sanitize_question, question)
    result = await asyncio.to_thread(query_rag, safe_question)
    await asyncio.to_thread(save_record, request.state.owner_id, "integrated_rag", safe_question, result)
    return result


@app.get("/history")
async def get_history(request: Request, limit: int = 50):
    """Return only redacted records owned by the authenticated user."""
    denied = authorize_scope(request, "history:read")
    if denied:
        return denied
    if limit < 1 or limit > 100:
        raise AppError("limit must be between 1 and 100.", 422)
    records = await asyncio.to_thread(list_records, request.state.owner_id, limit=limit)
    return {"count": len(records), "records": records}


@app.get("/history/search")
async def search_history(request: Request, question: str, limit: int = 10):
    """Search this user's stored redacted records by their local hashed vectors."""
    denied = authorize_scope(request, "history:read")
    if denied:
        return denied
    if limit < 1 or limit > 100:
        raise AppError("limit must be between 1 and 100.", 422)
    safe_question = await asyncio.to_thread(sanitize_question, question)
    matches = await asyncio.to_thread(search_records, request.state.owner_id, safe_question, limit=limit)
    return {"count": len(matches), "matches": matches}
