"""Topic-drift scoring and near-duplicate query detection (pure lexical, no ML)."""
from __future__ import annotations

from ..textutil import jaccard, terms


def build_profile(topic: str, objective: str, sub_questions: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for text in (topic, objective, *sub_questions):
        for t in terms(text):
            seen.setdefault(t, None)
    return list(seen)


def relevance(query: str, profile: list[str], learned: list[str] | None = None) -> float:
    """Fraction of the query's meaningful terms that appear in the session vocabulary.

    `learned` are terms from titles of pages the agent already read, so follow-up queries that use
    newly discovered names (a library, a person, a product) are not treated as drift.
    """
    q = set(terms(query))
    if not q:
        return 1.0  # nothing to judge
    vocab = set(profile) | set(learned or [])
    hits = 0
    for t in q:
        if t in vocab or any(len(t) > 4 and (t in v or v in t) and len(v) > 4 for v in vocab):
            hits += 1
    return hits / len(q)


def find_duplicate(query: str, previous: list[dict], freshness: str | None, site: str | None,
                   threshold: float = 0.8) -> dict | None:
    q = set(terms(query))
    if not q:
        return None
    for prev in previous:
        if (prev.get("freshness") or None) != (freshness or None) or (prev.get("site") or None) != (site or None):
            continue
        if jaccard(q, set(prev.get("terms", []))) >= threshold:
            return prev
    return None
