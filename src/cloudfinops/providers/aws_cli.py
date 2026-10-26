"""AWS adapter for `assess`: read-only collection through the aws CLI, and offline normalization.

Data sources (all read-only, logged in commands.log)
  identity    aws sts get-caller-identity, aws iam list-account-aliases
  costs       aws ce get-cost-and-usage (amortized): daily by service+region, daily by service+purchase type,
              30 days by usage type                                            -> raw/costs/*.json
              Optional: a CUR / FOCUS CSV (--billing-csv) for resource-level costs -> raw/costs/billing.csv
  inventory   per region: ec2 describe-*, rds, elbv2, autoscaling, ecs, apprunner, network-firewall;
              global: s3api                                                    -> raw/inventory/*.json
  usage       aws cloudwatch get-metric-data (batched)                         -> raw/metrics/*.json
  native recs aws compute-optimizer get-ec2-instance-recommendations           -> raw/recommendations/*.json

Required: the ReadOnlyAccess and AWSBillingReadOnlyAccess managed policies (or equivalent read actions).
Cost Explorer charges $0.01 per request; a run makes about 3-6.
"""

from __future__ import annotations

import json
import re
import shutil
import statistics
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..config import Config
from ..harness.errors import is_permission_error, short_error
from ..harness.runner import CliError, CliRunner
from ..ingest.billing import load_billing_csv
from ..models import CostRecord, Dataset, Relationship, Resource

PURCHASE_TYPE_MAP = {
    "On Demand Instances": "On-Demand", "Spot Instances": "Spot", "Savings Plans": "Commitment-Based",
    "Standard Reserved Instances": "Commitment-Based", "Convertible Reserved Instances": "Commitment-Based",
    "Dedicated Reserved Instances": "Commitment-Based", "Reserved": "Commitment-Based",
}
TYPE_SERVICE = {
    "ec2_instance": "EC2 Instances", "ebs_volume": "EBS Volumes", "ebs_snapshot": "EBS Snapshots",
    "elastic_ip": "Elastic IPs", "rds_instance": "RDS Databases", "load_balancer": "Load Balancers",
    "s3_bucket": "S3 Buckets", "aws.nat_gateway": "NAT Gateways", "aws.vpn_connection": "Site-to-Site VPN",
    "aws.network_firewall": "Network Firewall", "aws.asg": "Auto Scaling Groups", "aws.ecs_service": "ECS Services",
    "aws.apprunner_service": "App Runner", "aws.vpc_endpoint": "VPC Endpoints",
}
FREE_TYPES = {"aws.asg", "aws.vpc_endpoint"}  # an ASG's cost sits on its instances
UNSUPPORTED = ("could not connect to the endpoint", "unknownendpoint", "not supported", "not available in",
               "invalid region", "unrecognizedclient", "endpointconnectionerror")
REGION_LISTS = [  # (service key, raw name, command)
    ("ec2", "instances", ["ec2", "describe-instances"]),
    ("ec2", "volumes", ["ec2", "describe-volumes"]),
    ("ec2", "addresses", ["ec2", "describe-addresses"]),
    ("ec2", "snapshots", ["ec2", "describe-snapshots", "--owner-ids", "self"]),
    ("ec2", "nat_gateways", ["ec2", "describe-nat-gateways"]),
    ("ec2", "vpn_connections", ["ec2", "describe-vpn-connections"]),
    ("ec2", "vpc_endpoints", ["ec2", "describe-vpc-endpoints"]),
    ("rds", "db_instances", ["rds", "describe-db-instances"]),
    ("elbv2", "load_balancers", ["elbv2", "describe-load-balancers"]),
    ("autoscaling", "asgs", ["autoscaling", "describe-auto-scaling-groups"]),
]
METRIC_PERIOD = 3600
WHAT = {"costs": "Cost Explorer (needs AWSBillingReadOnlyAccess / ce:GetCostAndUsage)",
        "metrics": "CloudWatch metrics (needs cloudwatch:GetMetricData)",
        "compute-optimizer": "Compute Optimizer (not opted in, or no access)",
        "s3": "S3 bucket details"}


def local_key_problem(profile: str = "") -> str | None:
    """Spot an obviously malformed static access key in ~/.aws/credentials before calling AWS.

    Returns a readable problem description, or None (fine, or not a static-key profile). Never returns the secret.
    """
    import configparser
    import os

    path = os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or os.path.expanduser("~/.aws/credentials")
    name = profile or os.environ.get("AWS_PROFILE") or "default"
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error:
        return None
    if name not in parser:
        return None
    key = parser[name].get("aws_access_key_id", "").strip()
    secret = parser[name].get("aws_secret_access_key", "").strip()
    if not key and not secret:
        return None
    if not re.fullmatch(r"(AKIA|ASIA)[A-Z0-9]{16}", key) or len(secret) != 40 or " " in secret:
        return (f"The access key stored for profile '{name}' is not a valid AWS key (expected an access key ID of "
                f"20 characters starting AKIA and a 40-character secret; found {len(key)} and {len(secret)} characters). "
                f"Run `aws configure --profile {name}` and paste the values from IAM > Security credentials > "
                "Create access key.")
    return None


def _tags(tag_list) -> dict[str, str]:
    return {str(t.get("Key", "")).lower(): str(t.get("Value", "")) for t in (tag_list or [])}


def _p95(values: list[float]) -> float:
    vals = sorted(values)
    return vals[min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))] if vals else 0.0


def _read(path: Path, default):
    if not path.exists():
        return default
    text = path.read_text().strip()
    return json.loads(text) if text else default


class AwsProvider:
    name = "aws"
    cli = "aws"
    scope_label = "accounts"

    def __init__(self, cfg: Config | None = None):
        self.profile = (cfg.aws_profile if cfg else "") or ""

    def _aws(self, *args: str) -> list[str]:
        return ["aws", *args, "--output", "json"] + (["--profile", self.profile] if self.profile else [])

    # ------------------------------------------------------------------ session

    def preflight(self, runner: CliRunner) -> list[str]:
        if not runner.available("aws"):
            return ["AWS CLI (aws) not found. Install: https://docs.aws.amazon.com/cli/latest/userguide/"
                    "getting-started-install.html (macOS: brew install awscli)"]
        return []

    def ensure_login(self, runner: CliRunner, device_code: bool = False, tenant: str | None = None) -> dict:
        problem = local_key_problem(self.profile)
        if problem:
            raise CliError(["aws", "sts", "get-caller-identity"], 1, problem)
        try:
            ident = runner.run(self._aws("sts", "get-caller-identity"))
        except CliError as exc:
            msg = str(exc).lower()
            if "sso" in msg or "expired" in msg:
                runner.run(["aws", "sso", "login"] + (["--profile", self.profile] if self.profile else []),
                           interactive=True)
                ident = runner.run(self._aws("sts", "get-caller-identity"))
            else:
                raise CliError(["aws", "sts", "get-caller-identity"], 1,
                               "No valid AWS credentials. Run `aws configure` or `aws sso login` "
                               f"(add --profile for named profiles). {short_error(str(exc))}") from exc
        return {"user": {"name": ident.get("Arn", "?")}, "tenantId": ident.get("Account", "?")}

    def list_scopes(self, runner: CliRunner) -> list[dict]:
        ident = runner.run(self._aws("sts", "get-caller-identity"))
        alias = ""
        try:
            alias = (runner.run(self._aws("iam", "list-account-aliases")) or {}).get("AccountAliases", [""])[0]
        except (CliError, IndexError):
            pass
        return [{"id": ident["Account"], "name": alias or ident["Account"], "display_name": alias}]

    def describe_scope(self, s: dict) -> str:
        return f"{s['name']} ({s['id']})" if s.get("display_name") else s["id"]

    # ------------------------------------------------------------------ collection

    def collect(self, runner: CliRunner, scopes: list[dict], cfg: Config, log=print) -> dict:
        self.profile = cfg.aws_profile or self.profile
        account = scopes[0]["id"]
        end = date.today()
        start = end - timedelta(days=cfg.collection_days)
        manifest = {"provider": "aws", "subscriptions": scopes[:1], "period": {"start": str(start), "end": str(end)},
                    "metric_days": cfg.metric_days, "errors": [], "profile": self.profile or "default",
                    "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if len(scopes) > 1:
            manifest["errors"].append({"step": "scope", "scope": "accounts", "message":
                                       "Only the signed-in account is assessed; run once per account/profile"})
        lock = threading.Lock()
        denied: dict[str, str] = {}

        def record(step, scope, exc):
            with lock:
                log(f"  ! {step} ({scope}): {short_error(str(exc))}")
                manifest["errors"].append({"step": step, "scope": scope, "message": short_error(str(exc)),
                                           "permission": is_permission_error(str(exc))})

        def deny(step, exc):
            with lock:
                if step not in denied:
                    denied[step] = short_error(str(exc))
                    log(f"  ! no {step} access: skipping the rest of this step")

        def call(step, scope, cmd, save_as=None):
            """Run one read; permission errors deny the whole step, unsupported regions are skipped quietly."""
            if step in denied:
                return None
            try:
                return runner.run(cmd, save_as=save_as)
            except CliError as exc:
                msg = str(exc).lower()
                if is_permission_error(msg) or "optinrequired" in msg:
                    deny(step, exc)
                elif not any(u in msg for u in UNSUPPORTED):
                    record(step, scope, exc)
                return None

        regions = cfg.aws_regions or call("regions", "account", self._aws(
            "ec2", "describe-regions", "--region", "us-east-1", "--query", "Regions[].RegionName")) or ["us-east-1"]
        manifest["regions"] = regions

        log("Collecting costs (Cost Explorer, amortized)...")
        ce_queries = [("service_region", ["SERVICE", "REGION"], "DAILY", start),
                      ("purchase_type", ["SERVICE", "PURCHASE_TYPE"], "DAILY", start),
                      ("usage_type", ["SERVICE", "USAGE_TYPE"], "MONTHLY", end - timedelta(days=30))]
        for name, group_by, gran, q_start in ce_queries:
            rows = self._ce(runner, call, group_by, gran, q_start, end)
            if rows is None:
                break
            runner.save_json(f"costs/ce_{name}.json", rows)
        if cfg.aws_billing_csv:
            shutil.copy(cfg.aws_billing_csv, runner.raw_dir / "costs" / "billing.csv")
            log(f"  resource-level costs from {cfg.aws_billing_csv}")

        log(f"Collecting inventory in {len(regions)} region(s)...")
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda r: self._region_inventory(runner, call, r), regions))

        log("Collecting S3 buckets...")
        self._s3(runner, call, cfg)

        log("Collecting CloudWatch usage metrics...")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda r: self._region_metrics(runner, call, r, cfg), regions))

        for step, reason in denied.items():
            manifest["errors"].append({"step": "access", "scope": scopes[0]["name"], "permission": True,
                                       "message": f"{WHAT.get(step, step)}: {reason}"})
        if not (runner.raw_dir / "costs" / "ce_service_region.json").exists() and not cfg.aws_billing_csv:
            why = ("Cost Explorer access denied" if "costs" in denied else
                   "Cost Explorer returned no data (it may not be enabled for this account yet)")
            manifest["billing_note"] = (f"{why}, so spend is unknown. Grant ce:GetCostAndUsage (AWSBillingReadOnlyAccess) "
                                        "to this identity, or pass a CUR / billing CSV export with --billing-csv.")
        manifest["access_gaps"] = sorted(denied)
        (runner.run_dir / "scope.json").write_text(json.dumps(manifest, indent=2))
        return manifest

    def _ce(self, runner, call, group_by, granularity, start, end) -> list | None:
        results, token = [], None
        while True:
            cmd = self._aws("ce", "get-cost-and-usage", "--region", "us-east-1",
                            "--time-period", f"Start={start},End={end}", "--granularity", granularity,
                            "--metrics", "AmortizedCost", "--group-by",
                            *[f"Type=DIMENSION,Key={k}" for k in group_by])
            if token:
                cmd += ["--next-page-token", token]
            resp = call("costs", "account", cmd)
            if resp is None:
                return None
            results += resp.get("ResultsByTime", [])
            token = resp.get("NextPageToken")
            if not token:
                return results

    def _region_inventory(self, runner, call, region: str) -> None:
        for svc, name, cmd in REGION_LISTS:
            call(svc, region, self._aws(*cmd, "--region", region), save_as=f"inventory/{region}_{name}.json")
        # ECS services with task sizes
        clusters = (call("ecs", region, self._aws("ecs", "list-clusters", "--region", region)) or {}).get("clusterArns", [])
        services, task_defs = [], {}
        for cluster in clusters[:50]:
            arns = (call("ecs", region, self._aws("ecs", "list-services", "--cluster", cluster, "--region", region))
                    or {}).get("serviceArns", [])
            for i in range(0, len(arns), 10):
                desc = call("ecs", region, self._aws("ecs", "describe-services", "--cluster", cluster, "--services",
                                                     *arns[i:i + 10], "--region", region)) or {}
                services += desc.get("services", [])
        for td in {s.get("taskDefinition") for s in services if s.get("taskDefinition")}:
            d = call("ecs", region, self._aws("ecs", "describe-task-definition", "--task-definition", td,
                                              "--region", region)) or {}
            task_defs[td] = {k: d.get("taskDefinition", {}).get(k) for k in ("cpu", "memory", "requiresCompatibilities")}
        if services:
            runner.save_json(f"inventory/{region}_ecs_services.json", {"services": services, "task_definitions": task_defs})
        # App Runner
        ar = (call("apprunner", region, self._aws("apprunner", "list-services", "--region", region)) or {}).get(
            "ServiceSummaryList", [])
        details = [d.get("Service", {}) for d in (call("apprunner", region, self._aws(
            "apprunner", "describe-service", "--service-arn", s["ServiceArn"], "--region", region)) or {} for s in ar)]
        if details:
            runner.save_json(f"inventory/{region}_apprunner.json", details)
        # Network Firewall
        fws = (call("network-firewall", region, self._aws("network-firewall", "list-firewalls", "--region", region))
               or {}).get("Firewalls", [])
        fw_details = [call("network-firewall", region, self._aws("network-firewall", "describe-firewall",
                                                                  "--firewall-arn", f["FirewallArn"], "--region", region))
                      for f in fws]
        if fws:
            runner.save_json(f"inventory/{region}_firewalls.json", [d for d in fw_details if d])
        # Compute Optimizer (provider-native rightsizing)
        call("compute-optimizer", region, self._aws("compute-optimizer", "get-ec2-instance-recommendations",
                                                    "--region", region),
             save_as=f"recommendations/{region}_ec2.json")

    def _s3(self, runner, call, cfg) -> None:
        buckets = (call("s3", "global", self._aws("s3api", "list-buckets", "--region", "us-east-1")) or {}).get("Buckets", [])

        def details(b):
            name = b["Name"]
            loc = call("s3", name, self._aws("s3api", "get-bucket-location", "--bucket", name))
            if loc is None:
                return None
            region = loc.get("LocationConstraint") or "us-east-1"
            out = {"name": name, "region": region, "created": b.get("CreationDate"), "has_lifecycle": False, "tags": {}}
            try:
                rules = runner.run(self._aws("s3api", "get-bucket-lifecycle-configuration", "--bucket", name),
                                   absent_ok=("NoSuchLifecycleConfiguration",))
                out["has_lifecycle"] = bool(rules)
            except CliError:  # access denied or other failure: unknown, never "no policy"
                out["has_lifecycle"] = None
            try:
                tags = runner.run(self._aws("s3api", "get-bucket-tagging", "--bucket", name), absent_ok=("NoSuchTagSet",))
                out["tags"] = _tags((tags or {}).get("TagSet"))
            except CliError:
                pass
            return out

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = [r for r in pool.map(details, buckets) if r]
        runner.save_json("inventory/s3_buckets.json", rows)

    def _region_metrics(self, runner, call, region: str, cfg: Config) -> None:
        raw = runner.raw_dir
        queries = metric_queries(raw, region)
        if not queries:
            return
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=cfg.metric_days)
        for n in range(0, len(queries), 100):
            batch = queries[n:n + 100]
            qfile = raw / "metrics" / f"{region}_queries_{n // 100}.json"
            qfile.parent.mkdir(parents=True, exist_ok=True)
            qfile.write_text(json.dumps([q["query"] for q in batch]))
            resp = call("metrics", region, self._aws(
                "cloudwatch", "get-metric-data", "--metric-data-queries", f"file://{qfile}",
                "--start-time", start.isoformat(), "--end-time", end.isoformat(), "--region", region))
            if resp is None:
                return
            runner.save_json(f"metrics/{region}_{n // 100}.json", {
                "period": METRIC_PERIOD, "days": cfg.metric_days,
                "meta": {q["query"]["Id"]: q["meta"] for q in batch}, "results": resp.get("MetricDataResults", [])})

    # ------------------------------------------------------------------ normalization

    def load(self, run_dir: Path, cfg: Config) -> Dataset:
        run_dir = Path(run_dir)
        raw = run_dir / "raw"
        scope = json.loads((run_dir / "scope.json").read_text()) if (run_dir / "scope.json").exists() else {}
        account = (scope.get("subscriptions") or [{"id": ""}])[0]["id"]
        alias = (scope.get("subscriptions") or [{"name": account}])[0].get("name", account)
        resources: dict[str, Resource] = {}

        def add(res: Resource) -> Resource:
            res.provider, res.account = "aws", account
            res.service = res.service or TYPE_SERVICE.get(res.type, res.type)
            res.group = res.group or res.region
            resources[res.id] = res
            return res

        for region in scope.get("regions") or sorted({p.name.split("_")[0] for p in (raw / "inventory").glob("*_*.json")
                                                      if not p.name.startswith("s3")}):
            self._load_region(raw, region, add)
        for b in _read(raw / "inventory/s3_buckets.json", []):
            add(Resource(id=b["name"], type="s3_bucket", region=b["region"], name=b["name"], tags=b.get("tags", {}),
                         attributes={"has_lifecycle_policy": b.get("has_lifecycle", False), "standard_gb": 0.0,
                                     "created": str(b.get("created") or "")[:10]}))

        by_id = resources
        self._attach_metrics(raw, by_id)
        relationships = self._relationships(list(by_id.values()), by_id)
        records = self._costs(raw, by_id, account)
        extras = {
            "subscription_names": {account: alias}, "free_types": FREE_TYPES, "account_alias": alias,
            "pricing_model_daily": self._purchase_types(raw),
            "usage_types": self._usage_types(raw),
            "compute_optimizer": self._compute_optimizer(raw),
        }
        if records:
            scope.setdefault("period", {})
            scope["period"]["start"] = str(min(r.day for r in records))
            scope["period"]["end"] = str(max(r.day for r in records))
            if not any(r.resource_id for r in records):
                scope.setdefault("info_notes", []).append(
                    "Cost Explorer gives service-level costs; resource costs are list-price estimates. Pass a CUR export "
                    "with --billing-csv for resource-level costs.")
        return Dataset(provider="aws", records=records, resources=list(by_id.values()), relationships=relationships,
                       scope=scope, extras=extras, currency="USD")

    def _load_region(self, raw: Path, region: str, add) -> None:
        def rows(name, key=None):
            data = _read(raw / f"inventory/{region}_{name}.json", {} if key else [])
            return data.get(key, []) if key else data

        for resv in rows("instances", "Reservations"):
            for i in resv.get("Instances", []):
                state = i["State"]["Name"]
                if state == "terminated":
                    continue
                tags = _tags(i.get("Tags"))
                m = re.search(r"\((\d{4}-\d{2}-\d{2})", i.get("StateTransitionReason", ""))
                add(Resource(id=i["InstanceId"], type="ec2_instance", region=region, name=tags.get("name", ""),
                             sku=i["InstanceType"], tags=tags, attributes={
                                 "instance_type": i["InstanceType"], "state": state,
                                 "launch_time": str(i.get("LaunchTime", ""))[:10],
                                 "platform": i.get("PlatformDetails", ""), "architecture": i.get("Architecture", ""),
                                 "lifecycle": i.get("InstanceLifecycle", "on-demand"), "vpc_id": i.get("VpcId", ""),
                                 "asg": tags.get("aws:autoscaling:groupname", ""),
                                 "volume_ids": [b["Ebs"]["VolumeId"] for b in i.get("BlockDeviceMappings", []) if "Ebs" in b],
                                 **({"stopped_since": m.group(1)} if state == "stopped" and m else {})}))
        for v in rows("volumes", "Volumes"):
            add(Resource(id=v["VolumeId"], type="ebs_volume", region=region, sku=v["VolumeType"], tags=_tags(v.get("Tags")),
                         name=_tags(v.get("Tags")).get("name", ""), attributes={
                             "size_gb": v["Size"], "volume_type": v["VolumeType"], "state": v["State"],
                             "iops": v.get("Iops"), "attached_to": [a["InstanceId"] for a in v.get("Attachments", [])],
                             "created": str(v.get("CreateTime", ""))[:10]}))
        for a in rows("addresses", "Addresses"):
            add(Resource(id=a.get("AllocationId") or a["PublicIp"], type="elastic_ip", region=region,
                         name=a.get("PublicIp", ""), tags=_tags(a.get("Tags")), attributes={
                             "public_ip": a.get("PublicIp"), "association_id": a.get("AssociationId"),
                             "instance_id": a.get("InstanceId")}))
        for s in rows("snapshots", "Snapshots"):
            add(Resource(id=s["SnapshotId"], type="ebs_snapshot", region=region, tags=_tags(s.get("Tags")),
                         attributes={"size_gb": s["VolumeSize"], "start_time": str(s.get("StartTime", ""))[:10],
                                     "volume_id": s.get("VolumeId"), "storage_tier": s.get("StorageTier", "standard")}))
        endpoints = defaultdict(set)
        for e in rows("vpc_endpoints", "VpcEndpoints"):
            service = e.get("ServiceName", "").rsplit(".", 1)[-1]
            endpoints[e.get("VpcId", "")].add(f"{service}:{e.get('VpcEndpointType', '')}")
        for n in rows("nat_gateways", "NatGateways"):
            add(Resource(id=n["NatGatewayId"], type="aws.nat_gateway", region=region, tags=_tags(n.get("Tags")),
                         name=_tags(n.get("Tags")).get("name", ""), sku=n.get("ConnectivityType", "public"), attributes={
                             "state": n.get("State", ""), "vpc_id": n.get("VpcId", ""), "subnet_id": n.get("SubnetId", ""),
                             "vpc_endpoints": sorted(endpoints.get(n.get("VpcId", ""), set()))}))
        for v in rows("vpn_connections", "VpnConnections"):
            tunnels = [t.get("Status", "") for t in v.get("VgwTelemetry", [])]
            add(Resource(id=v["VpnConnectionId"], type="aws.vpn_connection", region=region, tags=_tags(v.get("Tags")),
                         name=_tags(v.get("Tags")).get("name", ""), sku=v.get("Type", ""),
                         attributes={"state": v.get("State", ""), "tunnels": tunnels}))
        for d in rows("db_instances", "DBInstances"):
            add(Resource(id=d["DBInstanceIdentifier"], type="rds_instance", region=region, sku=d["DBInstanceClass"],
                         name=d["DBInstanceIdentifier"], tags=_tags(d.get("TagList")), attributes={
                             "arn": d.get("DBInstanceArn", ""), "instance_class": d["DBInstanceClass"],
                             "engine": d.get("Engine", ""), "engine_version": d.get("EngineVersion", ""),
                             "multi_az": d.get("MultiAZ", False), "storage_gb": d.get("AllocatedStorage"),
                             "storage_type": d.get("StorageType", ""),
                             "state": "running" if d.get("DBInstanceStatus") == "available" else d.get("DBInstanceStatus", "")}))
        for lb in rows("load_balancers", "LoadBalancers"):
            arn = lb["LoadBalancerArn"]
            add(Resource(id=arn, type="load_balancer", region=region, name=lb["LoadBalancerName"], sku=lb.get("Type", ""),
                         attributes={"lb_type": lb.get("Type", ""), "state": (lb.get("State") or {}).get("Code", ""),
                                     "dimension": arn.split(":loadbalancer/", 1)[-1], "vpc_id": lb.get("VpcId", "")}))
        for g in rows("asgs", "AutoScalingGroups"):
            tags = _tags(g.get("Tags"))
            members = [i["InstanceId"] for i in g.get("Instances", [])]
            types = Counter(i.get("InstanceType", "") for i in g.get("Instances", []) if i.get("InstanceType"))
            add(Resource(id=g["AutoScalingGroupName"], type="aws.asg", region=region, name=g["AutoScalingGroupName"],
                         tags=tags, sku=types.most_common(1)[0][0] if types else "", attributes={
                             "desired": g.get("DesiredCapacity", 0), "min": g.get("MinSize", 0), "max": g.get("MaxSize", 0),
                             "instance_ids": members}))
        ecs = _read(raw / f"inventory/{region}_ecs_services.json", {})
        for s in ecs.get("services", []):
            td = ecs.get("task_definitions", {}).get(s.get("taskDefinition"), {})
            launch = s.get("launchType") or ("FARGATE" if any("FARGATE" in (c.get("capacityProvider") or "")
                                                              for c in s.get("capacityProviderStrategy", [])) else "EC2")
            cluster = s.get("clusterArn", "").rsplit("/", 1)[-1]
            vcpu, mem = float(td.get("cpu") or 0) / 1024, float(td.get("memory") or 0) / 1024
            add(Resource(id=s["serviceArn"], type="aws.ecs_service", region=region, name=s["serviceName"], tags=_tags(s.get("tags")),
                         group=cluster, sku=f"{vcpu:g} vCPU / {mem:g} GB" if vcpu else launch, attributes={
                             "cluster": cluster, "launch_type": launch, "desired": s.get("desiredCount", 0),
                             "running": s.get("runningCount", 0), "vcpu": vcpu, "memory_gb": mem}))
        for svc in _read(raw / f"inventory/{region}_apprunner.json", []):
            inst = svc.get("InstanceConfiguration") or {}
            add(Resource(id=svc["ServiceArn"], type="aws.apprunner_service", region=region, name=svc.get("ServiceName", ""),
                         sku=f"{inst.get('Cpu', '')} CPU / {inst.get('Memory', '')} MB", attributes={
                             "status": svc.get("Status", ""), "service_id": svc.get("ServiceId", ""),
                             "memory_gb": float(inst.get("Memory") or 2048) / 1024, "min_size": 1}))
        for d in _read(raw / f"inventory/{region}_firewalls.json", []):
            fw = d.get("Firewall", {})
            add(Resource(id=fw.get("FirewallArn", fw.get("FirewallName")), type="aws.network_firewall", region=region,
                         name=fw.get("FirewallName", ""), tags=_tags(fw.get("Tags")),
                         attributes={"endpoints": len(fw.get("SubnetMappings") or []), "vpc_id": fw.get("VpcId", "")}))

    def _attach_metrics(self, raw: Path, by_id: dict[str, Resource]) -> None:
        values: dict[tuple[str, str], list[float]] = defaultdict(list)
        periods = {}
        for f in sorted((raw / "metrics").glob("*.json")) if (raw / "metrics").exists() else []:
            if "_queries_" in f.name:
                continue
            payload = json.loads(f.read_text())
            for r in payload.get("results", []):
                meta = payload["meta"].get(r.get("Id"))
                if meta:
                    values[(meta["resource_id"], meta["key"])] += [float(v) for v in r.get("Values", [])]
                    periods[meta["resource_id"]] = (payload.get("period", METRIC_PERIOD), payload.get("days", 14))
        for (rid, key), vals in values.items():
            res = by_id.get(rid)
            if not res:
                continue
            m = res.metrics
            period, days = periods.get(rid, (METRIC_PERIOD, 14))
            m["metrics_window_days"] = days
            scale = 30 / days
            if key == "cpu_mean":
                m.update(cpu_avg=statistics.fmean(vals), cpu_p95=_p95(vals), cpu_avg_14d=statistics.fmean(vals),
                         uptime_share=min(1.0, len(vals) / max(1, days * 86400 / period)))
            elif key == "cpu_max":
                m.update(cpu_max=max(vals), cpu_max_14d=max(vals))
            elif key == "connections_max":
                m.update(connections_max_14d=max(vals), connections_max=max(vals))
            elif key == "requests":
                m.update(request_count_14d=sum(vals), requests_window=sum(vals), requests_30d=sum(vals) * scale)
            elif key == "nat_bytes":
                m.update(bytes_window=sum(vals), processed_gb_30d=sum(vals) / 1e9 * scale)
            elif key == "s3_bytes":
                res.attributes["standard_gb"] = round(max(vals) / 1024 ** 3, 2)
                m["stored_gb"] = res.attributes["standard_gb"]
        requested = _requested(raw)
        for res in by_id.values():  # metrics collected but no datapoints = no traffic
            if res.type in ("load_balancer", "aws.apprunner_service") and (res.id, "requests") in requested:
                res.metrics.setdefault("request_count_14d", 0.0)
                res.metrics.setdefault("requests_30d", 0.0)
            if res.type == "aws.nat_gateway" and (res.id, "nat_bytes") in requested:
                res.metrics.setdefault("bytes_window", 0.0)
                res.metrics.setdefault("processed_gb_30d", 0.0)

    def _relationships(self, resources: list[Resource], by_id: dict[str, Resource]) -> list[Relationship]:
        rels = []
        for r in resources:
            a = r.attributes
            if r.type == "ec2_instance":
                rels += [Relationship(v, r.id, "attached_to") for v in a.get("volume_ids", [])]
                if a.get("asg"):
                    rels.append(Relationship(r.id, a["asg"], "member_of"))
            elif r.type == "elastic_ip" and a.get("instance_id"):
                rels.append(Relationship(r.id, a["instance_id"], "ip_of"))
            elif r.type == "ebs_snapshot" and a.get("volume_id"):
                rels.append(Relationship(r.id, a["volume_id"], "snapshot_of"))
        out = []
        for rel in rels:
            if rel.src in by_id and rel.dst in by_id:
                out.append(rel)
                for x, y in ((rel.src, rel.dst), (rel.dst, rel.src)):
                    if y not in by_id[x].related:
                        by_id[x].related.append(y)
        return out

    def _costs(self, raw: Path, by_id: dict[str, Resource], account: str) -> list[CostRecord]:
        cur = raw / "costs" / "billing.csv"
        if cur.exists():
            aliases = {}
            for r in by_id.values():
                for key in (r.id, r.attributes.get("arn"), r.name):
                    if key:
                        aliases[str(key).lower()] = r
            records = load_billing_csv(cur)
            for rec in records:
                rid = rec.resource_id.lower()
                res = aliases.get(rid) or aliases.get(rid.rsplit(":", 1)[-1]) or aliases.get(rid.rsplit("/", 1)[-1])
                rec.resource_id = res.id if res else rec.resource_id
                rec.provider, rec.account = "aws", rec.account or account
                if res and not rec.tags:
                    rec.tags = res.tags
            return records
        records = []
        for period in _read(raw / "costs/ce_service_region.json", []):
            day = date.fromisoformat(period["TimePeriod"]["Start"])
            for g in period.get("Groups", []):
                service, region = g["Keys"]
                amount = g["Metrics"]["AmortizedCost"]
                cost = float(amount["Amount"])
                if abs(cost) < 1e-9:
                    continue
                records.append(CostRecord(day=day, cost=cost, service=service, account=account,
                                          region="" if region == "NoRegion" else region, provider="aws",
                                          currency=amount.get("Unit", "USD")))
        return records

    def _purchase_types(self, raw: Path) -> list[dict]:
        out = []
        for period in _read(raw / "costs/ce_purchase_type.json", []):
            day = date.fromisoformat(period["TimePeriod"]["Start"])
            for g in period.get("Groups", []):
                service, ptype = g["Keys"]
                out.append({"day": day, "service": service, "pricing_model": PURCHASE_TYPE_MAP.get(ptype, ptype),
                            "cost": float(g["Metrics"]["AmortizedCost"]["Amount"])})
        return out

    def _usage_types(self, raw: Path) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for period in _read(raw / "costs/ce_usage_type.json", []):
            for g in period.get("Groups", []):
                service, usage = g["Keys"]
                entry = out.setdefault(usage, {"service": service, "cost": 0.0})
                entry["cost"] += float(g["Metrics"]["AmortizedCost"]["Amount"])
        return out

    def _compute_optimizer(self, raw: Path) -> list[dict]:
        out = []
        for f in sorted((raw / "recommendations").glob("*_ec2.json")) if (raw / "recommendations").exists() else []:
            for rec in (_read(f, {}) or {}).get("instanceRecommendations", []):
                opts = rec.get("recommendationOptions") or [{}]
                saving = ((opts[0].get("savingsOpportunity") or {}).get("estimatedMonthlySavings") or {})
                out.append({"instance_id": rec.get("instanceArn", "").rsplit("/", 1)[-1], "finding": rec.get("finding", ""),
                            "current_type": rec.get("currentInstanceType", ""), "target_type": opts[0].get("instanceType", ""),
                            "monthly_savings": float(saving.get("value") or 0), "currency": saving.get("currency", "USD"),
                            "reasons": rec.get("findingReasonCodes", [])})
        return out


def _requested(raw: Path) -> set[tuple[str, str]]:
    """(resource, key) pairs that were queried, so 'no datapoints' can be read as zero usage."""
    out = set()
    for f in (raw / "metrics").glob("*.json") if (raw / "metrics").exists() else []:
        if "_queries_" not in f.name:
            out |= {(m["resource_id"], m["key"]) for m in (_read(f, {}).get("meta") or {}).values()}
    return out


def metric_queries(raw: Path, region: str) -> list[dict]:
    """CloudWatch GetMetricData queries for the resources collected in a region."""
    out = []

    def q(rid, key, ns, metric, dims, stat):
        out.append({"meta": {"resource_id": rid, "key": key},
                    "query": {"Id": f"q{len(out)}", "ReturnData": True, "MetricStat": {
                        "Metric": {"Namespace": ns, "MetricName": metric,
                                   "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]},
                        "Period": METRIC_PERIOD if key not in ("s3_bytes",) else 86400, "Stat": stat}}})

    for resv in _read(raw / f"inventory/{region}_instances.json", {}).get("Reservations", []):
        for i in resv.get("Instances", []):
            if i["State"]["Name"] == "running":
                q(i["InstanceId"], "cpu_mean", "AWS/EC2", "CPUUtilization", {"InstanceId": i["InstanceId"]}, "Average")
                q(i["InstanceId"], "cpu_max", "AWS/EC2", "CPUUtilization", {"InstanceId": i["InstanceId"]}, "Maximum")
    for d in _read(raw / f"inventory/{region}_db_instances.json", {}).get("DBInstances", []):
        dims = {"DBInstanceIdentifier": d["DBInstanceIdentifier"]}
        q(d["DBInstanceIdentifier"], "cpu_mean", "AWS/RDS", "CPUUtilization", dims, "Average")
        q(d["DBInstanceIdentifier"], "cpu_max", "AWS/RDS", "CPUUtilization", dims, "Maximum")
        q(d["DBInstanceIdentifier"], "connections_max", "AWS/RDS", "DatabaseConnections", dims, "Maximum")
    for lb in _read(raw / f"inventory/{region}_load_balancers.json", {}).get("LoadBalancers", []):
        if lb.get("Type") == "application":
            q(lb["LoadBalancerArn"], "requests", "AWS/ApplicationELB", "RequestCount",
              {"LoadBalancer": lb["LoadBalancerArn"].split(":loadbalancer/", 1)[-1]}, "Sum")
    for n in _read(raw / f"inventory/{region}_nat_gateways.json", {}).get("NatGateways", []):
        if n.get("State") == "available":
            q(n["NatGatewayId"], "nat_bytes", "AWS/NATGateway", "BytesOutToDestination", {"NatGatewayId": n["NatGatewayId"]}, "Sum")
    for g in _read(raw / f"inventory/{region}_asgs.json", {}).get("AutoScalingGroups", []):
        dims = {"AutoScalingGroupName": g["AutoScalingGroupName"]}
        q(g["AutoScalingGroupName"], "cpu_mean", "AWS/EC2", "CPUUtilization", dims, "Average")
        q(g["AutoScalingGroupName"], "cpu_max", "AWS/EC2", "CPUUtilization", dims, "Maximum")
    for s in _read(raw / f"inventory/{region}_ecs_services.json", {}).get("services", []):
        dims = {"ClusterName": s.get("clusterArn", "").rsplit("/", 1)[-1], "ServiceName": s["serviceName"]}
        q(s["serviceArn"], "cpu_mean", "AWS/ECS", "CPUUtilization", dims, "Average")
        q(s["serviceArn"], "cpu_max", "AWS/ECS", "CPUUtilization", dims, "Maximum")
    for a in _read(raw / f"inventory/{region}_apprunner.json", []):
        q(a["ServiceArn"], "requests", "AWS/AppRunner", "Requests",
          {"ServiceName": a.get("ServiceName", ""), "ServiceID": a.get("ServiceId", "")}, "Sum")
    for b in _read(raw / "inventory/s3_buckets.json", []):
        if b.get("region") == region:
            q(b["name"], "s3_bytes", "AWS/S3", "BucketSizeBytes", {"BucketName": b["name"], "StorageType": "StandardStorage"},
              "Average")
    return out
