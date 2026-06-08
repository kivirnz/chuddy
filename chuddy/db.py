"""SQLite database layer using aiosqlite."""

import asyncio
import logging
import sqlite3
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# ── Database connection ──────────────────────────────────────────────

_db_path: Path | None = None
_db_lock = asyncio.Lock()
_pool: aiosqlite.Connection | None = None


def init_db(data_dir: Path) -> Path:
    """Initialize database path. Must be called before any DB operations."""
    global _db_path
    data_dir.mkdir(parents=True, exist_ok=True)
    _db_path = data_dir / 'chuddy.db'
    return _db_path


def get_db_path() -> Path:
    if _db_path is None:
        raise RuntimeError('Database not initialized. Call init_db() first.')
    return _db_path


async def get_connection() -> aiosqlite.Connection:
    """Get or create a shared aiosqlite connection."""
    global _pool
    if _pool is None:
        _pool = await aiosqlite.connect(
            str(get_db_path()),
            timeout=30,
        )
        await _pool.execute('PRAGMA journal_mode=WAL')
        await _pool.execute('PRAGMA busy_timeout=5000')
        await _pool.execute('PRAGMA foreign_keys=ON')
        await _pool.commit()
    return _pool


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """Context manager for DB operations."""
    conn = await get_connection()
    try:
        yield conn
    except Exception:
        await conn.rollback()
        raise


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ── Schema ───────────────────────────────────────────────────────────

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    source TEXT NOT NULL DEFAULT 'BOT',
    from_chat_id INTEGER,
    from_user_id INTEGER,
    message_id INTEGER,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    file_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    duration REAL,
    width INTEGER,
    height INTEGER,
    thumb_path TEXT,
    title TEXT,
    saved_to_storage INTEGER DEFAULT 0,
    storage_path TEXT,
    is_converted INTEGER DEFAULT 0,
    converted_filename TEXT,
    converted_file_size INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL REFERENCES files(id),
    cache_id TEXT NOT NULL,
    cache_unique_id TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    date_timestamp INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(cache_unique_id)
);

CREATE TABLE IF NOT EXISTS allowed_chats (
    chat_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS punk_chats (
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0
);
'''


async def init_schema() -> None:
    """Create tables if they don't exist."""
    async with get_db() as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()
    logger.info('Database schema initialized')


# ── Task repository ──────────────────────────────────────────────────

async def task_create(
    url: str,
    source: str = 'BOT',
    from_chat_id: int | None = None,
    from_user_id: int | None = None,
    message_id: int | None = None,
) -> str:
    task_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        await db.execute(
            'INSERT INTO tasks (id, url, status, source, from_chat_id, from_user_id, message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (task_id, url, 'PENDING', source, from_chat_id, from_user_id, message_id, now),
        )
        await db.commit()
    return task_id


async def task_set_status(task_id: str, status: str, error: str | None = None) -> None:
    async with get_db() as db:
        if error:
            await db.execute(
                'UPDATE tasks SET status=?, error=? WHERE id=?',
                (status, error, task_id),
            )
        else:
            await db.execute(
                'UPDATE tasks SET status=? WHERE id=?',
                (status, task_id),
            )
        await db.commit()


async def task_get_status(task_id: str) -> str | None:
    async with get_db() as db:
        async with db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def task_get_or_create(url: str, source: str = 'BOT', **kwargs) -> tuple[str, str]:
    """Get existing pending task for URL or create new one. Returns (task_id, status)."""
    async with get_db() as db:
        async with db.execute(
            'SELECT id, status FROM tasks WHERE url=? AND status IN (?, ?)',
            (url, 'PENDING', 'PROCESSING'),
        ) as cur:
            row = await cur.fetchone()
        if row:
            return row[0], row[1]
    task_id = await task_create(url, source, **kwargs)
    return task_id, 'PENDING'


# ── File repository ──────────────────────────────────────────────────

async def file_create(
    task_id: str,
    file_type: str,
    filename: str,
    file_size: int,
    duration: float | None = None,
    width: int | None = None,
    height: int | None = None,
    thumb_path: str | None = None,
    title: str | None = None,
) -> str:
    file_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        await db.execute(
            '''INSERT INTO files
               (id, task_id, file_type, filename, file_size, duration, width, height, thumb_path, title, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (file_id, task_id, file_type, filename, file_size, duration, width, height, thumb_path, title, now),
        )
        await db.commit()
    return file_id


async def file_update(file_id: str, **kwargs) -> None:
    if not kwargs:
        return
    cols = ', '.join(f'{k}=?' for k in kwargs)
    vals = list(kwargs.values()) + [file_id]
    async with get_db() as db:
        await db.execute(f'UPDATE files SET {cols} WHERE id=?', vals)
        await db.commit()


# ── Cache repository ─────────────────────────────────────────────────

async def cache_save(
    file_id: str,
    cache_id: str,
    cache_unique_id: str,
    file_size: int,
    date_timestamp: int,
) -> None:
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        try:
            await db.execute(
                '''INSERT INTO file_cache (file_id, cache_id, cache_unique_id, file_size, date_timestamp, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (file_id, cache_id, cache_unique_id, file_size, date_timestamp, now),
            )
            await db.commit()
        except sqlite3.IntegrityError:
            pass  # Already cached


async def cache_lookup(cache_unique_id: str) -> str | None:
    """Return Telegram file_id if cached."""
    async with get_db() as db:
        async with db.execute(
            'SELECT cache_id FROM file_cache WHERE cache_unique_id=?',
            (cache_unique_id,),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


# ── Allowed chats ────────────────────────────────────────────────────

async def allowed_chats_get_all() -> set[int]:
    async with get_db() as db:
        async with db.execute('SELECT chat_id FROM allowed_chats') as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}


async def allowed_chats_add(chat_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            'INSERT OR IGNORE INTO allowed_chats (chat_id) VALUES (?)',
            (chat_id,),
        )
        await db.commit()


async def allowed_chats_remove(chat_id: int) -> bool:
    async with get_db() as db:
        await db.execute('DELETE FROM allowed_chats WHERE chat_id=?', (chat_id,))
        await db.commit()
        return db.total_changes > 0


# ── Punk mode ────────────────────────────────────────────────────────

async def punk_get_all_enabled() -> set[int]:
    async with get_db() as db:
        async with db.execute(
            'SELECT chat_id FROM punk_chats WHERE enabled=1',
        ) as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}


async def punk_set(chat_id: int, enabled: bool) -> None:
    async with get_db() as db:
        await db.execute(
            'INSERT INTO punk_chats (chat_id, enabled) VALUES (?, ?) '
            'ON CONFLICT(chat_id) DO UPDATE SET enabled=?',
            (chat_id, int(enabled), int(enabled)),
        )
        await db.commit()


async def punk_is_enabled(chat_id: int) -> bool:
    async with get_db() as db:
        async with db.execute(
            'SELECT enabled FROM punk_chats WHERE chat_id=?',
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
    return bool(row and row[0])


# ── DB cleanup (old tasks) ──────────────────────────────────────────

async def cleanup_old_tasks(days: int = 30) -> int:
    """Delete tasks older than N days. Returns count deleted."""
    async with get_db() as db:
        async with db.execute(
            "DELETE FROM tasks WHERE created_at < date('now', ?)",
            (f'-{days} days',),
        ) as cur:
            pass
        deleted = db.total_changes
        await db.commit()
    return deleted
