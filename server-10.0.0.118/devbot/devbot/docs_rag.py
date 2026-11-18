"""Document chunking, embedding (Ollama nomic-embed-text), and pgvector retrieval."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database
    from .llm import LLM

log = logging.getLogger(__name__)

# Official plain-text / llms.txt documentation endpoints for common frameworks
@dataclass(frozen=True)
class DocSource:
    url: str
    version_label: str  # Honest source label; these endpoints track current/stable docs.


KNOWN_DOC_SOURCES: dict[str, DocSource] = {
    "next": DocSource("https://nextjs.org/docs/llms.txt", "current upstream"),
    "react": DocSource("https://react.dev/llms.txt", "current upstream"),
    "tailwindcss": DocSource("https://tailwindcss.com/docs/llms.txt", "current upstream"),
    "prisma": DocSource("https://www.prisma.io/docs/llms.txt", "current upstream"),
    "discord.py": DocSource("https://discordpy.readthedocs.io/en/stable/api.html", "stable upstream"),
}


def _strip_html(raw: str) -> str:
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def doc_collection_key(pkg: str, version: str = "", *, versioned: bool = False) -> str:
    clean_pkg = re.sub(r"[^a-z0-9._-]+", "-", pkg.lower()).strip("-")
    major = re.sub(r"^[^0-9]*", "", version).split(".")[0]
    return f"{clean_pkg}-v{major}" if versioned and major else f"{clean_pkg}-current"


def chunk_text(text: str, chunk_size: int = 1400, overlap: int = 200) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph / line boundaries."""
    cleaned = text.replace("\r\n", "\n").strip()
    if not cleaned:
        return []
    if len(cleaned) <= chunk_size:
        return [cleaned]

    chunks: list[str] = []
    start = 0
    n = len(cleaned)
    while start < n:
        end = min(start + chunk_size, n)
        if end < n:
            # Prefer splitting at a paragraph or line boundary in the back half of the window
            half = start + (chunk_size // 2)
            para_break = cleaned.rfind("\n\n", half, end)
            line_break = cleaned.rfind("\n", half, end)
            if para_break != -1:
                end = para_break + 2
            elif line_break != -1:
                end = line_break + 1
        piece = cleaned[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(start + 1, end - overlap)
    return chunks


class DocsRAG:
    def __init__(self, db: Database, llm: LLM) -> None:
        self.db = db
        self.llm = llm

    async def ingest(self, doc_name: str, text: str, batch_size: int = 16) -> int:
        """Chunk text, embed all chunks in batches, and store in pgvector."""
        chunks = chunk_text(text)
        if not chunks:
            return 0
        embeddings: list[list[float]] = []
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            batch_embs = await self.llm.embed(batch)
            embeddings.extend(batch_embs)
        await self.db.replace_doc_chunks(doc_name, chunks, embeddings)
        return len(chunks)

    async def retrieve(self, doc_name: str, question: str, top_k: int = 5) -> list[tuple[int, float, str]]:
        """Return [(chunk_index, score, content), ...] for the top-k matching chunks."""
        embs = await self.llm.embed([question])
        if not embs:
            return []
        rows = await self.db.search_doc_chunks(doc_name, embs[0], top_k=top_k)
        return [(int(r["chunk_index"]), float(r["score"]), str(r["content"])) for r in rows]

    async def ingest_url(
        self, doc_name: str, url: str, *, requested_version: str = "",
        source_version: str = "unversioned", max_chars: int = 180_000,
    ) -> int:
        """Fetch a documentation URL (llms.txt, markdown, or HTML) and index it into pgvector."""
        import httpx

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ctype = (resp.headers.get("content-type") or "").lower()
            raw = resp.text
        text = _strip_html(raw) if "html" in ctype else raw
        count = await self.ingest(doc_name, text[:max_chars])
        if count:
            await self.db.upsert_doc_source(doc_name, url, requested_version, source_version)
        return count

    async def ensure_stack_docs(self, detected: list[tuple[str, str, str]]) -> list[str]:
        """Auto-fetch and cache official docs for detected frameworks if not already in pgvector."""
        existing_rows = await self.db.list_docs()
        existing = {str(r["doc_name"]) for r in existing_rows}
        ready: list[str] = []
        for _eco, pkg, ver in detected:
            base_pkg = pkg.lower().lstrip("@").split("/")[0]
            source = KNOWN_DOC_SOURCES.get(pkg.lower()) or KNOWN_DOC_SOURCES.get(base_pkg)
            if not source:
                continue
            key = doc_collection_key(base_pkg)
            if key not in existing:
                try:
                    n = await self.ingest_url(
                        key, source.url, requested_version=ver, source_version=source.version_label,
                    )
                    if n > 0:
                        existing.add(key)
                        ready.append(key)
                except Exception:
                    log.warning("Auto-ingest failed for %s (%s)", key, source.url, exc_info=True)
            else:
                ready.append(key)
        return ready

    async def retrieve_for_stack(self, doc_keys: list[str], query: str, max_chars: int = 3500) -> str:
        """Retrieve the most relevant ground-truth chunks across matched stack docs."""
        if not doc_keys:
            return ""
        embs = await self.llm.embed([query])
        if not embs:
            return ""
        hits: list[tuple[float, str, int, str]] = []
        for key in doc_keys:
            rows = await self.db.search_doc_chunks(key, embs[0], top_k=2)
            for r in rows:
                hits.append((float(r["score"]), key, int(r["chunk_index"]), str(r["content"])))
        hits.sort(reverse=True)

        parts: list[str] = []
        used = 0
        for score, key, idx, content in hits:
            if score < 0.35:
                continue
            block = f"[{key} · chunk {idx}]\n{content}"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts)
