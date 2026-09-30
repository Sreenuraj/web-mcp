"""Provider protocol, shared result type and API-key lookup (env -> OS keychain; never a config file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

KEYRING_SERVICE = "web-mcp"

KEY_ENV = {"brave": "BRAVE_API_KEY", "tavily": "TAVILY_API_KEY", "serper": "SERPER_API_KEY"}


class ProviderError(Exception):
    """Provider failed (network, auth, rate limit, parse). Chain will try the next provider."""

    def __init__(self, msg: str, *, permanent: bool = False):
        super().__init__(msg)
        self.permanent = permanent  # e.g. missing key / bad auth: skip for the rest of the process


@dataclass
class RawResult:
    title: str
    url: str
    snippet: str = ""
    published: str | None = None  # ISO date if known
    source: str = ""  # provider name


class SearchProvider(Protocol):
    name: str
    supports_freshness: bool

    async def search(self, query: str, freshness: str, max_results: int, site: str | None) -> list[RawResult]: ...

    async def aclose(self) -> None: ...


def get_api_key(provider: str) -> str | None:
    env = KEY_ENV.get(provider)
    if env and os.environ.get(env):
        return os.environ[env].strip()
    try:
        import keyring  # type: ignore

        v = keyring.get_password(KEYRING_SERVICE, provider)
        return v.strip() if v else None
    except Exception:
        return None


def with_site(query: str, site: str | None) -> str:
    return f"{query} site:{site}" if site else query
