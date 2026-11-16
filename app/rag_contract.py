"""Strict extractive response contract shared by both RAG APIs."""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core import AppError

MAX_GROUNDED_ANSWER_CHARS = 2000
MAX_GROUNDED_CLAIMS = 2

class GroundedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=1200)

class GroundedAnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[GroundedClaim] = Field(min_length=1, max_length=MAX_GROUNDED_CLAIMS)


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def ground_model_response(raw: str, excerpts: list[dict[str, str]]) -> dict:
    """Accept only source quotes that occur verbatim in retrieved evidence.

    The API response is extractive: the displayed answer is assembled from the
    validated quotes, so generated paraphrases cannot outgrow their citations.
    """
    try:
        draft = GroundedAnswerDraft.model_validate_json(raw)
    except (ValidationError, ValueError, TypeError) as exc:
        raise AppError(
            "RAG model returned an invalid evidence response.",
            502,
            stage="response_parsing",
            category="response_schema",
        ) from exc

    evidence_by_source: dict[str, list[str]] = {}
    for excerpt in excerpts:
        evidence_by_source.setdefault(excerpt["source"], []).append(_collapse_whitespace(excerpt["snippet"]))

    citations: list[dict[str, str]] = []
    answer_quotes: list[str] = []
    seen: set[tuple[str, str]] = set()
    for claim in draft.claims:
        quote = _collapse_whitespace(claim.quote)
        source_evidence = evidence_by_source.get(claim.source, [])
        if not quote or not any(quote == evidence for evidence in source_evidence):
            raise AppError(
                "RAG model returned a quote that does not match its cited excerpt exactly.",
                502,
                stage="output_validation",
                category="citation_mismatch",
            )
        citation_key = (claim.source, quote)
        if citation_key in seen:
            continue
        seen.add(citation_key)
        answer_quotes.append(quote)
        citations.append({"source": claim.source, "snippet": quote})

    answer = " ".join(answer_quotes)
    if not answer or len(answer) > MAX_GROUNDED_ANSWER_CHARS:
        raise AppError(
            "RAG model returned an answer outside the allowed length.",
            502,
            stage="output_validation",
            category="output_too_large",
        )
    return {"answer": answer, "citations": citations}


def offline_grounded_response(excerpts: list[dict[str, str]]) -> dict:
    """Return a deterministic extractive result without a provider call."""
    if not excerpts:
        return {"answer": "The local documents do not contain information that answers this question.", "citations": []}
    return ground_model_response(
        json.dumps({"claims": [{"source": excerpts[0]["source"], "quote": excerpts[0]["snippet"]}]}, ensure_ascii=False),
        excerpts,
    )
