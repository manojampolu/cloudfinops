from datetime import date, timedelta

import pytest

from cloudfinops.analysis import enrich_resource_costs, summarize
from cloudfinops.config import Config
from cloudfinops.models import CostRecord, Resource
from cloudfinops.pricing import downsize
from cloudfinops.rules import run_rules, total_savings

AS_OF = date(2026, 10, 15)


def ids(recs, rule_id):
    return {r.resource_id for r in recs if r.rule_id == rule_id}


def run(resources, records=None, cfg=None):
    cfg = cfg or Config()
    records = records or []
    enrich_resource_costs(resources, records, cfg)
    return run_rules(records, resources, cfg, as_of=AS_OF)


def test_unattached_volume_and_gp3():
    recs = run([
        Resource("vol-a", "ebs_volume", attributes={"size_gb": 100, "volume_type": "gp2", "state": "available"}),
        Resource("vol-b", "ebs_volume", attributes={"size_gb": 100, "volume_type": "gp2", "state": "in-use"}),
    ])
    assert ids(recs, "ebs-unattached") == {"vol-a"}
    assert ids(recs, "ebs-gp2-to-gp3") == {"vol-b"}
    unattached = next(r for r in recs if r.rule_id == "ebs-unattached")
    assert unattached.est_monthly_savings == pytest.approx(10.0)


def test_idle_vs_rightsize_are_exclusive():
    recs = run([
        Resource("i-idle", "ec2_instance", attributes={"instance_type": "m5.xlarge", "state": "running"},
                 metrics={"cpu_avg_14d": 1, "cpu_max_14d": 2}),
        Resource("i-big", "ec2_instance", attributes={"instance_type": "m5.xlarge", "state": "running"},
                 metrics={"cpu_avg_14d": 10, "cpu_max_14d": 30}),
        Resource("i-busy", "ec2_instance", attributes={"instance_type": "m5.xlarge", "state": "running"},
                 metrics={"cpu_avg_14d": 60, "cpu_max_14d": 95}),
    ])
    assert ids(recs, "ec2-idle") == {"i-idle"}
    assert ids(recs, "ec2-rightsize") == {"i-big"}


def test_billing_cost_preferred_over_estimate():
    res = Resource("vol-a", "ebs_volume", attributes={"size_gb": 100, "volume_type": "gp2", "state": "available"})
    records = [CostRecord(AS_OF - timedelta(days=i), 2.0, "EC2 - Other", resource_id="vol-a") for i in range(30)]
    run([res], records)
    assert res.cost_source == "billing"
    assert res.monthly_cost == pytest.approx(60.0)


def test_old_snapshot_and_retention_tag():
    recs = run([
        Resource("snap-old", "ebs_snapshot", attributes={"size_gb": 10, "start_time": "2025-01-01"}),
        Resource("snap-keep", "ebs_snapshot", tags={"retention": "permanent"},
                 attributes={"size_gb": 10, "start_time": "2025-01-01"}),
        Resource("snap-new", "ebs_snapshot", attributes={"size_gb": 10, "start_time": "2026-10-01"}),
    ])
    assert ids(recs, "ebs-snapshot-old") == {"snap-old"}


def test_nonprod_schedule_and_untagged():
    cfg = Config()
    tags = {"owner": "a", "environment": "dev", "cost-center": "x"}
    recs = run([
        Resource("i-dev", "ec2_instance", tags=tags, attributes={"instance_type": "t3.large", "state": "running"},
                 metrics={"cpu_avg_14d": 30, "cpu_max_14d": 80}),
        Resource("i-notags", "ec2_instance", attributes={"instance_type": "t3.large", "state": "running"}),
    ], cfg=cfg)
    sched = next(r for r in recs if r.rule_id == "nonprod-schedule")
    assert sched.resource_id == "i-dev"
    assert sched.est_monthly_savings == pytest.approx(sched.current_monthly_cost * (1 - 60 / 168))
    assert ids(recs, "untagged") == {"i-notags"}


def test_savings_plan_uses_baseline():
    records = [CostRecord(AS_OF - timedelta(days=i), 100.0 + (i % 3) * 10,
                          "Amazon Elastic Compute Cloud - Compute", pricing_category="On-Demand")
               for i in range(30)]
    recs = run([], records)
    sp = next(r for r in recs if r.rule_id == "savings-plan")
    assert sp.est_monthly_savings == pytest.approx(100.0 * 30 * Config().savings_plan_discount)


def test_total_savings_dedupes_per_resource():
    recs = run([
        Resource("i-big", "ec2_instance", attributes={"instance_type": "m5.2xlarge", "state": "running"},
                 metrics={"cpu_avg_14d": 10, "cpu_max_14d": 30}),
    ])
    per_resource = [r.est_monthly_savings for r in recs if r.resource_id == "i-big"]
    assert len(per_resource) >= 2  # rightsize + graviton (+ untagged)
    assert total_savings(recs) == pytest.approx(max(per_resource))


def test_downsize():
    assert downsize("m5.2xlarge") == "m5.xlarge"
    assert downsize("db.r5.large") == "db.r5.medium"
    assert downsize("t3.micro") is None
    assert downsize("weird") is None


def test_anomaly_detection():
    records = [CostRecord(AS_OF - timedelta(days=i), 10.0 + (i % 2), "S3") for i in range(30)]
    records.append(CostRecord(AS_OF - timedelta(days=2), 500.0, "S3"))
    s = summarize(records, Config())
    assert [a.day for a in s.anomalies] == [AS_OF - timedelta(days=2)]
