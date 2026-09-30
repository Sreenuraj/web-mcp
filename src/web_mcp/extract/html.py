"""HTML -> clean main-content text, with hidden-content removal, published date and outgoing links."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from ..textutil import iso_date

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em|rem|%)?\s*(?:;|$)|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)"
    r"|(?:left|top|text-indent)\s*:\s*-\d{3,}|height\s*:\s*0(?:px)?\s*;[^\"]*overflow\s*:\s*hidden|clip\s*:\s*rect\(0",
    re.I,
)
_STRIP_TAGS = ("script", "style", "noscript", "template", "iframe", "object", "embed", "svg", "canvas", "form",
               "nav", "footer", "aside", "dialog")


@dataclass
class Extracted:
    title: str = ""
    text: str = ""
    published: str | None = None
    links: list[str] = field(default_factory=list)
    kind: str = "html"


def _decode(body: bytes, content_type_hint: str = "") -> str:
    m = re.search(rb"charset=['\"]?([\w-]+)", body[:2048], re.I)
    for enc in ([m.group(1).decode("ascii", "ignore")] if m else []) + ["utf-8"]:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "replace")


def _remove_hidden(html: str) -> tuple[str, str | None, str, list[str]]:
    """Strip hidden / non-content elements. Returns (clean_html, published, title, links)."""
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    published = None
    for sel, attr in (
        ('meta[property="article:published_time"]', "content"),
        ('meta[name="article:published_time"]', "content"),
        ('meta[name="date"]', "content"),
        ('meta[name="pubdate"]', "content"),
        ('meta[itemprop="datePublished"]', "content"),
        ('meta[property="og:updated_time"]', "content"),
        ("time[datetime]", "datetime"),
    ):
        node = tree.css_first(sel)
        if node is not None and node.attributes.get(attr):
            published = iso_date(node.attributes[attr])
            if published:
                break
    if not published:
        for node in tree.css('script[type="application/ld+json"]'):
            try:
                data = json.loads(node.text() or "")
            except (json.JSONDecodeError, TypeError):
                continue
            items = data if isinstance(data, list) else [data]
            for it in items:
                if isinstance(it, dict):
                    d = it.get("datePublished") or it.get("dateModified")
                    if d and iso_date(str(d)):
                        published = iso_date(str(d))
                        break
            if published:
                break
    title = ""
    t = tree.css_first("title")
    if t is not None:
        title = re.sub(r"\s+", " ", t.text() or "").strip()
    og = tree.css_first('meta[property="og:title"]')
    if og is not None and og.attributes.get("content"):
        title = og.attributes["content"].strip()

    for tag in _STRIP_TAGS:
        for n in tree.css(tag):
            n.decompose()
    for n in tree.css("[hidden], [aria-hidden='true']"):
        n.decompose()
    for n in tree.css("[style]"):
        if _HIDDEN_STYLE.search(n.attributes.get("style") or ""):
            n.decompose()
    for n in tree.css("input[type=hidden], [class*='sr-only'], [class*='visually-hidden']"):
        n.decompose()
    return tree.html or "", published, title, []


def _links(html: str, base: str, limit: int = 100) -> list[str]:
    from selectolax.parser import HTMLParser

    out: list[str] = []
    seen = set()
    for a in HTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        rel = (a.attributes.get("rel") or "").lower()
        if "nofollow" in rel and "ugc" in rel:
            continue
        try:
            u = urljoin(base, href)
        except ValueError:
            continue
        if u.startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            out.append(u)
            if len(out) >= limit:
                break
    return out


def extract_html(body: bytes, url: str) -> Extracted:
    raw = _decode(body)
    try:
        cleaned, published, title, _ = _remove_hidden(raw)
    except Exception:
        cleaned, published, title = raw, None, ""
    links = []
    try:
        links = _links(cleaned, url)
    except Exception:
        pass
    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(
            cleaned, url=url, include_comments=False, include_tables=True, include_links=False,
            favor_recall=True, output_format="txt",
        ) or ""
        if not published:
            meta = trafilatura.extract_metadata(cleaned, default_url=url)
            if meta is not None and getattr(meta, "date", None):
                published = iso_date(meta.date)
        if not title:
            meta = trafilatura.extract_metadata(cleaned, default_url=url)
            title = (getattr(meta, "title", None) or "") if meta else ""
    except Exception:
        text = ""
    if len(text.strip()) < 200:  # trafilatura found little: fall back to plain text of the cleaned DOM
        try:
            from selectolax.parser import HTMLParser

            fallback = HTMLParser(cleaned).body
            ft = fallback.text(separator="\n", strip=True) if fallback is not None else ""
            if len(ft) > len(text):
                text = ft
        except Exception:
            pass
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return Extracted(title=title, text=text, published=published, links=links, kind="html")
