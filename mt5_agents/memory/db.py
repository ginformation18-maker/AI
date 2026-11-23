"""SQLite access boundary: short atomic writes, isolated connections, fail-closed errors.

Do not put MT5 calls inside DB transactions. Keep the file on local storage.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

BUSY_TIMEOUT_MS = 10_000


class PersistenceError(RuntimeError):
    """A required audit write was not durably acknowledged: block new orders."""


def database_path(db_path: str | Path) -> Path:
    return Path(db_path).expanduser().resolve()


def connect(db_path: str | Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(str(database_path(db_path)), timeout=BUSY_TIMEOUT_MS / 1000,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn
    except (sqlite3.Error, OSError) as exc:
        raise PersistenceError(f"SQLite connection unavailable; no new orders permitted: {exc}") from exc


@contextmanager
def read_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    except sqlite3.Error as exc:
        raise PersistenceError(f"SQLite read failed: {exc}") from exc
    finally:
        conn.close()


@contextmanager
def write_transaction(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE avoids read->write upgrades; no silent retries on failures."""
    conn = connect(db_path)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        except sqlite3.Error as exc:
            raise PersistenceError(f"SQLite audit commit failed; block order: {exc}") from exc
    finally:
        conn.close()
