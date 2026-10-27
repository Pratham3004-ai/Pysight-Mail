"""Small SQLite-backed vector/history store for redacted application records.

Vectors are hashed lexical features, not neural semantic embeddings. The database
stores only already-redacted text and is intended for the local demo.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("PYSIGHT_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "pysight.sqlite3"))
DIMENSIONS = 256


def _vector(text: str) -> list[float]:
    vector = [0.0] * DIMENSIONS
    terms = [term.casefold() for term in re.findall(r"[a-z0-9]+", text)]
    terms.extend(f"{a}_{b}" for a, b in zip(terms, terms[1:]))
    for term in terms:
        index = int.from_bytes(hashlib.blake2b(term.encode(), digest_size=4).digest(), "big") % DIMENSIONS
        vector[index] += 1.0
    magnitude = math.sqrt(sum(value * value for value in vector))
    return [value / magnitude for value in vector] if magnitude else vector


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("""CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
        input_text TEXT NOT NULL, output_json TEXT NOT NULL, vector_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS records_owner_created ON records(owner_id, created_at)")
    return connection


def save_record(owner_id: str, kind: str, input_text: str, output: dict, *, path: Path | None = None) -> str:
    """Save caller-supplied sanitized text/result and its local hashed vector."""
    record_id = uuid.uuid4().hex
    with closing(_connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record_id, owner_id, kind, input_text, json.dumps(output, ensure_ascii=False),
             json.dumps(_vector(input_text)), datetime.now(timezone.utc).isoformat()),
        )
    return record_id


def list_records(owner_id: str, *, limit: int = 50, path: Path | None = None) -> list[dict]:
    with closing(_connect(path)) as connection:
        rows = connection.execute(
            "SELECT id, kind, input_text, output_json, vector_json, created_at FROM records WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
            (owner_id, min(max(limit, 1), 100)),
        ).fetchall()
    return [{"id": row[0], "kind": row[1], "input": row[2], "result": json.loads(row[3]),
             "vector_dimensions": len(json.loads(row[4])), "created_at": row[5]} for row in rows]


def search_records(owner_id: str, query: str, *, limit: int = 10, path: Path | None = None) -> list[dict]:
    query_vector = _vector(query)
    with closing(_connect(path)) as connection:
        rows = connection.execute(
            "SELECT id, kind, input_text, output_json, vector_json, created_at FROM records WHERE owner_id=?",
            (owner_id,),
        ).fetchall()
    ranked = []
    for row in rows:
        stored_vector = json.loads(row[4])
        score = sum(left * right for left, right in zip(query_vector, stored_vector))
        if score > 0:
            ranked.append((score, row))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [{"id": row[0], "kind": row[1], "input": row[2], "result": json.loads(row[3]),
             "score": round(score, 4), "created_at": row[5]} for score, row in ranked[:min(max(limit, 1), 100)]]


def record_count(*, path: Path | None = None) -> int:
    with closing(_connect(path)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
