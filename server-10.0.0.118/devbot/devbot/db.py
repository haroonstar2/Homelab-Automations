"""Postgres storage (devbot database inside the existing saas-postgres with pgvector)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

import asyncpg

log = logging.getLogger(__name__)

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS threads (
    thread_id   BIGINT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- 'ask', 'note', 'doc', 'code', 'checkin', 'wrapup'
    seed        TEXT NOT NULL,          -- the message or topic that started the thread
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE threads ADD COLUMN IF NOT EXISTS meta JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS journal_entries (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL,          -- 'log', 'plan', 'wrapup', 'handoff', 'blocker'
    body        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_entries_created_at ON journal_entries (created_at);

CREATE TABLE IF NOT EXISTS doc_chunks (
    id          BIGSERIAL PRIMARY KEY,
    doc_name    TEXT NOT NULL,
    chunk_index INT NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(768) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS doc_chunks_doc_name ON doc_chunks (doc_name);
CREATE TABLE IF NOT EXISTS doc_sources (
    doc_name TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    requested_version TEXT NOT NULL DEFAULT '',
    source_version TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS code_tasks (
    thread_id   BIGINT PRIMARY KEY,
    repo        TEXT NOT NULL,
    branch      TEXT NOT NULL,
    task        TEXT NOT NULL,
    plan        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'planned',  -- 'planned', 'running', 'done', 'failed', 'cancelled'
    pr_url      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS code_subtasks (
    id              BIGSERIAL PRIMARY KEY,
    thread_id       BIGINT NOT NULL,
    sequence        INT NOT NULL,
    request         TEXT NOT NULL,
    plan            TEXT NOT NULL DEFAULT '',
    base_sha        TEXT NOT NULL DEFAULT '',
    required_paths  TEXT[] NOT NULL DEFAULT '{}',
    allowed_paths   TEXT[],
    model           TEXT NOT NULL DEFAULT 'smart',
    commit_title    TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'planned',
    verification    JSONB NOT NULL DEFAULT '{}'::jsonb,
    commit_sha      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thread_id, sequence)
);
CREATE INDEX IF NOT EXISTS code_subtasks_thread_sequence
ON code_subtasks (thread_id, sequence DESC);
"""


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


async def _ensure_database(url: str) -> None:
    """saas-postgres was initialised long ago, so create the devbot DB if missing."""
    parsed = urlparse(url)
    db_name = parsed.path.lstrip("/")
    admin_url = urlunparse(parsed._replace(path="/postgres"))
    conn = await asyncpg.connect(admin_url)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if not exists:
            log.info("creating database %s", db_name)
            await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()


class Database:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, url: str) -> "Database":
        await _ensure_database(url)
        pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    # --- threads ---------------------------------------------------------
    async def add_thread(
        self,
        thread_id: int,
        kind: str,
        seed: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        meta_json = json.dumps(meta or {})
        await self.pool.execute(
            """
            INSERT INTO threads (thread_id, kind, seed, meta)
            VALUES ($1, $2, $3, $4::jsonb)
            ON CONFLICT (thread_id) DO UPDATE SET kind = $2, seed = $3, meta = $4::jsonb
            """,
            thread_id,
            kind,
            seed,
            meta_json,
        )

    async def get_thread(self, thread_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM threads WHERE thread_id = $1", thread_id)

    # --- journal ---------------------------------------------------------
    async def add_journal_entry(self, kind: str, body: str) -> None:
        await self.pool.execute("INSERT INTO journal_entries (kind, body) VALUES ($1, $2)", kind, body)

    async def get_journal_entries_since(self, since: datetime) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM journal_entries WHERE created_at >= $1 ORDER BY created_at ASC",
            since,
        )

    async def get_latest_journal_entry(self, kind: str | None = None) -> asyncpg.Record | None:
        if kind:
            return await self.pool.fetchrow(
                "SELECT * FROM journal_entries WHERE kind = $1 ORDER BY created_at DESC LIMIT 1",
                kind,
            )
        return await self.pool.fetchrow("SELECT * FROM journal_entries ORDER BY created_at DESC LIMIT 1")

    # --- docs (pgvector) -------------------------------------------------
    async def replace_doc_chunks(
        self,
        doc_name: str,
        chunks: list[str],
        embeddings: list[list[float]],
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM doc_chunks WHERE doc_name = $1", doc_name)
                rows = [
                    (doc_name, idx, text, _vec_literal(emb))
                    for idx, (text, emb) in enumerate(zip(chunks, embeddings))
                ]
                await conn.executemany(
                    """
                    INSERT INTO doc_chunks (doc_name, chunk_index, content, embedding)
                    VALUES ($1, $2, $3, $4::vector)
                    """,
                    rows,
                )

    async def search_doc_chunks(
        self,
        doc_name: str,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT chunk_index, content, 1 - (embedding <=> $2::vector) AS score
            FROM doc_chunks
            WHERE doc_name = $1
            ORDER BY embedding <=> $2::vector ASC
            LIMIT $3
            """,
            doc_name,
            _vec_literal(query_embedding),
            top_k,
        )

    async def list_docs(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT doc_name, COUNT(*) AS chunks, MAX(created_at) AS updated_at
            FROM doc_chunks
            GROUP BY doc_name
            ORDER BY updated_at DESC
            """
        )

    async def upsert_doc_source(
        self, doc_name: str, source_url: str, requested_version: str, source_version: str,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO doc_sources (doc_name, source_url, requested_version, source_version)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (doc_name) DO UPDATE SET source_url=$2, requested_version=$3,
                source_version=$4, fetched_at=now()
            """,
            doc_name, source_url, requested_version, source_version,
        )

    # --- code tasks ------------------------------------------------------
    async def save_code_task(
        self,
        thread_id: int,
        repo: str,
        branch: str,
        task: str,
        plan: str,
        status: str = "planned",
        pr_url: str = "",
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO code_tasks (thread_id, repo, branch, task, plan, status, pr_url)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (thread_id) DO UPDATE
            SET repo = $2, branch = $3, task = $4, plan = $5, status = $6, pr_url = $7
            """,
            thread_id,
            repo,
            branch,
            task,
            plan,
            status,
            pr_url,
        )

    async def get_code_task(self, thread_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM code_tasks WHERE thread_id = $1", thread_id)

    async def recover_interrupted_code_tasks(self) -> None:
        await self.pool.execute("UPDATE code_tasks SET status = 'failed' WHERE status = 'running'")
        await self.pool.execute("UPDATE code_subtasks SET status = 'failed' WHERE status = 'running'")

    async def update_code_task_status(self, thread_id: int, status: str, pr_url: str = "") -> None:
        await self.pool.execute(
            "UPDATE code_tasks SET status = $2, pr_url = COALESCE(NULLIF($3, ''), pr_url) WHERE thread_id = $1",
            thread_id,
            status,
            pr_url,
        )

    async def create_code_subtask(
        self,
        thread_id: int,
        request: str,
        plan: str,
        *,
        required_paths: list[str] | tuple[str, ...] = (),
        allowed_paths: list[str] | tuple[str, ...] | None = None,
        model: str = "smart",
        commit_title: str = "",
        status: str = "planned",
    ) -> asyncpg.Record:
        return await self.pool.fetchrow(
            """
            INSERT INTO code_subtasks (
                thread_id, sequence, request, plan, required_paths, allowed_paths,
                model, commit_title, status
            )
            SELECT $1, COALESCE(MAX(sequence), 0) + 1, $2, $3, $4::text[], $5::text[], $6, $7, $8
            FROM code_subtasks WHERE thread_id = $1
            RETURNING *
            """,
            thread_id, request, plan, list(required_paths),
            None if allowed_paths is None else list(allowed_paths),
            model, commit_title, status,
        )

    async def get_latest_code_subtask(self, thread_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM code_subtasks WHERE thread_id = $1 ORDER BY sequence DESC LIMIT 1",
            thread_id,
        )

    async def update_code_subtask_plan(self, subtask_id: int, plan: str) -> None:
        await self.pool.execute(
            "UPDATE code_subtasks SET plan = $2 WHERE id = $1 AND status = 'planned'",
            subtask_id, plan,
        )

    async def update_code_subtask_result(
        self,
        subtask_id: int,
        status: str,
        *,
        base_sha: str = "",
        verification: dict[str, Any] | None = None,
        commit_sha: str = "",
    ) -> None:
        await self.pool.execute(
            """
            UPDATE code_subtasks
            SET status = $2,
                base_sha = COALESCE(NULLIF($3, ''), base_sha),
                verification = CASE WHEN $4::jsonb = '{}'::jsonb THEN verification ELSE $4::jsonb END,
                commit_sha = COALESCE(NULLIF($5, ''), commit_sha)
            WHERE id = $1
            """,
            subtask_id, status, base_sha, json.dumps(verification or {}), commit_sha,
        )

    async def ping(self) -> bool:
        return await self.pool.fetchval("SELECT 1") == 1
