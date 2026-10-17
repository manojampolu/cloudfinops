"""Azure optimization rules: scheduling, rightsizing, storage/orphan cleanup, logging, commitments.

Rules flag *review candidates* with evidence. They never conclude that a change is safe:
anything not provable from collected data goes into missing_evidence / validation_steps.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import timedelta

from ..models import Recommendation, Resource
from ..pricing_azure import disk_tier_price, vm_downsize
from .base import Context, account_rule, make, owner_of, resource_rule

COMPUTE_TYPES = ("azure.vm", "azure.vmss")
TAGGABLE = ("azure.vm", "azure.vmss", "azure.disk", "azure.app_service_plan", "azure.aks", "azure.log_workspace",
            "azure.storage_account", "azure.sql_db", "azure.appgw", "azure.lb", "azure.public_ip", "azure.snapshot")


def _cost(res: Resource) -> float:
    return res.monthly_cost or 0.0


def _money(v: float) -> str:
    return f"${v:,.2f}"


def is_non_prod(res: Resource, ctx: Context) -> str | None:
    """Return the reason a resource looks non-production, or None."""
    env = res.tags.get("environment") or res.tags.get("env") or ""
    if env:
        return f"tag environment={env}" if env.lower() in ctx.cfg.non_prod_env_values else None
    tokens = set(re.split(r"[-_.\s]+", f"{res.name} {res.group}".lower()))
    hit = tokens & set(ctx.cfg.non_prod_name_tokens)
    return f"name/resource group contains '{sorted(hit)[0]}'" if hit else None


def _cost_evidence(res: Resource) -> str:
    src = {"billing": "amortized billing, last 30 days", "estimate": "list-price estimate"}.get(res.cost_source, "unknown")
    return f"Current cost {_money(_cost(res))}/mo ({src})"


def _cpu_evidence(res: Resource) -> str:
    m = res.metrics
    return (f"CPU over {int(m.get('metrics_window_days', 14))} days: avg {m.get('cpu_avg', 0):.1f}%, "
            f"p95 {m.get('cpu_p95', 0):.1f}%, max {m.get('cpu_max', 0):.1f}%")


# =========================================================================== compute

@resource_rule("az-vm-stopped-billing", "VMs stopped but not deallocated (still billed for compute)", "azure.vm")
def vm_stopped_not_deallocated(res: Resource, ctx: Context):
    if res.attributes.get("power_state") != "stopped" or _cost(res) <= 1:
        return None
    return make("az-vm-stopped-billing", res, title=f"VM stopped but not deallocated ({res.sku})",
                category="waste", est_monthly_savings=_cost(res), confidence="high",
                evidence=[f"Power state: stopped (OS shutdown, compute still allocated)", _cost_evidence(res)],
                action="Deallocate the VM (Stop in portal / az vm deallocate) or delete it if no longer needed.",
                validation_steps=["Confirm with the owner that the VM is not expected to be running."])


@resource_rule("az-vm-deallocated", "Deallocated VMs that still pay for disks and IPs", "azure.vm")
def vm_deallocated(res: Resource, ctx: Context):
    if res.attributes.get("power_state") != "deallocated":
        return None
    continuing, items = ctx.related_cost(res, "azure.disk", "azure.public_ip")
    if continuing < 1:
        return None
    no_compute = _cost(res) < 1
    return make("az-vm-deallocated", res, title=f"Deallocated VM still paying {_money(continuing)}/mo for disks/IPs",
                category="waste", est_monthly_savings=continuing, current_monthly_cost=continuing,
                confidence="medium" if no_compute else "low",
                evidence=["Power state: deallocated",
                          "No compute charges in the last 30 days" if no_compute else "Compute charges seen recently",
                          *[f"{i.service}: {i.display_name} {_money(_cost(i))}/mo" for i in items]],
                missing_evidence=[] if no_compute else ["How long the VM has been deallocated"],
                action="If the VM is retired: snapshot disks, then delete VM, disks and IPs. "
                       "If occasionally used: move disks to Standard HDD.",
                validation_steps=["Confirm the VM is not part of a DR or seasonal process."])


@resource_rule("az-vm-idle", "Running VMs with near-zero CPU", "azure.vm")
def vm_idle(res: Resource, ctx: Context):
    m = res.metrics
    if res.attributes.get("state") != "running" or "cpu_max" not in m or m["cpu_max"] >= ctx.cfg.idle_cpu_max_pct:
        return None
    return make("az-vm-idle", res, title=f"Idle VM ({res.sku})", category="waste",
                est_monthly_savings=_cost(res), confidence="medium", risk="medium",
                evidence=[_cpu_evidence(res), _cost_evidence(res)],
                missing_evidence=["Network and disk activity were not evaluated"],
                action="Confirm with the owner; deallocate or delete. Disks/IPs keep billing until deleted.",
                validation_steps=["Check for scheduled/batch jobs and inbound connections before stopping."])


@resource_rule("az-vm-rightsize", "Over-provisioned VMs (one size down)", "azure.vm")
def vm_rightsize(res: Resource, ctx: Context):
    m, cfg = res.metrics, ctx.cfg
    if res.attributes.get("state") != "running" or "cpu_p95" not in m:
        return None
    if m["cpu_max"] < cfg.idle_cpu_max_pct:  # idle rule covers this
        return None
    if m["cpu_p95"] >= cfg.rightsize_cpu_p95_pct or m["cpu_avg"] >= cfg.rightsize_cpu_avg_pct:
        return None
    mem = m.get("mem_used_p95")
    if mem is not None and mem >= cfg.rightsize_mem_p95_pct:
        return None
    target = vm_downsize(res.sku)
    if not target:
        return None
    book = ctx.extras.get("pricebook")
    cur_p, cur_src = book.vm_hourly(res.sku, res.region) if book else (None, "unknown")
    new_p, new_src = book.vm_hourly(target, res.region) if book else (None, "unknown")
    assumptions = []
    if cur_p and new_p:
        ratio = 1 - new_p / cur_p
        assumptions.append(f"Price ratio from {new_src if new_src == cur_src else cur_src + '/' + new_src} prices: "
                           f"${cur_p:.3f}/h -> ${new_p:.3f}/h")
    else:
        ratio = 0.5
        assumptions.append("Target price unknown; assumed 50% (one size down halves vCPU and memory)")
    evidence = [_cpu_evidence(res), _cost_evidence(res)]
    missing = []
    if mem is None:
        missing.append("Guest memory utilization (Available Memory Bytes not collected for this VM)")
    else:
        evidence.append(f"Memory used p95 {mem:.0f}%")
    if res.attributes.get("advisor"):
        evidence.append("Azure Advisor also flags this VM: " + "; ".join(res.attributes["advisor"])[:200])
    return make("az-vm-rightsize", res, title=f"Downsize {res.sku} -> {target}", category="rightsizing",
                est_monthly_savings=_cost(res) * ratio, effort="medium", risk="medium",
                confidence="high" if mem is not None else "low",
                evidence=evidence, missing_evidence=missing, assumptions=assumptions,
                action=f"Resize to {target} in a maintenance window (requires a restart).",
                validation_steps=["Check month-end / seasonal peaks outside the metrics window",
                                  f"Confirm {target} supports the required disks, NICs and throughput",
                                  "Run a performance test or resize a non-prod twin first"])


@resource_rule("az-nonprod-schedule", "Non-production compute running 24/7", "azure.vm", "azure.vmss")
def nonprod_schedule(res: Resource, ctx: Context):
    m, cfg = res.metrics, ctx.cfg
    reason = is_non_prod(res, ctx)
    if not reason or res.attributes.get("aks_pool"):
        return None
    if res.type == "azure.vm" and res.attributes.get("state") != "running":
        return None
    if m.get("uptime_share", 0) < cfg.scheduling_min_uptime_share or m.get("cpu_max", 100) < cfg.idle_cpu_max_pct:
        return None
    off_share = 1 - cfg.non_prod_on_hours_per_week / 168
    continuing, items = ctx.related_cost(res, "azure.disk", "azure.public_ip")
    return make("az-nonprod-schedule", res, title=f"Schedule non-prod {res.service[:-1].lower()} off-hours ({res.sku})",
                category="scheduling", est_monthly_savings=_cost(res) * off_share, confidence="medium",
                evidence=[f"Classified non-production: {reason}",
                          f"Running {m['uptime_share']:.0%} of hours in the metrics window", _cost_evidence(res)],
                assumptions=[f"On {cfg.non_prod_on_hours_per_week:.0f}h/week (of 168h) -> {off_share:.0%} compute "
                             "saving; deallocated VMs stop compute billing",
                             f"Continuing costs while off: {_money(continuing)}/mo for {len(items)} disk(s)/IP(s)"],
                missing_evidence=["Agreed operating hours for this environment"],
                action="Use Azure Automation / Start-Stop VMs v2 or a Logic App schedule to deallocate "
                       "outside agreed hours.",
                validation_steps=["Check overnight jobs, backups and patch windows",
                                  "Check dependencies from shared or prod systems"])


@resource_rule("az-aks-nonprod-stop", "Non-production AKS clusters running 24/7", "azure.aks")
def aks_nonprod(res: Resource, ctx: Context):
    reason = is_non_prod(res, ctx)
    if not reason or res.attributes.get("power_state") not in ("running", ""):
        return None
    ng = res.attributes.get("node_resource_group", "")
    pools = [r for r in ctx.resources if r.type == "azure.vmss" and r.group.lower() == ng]
    node_cost = sum(_cost(p) for p in pools)
    if node_cost < 1:
        return None
    off_share = 1 - ctx.cfg.non_prod_on_hours_per_week / 168
    return make("az-aks-nonprod-stop", res, title=f"Stop non-prod AKS cluster off-hours ({res.attributes.get('node_count')} nodes)",
                category="scheduling", est_monthly_savings=node_cost * off_share, current_monthly_cost=node_cost,
                confidence="medium", effort="low",
                evidence=[f"Classified non-production: {reason}",
                          f"Node pools: " + ", ".join(f"{p['name']} {p['count']}x{p['vm_size']}"
                                                      for p in res.attributes.get("node_pools", [])),
                          f"Node VM cost {_money(node_cost)}/mo (VM scale sets in {ng})"],
                assumptions=[f"Cluster stopped outside {ctx.cfg.non_prod_on_hours_per_week:.0f}h/week"],
                missing_evidence=["Agreed operating hours", "Workloads that must run continuously (e.g. CI agents)"],
                related=[p.id for p in pools],
                action="Schedule `az aks stop` / `az aks start`, or scale user node pools to zero off-hours.",
                validation_steps=["Confirm persistent volumes and ingress IPs survive stop/start in this setup"])


@resource_rule("az-plan-empty", "App Service plans with no apps", "azure.app_service_plan")
def plan_empty(res: Resource, ctx: Context):
    if res.attributes.get("number_of_sites", 1) != 0 or _cost(res) < 1:
        return None
    return make("az-plan-empty", res, title=f"Empty App Service plan ({res.sku} x{res.attributes.get('capacity')})",
                category="waste", est_monthly_savings=_cost(res), confidence="high",
                evidence=["numberOfSites = 0", _cost_evidence(res)],
                action="Delete the plan (plans bill per instance even with no apps).",
                validation_steps=["Confirm no deployment pipeline is about to target this plan."])


@resource_rule("az-plan-scale-in", "Over-provisioned App Service plans", "azure.app_service_plan")
def plan_scale_in(res: Resource, ctx: Context):
    m, cap = res.metrics, int(res.attributes.get("capacity") or 1)
    if cap <= 1 or "cpu_p95" not in m or res.attributes.get("number_of_sites", 0) == 0:
        return None
    if m["cpu_p95"] >= ctx.cfg.plan_scale_in_cpu_p95_pct or m.get("mem_used_p95", 0) >= ctx.cfg.rightsize_mem_p95_pct:
        return None
    floor = 1 if is_non_prod(res, ctx) else 2  # keep redundancy for production plans
    target = max(floor, math.ceil(cap * m["cpu_p95"] / 60))
    if target >= cap:
        return None
    return make("az-plan-scale-in", res, title=f"Scale App Service plan in from {cap} to {target} instances",
                category="rightsizing", est_monthly_savings=_cost(res) * (cap - target) / cap,
                confidence="medium" if "mem_used_p95" in m else "low", effort="low",
                evidence=[_cpu_evidence(res), _cost_evidence(res),
                          *( [f"Memory p95 {m['mem_used_p95']:.0f}%"] if "mem_used_p95" in m else [])],
                assumptions=["Target keeps CPU p95 under ~60% after consolidation",
                             f"Minimum {floor} instance(s) kept ({'non-prod' if floor == 1 else 'production redundancy'})"],
                missing_evidence=[] if "mem_used_p95" in m else ["Plan memory utilization"],
                action="Lower the instance count or configure autoscale with a lower minimum.",
                validation_steps=["Keep >= 2 instances if the app has an availability SLA"])


# =========================================================================== storage & network

@resource_rule("az-disk-unattached", "Managed disks not attached to any VM", "azure.disk")
def disk_unattached(res: Resource, ctx: Context):
    if res.attributes.get("disk_state") != "unattached":
        return None
    age = ctx.age_days(res.attributes.get("last_ownership_update") or res.attributes.get("time_created"))
    return make("az-disk-unattached", res,
                title=f"Unattached disk ({res.attributes.get('size_gb')} GB {res.sku})",
                category="waste", est_monthly_savings=_cost(res), confidence="high",
                evidence=["diskState = Unattached", _cost_evidence(res),
                          *( [f"Unattached/unchanged for ~{age} days"] if age is not None else [])],
                action="Snapshot (if data may be needed) and delete the disk.",
                validation_steps=["Confirm with the owner that the data is not needed or is backed up."])


@resource_rule("az-pip-unassociated", "Public IPs not associated with any resource", "azure.public_ip")
def pip_unassociated(res: Resource, ctx: Context):
    if res.attributes.get("associated"):
        return None
    return make("az-pip-unassociated", res, title=f"Unassociated public IP ({res.sku or 'Standard'})",
                category="waste", est_monthly_savings=_cost(res), confidence="high",
                evidence=["No ipConfiguration or NAT gateway association", _cost_evidence(res)],
                action="Delete the public IP (check DNS records and firewall allow-lists first).")


@resource_rule("az-snapshot-old", "Old managed disk snapshots", "azure.snapshot")
def snapshot_old(res: Resource, ctx: Context):
    age = ctx.age_days(res.attributes.get("time_created"))
    if age is None or age <= ctx.cfg.snapshot_max_age_days:
        return None
    if res.tags.get("retention") in ("permanent", "legal-hold"):
        return None
    source_exists = res.attributes.get("source_id") in ctx.by_id
    return make("az-snapshot-old", res, title=f"Snapshot is {age} days old ({res.attributes.get('size_gb')} GB)",
                category="waste", est_monthly_savings=_cost(res),
                confidence="medium" if res.cost_source == "billing" else "low",
                evidence=[f"Created {age} days ago (threshold {ctx.cfg.snapshot_max_age_days})", _cost_evidence(res),
                          "Source disk still exists" if source_exists else "Source disk no longer exists"],
                assumptions=[] if res.cost_source == "billing" else
                ["Estimated at full size; incremental snapshots bill only changed data"],
                action="Delete if not required for retention, or apply an Azure Backup / lifecycle policy.",
                validation_steps=["Check backup/retention policy and legal-hold requirements."])


@resource_rule("az-disk-premium-lowiops", "Premium SSDs with low IOPS", "azure.disk")
def disk_premium_low_iops(res: Resource, ctx: Context):
    if not res.sku.lower().startswith("premium_") or res.attributes.get("disk_state") != "attached":
        return None
    iops = res.metrics.get("iops_p95")
    if iops is None or iops >= ctx.cfg.premium_disk_low_iops:
        return None
    gb = float(res.attributes.get("size_gb") or 0)
    prem, std = disk_tier_price(gb, "premium_lrs"), disk_tier_price(gb, "standardssd_lrs")
    ratio = 1 - std / prem if prem else 0.0
    return make("az-disk-premium-lowiops", res, title=f"Premium SSD -> Standard SSD ({int(gb)} GB)",
                category="modernization", est_monthly_savings=_cost(res) * ratio, confidence="medium", effort="low",
                evidence=[f"IOPS p95 {iops:.0f}, max {res.metrics.get('iops_max', 0):.0f} over "
                          f"{int(res.metrics.get('metrics_window_days', 14))} days", _cost_evidence(res)],
                assumptions=[f"Tier price ratio {std:.2f}/{prem:.2f} (Standard SSD / Premium SSD, LRS)"],
                action="Change the disk SKU to StandardSSD_LRS (requires the VM to be deallocated).",
                validation_steps=["Check latency SLOs: Standard SSD has higher latency than Premium",
                                  "Some VM sizes/SLA tiers require Premium disks for single-instance SLA"])


@resource_rule("az-lb-no-backend", "Load balancers / application gateways without backends", "azure.lb", "azure.appgw")
def lb_no_backend(res: Resource, ctx: Context):
    if res.attributes.get("backend_count", 1) > 0 or _cost(res) < 1:
        return None
    kind = "Application gateway" if res.type == "azure.appgw" else "Load balancer"
    return make("az-lb-no-backend", res, title=f"{kind} has no backend targets", category="waste",
                est_monthly_savings=_cost(res), confidence="high",
                evidence=["All backend pools are empty", _cost_evidence(res)],
                action=f"Delete the {kind.lower()} if the service it fronted is retired.")


# =========================================================================== logging

def _workspace_retention(ws: Resource, settings: dict, table: str) -> int:
    t = settings.get(table) or {}
    return int(t.get("retention_days") or ws.attributes.get("retention_days") or 30)


@account_rule("az-logging", "Log Analytics ingestion, table plans, retention and commitment tiers", provider="azure")
def logging_rules(ctx: Context) -> list[Recommendation]:
    cfg, recs = ctx.cfg, []
    price, basic = cfg.log_analytics_gb_price, cfg.log_basic_gb_price
    for ws_id, data in (ctx.extras.get("log_usage") or {}).items():
        ws = ctx.by_id.get(ws_id)
        if not ws:
            continue
        tables, settings = data["tables"], data["table_settings"]
        total_gb = sum(tables.values())
        base = dict(resource_type=ws.type, region=ws.region, account=ws.account, service="Log Analytics",
                    resource_name=ws.display_name, owner=owner_of(ws, cfg), related=[ws_id])
        moved_gb = 0.0  # volume proposed to leave the Analytics plan

        for table, gb in sorted(tables.items(), key=lambda kv: -kv[1]):
            if gb < cfg.log_large_table_gb_month:
                continue
            plan = (settings.get(table) or {}).get("plan", "Analytics")
            cost = gb * price
            rid = f"{ws_id}/tables/{table.lower()}"
            ev = [f"{table}: {gb:,.0f} GB billable ingestion in last 30 days (workspace total {total_gb:,.0f} GB)",
                  f"Table plan: {plan}"]
            if table in cfg.log_basic_eligible_tables and plan == "Analytics":
                moved_gb += gb
                recs.append(Recommendation(
                    rule_id="az-log-basic-plan", resource_id=rid, category="logging",
                    title=f"Move {table} ({gb:,.0f} GB/mo) to the Basic logs plan",
                    est_monthly_savings=gb * (price - basic), current_monthly_cost=cost, confidence="medium",
                    evidence=ev, assumptions=[f"Analytics ${price}/GB vs Basic ${basic}/GB ingestion (list prices)"],
                    missing_evidence=["Alert rules / workbooks that query this table"],
                    action=f"Set the {table} table plan to Basic (az monitor log-analytics workspace table update --plan Basic).",
                    validation_steps=["Basic logs: limited KQL, 30-day interactive retention, per-GB query charge",
                                      "Confirm no alert rules depend on this table"], **base))
            elif table == "ContainerLog":
                moved_gb += gb
                recs.append(Recommendation(
                    rule_id="az-log-containerlog-v2", resource_id=rid, category="logging",
                    title=f"Migrate ContainerLog ({gb:,.0f} GB/mo) to ContainerLogV2 + Basic plan",
                    est_monthly_savings=gb * (price - basic), current_monthly_cost=cost, confidence="low",
                    effort="medium", evidence=ev,
                    assumptions=["ContainerLogV2 supports the Basic plan; legacy ContainerLog does not"],
                    missing_evidence=["Queries/alerts using the legacy schema", "Which namespaces' logs are needed"],
                    action="Enable ContainerLogV2 in Container Insights data collection settings, then set Basic plan; "
                           "also filter noisy namespaces.",
                    validation_steps=["Update dashboards/alerts to the V2 schema"], **base))
            else:
                recs.append(Recommendation(
                    rule_id="az-log-review-ingestion", resource_id=rid, category="logging",
                    title=f"Review {table} ingestion ({gb:,.0f} GB/mo, {_money(cost)}/mo)",
                    est_monthly_savings=0.0, savings_low=0.0, savings_high=cost * 0.3, current_monthly_cost=cost,
                    confidence="low", effort="medium", evidence=ev,
                    assumptions=["Upper bound assumes 30% of volume is debug/verbose and can be filtered"],
                    missing_evidence=["Which log categories are required for troubleshooting / audit"],
                    action="Review diagnostic settings and DCR transformations to drop unneeded categories/columns"
                           + ("; prefer resource-specific tables over AzureDiagnostics" if table == "AzureDiagnostics" else ""),
                    validation_steps=["Agree log requirements with security/operations"], **base))

        analytics_gb = max(0.0, total_gb - moved_gb)
        moved_note = (f"Excludes {moved_gb:,.0f} GB/mo proposed for the Basic plan in other findings" if moved_gb else "")
        ret = int(ws.attributes.get("retention_days") or 30)
        if ret > cfg.log_max_interactive_retention_days and analytics_gb > 0:
            excess_months = (ret - cfg.log_max_interactive_retention_days) / 30
            saving = analytics_gb * excess_months * (cfg.log_retention_gb_month_price - cfg.log_archive_gb_month_price)
            recs.append(Recommendation(
                rule_id="az-log-retention", resource_id=f"{ws_id}/retention", category="logging",
                title=f"Interactive retention {ret} days -> {cfg.log_max_interactive_retention_days} days + archive",
                est_monthly_savings=saving, current_monthly_cost=None, confidence="low",
                evidence=[f"Workspace retention {ret} days", f"~{analytics_gb:,.0f} GB/month ingested on the Analytics plan"],
                assumptions=[*([moved_note] if moved_note else []), f"Steady-state stored volume ~ monthly ingestion x retention months; "
                             f"retention ${cfg.log_retention_gb_month_price}/GB-mo vs archive "
                             f"${cfg.log_archive_gb_month_price}/GB-mo"],
                missing_evidence=["Regulatory / security retention requirement", "Whether Sentinel is enabled "
                                  "(90 days included)"],
                action="Lower interactive retention and set total (archive) retention per table to meet policy.",
                validation_steps=["Confirm retention policy with security/compliance"], **base))

        gb_day = analytics_gb / 30
        if gb_day >= cfg.log_commitment_min_gb_day and ws.attributes.get("pricing_sku", "").lower() == "pergb2018":
            recs.append(Recommendation(
                rule_id="az-log-commitment-tier", resource_id=f"{ws_id}/tier", category="commitment",
                title=f"Use a Log Analytics commitment tier (~{gb_day:,.0f} GB/day on pay-as-you-go)",
                est_monthly_savings=analytics_gb * price * cfg.log_commitment_discount, confidence="medium",
                evidence=[f"Average Analytics-plan ingestion {gb_day:,.0f} GB/day", "Pricing tier: Pay-as-you-go (PerGB2018)"],
                assumptions=[*([moved_note] if moved_note else []),
                             f"~{cfg.log_commitment_discount:.0%} discount at the matching commitment tier"],
                action="Select the commitment tier at or just below average daily ingestion (31-day commitment).",
                validation_steps=["Apply after ingestion-reduction findings, so the tier matches the new volume"],
                **base))
    return recs


# =========================================================================== commitments & advisor

AZ_COMPUTE_SERVICES = {"Virtual Machines", "Azure App Service", "Container Instances", "Azure Container Apps"}


@account_rule("az-commitment", "Stable on-demand compute that could use Reservations / Savings Plan", provider="azure")
def commitment(ctx: Context) -> list[Recommendation]:
    rows = ctx.extras.get("pricing_model_daily") or []
    if not rows:
        return []
    end = max(r["day"] for r in rows)
    start = end - timedelta(days=ctx.cfg.lookback_days - 1)
    daily_od, total, committed = defaultdict(float), 0.0, 0.0
    for r in rows:
        if r["service"] not in AZ_COMPUTE_SERVICES or not (start <= r["day"] <= end):
            continue
        total += r["cost"]
        if r["pricing_model"] in ("OnDemand", "On Demand", ""):
            daily_od[r["day"]] += r["cost"]
        elif r["pricing_model"] in ("Reservation", "SavingsPlan"):
            committed += r["cost"]
    if len(daily_od) < 7 or not total:
        return []
    values = sorted(daily_od.values())
    baseline = values[int(len(values) * 0.1)]
    if baseline * 30 < 100:
        return []
    return [Recommendation(
        rule_id="az-commitment", resource_id="", scenario_group="__commitment", category="commitment",
        title=f"Commit ~${baseline / 24:,.2f}/hour of compute (Savings Plan or Reservations)",
        est_monthly_savings=baseline * 30 * ctx.cfg.savings_plan_discount, current_monthly_cost=total / ctx.cfg.lookback_days * 30,
        confidence="medium",
        evidence=[f"Commitment coverage of compute: {committed / total:.0%}",
                  f"Lowest-10% daily on-demand compute spend: {_money(baseline)}"],
        assumptions=[f"~{ctx.cfg.savings_plan_discount:.0%} blended discount (1-year Savings Plan); "
                     "Reservations save more for fixed VM families"],
        missing_evidence=["12-month demand forecast / migration roadmap"],
        action="Buy after rightsizing and cleanup; compare Advisor reservation recommendations.",
        validation_steps=["Agree the commitment amount with finance; prefer 1-year terms initially"],
    )]


@account_rule("az-advisor", "Azure Advisor cost recommendations (provider-native)", provider="azure")
def advisor(ctx: Context) -> list[Recommendation]:
    recs = []
    for a in ctx.extras.get("advisor") or []:
        if not a.get("monthly_savings"):
            continue
        res = ctx.by_id.get(a["resource_id"])
        text = f"{a['problem']} {a['solution']}".lower()
        commit = any(k in text for k in ("reserved", "reservation", "savings plan"))
        recs.append(Recommendation(
            rule_id="az-advisor", resource_id=a["resource_id"] if res else "",
            scenario_group="__commitment" if commit else (a["resource_id"] or f"__advisor_{a['id']}"),
            category="commitment" if commit else "advisor",
            title=f"Advisor: {a['solution'] or a['problem']}"[:140],
            est_monthly_savings=a["monthly_savings"], current_monthly_cost=res.monthly_cost if res else None,
            confidence="medium", evidence=[f"Azure Advisor ({a['impact']} impact): {a['problem']}"]
            + ([f"Current SKU {a['current_sku']} -> target {a['target_sku']}"] if a.get("target_sku") else []),
            assumptions=["Savings as reported by Azure Advisor"],
            action=a["solution"] or "Review in Azure Advisor.",
            validation_steps=["Cross-check with the related cloudfinops finding, if any (counted once)"],
            resource_type=res.type if res else "", region=res.region if res else "",
            account=res.account if res else "", owner=owner_of(res, ctx.cfg),
        ))
    return recs


# =========================================================================== governance

@account_rule("az-untagged", "Spend on resources missing required tags", provider="azure")
def untagged(ctx: Context) -> list[Recommendation]:
    by_sub: dict[str, list[tuple[Resource, list[str]]]] = defaultdict(list)
    for res in ctx.resources:
        if res.type not in TAGGABLE:
            continue
        missing = [t for t in ctx.cfg.required_tags if not res.tags.get(t.lower())]
        if missing:
            by_sub[res.account].append((res, missing))
    recs = []
    names = ctx.extras.get("subscription_names", {})
    for sub, items in by_sub.items():
        cost = sum(_cost(r) for r, _ in items)
        top = sorted(items, key=lambda x: -_cost(x[0]))[:8]
        recs.append(Recommendation(
            rule_id="az-untagged", resource_id="", scenario_group=f"__untagged_{sub}", category="governance",
            title=f"{len(items)} resources ({_money(cost)}/mo) missing required tags in {names.get(sub, sub)}",
            est_monthly_savings=0.0, current_monthly_cost=cost, confidence="high", account=sub,
            evidence=[f"{r.display_name} ({r.service}, {_money(_cost(r))}/mo): missing {', '.join(m)}" for r, m in top],
            action="Add owner/environment/cost-center tags; enforce with Azure Policy (require tag / inherit from RG).",
            validation_steps=["Agree a tagging standard with application owners"],
        ))
    return recs
