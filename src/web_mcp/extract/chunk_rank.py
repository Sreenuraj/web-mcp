"""Split a document into passages, rank with BM25 against a focus query and pack within a token budget."""
from __future__ import annotations

import math
import re
from collections import Counter

from ..textutil import count_tokens, terms

GAP = "[…]"


def chunk(text: str, target_tokens: int = 350) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for p in paras:
        pt = count_tokens(p)
        if pt > target_tokens * 1.6:  # split giant paragraphs by sentence
            sentences = re.split(r"(?<=[.!?])\s+", p)
            for s in sentences:
                st = count_tokens(s)
                if cur_tok + st > target_tokens and cur:
                    chunks.append("\n".join(cur))
                    cur, cur_tok = [], 0
                cur.append(s)
                cur_tok += st
            continue
        if cur_tok + pt > target_tokens and cur:
            chunks.append("\n".join(cur))
            cur, cur_tok = [], 0
        cur.append(p)
        cur_tok += pt
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def bm25_scores(chunks: list[str], query_terms: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    docs = [Counter(terms(c)) for c in chunks]
    lens = [sum(d.values()) or 1 for d in docs]
    avg = sum(lens) / max(len(lens), 1)
    n = len(docs)
    qs = set(query_terms)
    df = {t: sum(1 for d in docs if t in d) for t in qs}
    scores = []
    for d, ln in zip(docs, lens):
        s = 0.0
        for t in qs:
            f = d.get(t, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * ln / avg))
        scores.append(s)
    return scores


def select(text: str, focus_terms: list[str], max_tokens: int) -> tuple[list[str], bool]:
    """Return (passages in original order incl. GAP markers, truncated?)."""
    chunks = chunk(text)
    if not chunks:
        return [], False
    total = sum(count_tokens(c) for c in chunks)
    if total <= max_tokens:
        return chunks, False
    scores = bm25_scores(chunks, focus_terms) if focus_terms else [0.0] * len(chunks)
    # always keep the lead chunk (usually the abstract / intro) unless nothing else fits
    order = sorted(range(len(chunks)), key=lambda i: (-(scores[i] + (0.01 if i == 0 else 0)), i))
    picked: list[int] = []
    used = 0
    for i in order:
        ct = count_tokens(chunks[i])
        if used + ct > max_tokens:
            if not picked:  # the best chunk alone is over budget -> hard-trim it
                trimmed = chunks[i][: max_tokens * 4]
                chunks[i] = trimmed.rsplit(" ", 1)[0] + " …"
                picked.append(i)
                used = max_tokens
            continue
        picked.append(i)
        used += ct
        if used >= max_tokens * 0.95:
            break
    picked.sort()
    out: list[str] = []
    prev = -1
    for i in picked:
        if prev != -1 and i != prev + 1:
            out.append(GAP)
        elif prev == -1 and i != 0:
            out.append(GAP)
        out.append(chunks[i])
        prev = i
    if prev != len(chunks) - 1:
        out.append(GAP)
    return out, True
