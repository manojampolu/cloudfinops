"""Spend analysis: breakdowns, period-over-period change, anomalies, tag coverage."""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .config import Config
from .models import CostRecord, Resource
from . import pricing


@dataclass
class Anomaly:
    day: date
    service: str
    cost: float
    expected: float
    z_score: float

    @property
    def delta(self) -> float:
        return self.cost - self.expected


@dataclass
class PeriodChange:
    service: str
    previous: float
    current: float

    @property
    def delta(self) -> float:
        return self.current - self.previous

    @property
    def pct(self) -> float | None:
        return (self.delta / self.previous) if self.previous else None


@dataclass
class CostSummary:
    start: date
    end: date
    total: float
    window_total: float  # spend within the last `lookback_days`
    daily_avg: float
    run_rate_30d: float
    by_service: list[tuple[str, float]] = field(default_factory=list)
    by_account: list[tuple[str, float]] = field(default_factory=list)
    by_region: list[tuple[str, float]] = field(default_factory=list)
    top_resources: list[tuple[str, float]] = field(default_factory=list)
    changes: list[PeriodChange] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    tag_coverage: dict[str, float] = field(default_factory=dict)  # tag -> share of window spend
    fully_tagged_share: float = 0.0


def _ranked(totals: dict[str, float]) -> list[tuple[str, float]]:
    return sorted(((k or "(none)", v) for k, v in totals.items()), key=lambda kv: kv[1], reverse=True)


def window(records: list[CostRecord], days: int, end: date | None = None) -> list[CostRecord]:
    if not records:
        return []
    end = end or max(r.day for r in records)
    start = end - timedelta(days=days - 1)
    return [r for r in records if start <= r.day <= end]


def detect_anomalies(records: list[CostRecord], cfg: Config, history_days: int = 14) -> list[Anomaly]:
    daily: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for r in records:
        daily[r.service][r.day] += r.cost
    if not records:
        return []
    first, last = min(r.day for r in records), max(r.day for r in records)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]

    found = []
    for service, series in daily.items():
        values = [series.get(d, 0.0) for d in days]
        for i in range(7, len(values)):
            hist = values[max(0, i - history_days):i]
            mean = statistics.fmean(hist)
            std = statistics.pstdev(hist) or max(mean * 0.05, 0.01)
            z = (values[i] - mean) / std
            if z >= cfg.anomaly_z_threshold and values[i] - mean >= cfg.anomaly_min_delta:
                found.append(Anomaly(days[i], service, values[i], mean, z))
    return sorted(found, key=lambda a: (a.day, a.delta), reverse=True)


def summarize(records: list[CostRecord], cfg: Config, top_n: int = 10) -> CostSummary:
    if not records:
        today = date.today()
        return CostSummary(start=today, end=today, total=0.0, window_total=0.0, daily_avg=0.0, run_rate_30d=0.0)
    start, end = min(r.day for r in records), max(r.day for r in records)
    current = window(records, cfg.lookback_days, end)
    prev_end = end - timedelta(days=cfg.lookback_days)
    previous = [r for r in window(records, cfg.lookback_days, prev_end)] if prev_end >= start else []

    by_service, by_account, by_region, by_resource = (defaultdict(float) for _ in range(4))
    for r in current:
        by_service[r.service] += r.cost
        by_account[r.account] += r.cost
        by_region[r.region] += r.cost
        if r.resource_id:
            by_resource[r.resource_id] += r.cost

    window_total = sum(by_service.values())
    window_days = min(cfg.lookback_days, (end - start).days + 1)
    last7 = window(records, 7, end)
    run_rate = sum(r.cost for r in last7) / min(7, window_days) * 30

    changes = []
    if previous:
        prev_by_service = defaultdict(float)
        for r in previous:
            prev_by_service[r.service] += r.cost
        for svc in set(by_service) | set(prev_by_service):
            changes.append(PeriodChange(svc, prev_by_service.get(svc, 0.0), by_service.get(svc, 0.0)))
        changes.sort(key=lambda c: abs(c.delta), reverse=True)

    tag_coverage = {}
    if window_total:
        for tag in cfg.required_tags:
            tagged = sum(r.cost for r in current if r.tags.get(tag.lower()))
            tag_coverage[tag] = tagged / window_total
        fully = sum(r.cost for r in current if all(r.tags.get(t.lower()) for t in cfg.required_tags))
        fully_tagged_share = fully / window_total
    else:
        fully_tagged_share = 0.0

    anomalies = [a for a in detect_anomalies(records, cfg) if a.day > prev_end]

    return CostSummary(
        start=start, end=end,
        total=sum(r.cost for r in records),
        window_total=window_total,
        daily_avg=window_total / window_days,
        run_rate_30d=run_rate,
        by_service=_ranked(by_service)[:top_n],
        by_account=_ranked(by_account)[:top_n],
        by_region=_ranked(by_region)[:top_n],
        top_resources=_ranked(by_resource)[:top_n],
        changes=changes[:top_n],
        anomalies=anomalies[:top_n],
        tag_coverage=tag_coverage,
        fully_tagged_share=fully_tagged_share,
    )


def enrich_resource_costs(resources: list[Resource], records: list[CostRecord], cfg: Config) -> None:
    """Fill Resource.monthly_cost: billing data first, then list-price estimate."""
    recent = window(records, cfg.lookback_days)
    span = len({r.day for r in recent}) or 1
    by_resource = defaultdict(float)
    for r in recent:
        if r.resource_id:
            by_resource[r.resource_id] += r.cost

    for res in resources:
        if res.monthly_cost is not None:
            continue
        if res.id in by_resource:
            res.monthly_cost = by_resource[res.id] / span * 30
            res.cost_source = "billing"
            continue
        est = _estimate(res, cfg)
        if est is not None:
            res.monthly_cost = est
            res.cost_source = "estimate"


def _estimate(res: Resource, cfg: Config) -> float | None:
    if res.type.startswith("azure."):
        from .pricing_azure import estimate_monthly
        return estimate_monthly(res, cfg.hours_per_month)
    if res.type.startswith("gcp."):
        from .pricing_gcp import estimate_monthly as gcp_estimate
        return gcp_estimate(res, cfg.hours_per_month)
    if res.type.startswith("aws."):
        return pricing.estimate_aws(res, cfg.hours_per_month)
    a = res.attributes
    h = cfg.hours_per_month
    match res.type:
        case "ec2_instance":
            if a.get("state") != "running":
                return 0.0
            return pricing.ec2_monthly_any(a.get("instance_type", ""), h)
        case "ebs_volume":
            return pricing.ebs_monthly(a.get("volume_type", "gp2"), float(a.get("size_gb", 0)))
        case "ebs_snapshot":
            return pricing.SNAPSHOT_GB_MONTH * float(a.get("size_gb", 0))
        case "elastic_ip":
            return pricing.PUBLIC_IPV4_HOURLY * h
        case "load_balancer":
            return pricing.ALB_HOURLY * h
        case "rds_instance":
            if a.get("state", "running") != "running":
                return 0.0
            return pricing.rds_monthly_any(a.get("instance_class", ""), bool(a.get("multi_az")), h)
        case "s3_bucket":
            return pricing.S3_STANDARD_GB_MONTH * float(a.get("standard_gb", 0))
    return None
