"""SearXNG client for blocker research spikes (queries http://searxng:8080/search?format=json)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


class SearXNGClient:
    def __init__(self, base_url: str = "http://searxng:8080") -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def search(self, query: str, limit: int = 6) -> list[SearchHit]:
        resp = await self._http.get(
            f"{self.base_url}/search",
            params={"q": query, "format": "json"},
        )
        resp.raise_for_status()
        data = resp.json()
        hits: list[SearchHit] = []
        for item in data.get("results", [])[:limit]:
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            snippet = (item.get("content") or "").strip()
            if title and url:
                hits.append(SearchHit(title=title, url=url, snippet=snippet[:400]))
        return hits
