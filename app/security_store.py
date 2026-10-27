"""SQLite-backed API credentials and durable rate-limit counters."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone


SUPPORTED_SCOPES = frozenset({"emails:read", "sentiment:analyze", "rag:query", "history:read"})
MIN_RATE_WINDOW_SECONDS = 120
_USER_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class SecurityStoreError(ValueError):
    """Safe operator-facing error from local credential management."""


def _db_path():
    # Resolve lazily so test suites and deployments can override the shared DB path.
    from app import vector_store

    return vector_store.DB_PATH


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS api_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS api_tokens_user ON api_tokens(user_id, created_at)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS rate_limit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket TEXT NOT NULL,
            subject TEXT NOT NULL,
            occurred_at REAL NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS rate_limit_lookup ON rate_limit_events(bucket, subject, occurred_at)")
    return connection


def _validate_user_and_scopes(user_id: str, scopes) -> tuple[str, list[str]]:
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise SecurityStoreError("user-id must be 1–80 letters, digits, dots, underscores, or hyphens.")
    normalized = sorted(set(scopes or []))
    if not normalized:
        raise SecurityStoreError("At least one access scope is required.")
    unknown = sorted(set(normalized) - SUPPORTED_SCOPES)
    if unknown:
        raise SecurityStoreError("Unsupported access scope: " + ", ".join(unknown))
    return user_id, normalized


def _expiry(expires_in_days: int, now: datetime | None = None) -> tuple[str, str]:
    if not isinstance(expires_in_days, int) or not 1 <= expires_in_days <= 3650:
        raise SecurityStoreError("expires-in-days must be between 1 and 3650.")
    now = now or datetime.now(timezone.utc)
    return now.isoformat(), (now + timedelta(days=expires_in_days)).isoformat()


def _new_token() -> str:
    return "pysight_" + secrets.token_urlsafe(32)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _insert_token(connection: sqlite3.Connection, user_id: str, scopes: list[str], expires_in_days: int) -> tuple[str, str]:
    token = _new_token()
    created, expires = _expiry(expires_in_days)
    connection.execute(
        "INSERT INTO api_tokens(token_hash,user_id,scopes_json,created_at,expires_at,revoked_at) VALUES(?,?,?,?,?,NULL)",
        (_token_hash(token), user_id, json.dumps(scopes), created, expires),
    )
    return token, expires


def create_user_token(user_id: str, scopes, *, expires_in_days: int = 30) -> tuple[str, str]:
    """Create one token for a user; return its plaintext exactly once to the CLI."""
    user_id, scopes = _validate_user_and_scopes(user_id, scopes)
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT 1 FROM api_tokens WHERE user_id=? AND revoked_at IS NULL AND expires_at>? LIMIT 1",
            (user_id, datetime.now(timezone.utc).isoformat()),
        ).fetchone()
        if active:
            connection.rollback()
            raise SecurityStoreError("This user already has an active token. Use auth rotate to replace it.")
        token, expires = _insert_token(connection, user_id, scopes, expires_in_days)
        connection.commit()
        return token, expires


def rotate_user_token(user_id: str, scopes, *, expires_in_days: int = 30) -> tuple[str, str]:
    """Revoke prior user tokens and issue a replacement in one transaction."""
    user_id, scopes = _validate_user_and_scopes(user_id, scopes)
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute("SELECT 1 FROM api_tokens WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
        if not exists:
            connection.rollback()
            raise SecurityStoreError("No token exists for this user; use auth create-user first.")
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("UPDATE api_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, user_id))
        token, expires = _insert_token(connection, user_id, scopes, expires_in_days)
        connection.commit()
        return token, expires


def revoke_user_tokens(user_id: str) -> int:
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise SecurityStoreError("Invalid user-id.")
    with closing(_connect()) as connection, connection:
        cursor = connection.execute(
            "UPDATE api_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )
        return cursor.rowcount


def list_user_tokens() -> list[dict]:
    with closing(_connect()) as connection:
        rows = connection.execute(
            "SELECT user_id,scopes_json,created_at,expires_at,revoked_at FROM api_tokens ORDER BY user_id,created_at"
        ).fetchall()
    now = datetime.now(timezone.utc)
    items = []
    for user_id, scopes_json, created, expires, revoked in rows:
        expired = datetime.fromisoformat(expires) <= now
        items.append({
            "user_id": user_id,
            "scopes": json.loads(scopes_json),
            "created_at": created,
            "expires_at": expires,
            "status": "revoked" if revoked else "expired" if expired else "active",
        })
    return items


def authenticate_api_token(token: str) -> dict | None:
    if not token:
        return None
    now = datetime.now(timezone.utc).isoformat()
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT user_id,scopes_json,expires_at,revoked_at FROM api_tokens WHERE token_hash=?",
            (_token_hash(token),),
        ).fetchone()
    if row is None or row[3] is not None or row[2] <= now:
        return None
    return {"user_id": row[0], "scopes": frozenset(json.loads(row[1]))}


def effective_window_seconds(window_seconds: int) -> int:
    return max(MIN_RATE_WINDOW_SECONDS, int(window_seconds))


def consume_rate_limit(bucket: str, subject: str, limit: int, window_seconds: int, *, now: float | None = None) -> bool:
    """Atomically persist one request event, surviving application restarts."""
    if limit < 1:
        limit = 1
    window = effective_window_seconds(window_seconds)
    current = time.time() if now is None else now
    cutoff = current - window
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        # All clients in a bucket share its configured window, so old rows can be pruned safely.
        connection.execute("DELETE FROM rate_limit_events WHERE bucket=? AND occurred_at<=?", (bucket, cutoff))
        count = connection.execute(
            "SELECT COUNT(*) FROM rate_limit_events WHERE bucket=? AND subject=? AND occurred_at>?",
            (bucket, subject, cutoff),
        ).fetchone()[0]
        if count >= limit:
            connection.rollback()
            return False
        connection.execute(
            "INSERT INTO rate_limit_events(bucket,subject,occurred_at) VALUES(?,?,?)",
            (bucket, subject, current),
        )
        connection.commit()
        return True


def clear_rate_limit_events() -> None:
    """Clear persisted counters; intended for isolated test setup and local maintenance."""
    with closing(_connect()) as connection, connection:
        connection.execute("DELETE FROM rate_limit_events")
