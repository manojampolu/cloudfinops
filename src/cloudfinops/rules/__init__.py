"""Run all registered rules and rank the results."""

from __future__ import annotations

from datetime import date

from ..config import Config
from ..models import SEVERITY_ORDER, CostRecord, Recommendation, Relationship, Resource
from . import aws, azure, gcp  # noqa: F401  (registers rules)
from .base import RULES, Context, owner_of

COMPUTE_RESOURCE_TYPES = {"azure.vm", "azure.vmss", "azure.aks", "azure.app_service_plan", "ec2_instance",
                          "rds_instance", "gcp.vm", "gcp.run_service", "gcp.sql", "azure.container_app"}
COMMITMENT_RULES = {"az-commitment", "savings-plan"}


def run_rules(records: list[CostRecord], resources: list[Resource], cfg: Config,
              as_of: date | None = None, disabled: set[str] | None = None,
              provider: str | None = None, extras: dict | None = None,
              relationships: list[Relationship] | None = None) -> list[Recommendation]:
    disabled = disabled or set()
    as_of = as_of or (max(r.day for r in records) if records else date.today())
    if provider is None:
        provider = (records[0].provider if records else resources[0].provider if resources else "aws")
    ctx = Context(cfg=cfg, records=records, resources=resources, as_of=as_of, provider=provider,
                  extras=extras or {}, relationships=relationships or [])

    recs: list[Recommendation] = []
    for rule in RULES:
        if rule.id in disabled:
            continue
        if rule.resource_types:
            for res in resources:
                if res.type in rule.resource_types and (rec := rule.fn(res, ctx)):
                    recs.append(rec)
        elif rule.provider in ("", provider):
            recs.extend(rule.fn(ctx))

    for rec in recs:  # fill presentation fields from the inventory
        res = ctx.by_id.get(rec.resource_id)
        if res:
            rec.owner = rec.owner or owner_of(res, cfg)
            rec.resource_name = rec.resource_name or res.display_name
            rec.service = rec.service or res.service
            if not rec.related:
                rec.related = list(res.related)

    _size_commitments_after_optimization(recs, cfg)
    recs.sort(key=lambda r: (-r.est_monthly_savings, SEVERITY_ORDER[r.severity], r.rule_id))
    return recs


def _size_commitments_after_optimization(recs: list[Recommendation], cfg: Config) -> None:
    """Commitments must cover the compute that remains after cleanup, rightsizing and scheduling."""
    compute = [r for r in recs if r.resource_type in COMPUTE_RESOURCE_TYPES
               and r.category in ("waste", "rightsizing", "scheduling")]
    reducible = scenario_total(compute)["estimate"] if compute else 0.0
    for rec in recs:
        if rec.rule_id in COMMITMENT_RULES and cfg.savings_plan_discount:
            basis = rec.est_monthly_savings / cfg.savings_plan_discount
            new_basis = max(0.0, basis - reducible)
            rec.est_monthly_savings = new_basis * cfg.savings_plan_discount
            rec.savings_low, rec.savings_high = rec.est_monthly_savings * 0.6, rec.est_monthly_savings
            rec.assumptions.append(f"Commitment basis reduced from ${basis:,.0f} to ${new_basis:,.0f}/mo by "
                                   f"${reducible:,.0f}/mo of compute savings proposed in other findings")
    ours = [r.est_monthly_savings for r in recs if r.rule_id in COMMITMENT_RULES]
    for rec in recs:
        if rec.rule_id == "az-advisor" and rec.category == "commitment" and reducible:
            rec.confidence = "low"
            rec.missing_evidence.append("Advisor sizes reservations on current usage; "
                                        f"${reducible:,.0f}/mo of compute may be removed by other findings")
            rec.savings_high = rec.est_monthly_savings  # Advisor's figure is the upper bound
            if ours:
                rec.est_monthly_savings = min(rec.est_monthly_savings, max(ours))
                rec.assumptions.append("Counted at no more than the post-optimization commitment estimate")
            rec.savings_low = 0.0


def scenario_total(recs: list[Recommendation]) -> dict[str, float]:
    """Savings without double counting.

    Findings sharing a scenario_group (by default: the same resource) are alternatives, so only
    the largest counts. Groups are summed. Returns estimate plus low/high range.
    """
    best: dict[str, Recommendation] = {}
    for r in recs:
        cur = best.get(r.scenario_group)
        if cur is None or r.est_monthly_savings > cur.est_monthly_savings:
            best[r.scenario_group] = r
    return {
        "estimate": sum(r.est_monthly_savings for r in best.values()),
        "low": sum(r.savings_low or 0.0 for r in best.values()),
        "high": max(sum(r.savings_high or 0.0 for r in best.values()),
                    sum(r.est_monthly_savings for r in best.values())),
        "counted": len(best),
    }


def total_savings(recs: list[Recommendation]) -> float:
    return scenario_total(recs)["estimate"]


__all__ = ["run_rules", "total_savings", "scenario_total", "owner_of", "RULES"]
