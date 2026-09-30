from __future__ import annotations

import httpx

from ..textutil import iso_date
from .base import ProviderError, RawResult, with_site

_TIME_RANGE = {"day": "day", "week": "week", "month": "month", "year": "year"}


class SearxngProvider:
    name = "searxng"
    supports_freshness = True

    def __init__(self, base_url: str, timeout: float = 12.0):
        self.base = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout, trust_env=False, headers={"User-Agent": "web-mcp"})

    async def search(self, query, freshness, max_results, site):
        params = {"q": with_site(query, site), "format": "json", "safesearch": 0}
        if freshness in _TIME_RANGE:
            params["time_range"] = _TIME_RANGE[freshness]
        try:
            r = await self.client.get(f"{self.base}/search", params=params)
        except httpx.ConnectError as e:
            raise ProviderError(f"SearXNG not reachable at {self.base}") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"SearXNG error: {type(e).__name__}") from e
        if r.status_code == 403:
            raise ProviderError("SearXNG rejected JSON format (enable `json` under search.formats in settings.yml)", permanent=True)
        if r.status_code == 429:
            raise ProviderError("SearXNG rate limited")
        if r.status_code >= 400:
            raise ProviderError(f"SearXNG HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise ProviderError("SearXNG returned non-JSON") from e
        out = []
        for it in data.get("results", []):
            url = it.get("url")
            if not url:
                continue
            out.append(RawResult(
                title=(it.get("title") or url).strip(), url=url, snippet=(it.get("content") or "").strip(),
                published=iso_date(it.get("publishedDate") or it.get("published_date")), source=self.name,
            ))
            if len(out) >= max_results * 2:
                break
        return out

    async def aclose(self):
        await self.client.aclose()
