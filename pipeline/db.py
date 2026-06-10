"""SQLite database layer for job history persistence."""
from __future__ import annotations

from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    video_filename  TEXT NOT NULL DEFAULT '',
    video_size      INTEGER NOT NULL DEFAULT 0,
    asr_vendor      TEXT NOT NULL DEFAULT '',
    source_lang     TEXT NOT NULL DEFAULT '',
    target_lang     TEXT NOT NULL DEFAULT '',
    translate       INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'queued',
    stage           TEXT NOT NULL DEFAULT '',
    message         TEXT NOT NULL DEFAULT '',
    percent         INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS job_files (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    format  TEXT NOT NULL,
    tos_key TEXT NOT NULL,
    UNIQUE(job_id, format)
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""


async def init_db(db_path: Path) -> aiosqlite.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCHEMA)
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.commit()
    return conn


async def insert_job(
    conn: aiosqlite.Connection,
    *,
    id: str,
    video_filename: str,
    video_size: int,
    asr_vendor: str,
    source_lang: str,
    target_lang: str,
    translate: bool,
) -> None:
    await conn.execute(
        """INSERT INTO jobs (id, video_filename, video_size, asr_vendor,
           source_lang, target_lang, translate, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')""",
        (id, video_filename, video_size, asr_vendor, source_lang, target_lang, int(translate)),
    )
    await conn.commit()


async def update_job_progress(
    conn: aiosqlite.Connection,
    id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    message: str | None = None,
    percent: int | None = None,
    error: str | None = ...,
) -> None:
    parts: list[str] = ["updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"]
    params: list = []
    if status is not None:
        parts.append("status = ?")
        params.append(status)
    if stage is not None:
        parts.append("stage = ?")
        params.append(stage)
    if message is not None:
        parts.append("message = ?")
        params.append(message)
    if percent is not None:
        parts.append("percent = ?")
        params.append(percent)
    if error is not ...:
        parts.append("error = ?")
        params.append(error)
    if not parts:
        return
    params.append(id)
    await conn.execute(f"UPDATE jobs SET {', '.join(parts)} WHERE id = ?", params)
    await conn.commit()


async def complete_job(
    conn: aiosqlite.Connection,
    id: str,
    file_uploads: list[tuple[str, str]],
) -> None:
    await conn.execute(
        """UPDATE jobs SET status='complete', stage='done', message='完成',
           percent=100, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
           WHERE id = ?""",
        (id,),
    )
    if file_uploads:
        await conn.executemany(
            "INSERT OR IGNORE INTO job_files (job_id, format, tos_key) VALUES (?, ?, ?)",
            [(id, fmt, key) for fmt, key in file_uploads],
        )
    await conn.commit()


async def fail_job(
    conn: aiosqlite.Connection,
    id: str,
    error: str,
) -> None:
    await conn.execute(
        """UPDATE jobs SET status='error', error=?, stage='error',
           updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?""",
        (error, id),
    )
    await conn.commit()


async def mark_interrupted_jobs(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """UPDATE jobs SET status='error', error='服务器重启，任务中断',
           stage='error',
           updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
           WHERE status IN ('queued', 'processing')"""
    )
    await conn.commit()


async def list_jobs(
    conn: aiosqlite.Connection,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    cursor = await conn.execute(
        """SELECT id, video_filename, video_size, asr_vendor,
           source_lang, target_lang, translate, status, stage,
           message, percent, error, created_at, updated_at
           FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        (limit, offset),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_job(conn: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await conn.execute(
        """SELECT id, video_filename, video_size, asr_vendor,
           source_lang, target_lang, translate, status, stage,
           message, percent, error, created_at, updated_at
           FROM jobs WHERE id = ?""",
        (id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_job_files(conn: aiosqlite.Connection, id: str) -> list[dict]:
    cursor = await conn.execute(
        "SELECT format, tos_key FROM job_files WHERE job_id = ?", (id,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def count_jobs(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("SELECT COUNT(*) FROM jobs")
    row = await cursor.fetchone()
    return row[0] if row else 0


async def cleanup_old_jobs(conn: aiosqlite.Connection, days: int = 30) -> None:
    # Get TOS keys before deleting so we can clean up objects
    cursor = await conn.execute(
        """SELECT jf.tos_key FROM job_files jf
           JOIN jobs j ON jf.job_id = j.id
           WHERE j.created_at < datetime('now', ?)""",
        (f"-{days} days",),
    )
    rows = await cursor.fetchall()
    tos_keys = [r[0] for r in rows]

    # Delete old jobs (cascades to job_files)
    await conn.execute(
        "DELETE FROM jobs WHERE created_at < datetime('now', ?)",
        (f"-{days} days",),
    )
    await conn.commit()

    # Return TOS keys for caller to delete objects
    # (Called from webapp cleanup loop which has access to TosUploader)
    return tos_keys
