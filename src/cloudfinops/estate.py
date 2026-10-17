"""Estate map (provider -> subscription -> service -> SKU -> resources) and per-service profiles.

Large services are summarized instead of listed: grouped by SKU, then by utilization bucket,
showing only the top items by cost plus an "N others" roll-up. Full lists go to CSV.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from .analysis import window
from .config import Config
from .models import Dataset, Recommendation, Resource
from .rules import scenario_total

TOP_PER_GROUP = 10


@dataclass
class Node:
    label: str
    kind: str  # root | account | service | sku | bucket | resource | rollup | unmapped
    cost: float = 0.0
    count: int = 0
    detail: str = ""
    resource_id: str = ""
    findings: int = 0
    savings: float = 0.0
    badges: list[str] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)


@dataclass
class ServiceProfile:
    service: str
    count: int
    monthly_cost: float
    share: float
    previous_cost: float | None
    skus: list[tuple[str, int, float]]
    regions: list[tuple[str, int]]
    utilization: dict[str, float]  # median_cpu_avg, p95_cpu_p95, idle, low, normal, high, no_metrics
    metrics_coverage: float | None
    untagged_share: float
    top_resources: list[tuple[str, float, str]]  # name, cost, resource id
    findings: int
    est_savings: float
    advisor_recs: int


def utilization_bucket(res: Resource, cfg: Config) -> str:
    m = res.metrics
    if "cpu_avg" not in m and "cpu_avg_14d" not in m:
        if res.type in ("azure.vm", "azure.vmss", "azure.app_service_plan", "ec2_instance", "gcp.vm"):
            return "stopped" if res.attributes.get("state") not in ("running", None) else "no metrics"
        return ""
    cpu_max = m.get("cpu_max", m.get("cpu_max_14d", 0))
    p95 = m.get("cpu_p95", m.get("cpu_max_14d", 0))
    if cpu_max < cfg.idle_cpu_max_pct:
        return "idle"
    if p95 < cfg.rightsize_cpu_p95_pct:
        return "low"
    if p95 < 80:
        return "normal"
    return "high"


def _service_of(rid: str, fallback: str, by_id: dict[str, Resource]) -> str:
    res = by_id.get(rid)
    return res.service if res and res.service else fallback


def _resource_detail(res: Resource, by_id: dict[str, Resource]) -> str:
    parts = [p for p in (res.sku, res.region, f"rg {res.group}" if res.group else "",
                         res.attributes.get("power_state") or res.attributes.get("disk_state") or "") if p]
    m = res.metrics
    if "cpu_p95" in m:
        parts.append(f"CPU p95 {m['cpu_p95']:.0f}%")
    if "mem_used_p95" in m:
        parts.append(f"mem p95 {m['mem_used_p95']:.0f}%")
    if "iops_p95" in m:
        parts.append(f"IOPS p95 {m['iops_p95']:.0f}")
    for key, label in (("requests_30d", "requests 30d"), ("executions_30d", "executions 30d")):
        if key in m:
            parts.append(f"{label}: {m[key]:,.0f}")
    if res.attributes.get("methods"):
        top = sorted(res.attributes["methods"].items(), key=lambda kv: -kv[1])[:3]
        parts.append("top methods: " + ", ".join(f"{k} {v:,.0f}" for k, v in top))
    if res.attributes.get("size_gb") and res.type == "gcp.artifact_repo":
        parts.append(f"{res.attributes['size_gb']:,.2f} GB stored")
    if "stored_gb" in m:
        parts.append(f"{m['stored_gb']:,.2f} GB stored")
    if res.related:
        names = [by_id[i].display_name for i in res.related[:4] if i in by_id]
        more = len(res.related) - len(names)
        parts.append("linked: " + ", ".join(names) + (f" +{more}" if more > 0 else ""))
    return " · ".join(parts)


def anchor_id(resource_id: str, by_id: dict) -> str:
    """Map sub-resource finding ids (e.g. <workspace>/tables/x) to the inventory resource they belong to."""
    if resource_id in by_id:
        return resource_id
    for marker in ("/tables/", "/retention", "/tier"):
        if marker in resource_id:
            return resource_id.split(marker, 1)[0]
    return resource_id


def build(ds: Dataset, recs: list[Recommendation], cfg: Config) -> tuple[Node, list[ServiceProfile]]:
    by_id = {r.id: r for r in ds.resources}
    recs_by_res = defaultdict(list)
    for rec in recs:
        if rec.resource_id:
            recs_by_res[anchor_id(rec.resource_id, by_id)].append(rec)

    def res_node(r: Resource) -> Node:
        rr = recs_by_res.get(r.id, [])
        return Node(r.display_name, "resource", r.monthly_cost or 0.0, 1, _resource_detail(r, by_id), r.id,
                    len(rr), scenario_total(rr)["estimate"] if rr else 0.0,
                    [utilization_bucket(r, cfg)] if utilization_bucket(r, cfg) else [])

    def rollup(nodes: list[Node], label_prefix: str = "") -> list[Node]:
        nodes.sort(key=lambda n: -n.cost)
        if len(nodes) <= TOP_PER_GROUP:
            return nodes
        rest = nodes[TOP_PER_GROUP:]
        return nodes[:TOP_PER_GROUP] + [Node(f"{len(rest)} more {label_prefix}".strip(), "rollup",
                                             sum(n.cost for n in rest), len(rest),
                                             findings=sum(n.findings for n in rest),
                                             savings=sum(n.savings for n in rest))]

    def agg(node: Node) -> Node:
        node.cost = sum(c.cost for c in node.children)
        node.count = sum(c.count for c in node.children)
        node.findings = sum(c.findings for c in node.children)
        node.savings = sum(c.savings for c in node.children)
        return node

    names = ds.extras.get("subscription_names", {})
    root = Node(f"{ds.provider.upper()} estate", "root")
    by_account: dict[str, list[Resource]] = defaultdict(list)
    for r in ds.resources:
        by_account[r.account].append(r)

    recent = window(ds.records, cfg.lookback_days) if ds.records else []
    span = len({r.day for r in recent}) or 1
    unmapped = defaultdict(lambda: defaultdict(float))
    for rec in recent:
        if not rec.resource_id or rec.resource_id not in by_id:
            unmapped[rec.account][rec.service] += rec.cost / span * 30

    for account in sorted(set(by_account) | set(unmapped)):
        acc = Node(names.get(account, account) or "(unknown account)", "account", detail=account)
        by_service: dict[str, list[Resource]] = defaultdict(list)
        for r in by_account.get(account, []):
            by_service[r.service or r.type].append(r)
        for service, items in by_service.items():
            svc = Node(service, "service")
            if len(items) <= cfg.max_items_per_node:
                svc.children = rollup([res_node(r) for r in items], service.lower())
            else:
                by_sku: dict[str, list[Resource]] = defaultdict(list)
                for r in items:
                    by_sku[r.sku or "(no SKU)"].append(r)
                for sku, sku_items in by_sku.items():
                    sku_node = Node(sku, "sku")
                    buckets: dict[str, list[Resource]] = defaultdict(list)
                    for r in sku_items:
                        buckets[utilization_bucket(r, cfg)].append(r)
                    if len(sku_items) > TOP_PER_GROUP and len(buckets) > 1:
                        for b, b_items in buckets.items():
                            bn = Node(f"{b or 'all'} utilization" if b not in ("no metrics", "stopped") else b, "bucket")
                            bn.children = rollup([res_node(r) for r in b_items])
                            sku_node.children.append(agg(bn))
                        sku_node.children.sort(key=lambda n: -n.cost)
                    else:
                        sku_node.children = rollup([res_node(r) for r in sku_items])
                    svc.children.append(agg(sku_node))
                svc.children.sort(key=lambda n: -n.cost)
                svc.badges = [f"{len(by_sku)} SKUs"]
            acc.children.append(agg(svc))
        if unmapped.get(account):
            um = Node("Billed without matching inventory", "unmapped",
                      detail="Deleted resources, marketplace, support, shared or service-level charges")
            um.children = [Node(s, "unmapped", c, 0) for s, c in sorted(unmapped[account].items(), key=lambda kv: -kv[1])]
            um.cost = sum(c.cost for c in um.children)
            acc.children.append(um)
        acc.children.sort(key=lambda n: -n.cost)
        root.children.append(agg(acc))
        acc.cost = sum(c.cost for c in acc.children)
    root.children.sort(key=lambda n: -n.cost)
    agg(root)
    root.cost = sum(c.cost for c in root.children)

    return root, _profiles(ds, recs, cfg, by_id, recs_by_res)


def _profiles(ds: Dataset, recs, cfg: Config, by_id, recs_by_res) -> list[ServiceProfile]:
    by_service: dict[str, list[Resource]] = defaultdict(list)
    for r in ds.resources:
        by_service[r.service or r.type].append(r)

    prev = {}
    if ds.records:
        end = max(r.day for r in ds.records)
        prev_end = end - timedelta(days=cfg.lookback_days)
        if prev_end >= min(r.day for r in ds.records):
            for rec in window(ds.records, cfg.lookback_days, prev_end):
                s = _service_of(rec.resource_id, rec.service, by_id)
                prev[s] = prev.get(s, 0.0) + rec.cost
    total = sum(r.monthly_cost or 0 for r in ds.resources) or 1.0
    advisor_ids = Counter(a["resource_id"] for a in ds.extras.get("advisor") or [])

    profiles = []
    for service, items in by_service.items():
        cost = sum(r.monthly_cost or 0 for r in items)
        skus = defaultdict(lambda: [0, 0.0])
        for r in items:
            skus[r.sku or "(no SKU)"][0] += 1
            skus[r.sku or "(no SKU)"][1] += r.monthly_cost or 0
        regions = Counter(r.region for r in items)
        with_cpu = [r for r in items if "cpu_avg" in r.metrics or "cpu_avg_14d" in r.metrics]
        measurable = [r for r in items if utilization_bucket(r, cfg) not in ("",)]
        buckets = Counter(utilization_bucket(r, cfg) for r in measurable)
        util = {k: float(v) for k, v in buckets.items()}
        if with_cpu:
            util["median_cpu_avg"] = statistics.median(r.metrics.get("cpu_avg", r.metrics.get("cpu_avg_14d", 0))
                                                       for r in with_cpu)
            util["max_cpu_p95"] = max(r.metrics.get("cpu_p95", r.metrics.get("cpu_max_14d", 0)) for r in with_cpu)
        running = [r for r in measurable if utilization_bucket(r, cfg) != "stopped"]
        coverage = (len(with_cpu) / len(running)) if running else None
        untagged = sum(1 for r in items if not all(r.tags.get(t) for t in cfg.required_tags)) / len(items)
        svc_recs = [rec for r in items for rec in recs_by_res.get(r.id, [])]
        profiles.append(ServiceProfile(
            service=service, count=len(items), monthly_cost=cost, share=cost / total,
            previous_cost=prev.get(service), skus=sorted(((k, v[0], v[1]) for k, v in skus.items()), key=lambda x: -x[2])[:6],
            regions=regions.most_common(4), utilization=util, metrics_coverage=coverage, untagged_share=untagged,
            top_resources=[(r.display_name, r.monthly_cost or 0, r.id)
                           for r in sorted(items, key=lambda r: -(r.monthly_cost or 0))[:5]],
            findings=len(svc_recs), est_savings=scenario_total(svc_recs)["estimate"] if svc_recs else 0.0,
            advisor_recs=sum(advisor_ids.get(r.id, 0) for r in items),
        ))
    profiles.sort(key=lambda p: -p.monthly_cost)
    return profiles


def to_mermaid(root: Node, max_services: int = 8, max_skus: int = 3) -> str:
    def clean(s: str) -> str:
        return "".join(ch for ch in s if ch not in "()[]{}\"'`").strip() or "-"

    lines = ["mindmap", f"  root(({clean(root.label)} ${root.cost:,.0f}/mo))"]
    for acc in root.children:
        lines.append(f"    {clean(acc.label)} ${acc.cost:,.0f}")
        for svc in [c for c in acc.children if c.cost >= 1][:max_services]:
            extra = f" · {svc.findings} findings" if svc.findings else ""
            count = f" {svc.count} ·" if svc.kind != "unmapped" else ""
            lines.append(f"      {clean(svc.label)}{count} ${svc.cost:,.0f}{extra}")
            for sku in [c for c in svc.children if c.kind == "sku"][:max_skus]:
                lines.append(f"        {clean(sku.label)} x{sku.count} · ${sku.cost:,.0f}")
    return "\n".join(lines) + "\n"
