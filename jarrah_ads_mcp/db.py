"""SQLite persistence for MCP OAuth state, tokens, and client registrations."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get(
    "DB_PATH",
    str(Path(__file__).parent.parent / "gads_mcp.db"),
)

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                c = sqlite3.connect(DB_PATH, check_same_thread=False)
                c.row_factory = sqlite3.Row
                # Autocommit. Token exchange opens an explicit IMMEDIATE
                # transaction so a failure cannot commit a partial swap.
                c.isolation_level = None
                c.execute("PRAGMA journal_mode=WAL")
                _conn = c
    return _conn


def _exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """Run one statement. The connection is in autocommit mode."""
    with _lock:
        conn = _get_conn()
        return conn.execute(sql, params)


@contextmanager
def _immediate_txn():
    """BEGIN IMMEDIATE ... COMMIT, rolling back every statement on failure.

    Holds the process lock for the whole transaction. Do not call ``_exec``
    inside the block.
    """
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise


def init_db() -> None:
    with _lock:
        conn = _get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id   TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                created_at  INTEGER NOT NULL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS oauth_pending (
                google_state    TEXT PRIMARY KEY,
                mcp_state       TEXT,
                client_id       TEXT NOT NULL,
                redirect_uri    TEXT NOT NULL,
                code_challenge  TEXT NOT NULL,
                scopes          TEXT NOT NULL,
                google_rt_enc   TEXT,
                expires_at      INTEGER NOT NULL,
                bing_state      TEXT,
                customer_id     TEXT,
                login_customer_id TEXT
            );

            CREATE TABLE IF NOT EXISTS oauth_codes (
                code                    TEXT PRIMARY KEY,
                client_id               TEXT NOT NULL,
                redirect_uri            TEXT NOT NULL,
                redirect_uri_explicit   INTEGER NOT NULL,
                code_challenge          TEXT NOT NULL,
                scopes                  TEXT NOT NULL,
                google_rt_enc           TEXT NOT NULL,
                customer_id             TEXT NOT NULL,
                login_customer_id       TEXT,
                resource                TEXT,
                expires_at              INTEGER NOT NULL,
                bing_rt_enc             TEXT
            );

            CREATE TABLE IF NOT EXISTS oauth_access_tokens (
                token               TEXT PRIMARY KEY,
                client_id           TEXT NOT NULL,
                scopes              TEXT NOT NULL,
                google_rt_enc       TEXT NOT NULL,
                customer_id         TEXT NOT NULL,
                login_customer_id   TEXT,
                expires_at          INTEGER,
                bing_rt_enc         TEXT
            );

            CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
                token               TEXT PRIMARY KEY,
                client_id           TEXT NOT NULL,
                scopes              TEXT NOT NULL,
                google_rt_enc       TEXT NOT NULL,
                customer_id         TEXT NOT NULL,
                login_customer_id   TEXT,
                expires_at          INTEGER,
                bing_rt_enc         TEXT,
                access_token        TEXT
            );

            CREATE TABLE IF NOT EXISTS kv (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  INTEGER NOT NULL DEFAULT (unixepoch())
            );
        """)
        conn.commit()

        # Migrations for existing DBs (each ALTER TABLE is idempotent via try/except)
        _migrations = [
            "ALTER TABLE oauth_pending ADD COLUMN bing_state TEXT",
            "ALTER TABLE oauth_pending ADD COLUMN customer_id TEXT",
            "ALTER TABLE oauth_pending ADD COLUMN login_customer_id TEXT",
            "ALTER TABLE oauth_codes ADD COLUMN bing_rt_enc TEXT",
            "ALTER TABLE oauth_access_tokens ADD COLUMN bing_rt_enc TEXT",
            "ALTER TABLE oauth_refresh_tokens ADD COLUMN bing_rt_enc TEXT",
            "ALTER TABLE oauth_refresh_tokens ADD COLUMN access_token TEXT",
        ]
        for sql in _migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        conn.commit()


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def save_client(client_id: str, data: dict) -> None:
    _exec(
        "INSERT OR REPLACE INTO oauth_clients (client_id, data) VALUES (?, ?)",
        (client_id, json.dumps(data)),
    )


def get_client(client_id: str) -> dict | None:
    row = _exec(
        "SELECT data FROM oauth_clients WHERE client_id = ?",
        (client_id,),
    ).fetchone()
    return json.loads(row["data"]) if row else None


# ---------------------------------------------------------------------------
# Pending authorization (MCP → Google OAuth leg, awaiting Google callback)
# ---------------------------------------------------------------------------

def save_pending(
    google_state: str,
    mcp_state: str | None,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    scopes: list[str],
    expires_at: int,
) -> None:
    _exec(
        """INSERT INTO oauth_pending
               (google_state, mcp_state, client_id, redirect_uri,
                code_challenge, scopes, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (google_state, mcp_state, client_id, redirect_uri,
         code_challenge, json.dumps(scopes), expires_at),
    )


def get_pending(google_state: str) -> dict | None:
    row = _exec(
        "SELECT * FROM oauth_pending WHERE google_state = ?",
        (google_state,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["scopes"] = json.loads(d["scopes"])
    return d


def update_pending_google_rt(google_state: str, google_rt_enc: str) -> None:
    _exec(
        "UPDATE oauth_pending SET google_rt_enc = ? WHERE google_state = ?",
        (google_rt_enc, google_state),
    )


def delete_pending(google_state: str) -> None:
    _exec("DELETE FROM oauth_pending WHERE google_state = ?", (google_state,))


def update_pending_bing_state(
    google_state: str,
    bing_state: str,
    customer_id: str,
    login_customer_id: str | None,
) -> None:
    _exec(
        "UPDATE oauth_pending SET bing_state = ?, customer_id = ?, login_customer_id = ? WHERE google_state = ?",
        (bing_state, customer_id, login_customer_id, google_state),
    )


def get_pending_by_bing_state(bing_state: str) -> dict | None:
    row = _exec(
        "SELECT * FROM oauth_pending WHERE bing_state = ?",
        (bing_state,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["scopes"] = json.loads(d["scopes"])
    return d


# ---------------------------------------------------------------------------
# Authorization codes
# ---------------------------------------------------------------------------

def save_code(
    code: str,
    client_id: str,
    redirect_uri: str,
    redirect_uri_explicit: bool,
    code_challenge: str,
    scopes: list[str],
    google_rt_enc: str,
    customer_id: str,
    login_customer_id: str | None,
    resource: str | None,
    expires_at: int,
    bing_rt_enc: str | None = None,
) -> None:
    _exec(
        """INSERT INTO oauth_codes
               (code, client_id, redirect_uri, redirect_uri_explicit,
                code_challenge, scopes, google_rt_enc, customer_id,
                login_customer_id, resource, expires_at, bing_rt_enc)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (code, client_id, redirect_uri, int(redirect_uri_explicit),
         code_challenge, json.dumps(scopes), google_rt_enc,
         customer_id, login_customer_id, resource, expires_at, bing_rt_enc),
    )


def get_code(code: str) -> dict | None:
    row = _exec(
        "SELECT * FROM oauth_codes WHERE code = ?",
        (code,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["scopes"] = json.loads(d["scopes"])
    d["redirect_uri_explicit"] = bool(d["redirect_uri_explicit"])
    return d


def delete_code(code: str) -> None:
    _exec("DELETE FROM oauth_codes WHERE code = ?", (code,))


def redeem_authorization_code(
    code: str,
    access_token: str,
    refresh_token: str,
    access_expires_at: int,
    refresh_expires_at: int,
) -> bool:
    """Delete an authorization code and insert the tokens it issues, atomically.

    Returns False when the code is already gone, so it can be redeemed once.
    An insert failure rolls the deletion back.
    """
    with _immediate_txn() as conn:
        row = conn.execute(
            "DELETE FROM oauth_codes WHERE code = ? RETURNING *",
            (code,),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            """INSERT INTO oauth_access_tokens
                   (token, client_id, scopes, google_rt_enc,
                    customer_id, login_customer_id, expires_at, bing_rt_enc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                access_token,
                row["client_id"],
                row["scopes"],
                row["google_rt_enc"],
                row["customer_id"],
                row["login_customer_id"],
                access_expires_at,
                row["bing_rt_enc"],
            ),
        )
        conn.execute(
            """INSERT INTO oauth_refresh_tokens
                   (token, client_id, scopes, google_rt_enc,
                    customer_id, login_customer_id, expires_at, bing_rt_enc,
                    access_token)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                refresh_token,
                row["client_id"],
                row["scopes"],
                row["google_rt_enc"],
                row["customer_id"],
                row["login_customer_id"],
                refresh_expires_at,
                row["bing_rt_enc"],
                access_token,
            ),
        )
    return True


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------

def save_access_token(
    token: str,
    client_id: str,
    scopes: list[str],
    google_rt_enc: str,
    customer_id: str,
    login_customer_id: str | None,
    expires_at: int | None,
    bing_rt_enc: str | None = None,
) -> None:
    _exec(
        """INSERT INTO oauth_access_tokens
               (token, client_id, scopes, google_rt_enc,
                customer_id, login_customer_id, expires_at, bing_rt_enc)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (token, client_id, json.dumps(scopes), google_rt_enc,
         customer_id, login_customer_id, expires_at, bing_rt_enc),
    )


def get_access_token_row(token: str) -> dict | None:
    row = _exec(
        "SELECT * FROM oauth_access_tokens WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["scopes"] = json.loads(d["scopes"])
    return d


def delete_access_token(token: str) -> None:
    """Revoke an access token and any refresh token issued with it."""
    with _immediate_txn() as conn:
        conn.execute("DELETE FROM oauth_access_tokens WHERE token = ?", (token,))
        conn.execute(
            "DELETE FROM oauth_refresh_tokens WHERE access_token = ?",
            (token,),
        )


def delete_expired_access_tokens() -> None:
    """Delete access tokens whose expiry has passed. Called at startup."""
    _exec(
        "DELETE FROM oauth_access_tokens WHERE expires_at IS NOT NULL AND expires_at < unixepoch()"
    )


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------

def save_refresh_token(
    token: str,
    client_id: str,
    scopes: list[str],
    google_rt_enc: str,
    customer_id: str,
    login_customer_id: str | None,
    expires_at: int | None,
    bing_rt_enc: str | None = None,
) -> None:
    _exec(
        """INSERT INTO oauth_refresh_tokens
               (token, client_id, scopes, google_rt_enc,
                customer_id, login_customer_id, expires_at, bing_rt_enc)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (token, client_id, json.dumps(scopes), google_rt_enc,
         customer_id, login_customer_id, expires_at, bing_rt_enc),
    )


def get_refresh_token_row(token: str) -> dict | None:
    row = _exec(
        "SELECT * FROM oauth_refresh_tokens WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["scopes"] = json.loads(d["scopes"])
    return d


def delete_refresh_token(token: str) -> None:
    """Revoke a refresh token and the access token it was issued with."""
    with _immediate_txn() as conn:
        row = conn.execute(
            "DELETE FROM oauth_refresh_tokens WHERE token = ? RETURNING access_token",
            (token,),
        ).fetchone()
        if row and row["access_token"]:
            conn.execute(
                "DELETE FROM oauth_access_tokens WHERE token = ?",
                (row["access_token"],),
            )


def rotate_refresh_token(
    old_refresh_token: str,
    new_access_token: str,
    new_refresh_token: str,
    scopes: list[str],
    access_expires_at: int,
    refresh_expires_at: int,
) -> bool:
    """Swap a refresh token for a new pair and revoke its previous access token.

    Returns False when the old refresh token was already consumed. Any failure
    after the delete rolls the whole swap back.
    """
    scope_json = json.dumps(scopes)
    with _immediate_txn() as conn:
        row = conn.execute(
            "DELETE FROM oauth_refresh_tokens WHERE token = ? RETURNING *",
            (old_refresh_token,),
        ).fetchone()
        if row is None:
            return False
        previous_access = row["access_token"]
        if previous_access:
            conn.execute(
                "DELETE FROM oauth_access_tokens WHERE token = ?",
                (previous_access,),
            )
        conn.execute(
            """INSERT INTO oauth_access_tokens
                   (token, client_id, scopes, google_rt_enc,
                    customer_id, login_customer_id, expires_at, bing_rt_enc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_access_token,
                row["client_id"],
                scope_json,
                row["google_rt_enc"],
                row["customer_id"],
                row["login_customer_id"],
                access_expires_at,
                row["bing_rt_enc"],
            ),
        )
        conn.execute(
            """INSERT INTO oauth_refresh_tokens
                   (token, client_id, scopes, google_rt_enc,
                    customer_id, login_customer_id, expires_at, bing_rt_enc,
                    access_token)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_refresh_token,
                row["client_id"],
                scope_json,
                row["google_rt_enc"],
                row["customer_id"],
                row["login_customer_id"],
                refresh_expires_at,
                row["bing_rt_enc"],
                new_access_token,
            ),
        )
    return True


def kv_get(key: str) -> str | None:
    row = _exec("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    _exec(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, unixepoch()) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value),
    )


def replace_bing_refresh_token(old_token: str, new_token: str) -> int:
    """Replace a rotated Bing refresh token on access and refresh session rows.

    Fernet ciphertext includes a random IV, so rows are decrypted and compared
    as plaintext. Returns how many rows were updated.
    """
    if not old_token or not new_token or old_token == new_token:
        return 0
    from jarrah_ads_mcp.auth_provider import decrypt, encrypt

    new_enc = encrypt(new_token)
    updated = 0
    with _immediate_txn() as conn:
        for table in ("oauth_access_tokens", "oauth_refresh_tokens"):
            rows = conn.execute(
                f"SELECT token, bing_rt_enc FROM {table} WHERE bing_rt_enc IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    current = decrypt(row["bing_rt_enc"])
                except Exception:
                    continue
                if current != old_token:
                    continue
                conn.execute(
                    f"UPDATE {table} SET bing_rt_enc = ? WHERE token = ?",
                    (new_enc, row["token"]),
                )
                updated += 1
    return updated


def update_token_account(
    access_token: str,
    customer_id: str,
    login_customer_id: str | None,
) -> str | None:
    """Update customer_id/login_customer_id on the specific access token only.

    Does NOT update refresh tokens — doing so would affect all sessions sharing the
    same client_id. The in-memory _account_overrides dict in server.py handles the
    session-level switch immediately; this DB update ensures the access token row is
    consistent. The switch persists until the access token expires (1 hour); after a
    token refresh the session returns to the originally-authenticated account.

    Returns the client_id, or None if the access token was not found.
    """
    with _immediate_txn() as conn:
        row = conn.execute(
            "SELECT client_id FROM oauth_access_tokens WHERE token = ?",
            (access_token,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE oauth_access_tokens SET customer_id = ?, login_customer_id = ? WHERE token = ?",
            (customer_id, login_customer_id, access_token),
        )
        return row["client_id"]
