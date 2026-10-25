"""Optimization rules for AWS resources and billing data."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from .. import pricing
from ..analysis import window
from ..models import Recommendation, Resource
from .base import Context, account_rule, is_non_prod, make, owner_of, resource_rule


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
    if not _running(res) or cpu_max is None or cpu_max >= ctx.cfg.idle_cpu_max_pct or res.attributes.get("asg"):
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
    if not _running(res) or cpu_avg is None or cpu_max is None or res.attributes.get("asg"):
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
    reason = is_non_prod(res, ctx)
    if not reason or not _running(res) or res.attributes.get("asg"):
        return None
    env = res.tags.get("environment") or res.tags.get("env") or "non-prod"
    if res.metrics.get("cpu_max_14d", 100) < ctx.cfg.idle_cpu_max_pct:  # idle rule is better
        return None
    off_share = 1 - ctx.cfg.non_prod_on_hours_per_week / 168
    return make("nonprod-schedule", res, title=f"Schedule '{env}' {res.type.replace('_', ' ')} off-hours",
                category="scheduling", est_monthly_savings=_cost(res) * off_share, effort="low",
                detail=f"Classified non-production: {reason}. Running only {ctx.cfg.non_prod_on_hours_per_week:.0f}h/week "
                       f"instead of 168h cuts compute by {off_share:.0%}.",
                action="Use AWS Instance Scheduler / EventBridge Scheduler to stop nights and weekends.")


# --------------------------------------------------------------------------- storage

@resource_rule("s3-lifecycle", "Large S3 buckets without lifecycle / tiering", "s3_bucket")
def s3_lifecycle(res: Resource, ctx: Context):
    gb = float(res.attributes.get("standard_gb", 0))
    if gb < ctx.cfg.s3_large_bucket_gb or res.attributes.get("has_lifecycle_policy") is not False:
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
    if "account_alias" in ctx.extras:  # assess --provider aws: rolled up per account by aws-untagged
        return None
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
    daily = defaultdict(float)
    rows = ctx.extras.get("pricing_model_daily")
    if rows:  # Cost Explorer by purchase type (assess --provider aws)
        end = max(r["day"] for r in rows)
        start = end - timedelta(days=ctx.cfg.lookback_days - 1)
        for r in rows:
            if r["service"] in COMPUTE_SERVICES and r["pricing_model"] == "On-Demand" and start <= r["day"] <= end:
                daily[r["day"]] += r["cost"]
        has_pricing = True
    else:
        recent = window(ctx.records, ctx.cfg.lookback_days)
        compute = [r for r in recent if r.service in COMPUTE_SERVICES]
        if not compute:
            return []
        has_pricing = any(r.pricing_category for r in compute)
        for r in compute:
            if r.pricing_category in ("On-Demand", ""):
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
    usage = ctx.extras.get("usage_types")
    if usage:  # Cost Explorer usage types for the last 30 days (assess --provider aws)
        dt = sum(v["cost"] for k, v in usage.items()
                 if any(x in k.lower().replace("-", "") for x in ("datatransfer", "natgatewaybytes", "awsout")))
        dt *= ctx.cfg.lookback_days / 30
    else:
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


# =========================================================================== assess --provider aws: more services

# Confidence / evidence gaps for the original rules (applied in run_rules; they predate the evidence model)
LEGACY_PROFILE = {
    "ebs-unattached": ("high", []), "eip-unassociated": ("high", []), "ebs-gp2-to-gp3": ("high", []),
    "untagged": ("high", []), "ebs-snapshot-old": ("medium", []), "ec2-stopped-long": ("medium", []),
    "lb-idle": ("medium", []), "ec2-idle": ("medium", ["Network activity not evaluated"]),
    "rds-idle": ("medium", []), "savings-plan": ("medium", ["12-month demand forecast"]),
    "ec2-rightsize": ("low", ["Memory utilization (needs the CloudWatch agent)"]),
    "rds-rightsize": ("low", ["Freeable memory and IOPS headroom"]),
    "ec2-graviton": ("low", ["arm64 compatibility of AMI, agents and binaries"]),
    "nonprod-schedule": ("medium", ["Agreed operating hours"]),
    "s3-lifecycle": ("low", ["Access pattern of the data"]), "data-transfer": ("low", ["Traffic destinations"]),
}
SECURITY_SERVICES = ("Amazon GuardDuty", "Amazon Inspector", "AWS Security Hub", "Amazon Macie", "Amazon Detective")


def _money(v: float) -> str:
    return f"${v:,.2f}"


def _cost_ev(res: Resource) -> str:
    src = {"billing": "billing export", "estimate": "list-price estimate"}.get(res.cost_source, "unknown")
    return f"Current cost {_money(_cost(res))}/mo ({src})"


def _members(res: Resource, ctx: Context) -> list[Resource]:
    return [ctx.by_id[i] for i in res.attributes.get("instance_ids", []) if i in ctx.by_id]


@resource_rule("aws-nat-idle", "NAT gateways with no traffic", "aws.nat_gateway")
def nat_idle(res: Resource, ctx: Context):
    if res.attributes.get("state") != "available" or res.metrics.get("bytes_window") != 0:
        return None
    return make("aws-nat-idle", res, title="NAT gateway carried no traffic in 14 days", category="waste",
                est_monthly_savings=_cost(res), confidence="medium",
                evidence=["BytesOutToDestination = 0 over the metrics window", _cost_ev(res)],
                action="Delete the NAT gateway (and release its Elastic IP) if nothing in the VPC needs outbound access.")


@resource_rule("aws-nat-heavy", "NAT gateways processing a lot of data", "aws.nat_gateway")
def nat_heavy(res: Resource, ctx: Context):
    gb = res.metrics.get("processed_gb_30d", 0)
    if gb < ctx.cfg.nat_heavy_gb_30d:
        return None
    processing = gb * pricing.NAT_GATEWAY_GB
    eps = res.attributes.get("vpc_endpoints") or []
    has_s3 = any(e.startswith("s3:Gateway") for e in eps)
    has_ddb = any(e.startswith("dynamodb:Gateway") for e in eps)
    return make("aws-nat-heavy", res, title=f"NAT gateway processes ~{gb:,.0f} GB/month ({_money(processing)} processing)",
                category="modernization", est_monthly_savings=0.0, savings_low=0.0, savings_high=processing * 0.5,
                confidence="low",
                evidence=[f"~{gb:,.0f} GB/month through the NAT (${pricing.NAT_GATEWAY_GB}/GB)",
                          f"S3 gateway endpoint in VPC: {'yes' if has_s3 else 'no'}; DynamoDB: {'yes' if has_ddb else 'no'}"],
                assumptions=["Upper bound: half the traffic goes to AWS services reachable through free gateway endpoints"],
                missing_evidence=["Traffic destinations (VPC Flow Logs)"],
                action="Add S3/DynamoDB gateway endpoints (free) and interface endpoints for chatty AWS APIs; "
                       "keep NAT per AZ to avoid cross-AZ charges.")


@resource_rule("aws-vpn-down", "Site-to-Site VPN connections with all tunnels down", "aws.vpn_connection")
def vpn_down(res: Resource, ctx: Context):
    tunnels = res.attributes.get("tunnels") or []
    if res.attributes.get("state") != "available" or not tunnels or any(t == "UP" for t in tunnels):
        return None
    return make("aws-vpn-down", res, title="VPN connection with all tunnels down", category="waste",
                est_monthly_savings=_cost(res), confidence="medium",
                evidence=[f"Tunnel status: {', '.join(tunnels)}", _cost_ev(res)],
                action="Delete the VPN connection if the remote site is gone (billed hourly while it exists).")


@resource_rule("aws-network-firewall-nonprod", "Network Firewall in non-production", "aws.network_firewall")
def firewall_nonprod(res: Resource, ctx: Context):
    reason = is_non_prod(res, ctx)
    if not reason:
        return None
    return make("aws-network-firewall-nonprod", res,
                title=f"Non-prod Network Firewall with {res.attributes.get('endpoints', 1)} endpoint(s)", category="waste",
                est_monthly_savings=_cost(res), confidence="low",
                evidence=[f"Classified non-production: {reason}", _cost_ev(res)],
                missing_evidence=["Security requirement for traffic inspection in this environment"],
                action="Use a shared inspection VPC, or security groups/NACLs only, for non-production.")


@resource_rule("aws-asg-idle", "Auto Scaling groups with near-zero CPU", "aws.asg")
def asg_idle(res: Resource, ctx: Context):
    m = res.metrics
    cost = sum(_cost(i) for i in _members(res, ctx))
    if "cpu_max" not in m or m["cpu_max"] >= ctx.cfg.idle_cpu_max_pct or cost < 1:
        return None
    return make("aws-asg-idle", res, title=f"Idle Auto Scaling group ({res.attributes.get('desired')} x {res.sku})",
                category="waste", est_monthly_savings=cost, current_monthly_cost=cost, confidence="medium", risk="medium",
                evidence=[f"Group CPU max {m['cpu_max']:.1f}% over 14 days", f"{len(_members(res, ctx))} instance(s)"],
                related=res.attributes.get("instance_ids", []),
                action="Set desired/min capacity to 0 or delete the group after confirming with the owner.")


@resource_rule("aws-asg-rightsize", "Over-provisioned Auto Scaling groups", "aws.asg")
def asg_rightsize(res: Resource, ctx: Context):
    m, cfg = res.metrics, ctx.cfg
    cost = sum(_cost(i) for i in _members(res, ctx))
    if "cpu_p95" not in m or m.get("cpu_max", 0) < cfg.idle_cpu_max_pct or cost < 1:
        return None
    if m["cpu_p95"] >= cfg.rightsize_cpu_p95_pct or m["cpu_avg"] >= cfg.rightsize_cpu_avg_pct:
        return None
    target = pricing.downsize(res.sku)
    action = (f"Change the launch template to {target}" if target else "Lower min/desired capacity") + \
        " or add target-tracking scaling on CPU."
    return make("aws-asg-rightsize", res, title=f"Auto Scaling group runs at {m['cpu_p95']:.0f}% CPU p95 ({res.attributes.get('desired')} x {res.sku})",
                category="rightsizing", est_monthly_savings=cost * 0.5, current_monthly_cost=cost, confidence="low",
                effort="medium", evidence=[f"Group CPU avg {m['cpu_avg']:.0f}%, p95 {m['cpu_p95']:.0f}%", f"Desired {res.attributes.get('desired')}"],
                assumptions=["Half the capacity (smaller type or fewer instances) carries the load"],
                missing_evidence=["Memory utilization", "Scaling policies and peak events"],
                related=res.attributes.get("instance_ids", []), action=action)


@resource_rule("aws-asg-nonprod-schedule", "Non-production Auto Scaling groups running 24/7", "aws.asg")
def asg_schedule(res: Resource, ctx: Context):
    reason = is_non_prod(res, ctx)
    cost = sum(_cost(i) for i in _members(res, ctx))
    if not reason or not res.attributes.get("desired") or cost < 1:
        return None
    off = 1 - ctx.cfg.non_prod_on_hours_per_week / 168
    return make("aws-asg-nonprod-schedule", res, title=f"Schedule non-prod Auto Scaling group to 0 off-hours",
                category="scheduling", est_monthly_savings=cost * off, current_monthly_cost=cost, confidence="medium",
                evidence=[f"Classified non-production: {reason}", f"Desired capacity {res.attributes.get('desired')}"],
                missing_evidence=["Agreed operating hours"], related=res.attributes.get("instance_ids", []),
                action="Add scheduled actions (desired 0 at night/weekends, restore in the morning).")


@resource_rule("aws-ecs-rightsize", "Fargate services with low CPU", "aws.ecs_service")
def ecs_rightsize(res: Resource, ctx: Context):
    m, a = res.metrics, res.attributes
    if a.get("launch_type") != "FARGATE" or "cpu_p95" not in m or _cost(res) < 5:
        return None
    if m["cpu_p95"] >= 30 or m["cpu_avg"] >= 15:
        return None
    return make("aws-ecs-rightsize", res, title=f"Fargate service uses {m['cpu_p95']:.0f}% CPU p95 ({res.sku} x {a.get('running')})",
                category="rightsizing", est_monthly_savings=_cost(res) * 0.5, confidence="low", effort="medium",
                evidence=[f"CPU avg {m['cpu_avg']:.0f}%, p95 {m['cpu_p95']:.0f}% of task allocation", _cost_ev(res)],
                assumptions=["Half the task size (or task count) is enough"],
                missing_evidence=["Memory utilization (MemoryUtilization not collected)"],
                action="Reduce task CPU/memory in the task definition, or lower the desired count with service auto scaling.")


@resource_rule("aws-ecs-nonprod-schedule", "Non-production ECS services running 24/7", "aws.ecs_service")
def ecs_schedule(res: Resource, ctx: Context):
    reason = is_non_prod(res, ctx) or (is_non_prod(Resource(id="", type="", name=res.attributes.get("cluster", "")), ctx))
    if not reason or not res.attributes.get("desired") or _cost(res) < 1:
        return None
    off = 1 - ctx.cfg.non_prod_on_hours_per_week / 168
    return make("aws-ecs-nonprod-schedule", res, title="Scale non-prod ECS service to 0 off-hours", category="scheduling",
                est_monthly_savings=_cost(res) * off, confidence="medium",
                evidence=[f"Classified non-production: {reason}", f"Desired tasks {res.attributes.get('desired')}", _cost_ev(res)],
                missing_evidence=["Agreed operating hours"],
                action="Use Application Auto Scaling scheduled actions to set desired count 0 outside working hours.")


@resource_rule("aws-apprunner-unused", "App Runner services with no requests", "aws.apprunner_service")
def apprunner_unused(res: Resource, ctx: Context):
    if res.attributes.get("status") != "RUNNING" or res.metrics.get("requests_30d") != 0:
        return None
    return make("aws-apprunner-unused", res, title="App Runner service received no requests in 14 days", category="waste",
                est_monthly_savings=_cost(res), confidence="medium",
                evidence=["Requests = 0 over the metrics window", _cost_ev(res)],
                action="Pause the service (no charge while paused) or delete it.")


@resource_rule("aws-rds-nonprod-multiaz", "Non-production RDS with Multi-AZ", "rds_instance")
def rds_nonprod_multiaz(res: Resource, ctx: Context):
    reason = is_non_prod(res, ctx)
    if not reason or not res.attributes.get("multi_az") or _cost(res) < 1:
        return None
    return make("aws-rds-nonprod-multiaz", res, title=f"Non-prod RDS {res.sku} runs Multi-AZ", category="waste",
                est_monthly_savings=_cost(res) * 0.5, confidence="medium",
                evidence=[f"Classified non-production: {reason}", "MultiAZ = true", _cost_ev(res)],
                assumptions=["The standby doubles instance cost"],
                action="Convert to Single-AZ (modify-db-instance --no-multi-az) for non-production.")


def _major(engine: str, version: str) -> str:
    v = version or ""
    if engine == "aurora-mysql":
        return "5.7" if v.startswith("5.7") or ".2." in v else v.split(".")[0]
    if engine in ("mysql", "mariadb"):
        return ".".join(v.split(".")[:2])
    return v.split(".")[0]


@resource_rule("aws-rds-extended-support", "RDS engines in paid Extended Support", "rds_instance")
def rds_extended_support(res: Resource, ctx: Context):
    a = res.attributes
    engine, major = a.get("engine", ""), _major(a.get("engine", ""), a.get("engine_version", ""))
    if major not in (ctx.cfg.rds_extended_support.get(engine) or []):
        return None
    vcpu = pricing.instance_vcpu(res.sku) or 2
    est = vcpu * pricing.RDS_EXTENDED_SUPPORT_VCPU_HOURLY * ctx.cfg.hours_per_month * (2 if a.get("multi_az") else 1)
    seen = [k for k in (ctx.extras.get("usage_types") or {}) if "extendedsupport" in k.lower().replace("-", "")]
    return make("aws-rds-extended-support", res, title=f"{engine} {a.get('engine_version')} is in RDS Extended Support",
                scenario_group=f"{res.id}|extended-support",  # additive to rightsizing/scheduling of the same DB
                category="modernization", est_monthly_savings=est, confidence="medium" if seen else "low", effort="high",
                evidence=[f"Engine {engine} {a.get('engine_version')} ({vcpu} vCPU{', Multi-AZ' if a.get('multi_az') else ''})"]
                + ([f"Extended Support charges on the bill: {', '.join(seen[:3])}"] if seen else []),
                assumptions=[f"~${pricing.RDS_EXTENDED_SUPPORT_VCPU_HOURLY}/vCPU-hour (years 1-2 rate)"],
                missing_evidence=[] if seen else ["Extended Support line items not seen in Cost Explorer usage types"],
                action="Plan a major-version upgrade (blue/green deployment) to a version in standard support.",
                validation_steps=["Test the application against the new engine version"])


@account_rule("aws-compute-optimizer", "AWS Compute Optimizer rightsizing (provider-native)")
def compute_optimizer(ctx: Context) -> list[Recommendation]:
    recs = []
    for r in ctx.extras.get("compute_optimizer") or []:
        if r["finding"].upper() not in ("OVER_PROVISIONED", "OVERPROVISIONED") or not r["target_type"]:
            continue
        res = ctx.by_id.get(r["instance_id"])
        saving = r["monthly_savings"]
        if not saving and res:
            cur, new = pricing.ec2_monthly_any(r["current_type"]), pricing.ec2_monthly_any(r["target_type"])
            saving = max(0.0, (cur or 0) - (new or 0)) * res.metrics.get("uptime_share", 1.0)
        recs.append(Recommendation(
            rule_id="aws-compute-optimizer", resource_id=r["instance_id"], scenario_group=r["instance_id"],
            category="rightsizing", title=f"Compute Optimizer: {r['current_type']} -> {r['target_type']}",
            est_monthly_savings=saving, current_monthly_cost=res.monthly_cost if res else None, confidence="medium",
            resource_type="ec2_instance", region=res.region if res else "", account=res.account if res else "",
            owner=owner_of(res, ctx.cfg), effort="medium",
            evidence=[f"AWS Compute Optimizer finding: {r['finding']}"] + ([f"Reasons: {', '.join(r['reasons'])}"] if r["reasons"] else []),
            assumptions=["Savings as reported by Compute Optimizer" if r["monthly_savings"] else "Savings from list-price difference"],
            action=f"Change the instance type to {r['target_type']} (stop/start required).",
            validation_steps=["Cross-check with the cloudfinops rightsizing finding (counted once)"]))
    return recs


@account_rule("aws-security-nonprod", "Security service spend in non-production accounts")
def security_nonprod(ctx: Context) -> list[Recommendation]:
    from .azure import _is_non_prod_name
    alias = ctx.extras.get("account_alias", "")
    token = _is_non_prod_name(alias, ctx)
    recent = window(ctx.records, ctx.cfg.lookback_days)
    spend = defaultdict(float)
    for r in recent:
        if r.service in SECURITY_SERVICES:
            spend[r.service] += r.cost * 30 / ctx.cfg.lookback_days
    total = sum(spend.values())
    if not token or total < 10:
        return []
    return [Recommendation(
        rule_id="aws-security-nonprod", category="licensing", scenario_group="__security_nonprod",
        title=f"{_money(total)}/mo on security services in non-prod account '{alias}'",
        est_monthly_savings=0.0, savings_low=0.0, savings_high=total * 0.5, current_monthly_cost=total, confidence="low",
        evidence=[f"Account alias classified non-production ('{token}')",
                  *[f"{k}: {_money(v)}/mo" for k, v in sorted(spend.items(), key=lambda kv: -kv[1])]],
        assumptions=["Upper bound: half of this spend is optional in non-production"],
        missing_evidence=["Security team's required controls for non-production"],
        action="Review which detectors/plans are needed in non-prod (e.g. Inspector, Macie, GuardDuty protection plans).")]


@account_rule("aws-untagged", "Resources missing required tags (rolled up per account)")
def untagged_rollup(ctx: Context) -> list[Recommendation]:
    if "account_alias" not in ctx.extras:
        return []
    taggable = ("ec2_instance", "ebs_volume", "rds_instance", "load_balancer", "s3_bucket", "aws.nat_gateway",
                "aws.ecs_service", "aws.apprunner_service", "aws.asg")
    items = [(r, [t for t in ctx.cfg.required_tags if not r.tags.get(t.lower())]) for r in ctx.resources
             if r.type in taggable]
    items = [(r, m) for r, m in items if m]
    if not items:
        return []
    cost = sum(_cost(r) for r, _ in items)
    top = sorted(items, key=lambda x: -_cost(x[0]))[:8]
    alias = ctx.extras.get("account_alias", "")
    return [Recommendation(
        rule_id="aws-untagged", scenario_group="__untagged", category="governance", resource_name=alias,
        title=f"{len(items)} resources ({_money(cost)}/mo) missing required tags in {alias}",
        est_monthly_savings=0.0, current_monthly_cost=cost, confidence="high",
        evidence=[f"{r.display_name} ({r.service}, {_money(_cost(r))}/mo): missing {', '.join(m)}" for r, m in top],
        action="Add owner/environment/cost-center tags, activate them as cost allocation tags, and enforce with "
               "AWS Organizations tag policies.")]
