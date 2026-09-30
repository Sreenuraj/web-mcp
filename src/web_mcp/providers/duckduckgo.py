from __future__ import annotations

import asyncio

from ..textutil import iso_date
from .base import ProviderError, RawResult, with_site

_TIMELIMIT = {"day": "d", "week": "w", "month": "m", "year": "y"}


class DuckDuckGoProvider:
    """Key-less fallback using the `ddgs` package (unofficial; may be rate-limited or break)."""

    name = "duckduckgo"
    supports_freshness = True

    def __init__(self, timeout: float = 12.0):
        self.timeout = timeout

    def _sync(self, query, freshness, max_results, site):
        try:
            from ddgs import DDGS
        except ImportError as e:
            raise ProviderError("`ddgs` is not installed (pip install ddgs)", permanent=True) from e
        kwargs = {"max_results": max_results * 2}
        if freshness in _TIMELIMIT:
            kwargs["timelimit"] = _TIMELIMIT[freshness]
        try:
            ddgs = DDGS(timeout=int(self.timeout))
        except TypeError:
            ddgs = DDGS()
        try:
            rows = list(ddgs.text(with_site(query, site), **kwargs))
        except Exception as e:  # ddgs raises library-specific errors
            raise ProviderError(f"DuckDuckGo error: {type(e).__name__}: {str(e)[:100]}") from e
        return rows

    async def search(self, query, freshness, max_results, site):
        try:
            rows = await asyncio.wait_for(asyncio.to_thread(self._sync, query, freshness, max_results, site), self.timeout + 5)
        except asyncio.TimeoutError as e:
            raise ProviderError("DuckDuckGo timed out") from e
        out = []
        for it in rows:
            url = it.get("href") or it.get("url")
            if not url:
                continue
            out.append(RawResult(title=(it.get("title") or url).strip(), url=url, snippet=(it.get("body") or "").strip(),
                                 published=iso_date(it.get("date")), source=self.name))
        return out

    async def aclose(self):
        return None
