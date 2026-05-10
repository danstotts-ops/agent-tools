"""Snowflake query tool — cloud version using snowflake-connector-python with key-pair auth.

Auth: RSA private key from SNOWFLAKE_PRIVATE_KEY env var (PEM string, may be passphrase-protected).
Other env vars:
  SNOWFLAKE_ACCOUNT      e.g. 'xy12345.us-east-2.aws'
  SNOWFLAKE_USER         e.g. 'DAN_STOTTS'
  SNOWFLAKE_ROLE         e.g. 'MARKETING_ANALYST'
  SNOWFLAKE_WAREHOUSE    e.g. 'COMPUTE_WH'
  SNOWFLAKE_DATABASE     e.g. 'GOLD'   (optional, query can fully-qualify)
  SNOWFLAKE_PRIVATE_KEY_PASSPHRASE  optional, if the key is encrypted

Connection is cached in module state and reused. The `query()` interface
matches the old CLI-based version so callers don't change.
"""

from __future__ import annotations

import os
import threading

import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

_LOCK = threading.Lock()
_CONN: snowflake.connector.SnowflakeConnection | None = None


def _load_private_key() -> bytes:
    """Load PEM private key from env, return DER bytes for snowflake-connector-python."""
    pem = os.environ.get("SNOWFLAKE_PRIVATE_KEY")
    if not pem:
        raise RuntimeError("SNOWFLAKE_PRIVATE_KEY not set")
    # Allow keys with literal newlines OR `\n` escapes
    pem_bytes = pem.replace("\\n", "\n").encode("utf-8")
    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    pk = serialization.load_pem_private_key(
        pem_bytes,
        password=passphrase.encode("utf-8") if passphrase else None,
        backend=default_backend(),
    )
    return pk.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _connect() -> snowflake.connector.SnowflakeConnection:
    global _CONN
    with _LOCK:
        if _CONN is not None:
            try:
                with _CONN.cursor() as cur:
                    cur.execute("SELECT 1")
                return _CONN
            except Exception:
                try:
                    _CONN.close()
                except Exception:
                    pass
                _CONN = None
        kwargs = {
            "account": os.environ["SNOWFLAKE_ACCOUNT"],
            "user": os.environ["SNOWFLAKE_USER"],
            "private_key": _load_private_key(),
            "role": os.environ.get("SNOWFLAKE_ROLE"),
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE"),
            "database": os.environ.get("SNOWFLAKE_DATABASE"),
            "schema": os.environ.get("SNOWFLAKE_SCHEMA"),
            "client_session_keep_alive": True,
        }
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        _CONN = snowflake.connector.connect(**kwargs)
        return _CONN


def query_file(sql_path, max_rows: int = 1000) -> list[dict]:
    """Load a .sql file and execute it. Returns rows as a list of dicts.

    Convenience wrapper for cron scripts that store their SQL in adjacent files.
    Strips Jinja-style block comments and executes as a single statement.
    """
    from pathlib import Path
    sql = Path(sql_path).read_text()
    result = query(sql, max_rows=max_rows)
    if not result.get("ok"):
        import sys
        sys.stderr.write(f"SQL failed for {sql_path}: {result.get('error')}\n")
        return []
    return result.get("rows", [])


def query(sql: str, max_rows: int = 100) -> dict:
    """Execute a SELECT/SHOW. Returns {ok, rows, num_rows, columns, error}.

    Interface matches the old CLI-backed version so callers don't change.
    """
    if not sql or not sql.strip():
        return {"ok": False, "error": "Empty SQL"}
    try:
        conn = _connect()
        with conn.cursor(snowflake.connector.DictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchmany(max_rows)
            cols = [d[0] for d in (cur.description or [])]
            rows = [_coerce(r) for r in rows]
            return {
                "ok": True,
                "rows": rows,
                "num_rows": len(rows),
                "columns": cols,
            }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:1500]}"}


def _coerce(row: dict) -> dict:
    """Make a row JSON-serializable (Decimal -> float, datetime -> ISO)."""
    from datetime import date, datetime
    from decimal import Decimal

    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        elif isinstance(v, bytes):
            out[k] = v.decode("utf-8", "replace")
        else:
            out[k] = v
    return out
