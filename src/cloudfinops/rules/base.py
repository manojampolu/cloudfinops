"""Rule registry.

Two kinds of rules:
  * resource rules: fn(resource, ctx) -> Recommendation | None, run per matching resource type
  * account rules:  fn(ctx) -> list[Recommendation], run once per dataset (scoped to a provider)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from ..config import Config
from ..models import CostRecord, Recommendation, Relationship, Resource


@dataclass
class Context:
    cfg: Config
    records: list[CostRecord]
    resources: list[Resource]
    as_of: date
    provider: str = "aws"
    extras: dict = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)
    by_id: dict[str, Resource] = field(default_factory=dict)

    def __post_init__(self):
        self.by_id = {r.id: r for r in self.resources}

    def age_days(self, iso: str | None) -> int | None:
        if not iso:
            return None
        return (self.as_of - date.fromisoformat(str(iso)[:10])).days

    def related_cost(self, res: Resource, *types: str) -> tuple[float, list[Resource]]:
        items = [self.by_id[i] for i in res.related if i in self.by_id and (not types or self.by_id[i].type in types)]
        return sum(i.monthly_cost or 0.0 for i in items), items


@dataclass
class Rule:
    id: str
    description: str
    fn: Callable
    resource_types: tuple[str, ...] = ()  # empty -> account-level rule
    provider: str = ""  # account rules: which provider's data they understand

    @property
    def scope(self) -> str:
        return ", ".join(self.resource_types) if self.resource_types else f"{self.provider or 'any'} billing"


RULES: list[Rule] = []


def resource_rule(rule_id: str, description: str, *types: str):
    def deco(fn):
        RULES.append(Rule(rule_id, description, fn, types))
        return fn
    return deco


def account_rule(rule_id: str, description: str, provider: str = "aws"):
    def deco(fn):
        RULES.append(Rule(rule_id, description, fn, provider=provider))
        return fn
    return deco


def owner_of(res: Resource | None, cfg: Config) -> str:
    """Suggested reviewer: owner-like tag, else the resource group."""
    if res is None:
        return ""
    for key in cfg.owner_tag_keys:
        if res.tags.get(key):
            return res.tags[key]
    if res.provider == "gcp":
        return f"project:{res.account}"
    return f"rg:{res.group}" if res.group else ""


def make(rule_id: str, res: Resource, **kw) -> Recommendation:
    """Recommendation pre-filled from a resource."""
    kw.setdefault("current_monthly_cost", res.monthly_cost)
    return Recommendation(
        rule_id=rule_id, resource_id=res.id, resource_type=res.type,
        region=res.region, account=res.account, **kw,
    )
