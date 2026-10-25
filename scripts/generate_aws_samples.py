"""Generate a deterministic AWS sample run folder in aws CLI output shapes.

    python scripts/generate_aws_samples.py

Writes src/cloudfinops/samples/aws/{scope.json, raw/...}; `cloudfinops demo --provider aws` loads it through the
same normalization code as a live `assess --provider aws` run.
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cloudfinops import pricing  # noqa: E402
from cloudfinops.providers.aws_cli import metric_queries  # noqa: E402

OUT = ROOT / "src" / "cloudfinops" / "samples" / "aws"
END = date(2026, 10, 15)
DAYS = 60
ACCOUNT, ALIAS = "444455556666", "acme-workloads"
REGIONS = ["us-east-1", "eu-west-1"]
rnd = random.Random(11)

inv: dict[tuple[str, str], list] = defaultdict(list)  # (region, name) -> items
daily: list[tuple[str, str, float, str]] = []  # (service, region, cost/day, purchase type)
metric_spec: dict[str, dict] = {}  # resource id -> spec
buckets: list[dict] = []
ecs: dict[str, dict] = defaultdict(lambda: {"services": [], "task_definitions": {}})
apprunner: dict[str, list] = defaultdict(list)
firewalls: dict[str, list] = defaultdict(list)
co_recs: dict[str, list] = defaultdict(list)
counter = iter(range(100000))


def tags(**kw):
    return [{"Key": k, "Value": v} for k, v in kw.items()]


def nid(prefix):
    return f"{prefix}-{next(counter):017x}"


def ec2(region, name, itype, cpu=None, state="running", env="prod", owner="platform", extra_tags=None,
        volume_gb=100, stopped_since=None, asg=None):
    iid = nid("i")
    vid = nid("vol")
    t = tags(Name=name, environment=env, owner=owner, **({"cost-center": "cc-100"} if env == "prod" else {}))
    if asg:
        t.append({"Key": "aws:autoscaling:groupName", "Value": asg})
    inv[(region, "volumes")].append({"VolumeId": vid, "Size": volume_gb, "VolumeType": "gp2", "State": "in-use",
                                     "Attachments": [{"InstanceId": iid}], "CreateTime": "2025-01-10T00:00:00Z",
                                     "Tags": tags(Name=f"{name}-root")})
    inst = {"InstanceId": iid, "InstanceType": itype, "State": {"Name": state}, "LaunchTime": "2025-01-10T00:00:00Z",
            "PlatformDetails": "Linux/UNIX", "Architecture": "x86_64", "VpcId": "vpc-prod" if env == "prod" else "vpc-dev",
            "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": vid}}], "Tags": t,
            "StateTransitionReason": f"User initiated ({stopped_since} 10:00:00 GMT)" if stopped_since else ""}
    inv[(region, "instances")].append({"Instances": [inst]})
    if state == "running":
        daily.append(("Amazon Elastic Compute Cloud - Compute", region, pricing.ec2_monthly_any(itype) / 30, "On Demand Instances"))
        if cpu is not None:
            metric_spec[iid] = {"cpu": cpu}
    daily.append(("EC2 - Other", region, pricing.ebs_monthly("gp2", volume_gb) / 30, "On Demand Instances"))
    return iid


def build():
    r = "us-east-1"
    for i in range(6):
        ec2(r, f"web-{i + 1}", "m5.xlarge", cpu=35, owner="web-team")
    api = [ec2(r, f"api-{i + 1}", "m5.2xlarge", cpu=9, owner="api-team") for i in range(4)]
    for i in range(2):
        ec2(r, f"batch-{i + 1}", "c5.4xlarge", cpu=0.8, owner="data-team")
    for i in range(5):
        ec2(r, f"dev-app-{i + 1}", "t3.large", cpu=25, env="dev", owner="platform")
    ec2(r, "legacy-report", "m4.large", cpu=30, owner="finance-it")
    ec2(r, "old-test", "t2.medium", state="stopped", env="test", stopped_since="2026-03-01", volume_gb=200)
    dev_web = [ec2(r, f"asg-dev-web-{i + 1}", "m5.large", cpu=20, env="dev", asg="asg-dev-web") for i in range(3)]
    render = [ec2(r, f"render-{i + 1}", "c5.2xlarge", cpu=6, owner="media-team", asg="asg-render") for i in range(4)]
    for cur in api[:2]:
        co_recs[r].append({"instanceArn": f"arn:aws:ec2:{r}:{ACCOUNT}:instance/{cur}", "currentInstanceType": "m5.2xlarge",
                           "finding": "OVER_PROVISIONED", "findingReasonCodes": ["CPUOverprovisioned"],
                           "recommendationOptions": [{"instanceType": "m5.xlarge", "savingsOpportunity": {
                               "estimatedMonthlySavings": {"currency": "USD", "value": 138.7}}}]})

    for size, vtype in ((500, "gp2"), (200, "gp3"), (300, "io1"), (1000, "st1")):
        inv[(r, "volumes")].append({"VolumeId": nid("vol"), "Size": size, "VolumeType": vtype, "State": "available",
                                    "Attachments": [], "CreateTime": "2025-11-02T00:00:00Z"})
        daily.append(("EC2 - Other", r, pricing.ebs_monthly(vtype, size) / 30, "On Demand Instances"))
    for i in range(3):
        inv[(r, "addresses")].append({"AllocationId": nid("eipalloc"), "PublicIp": f"54.10.0.{i + 1}"})
        daily.append(("EC2 - Other", r, 0.005 * 24, "On Demand Instances"))
    inv[(r, "addresses")].append({"AllocationId": nid("eipalloc"), "PublicIp": "54.10.0.9", "AssociationId": "eipassoc-1",
                                  "InstanceId": api[0]})
    for i in range(10):
        age = 400 if i < 5 else 20
        inv[(r, "snapshots")].append({"SnapshotId": nid("snap"), "VolumeSize": 500,
                                      "StartTime": f"{END - timedelta(days=age)}T02:00:00Z", "StorageTier": "standard"})
        daily.append(("EC2 - Other", r, 0.05 * 500 / 30, "On Demand Instances"))

    inv[(r, "nat_gateways")] += [
        {"NatGatewayId": "nat-0prodheavy", "State": "available", "VpcId": "vpc-prod", "SubnetId": "subnet-a",
         "ConnectivityType": "public", "Tags": tags(Name="nat-prod-a", environment="prod", owner="network-team")},
        {"NatGatewayId": "nat-0idle", "State": "available", "VpcId": "vpc-dev", "SubnetId": "subnet-d",
         "ConnectivityType": "public", "Tags": tags(Name="nat-dev-unused", environment="dev", owner="network-team")}]
    metric_spec["nat-0prodheavy"] = {"nat_gb_per_day": 150}
    metric_spec["nat-0idle"] = {"nat_gb_per_day": 0}
    daily.append(("EC2 - Other", r, 0.045 * 24 * 2 + 150 * 0.045, "On Demand Instances"))
    inv[(r, "vpc_endpoints")].append({"VpcEndpointId": "vpce-1", "VpcId": "vpc-prod", "VpcEndpointType": "Interface",
                                      "ServiceName": "com.amazonaws.us-east-1.ecr.api"})
    inv[(r, "vpn_connections")] += [
        {"VpnConnectionId": "vpn-0oldbranch", "State": "available", "Type": "ipsec.1",
         "VgwTelemetry": [{"Status": "DOWN"}, {"Status": "DOWN"}], "Tags": tags(Name="vpn-old-branch")},
        {"VpnConnectionId": "vpn-0hq", "State": "available", "Type": "ipsec.1",
         "VgwTelemetry": [{"Status": "UP"}, {"Status": "DOWN"}], "Tags": tags(Name="vpn-hq")}]
    daily.append(("Amazon Virtual Private Cloud", r, 0.05 * 24 * 2, "On Demand Instances"))

    def rds(name, cls, engine, version, multi_az, env, cpu, conns):
        inv[(r, "db_instances")].append({
            "DBInstanceIdentifier": name, "DBInstanceArn": f"arn:aws:rds:{r}:{ACCOUNT}:db:{name}", "DBInstanceClass": cls,
            "Engine": engine, "EngineVersion": version, "MultiAZ": multi_az, "AllocatedStorage": 200, "StorageType": "gp3",
            "DBInstanceStatus": "available", "TagList": tags(environment=env, owner="data-team")})
        daily.append(("Amazon Relational Database Service", r, pricing.rds_monthly_any(cls, multi_az) / 30, "On Demand Instances"))
        metric_spec[name] = {"cpu": cpu, "conns": conns}

    rds("orders-db", "db.r5.2xlarge", "postgres", "16.4", True, "prod", 9, 140)
    rds("staging-db", "db.m5.large", "postgres", "12.19", True, "staging", 25, 15)
    rds("poc-db", "db.t3.medium", "mysql", "8.4.3", False, "dev", 1, 0)
    rds("legacy-mysql", "db.m5.large", "mysql", "5.7.44", False, "prod", 30, 40)

    for name, reqs in (("alb-prod", 50000), ("alb-old-campaign", 0)):
        arn = f"arn:aws:elasticloadbalancing:{r}:{ACCOUNT}:loadbalancer/app/{name}/{next(counter):016x}"
        inv[(r, "load_balancers")].append({"LoadBalancerArn": arn, "LoadBalancerName": name, "Type": "application",
                                           "State": {"Code": "active"}, "VpcId": "vpc-prod"})
        metric_spec[arn] = {"requests_per_h": reqs}
        daily.append(("Elastic Load Balancing", r, 0.0225 * 24 + (8 if reqs else 0), "On Demand Instances"))

    inv[(r, "asgs")] += [
        {"AutoScalingGroupName": "asg-dev-web", "DesiredCapacity": 3, "MinSize": 3, "MaxSize": 6,
         "Instances": [{"InstanceId": i, "InstanceType": "m5.large"} for i in dev_web], "Tags": tags(environment="dev", owner="platform")},
        {"AutoScalingGroupName": "asg-render", "DesiredCapacity": 4, "MinSize": 4, "MaxSize": 8,
         "Instances": [{"InstanceId": i, "InstanceType": "c5.2xlarge"} for i in render], "Tags": tags(environment="prod", owner="media-team")}]
    metric_spec["asg-dev-web"] = {"cpu": 20}
    metric_spec["asg-render"] = {"cpu": 6}

    def ecs_svc(cluster, name, cpu_units, mem_mb, count, cpu, env):
        arn = f"arn:aws:ecs:{r}:{ACCOUNT}:service/{cluster}/{name}"
        td = f"arn:aws:ecs:{r}:{ACCOUNT}:task-definition/{name}:3"
        ecs[r]["services"].append({"serviceArn": arn, "serviceName": name, "clusterArn": f"arn:aws:ecs:{r}:{ACCOUNT}:cluster/{cluster}",
                                   "launchType": "FARGATE", "desiredCount": count, "runningCount": count, "taskDefinition": td,
                                   "tags": [{"key": "environment", "value": env}]})
        ecs[r]["task_definitions"][td] = {"cpu": str(cpu_units), "memory": str(mem_mb), "requiresCompatibilities": ["FARGATE"]}
        cost = count * 730 * (cpu_units / 1024 * pricing.FARGATE_VCPU_HOURLY + mem_mb / 1024 * pricing.FARGATE_GB_HOURLY)
        daily.append(("Amazon Elastic Container Service", r, cost / 30, "On Demand Instances"))
        metric_spec[arn] = {"cpu": cpu}

    ecs_svc("prod", "svc-orders", 1024, 2048, 4, 35, "prod")
    ecs_svc("prod", "svc-reports", 2048, 4096, 2, 4, "prod")
    ecs_svc("dev", "svc-dev-api", 512, 1024, 2, 20, "dev")

    for name, reqs in (("ar-marketing-site", 0), ("ar-public-api", 300)):
        sid = f"{next(counter):032x}"
        apprunner[r].append({"ServiceArn": f"arn:aws:apprunner:{r}:{ACCOUNT}:service/{name}/{sid}", "ServiceName": name,
                             "ServiceId": sid, "Status": "RUNNING", "InstanceConfiguration": {"Cpu": "1024", "Memory": "2048"}})
        metric_spec[apprunner[r][-1]["ServiceArn"]] = {"requests_per_h": reqs}
        daily.append(("AWS App Runner", r, 2 * 0.007 * 24 + (3 if reqs else 0), "On Demand Instances"))

    firewalls[r].append({"Firewall": {"FirewallName": "nfw-dev", "FirewallArn": f"arn:aws:network-firewall:{r}:{ACCOUNT}:firewall/nfw-dev",
                                      "VpcId": "vpc-dev", "SubnetMappings": [{"SubnetId": "s1"}, {"SubnetId": "s2"}],
                                      "Tags": tags(environment="dev")}})
    daily.append(("AWS Network Firewall", r, 0.395 * 24 * 2, "On Demand Instances"))

    eu = "eu-west-1"
    ec2(eu, "eu-web-1", "m5.large", cpu=30, owner="web-team")
    ec2(eu, "eu-web-2", "m5.large", cpu=28, owner="web-team")
    inv[(eu, "volumes")].append({"VolumeId": nid("vol"), "Size": 750, "VolumeType": "gp2", "State": "available",
                                 "Attachments": [], "CreateTime": "2026-02-01T00:00:00Z"})
    daily.append(("EC2 - Other", eu, pricing.ebs_monthly("gp2", 750) / 30, "On Demand Instances"))

    for name, region, gb, lifecycle in (("acme-app-logs", "us-east-1", 30000, False), ("acme-assets", "eu-west-1", 50, True),
                                        ("acme-backups", "us-east-1", 5000, True)):
        buckets.append({"name": name, "region": region, "created": "2024-03-01T00:00:00Z", "has_lifecycle": lifecycle,
                        "tags": {"owner": "platform"}})
        metric_spec[name] = {"bytes": gb * 1024 ** 3}
        daily.append(("Amazon Simple Storage Service", region, gb * 0.023 / 30, "On Demand Instances"))

    daily.extend([("AWS Data Transfer", "us-east-1", 45.0, "On Demand Instances"),
              ("Amazon GuardDuty", "us-east-1", 4.0, "On Demand Instances"),
              ("Amazon CloudWatch", "us-east-1", 12.0, "On Demand Instances"),
              ("Savings Plans for AWS Compute usage", "NoRegion", 0.0, "Savings Plans")])


def ce_results():
    start = END - timedelta(days=DAYS)
    by_region, by_ptype = [], []
    for n in range(DAYS):
        day = start + timedelta(days=n)
        agg_r, agg_p = defaultdict(float), defaultdict(float)
        for svc, region, cost, ptype in daily:
            c = cost * rnd.uniform(0.97, 1.03)
            if svc == "AWS Data Transfer" and n == DAYS - 5:
                c += 380  # egress spike
            if svc == "Amazon Elastic Compute Cloud - Compute":  # some usage already covered by a Savings Plan
                agg_p[(svc, "Savings Plans")] += c * 0.15
                c *= 0.85
            agg_r[(svc, region)] += c
            agg_p[(svc, ptype)] += c
        period = {"TimePeriod": {"Start": str(day), "End": str(day + timedelta(days=1))}, "Estimated": False, "Total": {}}
        by_region.append({**period, "Groups": [{"Keys": list(k), "Metrics": {"AmortizedCost": {"Amount": f"{v:.6f}", "Unit": "USD"}}}
                                               for k, v in agg_r.items()]})
        by_ptype.append({**period, "Groups": [{"Keys": list(k), "Metrics": {"AmortizedCost": {"Amount": f"{v:.6f}", "Unit": "USD"}}}
                                              for k, v in agg_p.items()]})
    usage = {("Amazon Elastic Compute Cloud - Compute", "USE1-BoxUsage:m5.xlarge"): 840.0,
             ("EC2 - Other", "USE1-NatGateway-Bytes"): 205.0, ("EC2 - Other", "USE1-NatGateway-Hours"): 65.0,
             ("AWS Data Transfer", "USE1-DataTransfer-Out-Bytes"): 1730.0,
             ("Amazon Relational Database Service", "USE1-ExtendedSupport:Yr1-Yr2:PostgreSQL12"): 146.0,
             ("Amazon Relational Database Service", "USE1-ExtendedSupport:Yr1-Yr2:MySQL5.7"): 146.0}
    by_usage = [{"TimePeriod": {"Start": str(END - timedelta(days=30)), "End": str(END)}, "Estimated": False,
                 "Groups": [{"Keys": list(k), "Metrics": {"AmortizedCost": {"Amount": f"{v:.2f}", "Unit": "USD"}}} for k, v in usage.items()]}]
    return by_region, by_ptype, by_usage


def metric_results(raw: Path, region: str):
    queries = metric_queries(raw, region)
    end = datetime(END.year, END.month, END.day, tzinfo=timezone.utc)
    stamps = [end - timedelta(hours=6 * i) for i in range(14 * 4)]  # 6-hour points, newest first (as CloudWatch returns)
    results = []
    for q in queries:
        rid, key = q["meta"]["resource_id"], q["meta"]["key"]
        spec = metric_spec.get(rid, {})
        if key in ("cpu_mean", "cpu_max"):
            if "cpu" not in spec:
                continue
            vals = [min(100.0, spec["cpu"] * rnd.uniform(0.5, 1.3) * (2.2 if key == "cpu_max" else 1)) for _ in stamps]
        elif key == "connections_max":
            vals = [float(spec.get("conns", 0)) * rnd.uniform(0.7, 1.0) for _ in stamps]
        elif key == "requests":
            if not spec.get("requests_per_h"):
                vals = []  # CloudWatch returns no datapoints when there were no requests
            else:
                vals = [spec["requests_per_h"] * 6 * rnd.uniform(0.5, 1.5) for _ in stamps]
        elif key == "nat_bytes":
            vals = [] if not spec.get("nat_gb_per_day") else [spec["nat_gb_per_day"] / 4 * 1e9 * rnd.uniform(0.8, 1.2) for _ in stamps]
        elif key == "s3_bytes":
            vals = [spec["bytes"]] * 3
        else:
            continue
        results.append({"Id": q["query"]["Id"], "Label": key, "StatusCode": "Complete",
                        "Timestamps": [t.isoformat() for t in stamps[:len(vals)]], "Values": [round(v, 3) for v in vals]})
    return {"period": 21600, "days": 14, "meta": {q["query"]["Id"]: q["meta"] for q in queries}, "results": results}


def main():
    build()
    if OUT.exists():
        shutil.rmtree(OUT)
    raw = OUT / "raw"
    for d in ("inventory", "costs", "metrics", "recommendations"):
        (raw / d).mkdir(parents=True, exist_ok=True)
    w = lambda p, data: (raw / p).write_text(json.dumps(data, separators=(",", ":"), default=str))  # noqa: E731
    keys = {"instances": "Reservations", "volumes": "Volumes", "addresses": "Addresses", "snapshots": "Snapshots",
            "nat_gateways": "NatGateways", "vpn_connections": "VpnConnections", "vpc_endpoints": "VpcEndpoints",
            "db_instances": "DBInstances", "load_balancers": "LoadBalancers", "asgs": "AutoScalingGroups"}
    for region in REGIONS:
        for name, key in keys.items():
            w(f"inventory/{region}_{name}.json", {key: inv[(region, name)]})
        if ecs[region]["services"]:
            w(f"inventory/{region}_ecs_services.json", ecs[region])
        if apprunner[region]:
            w(f"inventory/{region}_apprunner.json", apprunner[region])
        if firewalls[region]:
            w(f"inventory/{region}_firewalls.json", firewalls[region])
        w(f"recommendations/{region}_ec2.json", {"instanceRecommendations": co_recs[region]})
    w("inventory/s3_buckets.json", buckets)
    by_region, by_ptype, by_usage = ce_results()
    w("costs/ce_service_region.json", by_region)
    w("costs/ce_purchase_type.json", by_ptype)
    w("costs/ce_usage_type.json", by_usage)
    for region in REGIONS:
        w(f"metrics/{region}_0.json", metric_results(raw, region))
    (OUT / "scope.json").write_text(json.dumps({
        "provider": "aws", "sample": True, "subscriptions": [{"id": ACCOUNT, "name": ALIAS, "display_name": ALIAS}],
        "regions": REGIONS, "period": {"start": str(END - timedelta(days=DAYS)), "end": str(END)}, "metric_days": 14,
        "collected_at": f"{END}T06:00:00+00:00", "errors": [], "profile": "default"}, indent=2))
    n = sum(len(v) for v in inv.values())
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"Wrote {n} inventory items, {len(metric_spec)} metric specs, {size / 1e6:.1f} MB to {OUT}")


if __name__ == "__main__":
    main()
