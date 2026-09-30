"""Domain policy: deny / allow (strict) / prefer / require-approval."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..config import DomainsCfg
from ..textutil import domain_of, matches_any

Verdict = Literal["ok", "deny", "approve"]


@dataclass
class DomainDecision:
    verdict: Verdict
    reason: str = ""


class DomainPolicy:
    def __init__(self, cfg: DomainsCfg):
        self.cfg = cfg

    def check_fetch(self, url: str) -> DomainDecision:
        host = domain_of(url)
        if not host:
            return DomainDecision("deny", "URL has no host")
        if matches_any(host, self.cfg.deny):
            return DomainDecision("deny", f"{host} is on the deny list")
        if self.cfg.allow and not matches_any(host, self.cfg.allow):
            return DomainDecision("deny", f"{host} is not on the strict allow list")
        if matches_any(host, self.cfg.require_approval):
            return DomainDecision("approve", f"{host} requires user approval")
        return DomainDecision("ok")

    def is_denied(self, url: str) -> bool:
        host = domain_of(url)
        return bool(host) and (
            matches_any(host, self.cfg.deny) or bool(self.cfg.allow and not matches_any(host, self.cfg.allow))
        )

    def boost(self, url: str) -> float:
        host = domain_of(url)
        return 1.0 if matches_any(host, self.cfg.prefer) else 0.0

    def check_site_filter(self, site: str) -> DomainDecision:
        return self.check_fetch("https://" + site.strip().lower().lstrip("*."))
