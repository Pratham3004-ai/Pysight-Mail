
"""Simple CLI sharing the same core as FastAPI."""
from __future__ import annotations

import argparse
import json
import os
import sys

from app.core import AppError, analyze_email, attach_diagnostic, list_emails, resolve_input
from app.security_store import (
    SUPPORTED_SCOPES,
    SecurityStoreError,
    create_user_token,
    list_user_tokens,
    revoke_user_tokens,
    rotate_user_token,
)

def _status(message: str) -> None:
    """Show safe progress information without showing email content."""
    print(f"  [..] {message}...", file=sys.stderr)


def _print_email_list(emails) -> None:
    print(f"\nFound {len(emails)} local email{'s' if len(emails) != 1 else ''}\n")
    for number, email in enumerate(emails, start=1):
        identifier = email.id or "no ID"
        subject = email.subject.strip() or "(no subject)"
        print(f"{number}. {identifier} - {subject}")
    print("\nTip: analyze one with: python -m app analyze --source file --path <filename>.json")

def _print_analysis(result) -> None:
    sentiment = result.sentiment.replace("_", " ").title()
    confidence = f"{result.confidence:.0%}"
    print("\nAnalysis complete\n" + "=" * 42)
    print(f"Sentiment:   {sentiment}")
    print(f"Confidence:  {confidence}")
    print(f"Explanation: {result.explanation}")
    print(f"Source:      {result.source}")
    print(f"Email ID:    {result.email_id or 'Not provided'}")
    print(f"Privacy:     {'Common PII masked' if result.pii_redacted else 'No supported PII found'}")
    print(f"Truncated:   {'Yes' if result.truncated else 'No'}")
    print(f"Model:       {result.model}")
    print("=" * 42)
    print("Use --json for machine-readable output.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app", description="Retrieve local emails or analyze one email using Gemini."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    emails_command = commands.add_parser("emails", help="List local JSON emails from the emails/ folder.")
    emails_command.add_argument("--json", action="store_true", help="Print the original JSON response for automation.")
    analyze = commands.add_parser("analyze", help="Analyze one email using the real Gemini API.")
    analyze.add_argument("--source", choices=("file", "text"), required=True)
    analyze.add_argument("--path", help="JSON filename within emails/; required for source=file")
    analyze.add_argument("--subject", help="Email subject (for source=text)")
    analyze.add_argument("--body", help="Email body (for source=text)")
    analyze.add_argument("--offline", action="store_true", help="Run the local mock pipeline without Gemini")
    analyze.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of the readable summary.")
    commands.add_parser("serve", help="Run the local API at http://127.0.0.1:8000")
    auth = commands.add_parser("auth", help="Provision distinct, expiring user API tokens locally.")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    scopes_help = "Allowed scopes: " + ", ".join(sorted(SUPPORTED_SCOPES))
    for command_name in ("create-user", "rotate"):
        token_command = auth_commands.add_parser(command_name, help=f"Issue a token; {scopes_help}")
        token_command.add_argument("--user-id", required=True)
        token_command.add_argument("--expires-in-days", type=int, default=30)
        token_command.add_argument("--scopes", nargs="+", choices=sorted(SUPPORTED_SCOPES), required=True)
    revoke = auth_commands.add_parser("revoke", help="Revoke all active tokens for a user.")
    revoke.add_argument("--user-id", required=True)
    auth_commands.add_parser("list", help="List token metadata without exposing token values.")
    args = parser.parse_args(argv)
    if getattr(args, "offline", False):
        os.environ["GEMINI_OFFLINE"] = "true"

    try:
        if args.command == "auth":
            if args.auth_command == "create-user":
                token, expires = create_user_token(args.user_id, args.scopes, expires_in_days=args.expires_in_days)
                print(f"User: {args.user_id}\nExpires: {expires}\nToken (shown once; copy into that user's Postman secret environment variable):\n{token}")
            elif args.auth_command == "rotate":
                token, expires = rotate_user_token(args.user_id, args.scopes, expires_in_days=args.expires_in_days)
                print(f"User: {args.user_id}\nExpires: {expires}\nPrevious tokens for this user are revoked. New token (shown once):\n{token}")
            elif args.auth_command == "revoke":
                count = revoke_user_tokens(args.user_id)
                print(f"Revoked {count} active token(s) for {args.user_id}.")
            else:
                for item in list_user_tokens():
                    print(json.dumps(item, ensure_ascii=False))
            return 0
        if args.command == "serve":
            import uvicorn
            print("Starting local API at http://127.0.0.1:8000 (press Ctrl+C to stop)", file=sys.stderr)
            uvicorn.run("app.api:app", host="127.0.0.1", port=8000, access_log=False, log_level="critical")
            return 0
        if args.command == "emails":
            if not args.json:
                _status("Loading local emails")
            emails = list_emails()
            if args.json:
                print(json.dumps({"count": len(emails), "emails": [e.model_dump(by_alias=True) for e in emails]}, indent=2, ensure_ascii=False))
            else:
                _print_email_list(emails)
            return 0
        if args.source == "file":
            if args.path is None or args.subject is not None or args.body is not None:
                raise AppError("source=file requires --path filename.json only.")
            if not args.json:
                _status("Loading the selected local email")
            email = resolve_input("file", {"path": args.path})
        else:
            if args.path is not None:
                raise AppError("source=text accepts --subject and --body, not --path.")
            body = args.body
            if body is None:
                # For PowerShell piped text: Get-Content ... | python -m app analyze --source text
                if sys.stdin.isatty():
                    raise AppError("Provide --body TEXT, or pipe the email body into the command.")
                body = sys.stdin.read(100_001)
            if not args.json:
                _status("Reading the supplied email text")
            email = resolve_input("text", {"subject": args.subject or "", "body": body})
        result = analyze_email(email, args.source, progress=None if args.json else _status)
        if args.json:
            print(result.model_dump_json(indent=2))
        else:
            _print_analysis(result)
        return 0
    except SecurityStoreError as exc:
        print(f"Authentication management error: {exc}", file=sys.stderr)
        return 2
    except AppError as exc:
        safe = attach_diagnostic(exc, operation="cli", source_type=getattr(args, "source", None))
        print(f"Error [{safe.diagnostic_id}]: {safe.message}", file=sys.stderr)
        return 1
    except Exception as exc:
        safe = attach_diagnostic(exc, operation="cli", source_type=getattr(args, "source", None))
        print(f"Error [{safe.diagnostic_id}]: {safe.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
