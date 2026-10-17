"""Optimization rules for AWS resources and billing data."""

from __future__ import annotations

from collections import defaultdict

from .. import pricing
from ..analysis import window
from ..models import Recommendation, Resource
from .base import Context, account_rule, make, resource_rule


def _cost(res: Resource) -> float:
    return res.monthly_cost or 0.0


def _running(res: Resource) -> bool:
    return res.attributes.get("state", "running") == "running"


# --------------------------------------------------------------------------- waste

@resource_rule("ebs-unattached", "EBS volumes not attached to any instance", "ebs_volume")
def ebs_unattached(res: Resource, ctx: Context):
    if res.attributes.get("state") != "available":
        return None
    return make("ebs-unattached", res,
                title=f"Unattached EBS volume ({res.attributes.get('size_gb')} GB {res.attributes.get('volume_type')})",
                category="waste", est_monthly_savings=_cost(res),
                detail="Volume is in 'available' state - you pay for provisioned storage that nothing uses.",
                action="Snapshot the volume if the data may be needed, then delete it.")


@resource_rule("eip-unassociated", "Elastic IPs not associated with a running resource", "elastic_ip")
def eip_unassociated(res: Resource, ctx: Context):
    if res.attributes.get("association_id"):
        return None
    return make("eip-unassociated", res, title="Unassociated Elastic IP",
                category="waste", est_monthly_savings=_cost(res),
                detail="Public IPv4 addresses are billed hourly whether or not they are in use.",
                action="Release the Elastic IP (check DNS records and allow-lists first).")


@resource_rule("ebs-snapshot-old", "Snapshots older than the retention threshold", "ebs_snapshot")
def snapshot_old(res: Resource, ctx: Context):
    age = ctx.age_days(res.attributes.get("start_time"))
    if age is None or age <= ctx.cfg.snapshot_max_age_days:
        return None
    if res.tags.get("retention") in ("permanent", "legal-hold"):
        return None
    return make("ebs-snapshot-old", res, title=f"EBS snapshot is {age} days old",
                category="waste", est_monthly_savings=_cost(res),
                detail=f"Older than {ctx.cfg.snapshot_max_age_days} days. "
                       "Estimate assumes full size; incremental snapshots may cost less.",
                action="Delete if not required for compliance, or adopt Data Lifecycle Manager / "
                       "move to EBS Snapshots Archive (75% cheaper).")


@resource_rule("ec2-stopped-long", "Instances stopped for a long time still paying for EBS", "ec2_instance")
def ec2_stopped_long(res: Resource, ctx: Context):
    if res.attributes.get("state") != "stopped":
        return None
    age = ctx.age_days(res.attributes.get("stopped_since"))
    if age is None or age <= ctx.cfg.stopped_instance_max_days:
        return None
    vols = [ctx.by_id[v] for v in res.attributes.get("volume_ids", []) if v in ctx.by_id]
    storage = sum(_cost(v) for v in vols)
    return make("ec2-stopped-long", res, title=f"Instance stopped for {age} days",
                category="waste", est_monthly_savings=storage, current_monthly_cost=storage,
                detail=f"Attached EBS volumes ({len(vols)}) keep billing while the instance is stopped.",
                action="Create an AMI/snapshots and terminate the instance and its volumes.")


@resource_rule("lb-idle", "Load balancers with no traffic", "load_balancer")
def lb_idle(res: Resource, ctx: Context):
    requests = res.metrics.get("request_count_14d")
    if requests is None or requests > 0:
        return None
    return make("lb-idle", res, title="Load balancer received no requests in 14 days",
                category="waste", est_monthly_savings=_cost(res),
                detail="Idle load balancers are billed hourly plus LCU charges.",
                action="Delete the load balancer if the service it fronted is retired.")


@resource_rule("ec2-idle", "Running instances with near-zero CPU", "ec2_instance")
def ec2_idle(res: Resource, ctx: Context):
    cpu_max = res.metrics.get("cpu_max_14d")
    if not _running(res) or cpu_max is None or cpu_max >= ctx.cfg.idle_cpu_max_pct:
        return None
    return make("ec2-idle", res, title=f"Idle EC2 instance ({res.attributes.get('instance_type')})",
                category="waste", est_monthly_savings=_cost(res), effort="low", risk="medium",
                detail=f"Max CPU {cpu_max:.1f}% over 14 days (threshold {ctx.cfg.idle_cpu_max_pct}%).",
                action="Confirm with the owner, then stop or terminate it.")


@resource_rule("rds-idle", "Databases with no connections", "rds_instance")
def rds_idle(res: Resource, ctx: Context):
    if res.metrics.get("connections_max_14d") != 0:
        return None
    return make("rds-idle", res, title=f"RDS instance has had no connections in 14 days",
                category="waste", est_monthly_savings=_cost(res), risk="medium",
                detail=f"{res.attributes.get('engine')} {res.attributes.get('instance_class')}",
                action="Take a final snapshot and delete it, or stop it (auto-restarts after 7 days).")


# --------------------------------------------------------------------------- rightsizing

@resource_rule("ec2-rightsize", "Over-provisioned instances", "ec2_instance")
def ec2_rightsize(res: Resource, ctx: Context):
    cpu_avg, cpu_max = res.metrics.get("cpu_avg_14d"), res.metrics.get("cpu_max_14d")
    if not _running(res) or cpu_avg is None or cpu_max is None:
        return None
    if cpu_max < ctx.cfg.idle_cpu_max_pct:  # handled by ec2-idle
        return None
    if cpu_avg >= ctx.cfg.rightsize_cpu_avg_pct or cpu_max >= ctx.cfg.rightsize_cpu_max_pct:
        return None
    itype = res.attributes.get("instance_type", "")
    target = pricing.downsize(itype)
    if not target:
        return None
    cur, new = pricing.EC2_HOURLY.get(itype), pricing.EC2_HOURLY.get(target)
    ratio = (1 - new / cur) if cur and new else 0.5
    return make("ec2-rightsize", res, title=f"Downsize {itype} -> {target}",
                category="rightsizing", est_monthly_savings=_cost(res) * ratio,
                effort="medium", risk="medium",
                detail=f"CPU avg {cpu_avg:.1f}% / max {cpu_max:.1f}% over 14 days. "
                       "Check memory utilization (CloudWatch agent) before resizing.",
                action=f"Change instance type to {target} during a maintenance window.")


@resource_rule("rds-rightsize", "Over-provisioned databases", "rds_instance")
def rds_rightsize(res: Resource, ctx: Context):
    cpu_avg, cpu_max = res.metrics.get("cpu_avg_14d"), res.metrics.get("cpu_max_14d")
    if cpu_avg is None or res.metrics.get("connections_max_14d") == 0:
        return None
    if cpu_avg >= ctx.cfg.rightsize_cpu_avg_pct or (cpu_max or 0) >= ctx.cfg.rightsize_cpu_max_pct:
        return None
    cls = res.attributes.get("instance_class", "")
    target = pricing.downsize(cls)
    if not target:
        return None
    return make("rds-rightsize", res, title=f"Downsize {cls} -> {target}",
                category="rightsizing", est_monthly_savings=_cost(res) * 0.5,
                effort="medium", risk="medium",
                detail=f"CPU avg {cpu_avg:.1f}% over 14 days. Verify freeable memory and IOPS first.",
                action=f"Modify the DB instance class to {target} in the next maintenance window.")


# --------------------------------------------------------------------------- modernization

@resource_rule("ebs-gp2-to-gp3", "gp2 volumes that can move to cheaper gp3", "ebs_volume")
def ebs_gp2_to_gp3(res: Resource, ctx: Context):
    if res.attributes.get("volume_type") != "gp2" or res.attributes.get("state") == "available":
        return None
    return make("ebs-gp2-to-gp3", res, title=f"Migrate gp2 -> gp3 ({res.attributes.get('size_gb')} GB)",
                category="modernization", est_monthly_savings=_cost(res) * 0.2,
                detail="gp3 is 20% cheaper per GB with baseline 3,000 IOPS / 125 MB/s included.",
                action="Modify volume type to gp3 (online, no downtime).")


@resource_rule("ec2-graviton", "x86 instances with a cheaper Graviton equivalent", "ec2_instance")
def ec2_graviton(res: Resource, ctx: Context):
    if not _running(res):
        return None
    itype = res.attributes.get("instance_type", "")
    parts = pricing.split_type(itype)
    if not parts:
        return None
    _, family, size = parts
    target_family = pricing.GRAVITON_EQUIVALENT.get(family)
    if not target_family:
        return None
    target = f"{target_family}.{size}"
    cur, new = pricing.EC2_HOURLY.get(itype), pricing.EC2_HOURLY.get(target)
    ratio = (1 - new / cur) if cur and new else 0.2
    prev_gen = family in pricing.PREVIOUS_GEN_FAMILIES
    return make("ec2-graviton", res,
                title=f"{'Previous-generation' if prev_gen else 'x86'} {itype} -> Graviton {target}",
                category="modernization", est_monthly_savings=_cost(res) * ratio,
                effort="medium", risk="low",
                detail="Graviton (arm64) offers ~20% lower price and better price-performance. "
                       "Requires arm64-compatible AMI and binaries."
                       + (f" If arm64 isn't feasible, move to {pricing.CURRENT_GEN_X86.get(family, 'a current-gen family')}." if prev_gen else ""),
                action=f"Test the workload on {target}, then switch.")


# --------------------------------------------------------------------------- scheduling

@resource_rule("nonprod-schedule", "Non-production resources running 24/7", "ec2_instance", "rds_instance")
def nonprod_schedule(res: Resource, ctx: Context):
    env = res.tags.get("environment", "").lower()
    if env not in ctx.cfg.non_prod_env_values or not _running(res):
        return None
    if res.metrics.get("cpu_max_14d", 100) < ctx.cfg.idle_cpu_max_pct:  # idle rule is better
        return None
    off_share = 1 - ctx.cfg.non_prod_on_hours_per_week / 168
    return make("nonprod-schedule", res, title=f"Schedule '{env}' {res.type.replace('_', ' ')} off-hours",
                category="scheduling", est_monthly_savings=_cost(res) * off_share, effort="low",
                detail=f"Running only {ctx.cfg.non_prod_on_hours_per_week:.0f}h/week instead of 168h "
                       f"cuts compute by {off_share:.0%}.",
                action="Use AWS Instance Scheduler / EventBridge Scheduler to stop nights and weekends.")


# --------------------------------------------------------------------------- storage

@resource_rule("s3-lifecycle", "Large S3 buckets without lifecycle / tiering", "s3_bucket")
def s3_lifecycle(res: Resource, ctx: Context):
    gb = float(res.attributes.get("standard_gb", 0))
    if gb < ctx.cfg.s3_large_bucket_gb or res.attributes.get("has_lifecycle_policy"):
        return None
    savings = gb * 0.5 * (pricing.S3_STANDARD_GB_MONTH - pricing.S3_INFREQUENT_GB_MONTH)
    return make("s3-lifecycle", res, title=f"S3 bucket with {gb / 1024:.1f} TB in Standard, no lifecycle policy",
                category="modernization", est_monthly_savings=savings,
                detail="Estimate assumes half the data is rarely accessed.",
                action="Enable S3 Intelligent-Tiering or lifecycle transitions; expire old versions "
                       "and incomplete multipart uploads.")


# --------------------------------------------------------------------------- governance

@resource_rule("untagged", "Resources missing required cost-allocation tags",
               "ec2_instance", "ebs_volume", "rds_instance", "load_balancer", "s3_bucket")
def untagged(res: Resource, ctx: Context):
    missing = [t for t in ctx.cfg.required_tags if not res.tags.get(t.lower())]
    if not missing:
        return None
    return make("untagged", res, title=f"Missing tags: {', '.join(missing)}",
                category="governance", est_monthly_savings=0.0,
                detail="Untagged spend can't be allocated to a team, which hides waste.",
                action="Add the tags and enforce them with AWS Organizations tag policies / SCPs.")


# --------------------------------------------------------------------------- account-level

COMPUTE_SERVICES = ("Amazon Elastic Compute Cloud - Compute", "Amazon EC2", "AWS Fargate",
                    "AWS Lambda", "Amazon Elastic Container Service")


@account_rule("savings-plan", "Steady on-demand compute spend that could be covered by a Savings Plan")
def savings_plan(ctx: Context) -> list[Recommendation]:
    recent = window(ctx.records, ctx.cfg.lookback_days)
    compute = [r for r in recent if r.service in COMPUTE_SERVICES]
    if not compute:
        return []
    has_pricing = any(r.pricing_category for r in compute)
    on_demand = [r for r in compute if r.pricing_category in ("On-Demand", "")]
    daily = defaultdict(float)
    for r in on_demand:
        daily[r.day] += r.cost
    if len(daily) < 7:
        return []
    values = sorted(daily.values())
    baseline = values[int(len(values) * 0.1)]  # p10: spend you reliably have every day
    if baseline * 30 < 100:
        return []
    savings = baseline * 30 * ctx.cfg.savings_plan_discount
    note = "" if has_pricing else " Billing data has no pricing category; all compute assumed On-Demand."
    return [Recommendation(
        rule_id="savings-plan", category="commitment",
        title=f"Commit ${baseline / 24:.2f}/hour via a Compute Savings Plan",
        est_monthly_savings=savings, current_monthly_cost=sum(daily.values()) / len(daily) * 30,
        effort="low", risk="low",
        detail=f"Your lowest-10% daily on-demand compute spend is ${baseline:,.2f}; committing to that "
               f"baseline is low-risk. Assumes ~{ctx.cfg.savings_plan_discount:.0%} discount (1-yr, no upfront)."
               + note,
        action="Review Cost Explorer > Savings Plans recommendations and purchase. "
               "Do this AFTER rightsizing/cleanup so you don't commit to waste.",
    )]


@account_rule("data-transfer", "High share of spend on data transfer")
def data_transfer(ctx: Context) -> list[Recommendation]:
    recent = window(ctx.records, ctx.cfg.lookback_days)
    total = sum(r.cost for r in recent)
    if not total:
        return []
    dt = sum(r.cost for r in recent
             if "data transfer" in r.service.lower() or "datatransfer" in r.usage_type.lower().replace("-", "")
             or "natgateway" in r.usage_type.lower().replace("-", ""))
    share = dt / total
    if share < ctx.cfg.data_transfer_share_warn:
        return []
    return [Recommendation(
        rule_id="data-transfer", category="governance",
        title=f"Data transfer / NAT is {share:.0%} of spend (${dt * 30 / ctx.cfg.lookback_days:,.0f}/mo)",
        current_monthly_cost=dt * 30 / ctx.cfg.lookback_days, est_monthly_savings=0.0,
        effort="medium", risk="low",
        detail="Common causes: NAT Gateway processing for S3/DynamoDB traffic, cross-AZ chatter, "
               "internet egress without a CDN.",
        action="Add S3/DynamoDB gateway VPC endpoints, keep chatty services in one AZ, front egress with CloudFront.",
    )]
