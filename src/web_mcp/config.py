"""Configuration models and loader.

Load order (later wins):
  defaults -> ~/.config/web-mcp/config.toml -> ./.web-mcp.toml -> $WEBMCP_CONFIG -> WEBMCP_<SECTION>__<KEY> env vars
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class BudgetPreset(_Base):
    searches: int = Field(8, ge=0)
    fetches: int = Field(10, ge=0)
    output_tokens: int = Field(40_000, ge=0)
    minutes: int = Field(15, ge=1)


class ProvidersCfg(_Base):
    chain: list[str] = ["searxng", "duckduckgo"]
    searxng_url: str = "http://localhost:8888"
    timeout_seconds: float = 12.0
    breaker_seconds: int = 60  # skip a failing provider for this long
    min_interval_seconds: float = 0.5  # spacing between provider calls


class BudgetCfg(_Base):
    quick: BudgetPreset = BudgetPreset(searches=3, fetches=3, output_tokens=12_000, minutes=5)
    standard: BudgetPreset = BudgetPreset(searches=8, fetches=10, output_tokens=40_000, minutes=15)
    deep: BudgetPreset = BudgetPreset(searches=20, fetches=25, output_tokens=120_000, minutes=40)


class LimitsCfg(_Base):
    daily_searches: int = 100
    daily_fetches: int = 150
    max_sessions_per_hour: int = 10
    max_concurrent_sessions: int = 3
    max_results: int = 10
    max_fetch_tokens: int = 4000
    max_query_chars: int = 300
    max_sub_questions: int = 5
    per_domain_rps: float = 1.0


class SessionCfg(_Base):
    ttl_minutes: int = 120  # session is dropped after this long, regardless of use
    idle_minutes: int = 10  # a session idle for this long no longer counts as "concurrent"


class ApprovalCfg(_Base):
    modes: list[Literal["relay", "elicitation", "deny"]] = ["relay"]
    timeout_seconds: int = 55  # elicitation only; keep below typical MCP tool-call timeouts (~60s)
    approval_ttl_seconds: int = 600  # relay: how long a pending request / recorded answer stays valid
    extend_factor: float = 0.5
    max_extensions: int = 3  # budget extensions per session; after that only "allow once" is offered
    max_denials_per_session: int = 2
    denial_cooldown_seconds: int = 60  # after a denial, don't prompt again for this long


class PolicyCfg(_Base):
    drift_action: Literal["warn", "approve", "block"] = "approve"
    drift_threshold: float = 0.15
    pii_action: Literal["warn", "block", "approve"] = "warn"
    secret_action: Literal["warn", "block"] = "block"
    allow_arbitrary_urls: bool = False
    respect_robots: bool = True
    strip_suspicious: bool = False


class DomainsCfg(_Base):
    deny: list[str] = []
    allow: list[str] = []  # non-empty => strict allowlist for fetch
    prefer: list[str] = ["docs.python.org", "github.com", "*.gov", "*.edu", "arxiv.org", "wikipedia.org"]
    require_approval: list[str] = ["x.com", "twitter.com", "facebook.com", "linkedin.com", "instagram.com"]


class FetchCfg(_Base):
    timeout_seconds: float = 15.0
    max_bytes: int = 5_000_000
    max_redirects: int = 5
    max_pdf_pages: int = 30
    allowed_ports: list[int] = [80, 443, 8080, 8443]
    user_agent: str = "web-mcp/0.1 (+local research agent)"
    unsafe_allow_private: bool = False  # DANGEROUS: disables SSRF protection (tests only)


class CacheCfg(_Base):
    enabled: bool = True
    ttl_hours: float = 24.0
    path: str = "~/.cache/web-mcp/cache.db"


class StateCfg(_Base):
    path: str = "~/.local/state/web-mcp/state.db"


class AuditCfg(_Base):
    enabled: bool = True
    path: str = "~/.local/state/web-mcp/audit.jsonl"


class Config(_Base):
    providers: ProvidersCfg = ProvidersCfg()
    budget: BudgetCfg = BudgetCfg()
    limits: LimitsCfg = LimitsCfg()
    session: SessionCfg = SessionCfg()
    approval: ApprovalCfg = ApprovalCfg()
    policy: PolicyCfg = PolicyCfg()
    domains: DomainsCfg = DomainsCfg()
    fetch: FetchCfg = FetchCfg()
    cache: CacheCfg = CacheCfg()
    state: StateCfg = StateCfg()
    audit: AuditCfg = AuditCfg()


class ConfigError(Exception):
    pass


def expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "web-mcp"


def config_paths() -> list[Path]:
    paths = [config_dir() / "config.toml", Path.cwd() / ".web-mcp.toml"]
    if os.environ.get("WEBMCP_CONFIG"):
        paths.append(expand(os.environ["WEBMCP_CONFIG"]))
    return paths


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides(env: dict[str, str]) -> dict:
    out: dict[str, Any] = {}
    for key, raw in env.items():
        if not key.startswith("WEBMCP_") or "__" not in key:
            continue
        path = key[len("WEBMCP_"):].lower().split("__")
        try:
            val: Any = json.loads(raw)
        except json.JSONDecodeError:
            val = raw
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = val
    return out


def _migrate(data: dict) -> list[str]:
    """Upgrade settings written by older versions instead of refusing to start. Returns human-readable notes."""
    notes: list[str] = []
    ap = data.get("approval")
    if isinstance(ap, dict):
        if "cli_wait_seconds" in ap:
            ap.pop("cli_wait_seconds")
            notes.append("approval.cli_wait_seconds was removed (approvals now go through the agent)")
        modes = ap.get("modes")
        if isinstance(modes, list):
            new_modes: list[str] = []
            for m in modes:
                if m in ("os_dialog", "cli"):
                    notes.append(f"approval mode '{m}' was removed; using 'relay' (the agent asks you in the chat)")
                    m = "relay"
                if m not in new_modes:
                    new_modes.append(m)
            ap["modes"] = new_modes
    return notes


def load_config(extra_paths: list[Path] | None = None, env: dict[str, str] | None = None) -> Config:
    data: dict[str, Any] = {}
    for p in (extra_paths if extra_paths is not None else config_paths()):
        if p.is_file():
            try:
                data = _deep_merge(data, tomllib.loads(p.read_text(encoding="utf-8")))
            except (tomllib.TOMLDecodeError, OSError) as e:
                raise ConfigError(f"Cannot read config {p}: {e}") from e
    data = _deep_merge(data, _env_overrides(dict(os.environ if env is None else env)))
    for note in _migrate(data):
        print(f"web-mcp: config note: {note}", file=sys.stderr)
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration: {e}") from e


CONFIG_TEMPLATE = '''\
# web-mcp configuration. Every key is optional; values shown are the defaults.
# Env override: WEBMCP_<SECTION>__<KEY>=value   e.g. WEBMCP_LIMITS__DAILY_SEARCHES=50
# API keys are NOT read from this file: use env (BRAVE_API_KEY, TAVILY_API_KEY, SERPER_API_KEY)
# or the OS keychain (`web-mcp key set brave`).

[providers]
chain = ["searxng", "duckduckgo"]     # fallback order: searxng, duckduckgo, brave, tavily, serper
searxng_url = "http://localhost:8888"
timeout_seconds = 12
breaker_seconds = 60                  # skip a failing provider for N seconds
min_interval_seconds = 0.5

[budget.quick]
searches = 3
fetches = 3
output_tokens = 12000
minutes = 5

[budget.standard]
searches = 8
fetches = 10
output_tokens = 40000
minutes = 15

[budget.deep]
searches = 20
fetches = 25
output_tokens = 120000
minutes = 40

[limits]
daily_searches = 100
daily_fetches = 150
max_sessions_per_hour = 10
max_concurrent_sessions = 3
max_results = 10
max_fetch_tokens = 4000
max_query_chars = 300
max_sub_questions = 5
per_domain_rps = 1.0

[session]
ttl_minutes = 120
idle_minutes = 10

[approval]
modes = ["relay"]             # relay = agent asks you in the chat; optional: "elicitation" (in-client prompt), "deny"
timeout_seconds = 55          # elicitation only
approval_ttl_seconds = 600    # relay: validity of a pending request / recorded answer
extend_factor = 0.5
max_extensions = 3
max_denials_per_session = 2
denial_cooldown_seconds = 60

[policy]
drift_action = "approve"      # warn | approve | block
drift_threshold = 0.15
pii_action = "warn"           # warn | block | approve
secret_action = "block"       # warn | block
allow_arbitrary_urls = false  # false: only URLs seen in results / fetched pages
respect_robots = true
strip_suspicious = false      # true: drop passages that look like prompt injection

[domains]
deny = []
allow = []                    # non-empty => strict allowlist for fetching
prefer = ["docs.python.org", "github.com", "*.gov", "*.edu", "arxiv.org", "wikipedia.org"]
require_approval = ["x.com", "twitter.com", "facebook.com", "linkedin.com", "instagram.com"]

[fetch]
timeout_seconds = 15
max_bytes = 5000000
max_redirects = 5
max_pdf_pages = 30
allowed_ports = [80, 443, 8080, 8443]
user_agent = "web-mcp/0.1 (+local research agent)"
unsafe_allow_private = false  # DANGEROUS: disables SSRF protection

[cache]
enabled = true
ttl_hours = 24
path = "~/.cache/web-mcp/cache.db"

[state]
path = "~/.local/state/web-mcp/state.db"

[audit]
enabled = true
path = "~/.local/state/web-mcp/audit.jsonl"
'''
