"""Fallback chain with per-provider circuit breaker and spacing."""
from __future__ import annotations

import asyncio
import time

from ..config import ProvidersCfg
from .base import ProviderError, RawResult, SearchProvider
from .duckduckgo import DuckDuckGoProvider
from .keyed import BraveProvider, SerperProvider, TavilyProvider
from .searxng import SearxngProvider


def build_providers(cfg: ProvidersCfg) -> list[SearchProvider]:
    factories = {
        "searxng": lambda: SearxngProvider(cfg.searxng_url, cfg.timeout_seconds),
        "duckduckgo": lambda: DuckDuckGoProvider(cfg.timeout_seconds),
        "brave": lambda: BraveProvider(cfg.timeout_seconds),
        "tavily": lambda: TavilyProvider(cfg.timeout_seconds),
        "serper": lambda: SerperProvider(cfg.timeout_seconds),
    }
    out = []
    for name in cfg.chain:
        if name in factories:
            out.append(factories[name]())
    return out


class ProviderChain:
    def __init__(self, providers: list[SearchProvider], breaker_seconds: int = 60, min_interval: float = 0.5):
        self.providers = providers
        self.breaker_seconds = breaker_seconds
        self.min_interval = min_interval
        self._open_until: dict[str, float] = {}
        self._dead: set[str] = set()
        self._last_call: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def search(self, query: str, freshness: str, max_results: int, site: str | None
                     ) -> tuple[list[RawResult], str, list[str]]:
        """Returns (results, provider_name, notes). Raises ProviderError if every provider failed."""
        errors: list[str] = []
        empty_from: list[str] = []
        for p in self.providers:
            if p.name in self._dead:
                continue
            if self._open_until.get(p.name, 0) > time.time():
                continue
            async with self._lock:  # space calls out to be polite to upstream engines
                wait = self._last_call.get(p.name, 0) + self.min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call[p.name] = time.monotonic()
            try:
                res = await p.search(query, freshness, max_results, site)
            except ProviderError as e:
                errors.append(f"{p.name}: {e}")
                if e.permanent:
                    self._dead.add(p.name)
                else:
                    self._open_until[p.name] = time.time() + self.breaker_seconds
                continue
            except Exception as e:  # never let a provider bug kill the tool call
                errors.append(f"{p.name}: {type(e).__name__}")
                self._open_until[p.name] = time.time() + self.breaker_seconds
                continue
            if res:
                return res, p.name, [f"{n} unavailable" for n in [e.split(':')[0] for e in errors]] if errors else []
            empty_from.append(p.name)
        if empty_from and not errors:
            return [], empty_from[0], []
        if empty_from:
            return [], empty_from[0], [f"errors: {'; '.join(errors)}"]
        if not self.providers or all(p.name in self._dead for p in self.providers):
            errors.append("no usable search provider (run `web-mcp doctor`)")
        raise ProviderError("; ".join(errors) or "all providers are temporarily disabled; retry in a minute")

    def status(self) -> dict[str, str]:
        now = time.time()
        return {p.name: ("disabled" if p.name in self._dead else "cooling-down" if self._open_until.get(p.name, 0) > now else "ok")
                for p in self.providers}

    async def aclose(self):
        for p in self.providers:
            try:
                await p.aclose()
            except Exception:
                pass
