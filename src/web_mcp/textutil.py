"""Small text / URL helpers shared across modules."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STOPWORDS = frozenset(
    """a an and are as at be but by can could do does for from had has have how i if in into is it its
    just me more most my no not of on or our out so than that the their them then there these they this
    to up us was we were what when where which who why will with would you your about after all also any
    been being both did each few get got here his her him however know like make many much new now off
    only other over own same should since some such take tell too use used using very via vs way well
    latest current recent best top""".split()
)

_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|dclid|msclkid|yclid|mc_cid|mc_eid|igshid|ref|ref_src|_hsenc|_hsmi|cmpid|spm)$", re.I)
_WORD = re.compile(r"[a-z0-9][a-z0-9_.+#]*", re.I)


def stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s", "ly"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def terms(text: str, *, keep_numbers: bool = True) -> list[str]:
    out = []
    for m in _WORD.finditer(text.lower()):
        w = m.group(0).strip(".-_+")
        if not w or w in STOPWORDS:
            continue
        if not keep_numbers and w.replace(".", "").isdigit():
            continue
        out.append(stem(w))
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def canonical_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = p.port
    netloc = host if (port is None or (scheme, port) in {("http", 80), ("https", 443)}) else f"{host}:{port}"
    if ":" in host:  # ipv6 literal
        netloc = f"[{host}]" + (f":{port}" if port and (scheme, port) not in {("http", 80), ("https", 443)} else "")
    q = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k))
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((scheme, netloc, path, urlencode(q), ""))


def host_matches(host: str, pattern: str) -> bool:
    """`example.com` and `*.example.com` both match example.com and its subdomains; `*.gov` matches any .gov host."""
    host = host.lower().rstrip(".")
    p = pattern.lower().strip()
    if p.startswith("*."):
        p = p[2:]
    elif p.startswith("."):
        p = p[1:]
    if not p:
        return False
    return host == p or host.endswith("." + p)


def matches_any(host: str, patterns: list[str]) -> bool:
    return any(host_matches(host, p) for p in patterns)


_REL = re.compile(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago", re.I)


def parse_date(value: str | None) -> datetime | None:
    """Tolerant date parser: ISO-8601, RFC-2822, 'Sep 2, 2026', '3 days ago'. Returns aware UTC datetime."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = _REL.search(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}[unit] * n
        return datetime.now(timezone.utc) - timedelta(days=days)
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime

    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def iso_date(value: str | None) -> str | None:
    d = parse_date(value)
    return d.date().isoformat() if d else None


FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 31, "year": 366}

_tiktoken = None
_tiktoken_tried = False


def count_tokens(text: str) -> int:
    global _tiktoken, _tiktoken_tried
    if not _tiktoken_tried:
        _tiktoken_tried = True
        try:
            import tiktoken  # type: ignore

            _tiktoken = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _tiktoken = None
    if _tiktoken is not None:
        try:
            return len(_tiktoken.encode(text, disallowed_special=()))
        except Exception:
            pass
    return len(text) // 4 + 1


def fmt_k(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)
