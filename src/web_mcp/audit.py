"""Append-only JSONL audit log. Never raises: auditing must not break a tool call."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class Audit:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.path = None

    def log(self, **fields) -> None:
        if self.path is None:
            return
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
        try:
            line = json.dumps(rec, ensure_ascii=False, default=str)
            with self._lock:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, (line + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
        except OSError:
            pass


def read_audit(path: Path, since_hours: float | None = None) -> list[dict]:
    if not path.is_file():
        return []
    cutoff = time.time() - since_hours * 3600 if since_hours else None
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cutoff is not None:
            try:
                if datetime.fromisoformat(rec["ts"]).timestamp() < cutoff:
                    continue
            except (KeyError, ValueError):
                continue
        out.append(rec)
    return out


def summarize(records: list[dict]) -> str:
    if not records:
        return "No audit records."
    from collections import Counter

    by_tool = Counter(r.get("tool", "?") for r in records)
    by_dec = Counter(r.get("decision", "?") for r in records)
    sessions = {r.get("session") for r in records if r.get("session")}
    lines = [
        f"records: {len(records)}   sessions: {len(sessions)}",
        "by tool:     " + ", ".join(f"{k}={v}" for k, v in sorted(by_tool.items())),
        "by decision: " + ", ".join(f"{k}={v}" for k, v in sorted(by_dec.items())),
        "",
        "last 15:",
    ]
    for r in records[-15:]:
        target = r.get("query") or r.get("url") or r.get("topic") or ""
        lines.append(f"  {r.get('ts','')[11:19]} {r.get('tool','?'):<14} {r.get('decision','?'):<9} {str(target)[:70]}  {r.get('reason','') or ''}")
    return "\n".join(lines)
