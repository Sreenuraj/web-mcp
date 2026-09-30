"""Providers that need an API key: Brave, Tavily, Serper."""
from __future__ import annotations

import httpx

from ..textutil import iso_date
from .base import ProviderError, RawResult, get_api_key, with_site


class _Keyed:
    name = ""
    supports_freshness = True

    def __init__(self, timeout: float = 12.0):
        self.client = httpx.AsyncClient(timeout=timeout, trust_env=False, headers={"User-Agent": "web-mcp"})

    def _key(self) -> str:
        k = get_api_key(self.name)
        if not k:
            raise ProviderError(f"{self.name}: no API key (set env or `web-mcp key set {self.name}`)", permanent=True)
        return k

    def _check(self, r: httpx.Response):
        if r.status_code in (401, 403):
            raise ProviderError(f"{self.name}: authentication failed", permanent=True)
        if r.status_code == 429:
            raise ProviderError(f"{self.name}: rate limited / quota exceeded")
        if r.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {r.status_code}")

    async def _call(self, method: str, url: str, **kw) -> dict:
        try:
            r = await self.client.request(method, url, **kw)
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name}: {type(e).__name__}") from e
        self._check(r)
        try:
            return r.json()
        except ValueError as e:
            raise ProviderError(f"{self.name}: bad JSON") from e

    async def aclose(self):
        await self.client.aclose()


class BraveProvider(_Keyed):
    name = "brave"

    async def search(self, query, freshness, max_results, site):
        params = {"q": with_site(query, site), "count": min(max_results * 2, 20), "text_decorations": "false"}
        params["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}.get(freshness, "")
        if not params["freshness"]:
            del params["freshness"]
        data = await self._call("GET", "https://api.search.brave.com/res/v1/web/search", params=params,
                                headers={"X-Subscription-Token": self._key(), "Accept": "application/json"})
        out = []
        for it in (data.get("web") or {}).get("results", []):
            if it.get("url"):
                out.append(RawResult(it.get("title") or it["url"], it["url"], it.get("description") or "",
                                     iso_date(it.get("page_age") or it.get("age")), self.name))
        return out


class TavilyProvider(_Keyed):
    name = "tavily"

    async def search(self, query, freshness, max_results, site):
        body = {"query": query, "max_results": min(max_results * 2, 20), "search_depth": "basic",
                "include_answer": False}
        if freshness in ("day", "week", "month", "year"):
            body["time_range"] = freshness
        if site:
            body["include_domains"] = [site]
        data = await self._call("POST", "https://api.tavily.com/search", json=body,
                                headers={"Authorization": f"Bearer {self._key()}"})
        return [RawResult(it.get("title") or it["url"], it["url"], it.get("content") or "",
                          iso_date(it.get("published_date")), self.name)
                for it in data.get("results", []) if it.get("url")]


class SerperProvider(_Keyed):
    name = "serper"

    async def search(self, query, freshness, max_results, site):
        body = {"q": with_site(query, site), "num": min(max_results * 2, 20)}
        tbs = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}.get(freshness)
        if tbs:
            body["tbs"] = tbs
        data = await self._call("POST", "https://google.serper.dev/search", json=body, headers={"X-API-KEY": self._key()})
        return [RawResult(it.get("title") or it["link"], it["link"], it.get("snippet") or "", iso_date(it.get("date")), self.name)
                for it in data.get("organic", []) if it.get("link")]
