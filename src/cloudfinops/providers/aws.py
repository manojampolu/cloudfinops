"""Collect billing data (Cost Explorer) and a resource inventory from AWS.

Requires `pip install cloudfinops[aws]` and read-only credentials. Minimum IAM actions:
  ce:GetCostAndUsage, sts:GetCallerIdentity, ec2:Describe*, rds:DescribeDBInstances,
  elasticloadbalancing:Describe*, cloudwatch:GetMetricStatistics,
  s3:ListAllMyBuckets, s3:GetBucketLocation, s3:GetLifecycleConfiguration, s3:GetBucketTagging
(the AWS managed policy `ReadOnlyAccess` + `AWSBillingReadOnlyAccess` covers all of them).

Note: Cost Explorer charges $0.01 per API request.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from ..harness.errors import short_error
from ..models import CostRecord, Resource

log = logging.getLogger(__name__)

GROUP_KEYS = {"account": "LINKED_ACCOUNT", "region": "REGION", "purchase_type": "PURCHASE_TYPE"}
PURCHASE_TYPE_MAP = {
    "On Demand Instances": "On-Demand", "Spot Instances": "Spot",
    "Savings Plans": "Commitment-Based", "Standard Reserved Instances": "Commitment-Based",
    "Convertible Reserved Instances": "Commitment-Based", "Dedicated Reserved Instances": "Commitment-Based",
}
METRIC_DAYS = 14
DENIED_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedException",
                "AuthorizationError", "AuthorizationErrorException", "OptInRequired"}
_DENIED: set[str] = set()  # services denied during the current collect_inventory() call


def _error_code(exc: Exception) -> str:
    return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")


def _is_denied(exc: Exception) -> bool:
    return _error_code(exc) in DENIED_CODES


def _account_id(session) -> str:
    """Caller account, with a readable error when credentials are missing or expired."""
    try:
        return session.client("sts").get_caller_identity()["Account"]
    except Exception as exc:
        name = type(exc).__name__
        if name in ("NoCredentialsError", "PartialCredentialsError") or _error_code(exc) in (
                "ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "UnrecognizedClientException"):
            raise ValueError("No valid AWS credentials. Run `aws configure` or `aws sso login` "
                             "(and pass --profile if you use named profiles).") from exc
        raise


def _session(profile: str | None):
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("boto3 is required: pip install 'cloudfinops[aws]'") from exc
    return boto3.Session(profile_name=profile)


def _tags(tag_list) -> dict[str, str]:
    return {t["Key"].lower(): t["Value"] for t in (tag_list or [])}


# --------------------------------------------------------------------------- billing

def collect_costs(days: int = 60, group_by: str = "account", profile: str | None = None) -> list[CostRecord]:
    session = _session(profile)
    ce = session.client("ce", region_name="us-east-1")
    end = date.today()
    start = end - timedelta(days=days)
    second = GROUP_KEYS[group_by]
    account_id = _account_id(session)

    records, token = [], None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY", Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": second}],
        )
        if token:
            kwargs["NextPageToken"] = token
        try:
            resp = ce.get_cost_and_usage(**kwargs)
        except Exception as exc:
            if _is_denied(exc):
                raise ValueError("Cost Explorer access denied (needs ce:GetCostAndUsage, e.g. the "
                                 f"AWSBillingReadOnlyAccess policy): {short_error(str(exc))}") from exc
            if _error_code(exc) == "DataUnavailableException":
                raise ValueError("Cost Explorer has no data for this account yet. Enable Cost Explorer in the "
                                 "Billing console; data appears within about 24 hours.") from exc
            raise
        for period in resp["ResultsByTime"]:
            day = date.fromisoformat(period["TimePeriod"]["Start"])
            for g in period["Groups"]:
                service, key = g["Keys"]
                cost = float(g["Metrics"]["UnblendedCost"]["Amount"])
                if abs(cost) < 1e-6:
                    continue
                rec = CostRecord(day=day, cost=cost, service=service, account=account_id)
                if group_by == "account":
                    rec.account = key
                elif group_by == "region":
                    rec.region = key
                else:
                    rec.pricing_category = PURCHASE_TYPE_MAP.get(key, key)
                records.append(rec)
        token = resp.get("NextPageToken")
        if not token:
            break
    log.info("Collected %d cost records", len(records))
    return records


# --------------------------------------------------------------------------- inventory

def _cw_stats(cw, namespace: str, metric: str, dims: dict[str, str], stats: list[str]) -> dict[str, float] | None:
    if "cloudwatch" in _DENIED:
        return None
    end = datetime.now(timezone.utc)
    try:
        resp = cw.get_metric_statistics(
            Namespace=namespace, MetricName=metric,
            Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
            StartTime=end - timedelta(days=METRIC_DAYS), EndTime=end, Period=86400, Statistics=stats,
        )
    except Exception as exc:
        if not _is_denied(exc):
            raise
        if "cloudwatch" not in _DENIED:  # inventory continues without utilization data
            log.warning("CloudWatch access denied; continuing without utilization metrics: %s", short_error(str(exc)))
        _DENIED.add("cloudwatch")
        return None
    points = resp.get("Datapoints", [])
    if not points:
        return None
    out = {}
    if "Average" in stats:
        out["avg"] = sum(p["Average"] for p in points) / len(points)
    if "Maximum" in stats:
        out["max"] = max(p["Maximum"] for p in points)
    if "Sum" in stats:
        out["sum"] = sum(p["Sum"] for p in points)
    return out


_STOPPED_RE = re.compile(r"\((\d{4}-\d{2}-\d{2})")


def _ec2(session, region: str, account: str) -> list[Resource]:
    ec2, cw = session.client("ec2", region_name=region), session.client("cloudwatch", region_name=region)
    out: list[Resource] = []

    for page in ec2.get_paginator("describe_instances").paginate():
        for resv in page["Reservations"]:
            for i in resv["Instances"]:
                state = i["State"]["Name"]
                if state == "terminated":
                    continue
                res = Resource(
                    id=i["InstanceId"], type="ec2_instance", region=region, account=account,
                    tags=_tags(i.get("Tags")),
                    attributes={
                        "instance_type": i["InstanceType"], "state": state,
                        "launch_time": i["LaunchTime"].isoformat(),
                        "platform": i.get("PlatformDetails", ""),
                        "architecture": i.get("Architecture", ""),
                        "lifecycle": i.get("InstanceLifecycle", "on-demand"),
                        "volume_ids": [b["Ebs"]["VolumeId"] for b in i.get("BlockDeviceMappings", []) if "Ebs" in b],
                    },
                )
                if state == "stopped" and (m := _STOPPED_RE.search(i.get("StateTransitionReason", ""))):
                    res.attributes["stopped_since"] = m.group(1)
                if state == "running":
                    if s := _cw_stats(cw, "AWS/EC2", "CPUUtilization", {"InstanceId": res.id}, ["Average", "Maximum"]):
                        res.metrics.update(cpu_avg_14d=s["avg"], cpu_max_14d=s["max"])
                out.append(res)

    for page in ec2.get_paginator("describe_volumes").paginate():
        for v in page["Volumes"]:
            out.append(Resource(
                id=v["VolumeId"], type="ebs_volume", region=region, account=account, tags=_tags(v.get("Tags")),
                attributes={"size_gb": v["Size"], "volume_type": v["VolumeType"], "state": v["State"],
                            "iops": v.get("Iops"), "throughput": v.get("Throughput"),
                            "attached_to": [a["InstanceId"] for a in v.get("Attachments", [])]},
            ))

    for a in ec2.describe_addresses()["Addresses"]:
        out.append(Resource(
            id=a.get("AllocationId") or a["PublicIp"], type="elastic_ip", region=region, account=account,
            tags=_tags(a.get("Tags")),
            attributes={"public_ip": a["PublicIp"], "association_id": a.get("AssociationId"),
                        "instance_id": a.get("InstanceId")},
        ))

    for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
        for s in page["Snapshots"]:
            out.append(Resource(
                id=s["SnapshotId"], type="ebs_snapshot", region=region, account=account, tags=_tags(s.get("Tags")),
                attributes={"size_gb": s["VolumeSize"], "start_time": s["StartTime"].isoformat(),
                            "volume_id": s.get("VolumeId"), "storage_tier": s.get("StorageTier", "standard")},
            ))
    return out


def _rds(session, region: str, account: str) -> list[Resource]:
    rds, cw = session.client("rds", region_name=region), session.client("cloudwatch", region_name=region)
    out = []
    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page["DBInstances"]:
            ident = db["DBInstanceIdentifier"]
            res = Resource(
                id=ident, type="rds_instance", region=region, account=account, tags=_tags(db.get("TagList")),
                attributes={"instance_class": db["DBInstanceClass"], "engine": db["Engine"],
                            "multi_az": db.get("MultiAZ", False), "state": "running" if db["DBInstanceStatus"] == "available" else db["DBInstanceStatus"],
                            "storage_gb": db.get("AllocatedStorage"), "storage_type": db.get("StorageType")},
            )
            dims = {"DBInstanceIdentifier": ident}
            if s := _cw_stats(cw, "AWS/RDS", "CPUUtilization", dims, ["Average", "Maximum"]):
                res.metrics.update(cpu_avg_14d=s["avg"], cpu_max_14d=s["max"])
            if s := _cw_stats(cw, "AWS/RDS", "DatabaseConnections", dims, ["Maximum"]):
                res.metrics["connections_max_14d"] = s["max"]
            out.append(res)
    return out


def _elb(session, region: str, account: str) -> list[Resource]:
    elb, cw = session.client("elbv2", region_name=region), session.client("cloudwatch", region_name=region)
    lbs = [lb for page in elb.get_paginator("describe_load_balancers").paginate() for lb in page["LoadBalancers"]]
    tags = {}
    for i in range(0, len(lbs), 20):
        arns = [lb["LoadBalancerArn"] for lb in lbs[i:i + 20]]
        for d in elb.describe_tags(ResourceArns=arns)["TagDescriptions"]:
            tags[d["ResourceArn"]] = _tags(d.get("Tags"))
    out = []
    for lb in lbs:
        arn = lb["LoadBalancerArn"]
        res = Resource(id=lb["LoadBalancerName"], type="load_balancer", region=region, account=account,
                       tags=tags.get(arn, {}), attributes={"arn": arn, "lb_type": lb["Type"]})
        if lb["Type"] == "application":
            dim = arn.split(":loadbalancer/", 1)[1]
            s = _cw_stats(cw, "AWS/ApplicationELB", "RequestCount", {"LoadBalancer": dim}, ["Sum"])
            res.metrics["request_count_14d"] = s["sum"] if s else 0.0
        out.append(res)
    return out


def _s3(session, account: str) -> list[Resource]:
    from botocore.exceptions import ClientError

    s3 = session.client("s3")
    out, skipped = [], []
    for b in s3.list_buckets().get("Buckets", []):
        name = b["Name"]
        try:
            region = s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
        except ClientError as exc:
            skipped.append((name, short_error(str(exc))))
            continue
        res = Resource(id=name, type="s3_bucket", region=region, account=account)
        try:
            s3.get_bucket_lifecycle_configuration(Bucket=name)
            res.attributes["has_lifecycle_policy"] = True
        except ClientError:
            res.attributes["has_lifecycle_policy"] = False
        try:
            res.tags = _tags(s3.get_bucket_tagging(Bucket=name).get("TagSet"))
        except ClientError:
            pass
        cw = session.client("cloudwatch", region_name=region)
        s = _cw_stats(cw, "AWS/S3", "BucketSizeBytes", {"BucketName": name, "StorageType": "StandardStorage"},
                      ["Maximum"])
        res.attributes["standard_gb"] = round(s["max"] / 1024 ** 3, 2) if s else 0.0
        out.append(res)
    if skipped:
        log.warning("Skipped %d bucket(s) that could not be read, e.g. %s: %s", len(skipped), *skipped[0])
    return out


def collect_inventory(regions: list[str] | None = None, profile: str | None = None,
                      include_s3: bool = True, gaps: dict | None = None) -> list[Resource]:
    """Scan regions. A service that is access-denied once is skipped everywhere and reported once in `gaps`."""
    session = _session(profile)
    account = _account_id(session)
    _DENIED.clear()
    if not regions:
        ec2 = session.client("ec2", region_name=session.region_name or "us-east-1")
        regions = [r["RegionName"] for r in ec2.describe_regions()["Regions"]]

    resources: list[Resource] = []
    for region in regions:
        log.info("Scanning %s", region)
        for collector in (_ec2, _rds, _elb):
            svc = collector.__name__.strip("_")
            if svc in _DENIED:
                continue
            try:
                resources += collector(session, region, account)
            except Exception as exc:  # keep scanning other regions/services
                if _is_denied(exc):
                    _DENIED.add(svc)
                    log.warning("%s access denied; skipping it in all regions: %s", svc, short_error(str(exc)))
                else:
                    log.warning("%s in %s failed: %s", svc, region, short_error(str(exc)))
    if include_s3:
        try:
            resources += _s3(session, account)
        except Exception as exc:
            if _is_denied(exc):
                _DENIED.add("s3")
            log.warning("s3 scan failed: %s", short_error(str(exc)))
    if gaps is not None:
        gaps["denied"] = sorted(_DENIED)
    log.info("Collected %d resources", len(resources))
    return resources
