"""Run the deterministic, no-network security demonstration."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from app import vector_store
from app.api import app
from app.core import AppError, Email, prepare_email, analyze_email
from app.security_store import SUPPORTED_SCOPES, clear_rate_limit_events, create_user_token

ROOT = Path(__file__).resolve().parent

def _run_cases(client: TestClient) -> None:
    for filename in ("normal.json", "pii.json", "injection.json"):
        for case in json.loads((ROOT / "cases" / filename).read_text(encoding="utf-8")):
            email = Email(subject=case["subject"], body=case["body"] or "x" * 10_001)
            try:
                subject, body, _truncated, redacted = prepare_email(email)
                if len(body) > 10_000:
                    raise AppError("Email body exceeds maximum normalized length.", 422)
                # Analyze to exercise the same guardrail and deterministic response path.
                result = analyze_email(email, "payload")
                sent = json.dumps({"subject": subject, "body": body}, ensure_ascii=False)
                original = json.dumps({"subject": case["subject"], "body": case["body"]}, ensure_ascii=False)
                print(f"{case['name']} -> original: {original[:180]} | sent to model: {sent[:180]} | response: {result.sentiment}; pii_redacted={redacted}")
            except AppError as exc:
                print(f"{case['name']} -> blocked: no model input | response: HTTP {exc.status_code} {exc.message}")

    for case in json.loads((ROOT / "cases" / "abuse.json").read_text(encoding="utf-8")):
        if case["name"] == "long body":
            response = client.post("/analyze", json={"source":"payload", "data":{"subject":case["subject"],"body":"x" * case["body_chars"]}})
            print(f"{case['name']} -> blocked: no model input | HTTP {response.status_code}")
        else:
            for n in range(5):
                response = client.post("/analyze", json={"source":"payload", "data":{"subject":case["subject"],"body":case["body"]}})
                print(f"{case['name']} #{n+1} -> model input: {'offline sanitized email' if response.status_code == 200 else 'blocked'} | HTTP {response.status_code}")

    for question in ("How long do refunds take?", "What is the capital of Mars?"):
        response = client.post("/rag/query", json={"question": question})
        print(f"RAG {question!r} -> retrieved local snippets | HTTP {response.status_code} | {response.json()}")


def run() -> None:
    with patch.dict(os.environ, {
        "GEMINI_OFFLINE": "true",
        "RATE_LIMIT_REQUESTS": "3",
        "RATE_LIMIT_WINDOW_SECONDS": "120",
        "RAG_RATE_LIMIT_REQUESTS": "10",
        "RAG_RATE_LIMIT_WINDOW_SECONDS": "120",
    }):
        demo_db = ROOT.parent / "tests" / "_runtime" / "demo-run.sqlite3"
        demo_db.parent.mkdir(parents=True, exist_ok=True)
        with patch.object(vector_store, "DB_PATH", demo_db):
            clear_rate_limit_events()
            token, _ = create_user_token("demo-runner-" + uuid.uuid4().hex, SUPPORTED_SCOPES)
            client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
            _run_cases(client)

if __name__ == "__main__":
    run()
