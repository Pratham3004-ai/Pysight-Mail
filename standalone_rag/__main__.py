"""Ingest synthetic emails, query the standalone RAG, or serve its API."""
from __future__ import annotations

import argparse
import json
import sys

from app.core import AppError, attach_diagnostic
from standalone_rag.core import answer_question, ingest

def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m standalone_rag")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ingest", help="Redact, vectorize, and persist the synthetic email corpus locally.")
    commands.add_parser("serve", help="Run the standalone RAG API at http://127.0.0.1:8010.")
    query = commands.add_parser("query", help="Ask a question against the local email index.")
    query.add_argument("question")
    args = parser.parse_args()

    try:
        if args.command == "ingest":
            count = ingest()
            print(f"Indexed {count} redacted email chunks in the local SQLite vector store.")
            return 0
        if args.command == "query":
            print(json.dumps(answer_question(args.question), indent=2, ensure_ascii=False))
            return 0
        import uvicorn
        uvicorn.run("standalone_rag.api:app", host="127.0.0.1", port=8010, access_log=False, log_level="warning")
        return 0
    except AppError as exc:
        safe = attach_diagnostic(exc, operation="standalone_rag_cli")
        print(f"Error [{safe.diagnostic_id}]: {safe.message}", file=sys.stderr)
        return 1
    except Exception as exc:
        safe = attach_diagnostic(exc, operation="standalone_rag_cli")
        print(f"Error [{safe.diagnostic_id}]: {safe.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
