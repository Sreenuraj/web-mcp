"""Query guardrails: stop secrets, PII and pasted code/logs from being sent to third-party search engines."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("OpenAI/Anthropic-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe key", re.compile(r"\b[sr]k_(live|test)_[0-9a-zA-Z]{16,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("credential assignment", re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token|bearer)\s*[:=]\s*\S{6,}")),
    ("URL with credentials", re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
]
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("phone number", re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)")),
    ("local file path", re.compile(r"(?:(?<![\w/])/(?:Users|home|private|var|etc|opt|root)/[^\s]+|\b[A-Za-z]:\\(?:Users|Windows)\\[^\s]+|~/[^\s]+)")),
    ("IP address", re.compile(r"\b(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?\b")),
]
_CODE_HINTS = [
    re.compile(r"[{};]\s*$", re.M),
    re.compile(r"^\s*(def|class|import|from|function|const|let|var|public|private|#include|SELECT|INSERT|UPDATE)\b", re.M),
    re.compile(r"(?m)^\s*at [\w.$<>]+\(.*\)$"),  # stack frame
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"(?m)^\s*(ERROR|WARN|INFO|DEBUG)\b.*\d{2}:\d{2}:\d{2}"),
]


@dataclass
class LeakReport:
    secrets: list[str] = field(default_factory=list)
    pii: list[str] = field(default_factory=list)
    too_long: bool = False
    looks_like_code: bool = False


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = {c: s.count(c) for c in set(s)}
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def scan_query(query: str, max_chars: int) -> LeakReport:
    r = LeakReport()
    for name, pat in _SECRET_PATTERNS:
        if pat.search(query):
            r.secrets.append(name)
    # high-entropy opaque tokens (>=24 chars, mixed classes)
    for tok in re.findall(r"[A-Za-z0-9+/=_-]{24,}", query):
        if (
            _entropy(tok) > 3.8
            and re.search(r"[A-Za-z]", tok)
            and re.search(r"\d", tok)
            and not re.fullmatch(r"[a-z0-9-]+", tok)  # slugs like "my-long-article-name-2024"
        ):
            r.secrets.append("high-entropy token")
            break
    for name, pat in _PII_PATTERNS:
        for m in pat.finditer(query):
            if name == "phone number" and len(re.sub(r"\D", "", m.group(0))) < 10:
                continue
            r.pii.append(name)
            break
    for m in re.finditer(r"\b(?:\d[ -]?){13,19}\b", query):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            r.pii.append("payment card number")
            break
    r.too_long = len(query) > max_chars
    hits = sum(1 for p in _CODE_HINTS if p.search(query))
    r.looks_like_code = ("\n" in query.strip() and hits >= 1) or hits >= 2
    r.pii = list(dict.fromkeys(r.pii))
    r.secrets = list(dict.fromkeys(r.secrets))
    return r
