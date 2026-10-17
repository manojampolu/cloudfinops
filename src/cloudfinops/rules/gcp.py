"""GCP optimization rules. Like the Azure rules, these flag review candidates with evidence; nothing is changed."""

from __future__ import annotations

from collections import defaultdict

from ..models import Recommendation, Resource
from ..pricing_gcp import (ARTIFACT_FREE_GB, ARTIFACT_GB_MONTH, GCS_GB_MONTH, LOGGING_FREE_GIB, LOGGING_GIB,
                           LOGGING_RETENTION_GIB_MONTH, MACHINE_HOURLY, machine_downsize, run_idle_monthly)
from .azure import is_non_prod
from .base import Context, account_rule, make, owner_of, resource_rule

# APIs that are part of the platform and normally see little direct traffic
PLATFORM_APIS = {
    "serviceusage.googleapis.com", "cloudresourcemanager.googleapis.com", "iam.googleapis.com",
    "iamcredentials.googleapis.com", "logging.googleapis.com", "monitoring.googleapis.com", "cloudtrace.googleapis.com",
    "storage-api.googleapis.com", "storage-component.googleapis.com", "storage.googleapis.com", "telemetry.googleapis.com",
    "cloudasset.googleapis.com", "recommender.googleapis.com", "oslogin.googleapis.com", "servicemanagement.googleapis.com",
    "bigquerystorage.googleapis.com", "datastore.googleapis.com", "sql-component.googleapis.com",
    "cloudapis.googleapis.com", "pubsub.googleapis.com", "containerregistry.googleapis.com",
    "artifactregistry.googleapis.com", "cloudbuild.googleapis.com", "run.googleapis.com", "cloudfunctions.googleapis.com",
    "privilegedaccessmanager.googleapis.com", "appoptimize.googleapis.com", "geminicloudassist.googleapis.com",
    "cloudaicompanion.googleapis.com", "firebase.googleapis.com", "firebaseinstallations.googleapis.com",
    "fcm.googleapis.com", "fcmregistrations.googleapis.com", "firebaserules.googleapis.com",
}
TAGGABLE = ("gcp.vm", "gcp.disk", "gcp.bucket", "gcp.run_service", "gcp.function", "gcp.sql", "gcp.gke",
            "gcp.artifact_repo")


def _cost(res: Resource) -> float:
    return res.monthly_cost or 0.0


def _m(v: float) -> str:
    return f"${v:,.2f}"


def _cost_ev(res: Resource) -> str:
    src = {"billing": "billing export, last 30 days", "estimate": "list-price estimate (no billing export)"}.get(
        res.cost_source, "unknown")
    return f"Current cost {_m(_cost(res))}/mo ({src})"


def _cost_missing(res: Resource) -> list[str]:
    return [] if res.cost_source == "billing" else ["Actual cost (no billing export; list-price estimate used)"]


# =========================================================================== compute engine

@resource_rule("gcp-vm-idle", "Running VMs with near-zero CPU", "gcp.vm")
def vm_idle(res: Resource, ctx: Context):
    m = res.metrics
    if res.attributes.get("state") != "running" or "cpu_max" not in m or m["cpu_max"] >= ctx.cfg.idle_cpu_max_pct:
        return None
    return make("gcp-vm-idle", res, title=f"Idle VM ({res.sku})", category="waste", est_monthly_savings=_cost(res),
                confidence="medium", risk="medium",
                evidence=[f"CPU over 14 days: avg {m['cpu_avg']:.1f}%, max {m['cpu_max']:.1f}%", _cost_ev(res)],
                missing_evidence=["Network activity not evaluated", *_cost_missing(res)],
                action="Confirm with the owner, then stop (disks keep billing) or delete the VM.",
                validation_steps=["Check cron/batch jobs and inbound traffic"])


@resource_rule("gcp-vm-rightsize", "Over-provisioned VMs (one size down)", "gcp.vm")
def vm_rightsize(res: Resource, ctx: Context):
    m, cfg = res.metrics, ctx.cfg
    if res.attributes.get("state") != "running" or "cpu_p95" not in m or m["cpu_max"] < cfg.idle_cpu_max_pct:
        return None
    if m["cpu_p95"] >= cfg.rightsize_cpu_p95_pct or m["cpu_avg"] >= cfg.rightsize_cpu_avg_pct:
        return None
    mem = m.get("mem_used_p95")
    if mem is not None and mem >= cfg.rightsize_mem_p95_pct:
        return None
    target = machine_downsize(res.sku)
    if not target:
        return None
    cur, new = MACHINE_HOURLY.get(res.sku), MACHINE_HOURLY.get(target)
    ratio = 1 - new / cur if cur and new else 0.5
    return make("gcp-vm-rightsize", res, title=f"Downsize {res.sku} -> {target}", category="rightsizing",
                est_monthly_savings=_cost(res) * ratio, confidence="high" if mem is not None else "low",
                effort="medium", risk="medium",
                evidence=[f"CPU p95 {m['cpu_p95']:.0f}%, avg {m['cpu_avg']:.0f}% over 14 days", _cost_ev(res)]
                + ([f"Memory used p95 {mem:.0f}%"] if mem is not None else []),
                missing_evidence=([] if mem is not None else ["Memory utilization (install the Ops Agent)"]) + _cost_missing(res),
                action=f"Stop the VM, change machine type to {target}, start it (or apply the Recommender suggestion).",
                validation_steps=["Check peaks outside the 14-day window", "Consider custom machine types"])


@resource_rule("gcp-nonprod-schedule", "Non-production VMs running 24/7", "gcp.vm")
def vm_schedule(res: Resource, ctx: Context):
    reason = is_non_prod(res, ctx)
    m = res.metrics
    if not reason or res.attributes.get("state") != "running" or m.get("uptime_share", 0) < ctx.cfg.scheduling_min_uptime_share:
        return None
    off = 1 - ctx.cfg.non_prod_on_hours_per_week / 168
    return make("gcp-nonprod-schedule", res, title=f"Schedule non-prod VM off-hours ({res.sku})", category="scheduling",
                est_monthly_savings=_cost(res) * off, confidence="medium",
                evidence=[f"Classified non-production: {reason}", f"Running {m['uptime_share']:.0%} of hours", _cost_ev(res)],
                missing_evidence=["Agreed operating hours", *_cost_missing(res)],
                action="Attach an instance schedule (Compute Engine > Instance schedules) to stop outside working hours.",
                validation_steps=["Check overnight jobs and dependencies"])


@resource_rule("gcp-vm-stopped", "Stopped VMs still paying for disks", "gcp.vm")
def vm_stopped(res: Resource, ctx: Context):
    if res.attributes.get("state") != "stopped":
        return None
    continuing, items = ctx.related_cost(res, "gcp.disk", "gcp.address")
    if continuing < 0.5:
        return None
    age = ctx.age_days(res.attributes.get("stopped_since"))
    return make("gcp-vm-stopped", res, title=f"Stopped VM still paying {_m(continuing)}/mo for disks/IPs",
                category="waste", est_monthly_savings=continuing, current_monthly_cost=continuing,
                confidence="medium" if age and age > 30 else "low",
                evidence=[f"Status {res.attributes.get('status')}" + (f" for {age} days" if age else ""),
                          *[f"{i.service}: {i.display_name} {_m(_cost(i))}/mo" for i in items]],
                action="Snapshot/machine-image if needed, then delete the VM and its disks.")


@resource_rule("gcp-disk-unattached", "Persistent disks not attached to any VM", "gcp.disk")
def disk_unattached(res: Resource, ctx: Context):
    if res.attributes.get("attached"):
        return None
    age = ctx.age_days(res.attributes.get("last_detached") or res.attributes.get("created"))
    return make("gcp-disk-unattached", res, title=f"Unattached disk ({res.attributes.get('size_gb'):.0f} GB {res.sku})",
                category="waste", est_monthly_savings=_cost(res), confidence="high",
                evidence=["No users attached", _cost_ev(res)] + ([f"Detached/created ~{age} days ago"] if age else []),
                missing_evidence=_cost_missing(res),
                action="Snapshot if the data may be needed, then delete the disk.")


@resource_rule("gcp-ip-unused", "Reserved external IPs not in use", "gcp.address")
def ip_unused(res: Resource, ctx: Context):
    if res.attributes.get("status") != "RESERVED" or res.attributes.get("address_type") != "EXTERNAL":
        return None
    return make("gcp-ip-unused", res, title=f"Unused static external IP ({res.attributes.get('address')})",
                category="waste", est_monthly_savings=_cost(res), confidence="high",
                evidence=["Status RESERVED (not attached)", _cost_ev(res)],
                action="Release the address (check DNS and allow-lists first).")


@resource_rule("gcp-snapshot-old", "Old disk snapshots", "gcp.snapshot")
def snapshot_old(res: Resource, ctx: Context):
    age = ctx.age_days(res.attributes.get("created"))
    if age is None or age <= ctx.cfg.snapshot_max_age_days:
        return None
    return make("gcp-snapshot-old", res, title=f"Snapshot is {age} days old", category="waste",
                est_monthly_savings=_cost(res), confidence="medium",
                evidence=[f"Created {age} days ago", _cost_ev(res)], missing_evidence=_cost_missing(res),
                action="Delete if not needed for retention, or move to archive snapshots.")


# =========================================================================== serverless & storage

@resource_rule("gcp-run-min-instances", "Cloud Run minimum instances on low traffic", "gcp.run_service")
def run_min_instances(res: Resource, ctx: Context):
    a, m = res.attributes, res.metrics
    if not a.get("min_instances"):
        return None
    req = m.get("requests_30d")
    if req is not None and req >= ctx.cfg.gcp_run_low_requests_30d:
        return None
    idle = run_idle_monthly(a.get("cpu", 1.0), a.get("memory_gib", 0.5), a["min_instances"])
    if a.get("cpu_always_allocated"):
        idle *= 4  # instance-based billing for always-allocated CPU is several times the idle rate
    return make("gcp-run-min-instances", res,
                title=f"Cloud Run keeps {a['min_instances']} warm instance(s) for "
                      f"{'no' if req == 0 else 'little' if req is not None else 'unknown'} traffic",
                category="waste", est_monthly_savings=_cost(res) if res.cost_source == "billing" else idle,
                confidence="medium" if req is not None else "low",
                evidence=[f"min-instances = {a['min_instances']} ({res.sku})",
                          f"Requests in last 30 days: {req:,.0f}" if req is not None else "Request metrics unavailable",
                          f"CPU always allocated: {'yes' if a.get('cpu_always_allocated') else 'no'}"],
                assumptions=["Estimated from Cloud Run idle-instance list prices"] if res.cost_source != "billing" else [],
                missing_evidence=_cost_missing(res) + ([] if req is not None else ["Request volume"]),
                action="Set min-instances to 0 (accept cold starts) or reduce it; keep CPU throttling on for low traffic.",
                validation_steps=["Check cold-start latency requirements"])


@resource_rule("gcp-run-unused", "Cloud Run services with no requests", "gcp.run_service")
def run_unused(res: Resource, ctx: Context):
    if res.metrics.get("requests_30d") != 0 or res.attributes.get("min_instances"):
        return None
    return make("gcp-run-unused", res, title="Cloud Run service received no requests in 30 days", category="governance",
                est_monthly_savings=0.0, confidence="high",
                evidence=["0 requests in 30 days", "min-instances = 0 (no idle compute charge)"],
                assumptions=["Direct savings are small (images in Artifact Registry); main benefit is a smaller attack surface"],
                action="Delete the service (and its container images) if it is no longer used.")


@resource_rule("gcp-function-unused", "Cloud Functions with no executions", "gcp.function")
def function_unused(res: Resource, ctx: Context):
    if res.metrics.get("executions_30d") != 0:
        return None
    return make("gcp-function-unused", res, title=f"Cloud Function ({res.sku}) had no executions in 30 days",
                category="governance", est_monthly_savings=0.0, confidence="high",
                evidence=["0 executions in 30 days", f"Runtime {res.attributes.get('runtime')}",
                          f"Last updated {res.attributes.get('updated')}"],
                assumptions=["Each deployment also leaves container images in Artifact Registry (gcf-artifacts)"],
                action="Delete unused functions; clean up their build images.")


@resource_rule("gcp-artifact-cleanup", "Artifact Registry repositories without cleanup policies", "gcp.artifact_repo")
def artifact_cleanup(res: Resource, ctx: Context):
    gb = res.attributes.get("size_gb", 0)
    if gb < ctx.cfg.gcp_artifact_large_gb or res.attributes.get("cleanup_policies"):
        return None
    reclaim = max(0.0, gb * 0.8 - ARTIFACT_FREE_GB)
    hint = " (Cloud Functions build images)" if "gcf" in res.name else ""
    return make("gcp-artifact-cleanup", res, title=f"Artifact Registry repo '{res.name}' holds {gb:,.1f} GB with no cleanup policy{hint}",
                category="modernization", est_monthly_savings=reclaim * ARTIFACT_GB_MONTH, confidence="medium",
                evidence=[f"Repository size {gb:,.2f} GB ({res.sku})", "No cleanup policies configured"],
                assumptions=["~80% of stored images are old versions that a keep-recent policy would delete",
                             f"Storage ${ARTIFACT_GB_MONTH}/GB-month beyond {ARTIFACT_FREE_GB} GB free"],
                missing_evidence=_cost_missing(res),
                action="Add a cleanup policy (e.g. keep the 5 most recent versions, delete untagged images older than 30 days); "
                       "run it in dry-run mode first.",
                validation_steps=["Make sure rollback images you rely on are tagged/kept"])


@resource_rule("gcp-bucket-lifecycle", "Large buckets without lifecycle rules", "gcp.bucket")
def bucket_lifecycle(res: Resource, ctx: Context):
    gb = res.metrics.get("stored_gb", 0)
    if gb < ctx.cfg.gcp_bucket_large_gb or res.attributes.get("has_lifecycle") or res.sku not in ("STANDARD", "MULTI_REGIONAL", "REGIONAL"):
        return None
    saving = gb * 0.5 * (GCS_GB_MONTH.get(res.sku, 0.02) - GCS_GB_MONTH["NEARLINE"])
    return make("gcp-bucket-lifecycle", res, title=f"Bucket with {gb:,.0f} GB in {res.sku} and no lifecycle rules",
                category="modernization", est_monthly_savings=saving, confidence="low",
                evidence=[f"Stored {gb:,.1f} GB", "No lifecycle rules"],
                assumptions=["Half the data is rarely read and can move to Nearline/Autoclass"],
                missing_evidence=["Access pattern of the data", *_cost_missing(res)],
                action="Enable Autoclass or lifecycle rules (Nearline after 30 days, delete old versions).")


# =========================================================================== account-level

@account_rule("gcp-logging", "Cloud Logging ingestion above the free tier and long retention", provider="gcp")
def logging(ctx: Context) -> list[Recommendation]:
    recs = []
    for p, by_type in (ctx.extras.get("log_ingest") or {}).items():
        total = sum(by_type.values())
        billable = max(0.0, total - LOGGING_FREE_GIB)
        if billable <= 0:
            continue
        top = sorted(by_type.items(), key=lambda kv: -kv[1])[:5]
        recs.append(Recommendation(
            rule_id="gcp-log-ingestion", resource_id=f"//logging.googleapis.com/projects/{p}/ingestion", account=p,
            category="logging", title=f"Log ingestion {total:,.0f} GiB/mo in {p} ({billable:,.0f} GiB billable)",
            current_monthly_cost=billable * LOGGING_GIB, est_monthly_savings=0.0, savings_low=0.0,
            savings_high=billable * LOGGING_GIB * 0.5, confidence="low", service="Cloud Logging",
            evidence=[f"{k or 'other'}: {v:,.1f} GiB" for k, v in top],
            assumptions=["Upper bound: half of billable volume can be excluded (debug logs, health checks)"],
            missing_evidence=["Which logs are needed for operations / audit"],
            action="Add exclusion filters on the _Default sink for noisy resources; lower app log levels.",
            validation_steps=["Agree log requirements with operations/security"]))
    for res in ctx.resources:
        if res.type != "gcp.log_bucket" or res.name == "_Required":
            continue
        days = res.attributes.get("retention_days", 30)
        gib = sum((ctx.extras.get("log_ingest") or {}).get(res.account, {}).values())
        if days > 30 and gib:
            saving = gib * (days - 30) / 30 * LOGGING_RETENTION_GIB_MONTH
            recs.append(Recommendation(
                rule_id="gcp-log-retention", resource_id=res.id, account=res.account, category="logging",
                title=f"Log bucket '{res.name}' keeps logs {days} days", est_monthly_savings=saving, confidence="low",
                resource_type=res.type, service="Cloud Logging", resource_name=res.name,
                evidence=[f"Retention {days} days", f"~{gib:,.0f} GiB/month ingested in project"],
                assumptions=[f"Retention beyond 30 days costs ${LOGGING_RETENTION_GIB_MONTH}/GiB-month"],
                missing_evidence=["Retention policy requirement"],
                action="Reduce retention, or route long-term logs to a Cloud Storage archive bucket."))
    return recs


@account_rule("gcp-recommender", "Google Recommender findings (provider-native)", provider="gcp")
def recommender(ctx: Context) -> list[Recommendation]:
    recs = []
    for r in ctx.extras.get("recommendations") or []:
        res = ctx.by_id.get(r["resource_id"])
        project_rec = r["recommender"] in ("CLEANUP_PROJECT", "RECLAIM_PROJECT") or "projectUtilization" in r["name"]
        recs.append(Recommendation(
            rule_id="gcp-recommender", resource_id=r["resource_id"] if res else (f"//cloudresourcemanager.googleapis.com/projects/{r['project']}" if project_rec else r["resource_id"]),
            account=r["project"], category="waste" if r["monthly_savings"] or project_rec else "advisor",
            title=f"Recommender: {r['description']}"[:150], est_monthly_savings=r["monthly_savings"],
            current_monthly_cost=res.monthly_cost if res else None, confidence="medium",
            resource_type=res.type if res else "", resource_name=res.display_name if res else r["project"],
            service=res.service if res else ("Projects" if project_rec else ""), owner=owner_of(res, ctx.cfg),
            evidence=[f"Google Recommender ({r['recommender'] or 'recommendation'}, priority {r['priority']})"],
            assumptions=[f"Savings as projected by Google, converted from {r['currency']} to USD"] if r["monthly_savings"] else [],
            action=r["description"], validation_steps=["Review the recommendation details in the Cloud console"]))
    return recs


@account_rule("gcp-idle-billed-project", "Billing-linked projects with no API activity", provider="gcp")
def idle_billed_project(ctx: Context) -> list[Recommendation]:
    recs = []
    measured = ctx.extras.get("api_metrics_projects") or set()
    for p in sorted(ctx.extras.get("billed_projects") or set()):
        if p not in measured:
            continue
        apis = [r for r in ctx.resources if r.account == p and r.type == "gcp.api"]
        total = sum(r.metrics.get("requests_30d", 0) for r in apis)
        spend = sum(r.cost for r in ctx.records if r.account == p)
        if total > 0 or spend > 1:
            continue
        recs.append(Recommendation(
            rule_id="gcp-idle-billed-project", resource_id=f"//cloudresourcemanager.googleapis.com/projects/{p}",
            account=p, category="governance", service="Projects", resource_name=p,
            title=f"Billing-linked project {p} had no API requests in 30 days", est_monthly_savings=0.0,
            confidence="high", evidence=[f"{len(apis)} enabled API(s), 0 requests in 30 days", "Billing account linked"],
            assumptions=["No current spend, but any leaked key or credential in this project can create charges"],
            action="Unlink billing or shut the project down if it is not needed; otherwise restrict API keys.",
            validation_steps=["Confirm with the project owner; shutdown is reversible for 30 days"]))
    return recs


@account_rule("gcp-unused-apis", "Enabled APIs that receive no traffic", provider="gcp")
def unused_apis(ctx: Context) -> list[Recommendation]:
    recs = []
    measured = ctx.extras.get("api_metrics_projects") or set()
    by_project: dict[str, list[Resource]] = defaultdict(list)
    for r in ctx.resources:
        if r.type == "gcp.api" and r.account in measured and r.metrics.get("requests_30d", 0) == 0 \
                and r.name not in PLATFORM_APIS:
            by_project[r.account].append(r)
    for p, apis in by_project.items():
        if len(apis) < 2:
            continue
        recs.append(Recommendation(
            rule_id="gcp-unused-apis", resource_id=f"//serviceusage.googleapis.com/projects/{p}/unused", account=p,
            category="governance", service="APIs & services", resource_name=p,
            title=f"{len(apis)} enabled APIs in {p} had no requests in 30 days", est_monthly_savings=0.0,
            confidence="high", evidence=[", ".join(sorted(a.name.replace('.googleapis.com', '') for a in apis))[:600]],
            assumptions=["No direct savings; fewer enabled APIs reduce the blast radius of leaked credentials"],
            action="Disable APIs that are not needed (gcloud services disable ...), after confirming with owners."))
    return recs


@account_rule("gcp-unlabelled", "Resources missing required labels", provider="gcp")
def unlabelled(ctx: Context) -> list[Recommendation]:
    by_p: dict[str, list[Resource]] = defaultdict(list)
    for r in ctx.resources:
        if r.type in TAGGABLE and not all(r.tags.get(t.lower()) for t in ctx.cfg.required_tags):
            by_p[r.account].append(r)
    return [Recommendation(
        rule_id="gcp-unlabelled", resource_id=f"//cloudresourcemanager.googleapis.com/projects/{p}/labels", account=p,
        category="governance", service="Labels", resource_name=p,
        title=f"{len(items)} resources in {p} lack labels ({', '.join(ctx.cfg.required_tags)})",
        est_monthly_savings=0.0, current_monthly_cost=sum(_cost(r) for r in items), confidence="high",
        evidence=[f"{r.display_name} ({r.service})" for r in items[:8]],
        action="Add owner/environment/cost-center labels so billing export can be grouped by team.")
        for p, items in by_p.items()]
