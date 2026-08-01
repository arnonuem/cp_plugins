"""Identity storage — ``(channel, external_id) -> principal`` and roles.

Two tables, deliberately small:

* ``bindings`` maps a *channel-specific* address to a channel-independent
  principal.  The key is the PAIR, never the bare Discord user id: the same
  human reaches Code Puppy from the CLI today and from Discord tomorrow, and a
  channel-specific identity means N unconnected permission lists that can only
  say "this Discord id may" instead of "Wayne may".  It is the one part of this
  layer that cannot be retrofitted cheaply — later it would be a data migration
  across live permission data.
* ``principal_roles`` holds the two INDEPENDENT axes.  ``TALKER`` ("may talk to
  the bot") is never allowed to imply ``APPROVER`` ("may release ``rm -rf``");
  see ``authz`` and the negative proof in AC-18.

The database lives under ``~/.code_puppy/discord/authz.db`` and explicitly NOT
inside the plugin directory: project plugins are trusted via a SHA-256 over
their directory (CONTRIBUTING.md), so a runtime database next to the code would
change that hash on every write and force re-approval on every start.

It is created private (``0o700`` directory, ``0o600`` file), matching the rest
of the project (``config.py:290``, ``mcp_/registry.py:45``).  Not cosmetic:
write access to this file IS a complete authorization bypass — a row in
``principal_roles`` is an approver.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, Set

DB_PATH_ENV = "CODE_PUPPY_DISCORD_AUTHZ_DB"
"""Overrides the database location (tests, alternate deployments)."""


class Role(str, Enum):
    """The two independent permission axes.

    Deliberately NOT a hierarchy: ``APPROVER`` does not contain ``TALKER`` and
    ``TALKER`` does not imply ``APPROVER``.  Any ordering between them would be
    the derivation AC-18 forbids, expressed as a type.
    """

    TALKER = "talker"
    APPROVER = "approver"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bindings (
    channel      TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    principal    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (channel, external_id)
);

CREATE INDEX IF NOT EXISTS idx_bindings_principal ON bindings(principal);

CREATE TABLE IF NOT EXISTS principal_roles (
    principal    TEXT NOT NULL,
    role         TEXT NOT NULL,
    granted_at   TEXT NOT NULL,
    PRIMARY KEY (principal, role)
);
"""

# ``busy_timeout`` first: switching to WAL takes a brief exclusive lock, and a
# concurrent first write would otherwise fail immediately instead of waiting.
PRAGMAS = (
    "PRAGMA busy_timeout=5000",
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
)

DIR_MODE = 0o700
"""Owner-only directory, as everywhere else in the project."""

FILE_MODE = 0o600
"""Owner-only database.  Write access here is a full authorization bypass."""

_INIT_GUARD = threading.Lock()
_INITIALIZED: Set[Path] = set()


class BindingError(RuntimeError):
    """Raised when identity data is malformed or unusable."""


def db_path() -> Path:
    """Location of the authorization database."""
    override = os.environ.get(DB_PATH_ENV)
    if override:
        return Path(override)
    return Path.home() / ".code_puppy" / "discord" / "authz.db"


def forget_initialized_paths() -> None:
    """Drop the "schema already created" memo (used when the path changes)."""
    with _INIT_GUARD:
        _INITIALIZED.clear()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_schema(path: Path, conn: sqlite3.Connection) -> None:
    with _INIT_GUARD:
        if path in _INITIALIZED:
            return
        conn.executescript(SCHEMA_SQL)
        _INITIALIZED.add(path)


def _restrict(path: Path, mode: int) -> None:
    """Best-effort ``chmod``.  A filesystem that cannot express *mode* (or a
    file another owner created) must not take the bot down — the permission is
    hardening, not a precondition for correctness.
    """
    try:
        os.chmod(path, mode)
    except OSError:
        # Windows ignores most POSIX bits, and some network mounts refuse the
        # call outright.  Nothing here changes what the database contains.
        pass


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield a short-lived connection with the schema in place.

    Short-lived by design — open, work, close.  WAL makes many readers plus one
    writer safe, including across processes.
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    # ``mkdir`` only applies its mode when it actually creates the directory,
    # so tighten unconditionally: an existing world-readable directory is
    # exactly the case worth fixing.
    _restrict(path.parent, DIR_MODE)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        for pragma in PRAGMAS:
            conn.execute(pragma)
        # After the PRAGMAs, so WAL's sidecars exist and are covered too: they
        # hold committed rows and are just as sensitive as the database.
        for target in (
            path,
            *(path.with_name(path.name + s) for s in ("-wal", "-shm")),
        ):
            if target.exists():
                _restrict(target, FILE_MODE)
        _ensure_schema(path, conn)
        yield conn
    finally:
        conn.close()


def _require(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BindingError(f"{field} must be a non-empty string, got {value!r}")
    return value.strip()


# --------------------------------------------------------------------------- #
# Bindings
# --------------------------------------------------------------------------- #


def bind(channel: str, external_id: str, principal: str) -> None:
    """Point ``(channel, external_id)`` at *principal*, replacing any previous.

    Binding is IDENTITY only — it grants nothing on its own.  Permissions come
    from :func:`grant`.
    """
    channel = _require(channel, "channel")
    external_id = _require(external_id, "external_id")
    principal = _require(principal, "principal")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO bindings(channel, external_id, principal, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(channel, external_id) DO UPDATE SET principal = excluded.principal",
            (channel, external_id, principal, _now_iso()),
        )


def unbind(channel: str, external_id: str) -> bool:
    """Remove a binding.  Returns ``True`` when a row was actually deleted."""
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM bindings WHERE channel = ? AND external_id = ?",
            (channel, external_id),
        )
        return cursor.rowcount > 0


def resolve_principal(channel: str, external_id: str) -> str | None:
    """The principal behind a channel address, or ``None`` if unknown.

    ``None`` is the fail-closed answer every caller must treat as "stranger"
    (INV-3).
    """
    if not channel or not external_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT principal FROM bindings WHERE channel = ? AND external_id = ?",
            (str(channel), str(external_id)),
        ).fetchone()
    return row["principal"] if row else None


def identities_of(principal: str) -> list[tuple[str, str]]:
    """Every ``(channel, external_id)`` pair bound to *principal*."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT channel, external_id FROM bindings WHERE principal = ? "
            "ORDER BY channel, external_id",
            (principal,),
        ).fetchall()
    return [(row["channel"], row["external_id"]) for row in rows]


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #


def grant(principal: str, role: Role) -> None:
    """Give *principal* a role.  Idempotent."""
    principal = _require(principal, "principal")
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO principal_roles(principal, role, granted_at) "
            "VALUES (?, ?, ?)",
            (principal, Role(role).value, _now_iso()),
        )


def revoke(principal: str, role: Role) -> bool:
    """Take a role away.  Returns ``True`` when the principal actually had it."""
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM principal_roles WHERE principal = ? AND role = ?",
            (principal, Role(role).value),
        )
        return cursor.rowcount > 0


def has_role(principal: str | None, role: Role) -> bool:
    """Whether *principal* holds *role*.  ``None`` never holds anything."""
    if not principal:
        return False
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM principal_roles WHERE principal = ? AND role = ?",
            (principal, Role(role).value),
        ).fetchone()
    return row is not None


def roles_of(principal: str | None) -> Set[Role]:
    """All roles held by *principal*."""
    if not principal:
        return set()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role FROM principal_roles WHERE principal = ?", (principal,)
        ).fetchall()
    return {Role(row["role"]) for row in rows}


def principals_with_role(role: Role) -> Set[str]:
    """Every principal holding *role* — the basis for config reconciliation."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT principal FROM principal_roles WHERE role = ?",
            (Role(role).value,),
        ).fetchall()
    return {row["principal"] for row in rows}
