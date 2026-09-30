"""Research sessions: plan, budget accounting, result registry. Persisted to SQLite so they survive restarts."""
from __future__ import annotations

import math
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

from .config import BudgetPreset, Config
from .policy.relevance import build_profile
from .storage import Storage
from .textutil import canonical_url, fmt_k, terms

MAX_REGISTRY = 300
MAX_LINKS = 400
MAX_QUERIES = 60


def today_str() -> str:
    return date.today().isoformat()


@dataclass
class Limits:
    searches: int
    fetches: int
    output_tokens: int
    seconds: int

    @classmethod
    def from_preset(cls, p: BudgetPreset) -> Limits:
        return cls(p.searches, p.fetches, p.output_tokens, p.minutes * 60)


@dataclass
class Session:
    id: str
    topic: str
    objective: str
    sub_questions: list[str]
    depth: str
    freshness: str
    created: float
    last_active: float
    limits: Limits
    base: Limits  # original preset, used to size extensions
    used_searches: int = 0
    used_fetches: int = 0
    used_tokens: int = 0
    extensions: int = 0
    unlimited: bool = False  # user chose "allow for the rest of this session"
    denials: int = 0
    last_denial: float = 0.0
    ended: bool = False
    end_reason: str = ""
    profile: list[str] = field(default_factory=list)
    learned: list[str] = field(default_factory=list)
    queries: list[dict] = field(default_factory=list)  # {q, terms, freshness, site, ids}
    registry: dict[str, dict] = field(default_factory=dict)  # "S1" -> {title,url,snippet,published,source}
    next_id: int = 1
    seen: dict[str, str] = field(default_factory=dict)  # canonical url -> id
    fetched: list[str] = field(default_factory=list)  # canonical urls fetched
    linked: list[str] = field(default_factory=list)  # canonical urls found on fetched pages
    approved_urls: list[str] = field(default_factory=list)
    approved_kinds: list[str] = field(default_factory=list)  # e.g. "drift" approved for whole session

    # ---- budget --------------------------------------------------------------------------
    def elapsed(self, now: float | None = None) -> float:
        return (now or time.time()) - self.created

    def remaining_tokens(self) -> int:
        return max(self.limits.output_tokens - self.used_tokens, 0)

    def exhausted(self, kind: str, now: float | None = None) -> str | None:
        """Return the name of the limit that blocks the given action ('search'|'fetch'), or None."""
        if self.unlimited:
            return None
        if self.elapsed(now) >= self.limits.seconds:
            return "time"
        if kind == "search" and self.used_searches >= self.limits.searches:
            return "searches"
        if kind == "fetch" and self.used_fetches >= self.limits.fetches:
            return "fetches"
        if self.remaining_tokens() < 200:
            return "tokens"
        return None

    def extend(self, factor: float) -> None:
        self.limits.searches += max(1, math.ceil(self.base.searches * factor))
        self.limits.fetches += max(1, math.ceil(self.base.fetches * factor))
        self.limits.output_tokens += max(500, math.ceil(self.base.output_tokens * factor))
        self.limits.seconds += max(60, math.ceil(self.base.seconds * factor))
        self.extensions += 1

    def budget_line(self) -> str:
        mins_left = max(int((self.limits.seconds - self.elapsed()) // 60), 0)
        if self.unlimited:
            return (f"budget: session unlimited (user-approved) · used search {self.used_searches} · "
                    f"fetch {self.used_fetches} · tokens {fmt_k(self.used_tokens)}")
        return (f"budget: search {self.used_searches}/{self.limits.searches} · fetch {self.used_fetches}/{self.limits.fetches}"
                f" · tokens {fmt_k(self.used_tokens)}/{fmt_k(self.limits.output_tokens)} · {mins_left}m left")

    # ---- registry -------------------------------------------------------------------------
    def register(self, item: dict) -> str:
        """Add a search result to the registry, returning its stable ID (deduped by canonical URL)."""
        key = canonical_url(item["url"])
        if key in self.seen:
            rid = self.seen[key]
            # enrich if we now have more information
            cur = self.registry.get(rid)
            if cur is not None:
                for f in ("published", "snippet"):
                    if not cur.get(f) and item.get(f):
                        cur[f] = item[f]
            return rid
        if len(self.registry) >= MAX_REGISTRY:
            return ""
        rid = f"S{self.next_id}"
        self.next_id += 1
        self.registry[rid] = {k: item.get(k) for k in ("title", "url", "snippet", "published", "source")}
        self.seen[key] = rid
        return rid

    def url_known(self, url: str) -> bool:
        c = canonical_url(url)
        return c in self.seen or c in self.linked or c in self.approved_urls

    def add_links(self, urls: list[str]) -> None:
        have = set(self.linked)
        for u in urls:
            c = canonical_url(u)
            if c not in have and len(self.linked) < MAX_LINKS:
                self.linked.append(c)
                have.add(c)

    def learn(self, title: str) -> None:
        have = set(self.profile) | set(self.learned)
        for t in terms(title):
            if t not in have and len(self.learned) < 200:
                self.learned.append(t)
                have.add(t)

    # ---- persistence ----------------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Session:
        d = dict(d)
        d["limits"] = Limits(**d["limits"])
        d["base"] = Limits(**d["base"])
        return cls(**d)


class SessionStore:
    def __init__(self, cfg: Config, storage: Storage):
        self.cfg = cfg
        self.storage = storage
        self._mem: dict[str, Session] = {}

    def create(self, topic: str, objective: str, sub_questions: list[str], depth: str, freshness: str) -> Session:
        preset: BudgetPreset = getattr(self.cfg.budget, depth)
        now = time.time()
        s = Session(
            id="r_" + secrets.token_hex(4),
            topic=topic, objective=objective, sub_questions=sub_questions, depth=depth, freshness=freshness,
            created=now, last_active=now,
            limits=Limits.from_preset(preset), base=Limits.from_preset(preset),
            profile=build_profile(topic, objective, sub_questions),
        )
        self._mem[s.id] = s
        self.storage.log_session_start(now)
        self.save(s)
        return s

    def get(self, sid: str) -> Session | None:
        s = self._mem.get(sid)
        if s is None:
            d = self.storage.load_session(sid)
            if d is None:
                return None
            try:
                s = Session.from_dict(d)
            except (TypeError, KeyError):
                return None
            self._mem[sid] = s
        if time.time() - s.last_active > self.cfg.session.ttl_minutes * 60:
            s.ended, s.end_reason = True, "expired"
        return s

    def touch(self, s: Session) -> None:
        s.last_active = time.time()
        self.save(s)

    def save(self, s: Session) -> None:
        if len(s.queries) > MAX_QUERIES:
            s.queries = s.queries[-MAX_QUERIES:]
        self.storage.save_session(s.id, s.to_dict(), s.last_active, s.ended)

    def end(self, s: Session, reason: str) -> None:
        s.ended, s.end_reason = True, reason
        self.save(s)

    def active_count(self) -> int:
        return self.storage.count_active_sessions(time.time() - self.cfg.session.idle_minutes * 60)

    def started_last_hour(self) -> int:
        return self.storage.count_sessions_since(time.time() - 3600)

    def purge(self) -> None:
        self.storage.purge_sessions(time.time() - self.cfg.session.ttl_minutes * 60 * 2)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
