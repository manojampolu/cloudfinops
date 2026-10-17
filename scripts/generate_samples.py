"""Generate deterministic sample billing + inventory data into src/cloudfinops/samples/.

    python scripts/generate_samples.py
"""

from __future__ import annotations

import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cloudfinops.ingest import write_billing_csv, write_inventory  # noqa: E402
from cloudfinops.models import CostRecord, Resource  # noqa: E402
from cloudfinops.pricing import EC2_HOURLY, RDS_HOURLY  # noqa: E402

END = date(2026, 10, 15)
DAYS = 60
PROD, DEV = "111111111111", "222222222222"
R = "us-east-1"
TEAM = {"owner": "payments", "environment": "prod", "cost-center": "cc-100"}


def ec2(id, itype, cpu_avg, cpu_max, tags, account=PROD, state="running", **attrs):
    return Resource(id=id, type="ec2_instance", region=R, account=account, tags=tags,
                    attributes={"instance_type": itype, "state": state, **attrs},
                    metrics={} if state != "running" else {"cpu_avg_14d": cpu_avg, "cpu_max_14d": cpu_max})


def vol(id, gb, vtype, state="in-use", account=PROD, tags=None):
    return Resource(id=id, type="ebs_volume", region=R, account=account, tags=tags or {},
                    attributes={"size_gb": gb, "volume_type": vtype, "state": state})


def build_inventory() -> list[Resource]:
    dev = {"owner": "platform", "environment": "dev", "cost-center": "cc-200"}
    return [
        ec2("i-0a1prodapi01", "m5.2xlarge", 11.0, 34.0, TEAM, volume_ids=["vol-0prodapi01"]),
        ec2("i-0a1prodapi02", "m5.2xlarge", 12.5, 38.0, TEAM, volume_ids=["vol-0prodapi02"]),
        ec2("i-0a1prodweb01", "c5.xlarge", 55.0, 88.0, TEAM),
        ec2("i-0a1legacy01", "m4.xlarge", 31.0, 72.0, {"owner": "billing", "environment": "prod"}),
        ec2("i-0a1batch01", "r5.xlarge", 0.6, 2.1, {"owner": "data", "environment": "prod", "cost-center": "cc-300"}),
        ec2("i-0d2devapp01", "m5.xlarge", 24.0, 61.0, dev, account=DEV),
        ec2("i-0d2devapp02", "t3.large", 26.0, 70.0, dev, account=DEV),
        ec2("i-0d2oldtest", "t2.medium", 0, 0, {}, account=DEV, state="stopped",
            stopped_since="2026-05-02", volume_ids=["vol-0oldtest01"]),
        vol("vol-0prodapi01", 500, "gp2", tags=TEAM),
        vol("vol-0prodapi02", 500, "gp2", tags=TEAM),
        vol("vol-0oldtest01", 200, "gp2", account=DEV),
        vol("vol-0orphan01", 1000, "gp2", state="available", account=DEV),
        vol("vol-0orphan02", 250, "io1", state="available"),
        Resource(id="eipalloc-0aa1", type="elastic_ip", region=R, account=PROD, attributes={"association_id": None}),
        Resource(id="eipalloc-0aa2", type="elastic_ip", region=R, account=DEV, attributes={"association_id": None}),
        Resource(id="eipalloc-0aa3", type="elastic_ip", region=R, account=PROD,
                 attributes={"association_id": "eipassoc-1"}),
        Resource(id="snap-0old01", type="ebs_snapshot", region=R, account=PROD,
                 attributes={"size_gb": 500, "start_time": "2025-11-20"}),
        Resource(id="snap-0old02", type="ebs_snapshot", region=R, account=PROD,
                 attributes={"size_gb": 800, "start_time": "2025-06-01"}),
        Resource(id="snap-0recent", type="ebs_snapshot", region=R, account=PROD,
                 attributes={"size_gb": 500, "start_time": "2026-10-01"}),
        Resource(id="orders-db", type="rds_instance", region=R, account=PROD, tags=TEAM,
                 attributes={"instance_class": "db.r5.2xlarge", "engine": "postgres", "multi_az": True},
                 metrics={"cpu_avg_14d": 9.0, "cpu_max_14d": 31.0, "connections_max_14d": 140}),
        Resource(id="staging-db", type="rds_instance", region=R, account=DEV, tags={**dev, "environment": "staging"},
                 attributes={"instance_class": "db.m5.large", "engine": "mysql", "multi_az": False},
                 metrics={"cpu_avg_14d": 22.0, "cpu_max_14d": 58.0, "connections_max_14d": 12}),
        Resource(id="poc-analytics-db", type="rds_instance", region=R, account=DEV, tags={"owner": "data"},
                 attributes={"instance_class": "db.m5.xlarge", "engine": "postgres", "multi_az": False},
                 metrics={"cpu_avg_14d": 1.2, "cpu_max_14d": 3.0, "connections_max_14d": 0}),
        Resource(id="old-campaign-alb", type="load_balancer", region=R, account=PROD, tags={"owner": "marketing"},
                 attributes={"lb_type": "application"}, metrics={"request_count_14d": 0}),
        Resource(id="prod-alb", type="load_balancer", region=R, account=PROD, tags=TEAM,
                 attributes={"lb_type": "application"}, metrics={"request_count_14d": 48_000_000}),
        Resource(id="acme-app-logs", type="s3_bucket", region=R, account=PROD, tags=TEAM,
                 attributes={"standard_gb": 42_000, "has_lifecycle_policy": False}),
        Resource(id="acme-static-assets", type="s3_bucket", region=R, account=PROD, tags=TEAM,
                 attributes={"standard_gb": 120, "has_lifecycle_policy": True}),
    ]


def build_billing(inventory: list[Resource]) -> list[CostRecord]:
    rnd = random.Random(42)
    records = []
    start = END - timedelta(days=DAYS - 1)
    for n in range(DAYS):
        day = start + timedelta(days=n)
        weekday = day.weekday() < 5
        growth = 1 + n * 0.003
        for res in inventory:
            a, cost, svc = res.attributes, None, None
            if res.type == "ec2_instance" and a["state"] == "running":
                cost, svc = EC2_HOURLY[a["instance_type"]] * 24, "Amazon Elastic Compute Cloud - Compute"
            elif res.type == "ebs_volume":
                rate = {"gp2": 0.10, "io1": 0.125}[a["volume_type"]]
                cost, svc = rate * a["size_gb"] / 30, "EC2 - Other"
            elif res.type == "rds_instance":
                cost = RDS_HOURLY[a["instance_class"]] * 24 * (2 if a["multi_az"] else 1)
                svc = "Amazon Relational Database Service"
            elif res.type == "s3_bucket":
                cost, svc = a["standard_gb"] * 0.023 / 30 * growth, "Amazon Simple Storage Service"
            elif res.type == "load_balancer":
                cost, svc = 0.0225 * 24 + (8.0 if res.metrics["request_count_14d"] else 0), "Elastic Load Balancing"
            if cost is None:
                continue
            records.append(CostRecord(day=day, cost=round(cost * rnd.uniform(0.97, 1.03), 4), service=svc,
                                      account=res.account, region=res.region, resource_id=res.id,
                                      pricing_category="On-Demand", tags=res.tags))
        # Unattributed shared costs
        nat = 95.0 * (1.15 if weekday else 0.8) * rnd.uniform(0.9, 1.1)
        records.append(CostRecord(day=day, cost=round(nat, 2), service="EC2 - Other", account=PROD, region=R,
                                  usage_type="USE1-NatGateway-Bytes", pricing_category="On-Demand"))
        egress = 38.0 * rnd.uniform(0.85, 1.15) + (420.0 if day == END - timedelta(days=4) else 0)
        records.append(CostRecord(day=day, cost=round(egress, 2), service="AWS Data Transfer", account=PROD,
                                  region=R, usage_type="USE1-DataTransfer-Out-Bytes"))
        records.append(CostRecord(day=day, cost=round(14.0 * rnd.uniform(0.9, 1.1), 2),
                                  service="Amazon CloudWatch", account=PROD, region=R))
        records.append(CostRecord(day=day, cost=round(9.0 * rnd.uniform(0.9, 1.1), 2),
                                  service="AWS Lambda", account=DEV, region=R, pricing_category="On-Demand",
                                  tags={"owner": "platform", "environment": "dev"}))
    return records


def main() -> None:
    out = ROOT / "src" / "cloudfinops" / "samples"
    inventory = build_inventory()
    write_billing_csv(build_billing(inventory), out / "billing.csv")
    write_inventory(inventory, out / "inventory.json")
    # Inventory snapshot is for the sample date, not "now"
    data = json.loads((out / "inventory.json").read_text())
    data["generated_at"] = f"{END.isoformat()}T00:00:00+00:00"
    (out / "inventory.json").write_text(json.dumps(data, indent=2))
    print(f"Wrote samples to {out}")


if __name__ == "__main__":
    main()
