"""Google OAuth and Gmail message access for authenticated Pysight users."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from app import vector_store

GOOGLE_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
STATE_TTL_SECONDS = 600


def _connect() -> sqlite3.Connection:
    db_path = vector_store.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS gmail_oauth_states (
            state_hash TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS gmail_connections (
            owner_id TEXT PRIMARY KEY,
            gmail_address TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    return connection


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def create_oauth_state(owner_id: str) -> str:
    state = secrets.token_urlsafe(32)
    expires_at = (_now() + timedelta(seconds=STATE_TTL_SECONDS)).isoformat()
    with closing(_connect()) as connection, connection:
        connection.execute(
            "INSERT INTO gmail_oauth_states(state_hash,owner_id,expires_at,used_at) VALUES(?,?,?,NULL)",
            (_state_hash(state), owner_id, expires_at),
        )
    return state


def consume_oauth_state(state: str) -> str | None:
    if not state:
        return None
    now = _now().isoformat()
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner_id FROM gmail_oauth_states WHERE state_hash=? AND used_at IS NULL AND expires_at>?",
            (_state_hash(state), now),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        connection.execute(
            "UPDATE gmail_oauth_states SET used_at=? WHERE state_hash=?",
            (now, _state_hash(state)),
        )
        connection.commit()
        return row[0]


def _client_secrets_path() -> str:
    return os.environ.get(
        "GOOGLE_CLIENT_SECRETS_FILE",
        str(Path(__file__).resolve().parent.parent / "secrets" / "google-client.json"),
    )


def _redirect_uri() -> str:
    return os.environ.get("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/auth/google/callback")


def _oauth_flow(state: str | None = None):
    from google_auth_oauthlib.flow import Flow

    secrets_path = Path(_client_secrets_path())
    if not secrets_path.is_file():
        raise ValueError("Google OAuth client file is missing. Configure GOOGLE_CLIENT_SECRETS_FILE.")

    client_config = json.loads(secrets_path.read_text(encoding="utf-8"))
    if "web" not in client_config:
        raise ValueError(
            "Google OAuth client must be type 'Web application', not 'Desktop app'. "
            "Download a Web application client JSON and set GOOGLE_CLIENT_SECRETS_FILE."
        )

    redirect_uri = _redirect_uri()
    registered_redirects = client_config["web"].get("redirect_uris", [])
    if redirect_uri not in registered_redirects:
        raise ValueError(
            "GOOGLE_REDIRECT_URI is not registered in the Google OAuth client. "
            f"Add exactly '{redirect_uri}' as an Authorized redirect URI."
        )

    flow = Flow.from_client_secrets_file(
        _client_secrets_path(),
        scopes=[GOOGLE_SCOPE],
        state=state,
    )
    flow.autogenerate_code_verifier = False
    flow.redirect_uri = _redirect_uri()
    return flow


def authorization_url(owner_id: str) -> str:
    state = create_oauth_state(owner_id)
    flow = _oauth_flow(state)
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return url


def _gmail_service(credentials):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _save_connection(owner_id: str, credentials, address: str) -> dict:
    now = _now().isoformat()
    if not credentials.refresh_token:
        raise ValueError("Google did not return a refresh token. Reconnect with consent.")
    with closing(_connect()) as connection, connection:
        connection.execute(
            """INSERT INTO gmail_connections(owner_id,gmail_address,refresh_token,scopes_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(owner_id) DO UPDATE SET gmail_address=excluded.gmail_address,
               refresh_token=excluded.refresh_token, scopes_json=excluded.scopes_json,
               updated_at=excluded.updated_at""",
            (owner_id, address, credentials.refresh_token, json.dumps([GOOGLE_SCOPE]), now, now),
        )
    return {"owner_id": owner_id, "gmail_address": address}


def connect_local(owner_id: str) -> dict:
    """Connect a user through Google's desktop loopback OAuth flow."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(_client_secrets_path(), [GOOGLE_SCOPE])
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    profile = _gmail_service(credentials).users().getProfile(userId="me").execute()
    address = profile.get("emailAddress")
    if not address:
        raise ValueError("Google did not return a Gmail address.")
    return _save_connection(owner_id, credentials, address)


def exchange_code(state: str, code: str) -> dict:
    owner_id = consume_oauth_state(state)
    if owner_id is None:
        raise ValueError("Invalid, expired, or already used OAuth state.")
    flow = _oauth_flow(state)
    flow.fetch_token(code=code)
    credentials = flow.credentials
    if not credentials.refresh_token:
        raise ValueError("Google did not return a refresh token. Reconnect with consent.")
    profile = _gmail_service(credentials).users().getProfile(userId="me").execute()
    address = profile.get("emailAddress")
    if not address:
        raise ValueError("Google did not return a Gmail address.")
    return _save_connection(owner_id, credentials, address)


def connection_status(owner_id: str) -> dict | None:
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT gmail_address,scopes_json,updated_at FROM gmail_connections WHERE owner_id=?",
            (owner_id,),
        ).fetchone()
    if row is None:
        return None
    return {"gmail_address": row[0], "scopes": json.loads(row[1]), "updated_at": row[2]}


def _credentials_for(owner_id: str):
    from google.oauth2.credentials import Credentials

    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT refresh_token FROM gmail_connections WHERE owner_id=?",
            (owner_id,),
        ).fetchone()
    if row is None:
        raise LookupError("No Gmail account is connected for this user.")
    client_config = json.loads(Path(_client_secrets_path()).read_text(encoding="utf-8"))
    section = client_config.get("web") or client_config.get("installed")
    if not section:
        raise ValueError("Google client secrets must contain a web or installed configuration.")
    return Credentials(
        token=None,
        refresh_token=row[0],
        token_uri=section["token_uri"],
        client_id=section["client_id"],
        client_secret=section["client_secret"],
        scopes=[GOOGLE_SCOPE],
    )


def _header(headers: list[dict], name: str) -> str:
    wanted = name.casefold()
    return next((item.get("value", "") for item in headers if item.get("name", "").casefold() == wanted), "")


def _message_summary(message: dict) -> dict:
    payload = message.get("payload", {})
    headers = payload.get("headers", [])
    date = _header(headers, "Date")
    try:
        received_at = parsedate_to_datetime(date).isoformat() if date else None
    except (TypeError, ValueError, OverflowError):
        received_at = date or None
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "label_ids": message.get("labelIds", []),
        "sender": _header(headers, "From"),
        "recipient": _header(headers, "To"),
        "subject": _header(headers, "Subject"),
        "received_at": received_at,
        "snippet": message.get("snippet", ""),
    }


def list_messages(owner_id: str, *, query: str | None, max_results: int, page_token: str | None,
                  sort_by: str, order: str) -> dict:
    service = _gmail_service(_credentials_for(owner_id))
    params = {"userId": "me", "maxResults": max_results}
    if query:
        params["q"] = query
    if page_token:
        params["pageToken"] = page_token
    result = service.users().messages().list(**params).execute()
    items = []
    for item in result.get("messages", []):
        message = service.users().messages().get(userId="me", id=item["id"], format="metadata", metadataHeaders=["From", "To", "Subject", "Date"]).execute()
        items.append(_message_summary(message))
    key = {"date": "received_at", "subject": "subject", "sender": "sender"}[sort_by]
    items.sort(key=lambda item: (item.get(key) or "").casefold(), reverse=order == "desc")
    return {"count": len(items), "messages": items, "next_page_token": result.get("nextPageToken")}


def get_message(owner_id: str, message_id: str) -> dict:
    service = _gmail_service(_credentials_for(owner_id))
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    result = _message_summary(message)
    result["body"] = _extract_text(message.get("payload", {}))
    return result


def _extract_text(part: dict) -> str:
    mime_type = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if data and (mime_type == "text/plain" or not part.get("parts")):
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    extracted = [_extract_text(child) for child in part.get("parts", [])]
    return "\n".join(value for value in extracted if value).strip()