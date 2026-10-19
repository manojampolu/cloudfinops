"""Data-quality checks: tell the reader what the findings can and cannot be trusted for."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from .analysis import CostSummary, window
from .config import Config
from .models import Dataset
from .providers.gcp import is_permission_error, short_error

METRIC_TYPES = {"azure.vm": "cpu_avg", "azure.app_service_plan": "cpu_avg", "ec2_instance": "cpu_avg_14d",
                "gcp.vm": "cpu_avg"}


@dataclass
class QualityIssue:
    level: str  # ok | info | warn | error
    area: str
    message: str
    impact: str = ""


def check(ds: Dataset, summary: CostSummary, cfg: Config) -> list[QualityIssue]:
    out: list[QualityIssue] = []
    recs = ds.records

    # --- collection errors
    errors = defaultdict(list)
    for e in ds.scope.get("errors", []):
        errors[e["step"]].append(e)
    for step, items in errors.items():
        first = short_error(items[0]["message"])
        if step == "access":
            out.append(QualityIssue("warn", "access", f"Not assessed: {first}. Projects: {items[0]['scope']}",
                                    "These projects are excluded from every total and finding."))
            continue
        denied = sum(1 for i in items if i.get("permission", is_permission_error(i["message"])))
        level = "error" if step in ("inventory", "costs") and denied < len(items) else "warn"
        detail = f" ({denied} permission denied)" if denied else ""
        out.append(QualityIssue(level, f"collection:{step}",
                                f"{len(items)} {step} call(s) failed{detail}, e.g. {items[0]['scope']}: {first}",
                                "Findings depending on this data are missing or marked as needing validation."))

    # --- billing coverage
    if not recs:
        note = ds.scope.get("billing_note") or "No billing data was loaded."
        out.append(QualityIssue("error", "billing", note,
                                "Spend totals are unknown; savings use list-price estimates and Recommender projections."))
    else:
        days = {r.day for r in recs}
        span = (max(days) - min(days)).days + 1
        missing = span - len(days)
        if missing:
            out.append(QualityIssue("warn", "billing", f"{missing} day(s) without any billing rows in {span}-day period.",
                                    "Trends and run-rate may be understated."))
        if span < cfg.lookback_days * 2:
            out.append(QualityIssue("info", "billing", f"Billing covers {span} days; period-over-period comparison "
                                    f"needs {cfg.lookback_days * 2}.", "Change vs previous period is partial."))
        else:
            out.append(QualityIssue("ok", "billing", f"Billing covers {span} days ({min(days)} to {max(days)})."))
        currencies = {r.currency for r in recs}
        if len(currencies) > 1:
            out.append(QualityIssue("error", "billing", f"Multiple currencies in billing data: {sorted(currencies)}.",
                                    "Totals mix currencies; split the assessment per currency."))

    # --- inventory <-> billing reconciliation
    if recs and ds.resources:
        recent = window(recs, cfg.lookback_days)
        total = sum(r.cost for r in recent) or 1.0
        ids = {r.id for r in ds.resources}
        unmatched = defaultdict(float)
        for r in recent:
            if not r.resource_id or r.resource_id not in ids:
                unmatched[r.service] += r.cost
        share = sum(unmatched.values()) / total
        top = ", ".join(f"{k} ${v:,.0f}" for k, v in sorted(unmatched.items(), key=lambda kv: -kv[1])[:4])
        out.append(QualityIssue("warn" if share > 0.15 else "ok", "reconciliation",
                                f"{share:.0%} of spend has no matching resource in inventory ({top or 'none'}).",
                                "Deleted resources, marketplace, support and shared charges are not analyzed per resource."
                                if share else ""))
        free = ds.extras.get("free_types", set())
        no_cost = [r for r in ds.resources if r.type not in free and r.cost_source != "billing"]
        if no_cost:
            est = sum(1 for r in no_cost if r.cost_source == "estimate")
            out.append(QualityIssue("info", "reconciliation",
                                    f"{len(no_cost)} resources have no billing rows ({est} priced from list prices).",
                                    "Their savings are estimates and marked accordingly."))

    # --- utilization coverage
    for rtype, key in METRIC_TYPES.items():
        pool = [r for r in ds.resources if r.type == rtype and r.attributes.get("state", "running") == "running"]
        if not pool:
            continue
        covered = sum(1 for r in pool if key in r.metrics)
        level = "ok" if covered == len(pool) else "warn" if covered < 0.8 * len(pool) else "info"
        out.append(QualityIssue(level, "utilization", f"{rtype}: CPU metrics for {covered}/{len(pool)} running resources.",
                                "" if level == "ok" else "Rightsizing/scheduling not assessed for resources without metrics."))
        if rtype in ("azure.vm", "gcp.vm"):
            mem = sum(1 for r in pool if "mem_used_p95" in r.metrics)
            out.append(QualityIssue("ok" if mem == len(pool) else "warn", "utilization",
                                    f"Guest memory metrics for {mem}/{len(pool)} running VMs.",
                                    "" if mem == len(pool) else
                                    "Rightsizing without memory data is low confidence (needs validation)."))

    # --- tagging
    if summary.window_total:
        level = "ok" if summary.fully_tagged_share >= 0.9 else "warn"
        out.append(QualityIssue(level, "ownership", f"{summary.fully_tagged_share:.0%} of spend carries all required tags "
                                f"({', '.join(cfg.required_tags)}).",
                                "" if level == "ok" else "Owners are suggested from resource groups where tags are missing."))
    return out


def period_label(summary: CostSummary, cfg: Config) -> str:
    start = summary.end - timedelta(days=cfg.lookback_days - 1)
    return f"{max(start, summary.start)} to {summary.end}"
