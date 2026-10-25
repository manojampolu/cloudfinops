"""`assess --provider aws`: normalization from aws CLI output, rules, and read-only collection."""

import json
from importlib import resources as pkg_resources
from pathlib import Path

import pytest

from cloudfinops.analysis import enrich_resource_costs
from cloudfinops.assessment import assess
from cloudfinops.config import Config
from cloudfinops.harness.runner import CliError, check_read_only
from cloudfinops.providers.aws_cli import AwsProvider
from cloudfinops.rules.aws import _major
from cloudfinops.rules import run_rules

SAMPLE = Path(str(pkg_resources.files("cloudfinops") / "samples" / "aws"))


@pytest.fixture(scope="module")
def ds():
    return AwsProvider().load(SAMPLE, Config())


@pytest.fixture(scope="module")
def recs(ds):
    cfg = Config()
    enrich_resource_costs(ds.resources, ds.records, cfg)
    return run_rules(ds.records, ds.resources, cfg, provider="aws", extras=ds.extras, relationships=ds.relationships)


def names(recs, rule):
    return {r.resource_name for r in recs if r.rule_id == rule}


def test_normalization(ds):
    types = {r.type for r in ds.resources}
    assert {"ec2_instance", "ebs_volume", "rds_instance", "load_balancer", "s3_bucket", "aws.nat_gateway",
            "aws.vpn_connection", "aws.asg", "aws.ecs_service", "aws.apprunner_service", "aws.network_firewall"} <= types
    web = next(r for r in ds.resources if r.name == "web-1")
    assert web.metrics["cpu_avg_14d"] > 20 and web.metrics["uptime_share"] == pytest.approx(1.0)
    assert any(ds.extras["pricing_model_daily"]) and ds.extras["usage_types"]
    assert ds.records and not any(r.resource_id for r in ds.records)  # Cost Explorer is service-level
    bucket = next(r for r in ds.resources if r.name == "acme-app-logs")
    assert bucket.attributes["standard_gb"] > 20000 and bucket.attributes["has_lifecycle_policy"] is False
    asg = next(r for r in ds.resources if r.name == "asg-render")
    assert len(asg.related) == 4  # member instances linked


def test_rules(recs):
    assert names(recs, "aws-nat-idle") == {"nat-dev-unused"}
    assert names(recs, "aws-nat-heavy") == {"nat-prod-a"}
    assert names(recs, "aws-vpn-down") == {"vpn-old-branch"}  # vpn-hq has one tunnel up
    assert names(recs, "aws-network-firewall-nonprod") == {"nfw-dev"}
    assert names(recs, "aws-asg-nonprod-schedule") == {"asg-dev-web"}
    assert "asg-render" in names(recs, "aws-asg-rightsize")
    assert names(recs, "aws-ecs-rightsize") == {"svc-reports"}
    assert names(recs, "aws-ecs-nonprod-schedule") == {"svc-dev-api"}
    assert names(recs, "aws-apprunner-unused") == {"ar-marketing-site"}
    assert names(recs, "aws-rds-nonprod-multiaz") == {"staging-db"}
    assert names(recs, "aws-rds-extended-support") == {"staging-db", "legacy-mysql"}
    assert names(recs, "lb-idle") == {"alb-old-campaign"}
    assert len([r for r in recs if r.rule_id == "aws-compute-optimizer"]) == 2
    assert not names(recs, "untagged") and len([r for r in recs if r.rule_id == "aws-untagged"]) == 1
    # ASG members are judged at group level, not per instance
    assert not any(n.startswith(("render-", "asg-dev-web-")) for n in names(recs, "ec2-rightsize") | names(recs, "nonprod-schedule"))
    ext = next(r for r in recs if r.rule_id == "aws-rds-extended-support" and r.resource_name == "staging-db")
    assert ext.confidence == "medium" and ext.scenario_group.endswith("|extended-support")  # seen on the bill
    assert all(r.evidence for r in recs if r.est_monthly_savings > 0)


def test_major_versions():
    assert _major("postgres", "12.19") == "12" and _major("mysql", "5.7.44") == "5.7"
    assert _major("aurora-mysql", "5.7.mysql_aurora.2.11.2") == "5.7" and _major("mysql", "8.0.35") == "8.0"


def test_report_and_quality(tmp_path):
    result = assess(AwsProvider().load(SAMPLE, Config()), Config(), tmp_path)
    msgs = [q.message for q in result.quality]
    assert any("service-level" in m for m in msgs)
    assert not any("no matching resource" in m for m in msgs)
    assert "AWS estate" in result.outputs["report"].read_text()


class FakeAws:
    """Replays the sample raw files as aws CLI output; Cost Explorer is denied; App Runner unsupported in eu-west-1."""

    def __init__(self, tmp_path, deny_costs=True):
        self.run_dir = Path(tmp_path)
        self.raw_dir = self.run_dir / "raw"
        self.raw_dir.mkdir(parents=True)
        self.calls, self.deny_costs = [], deny_costs
        self.metric_files = []

    def run(self, cmd, save_as=None, parse_json=True, interactive=False, absent_ok=()):
        check_read_only(cmd)
        self.calls.append(cmd)
        try:
            return self._answer(cmd, save_as)
        except CliError as exc:
            if any(code in exc.stderr for code in absent_ok):
                return None
            raise

    def _answer(self, cmd, save_as):
        words = [w for w in cmd if not w.startswith("--")]
        region = cmd[cmd.index("--region") + 1] if "--region" in cmd else "us-east-1"
        svc, op = cmd[1], cmd[2]
        if (svc, op) == ("ce", "get-cost-and-usage") and self.deny_costs:
            raise CliError(cmd, 254, "An error occurred (AccessDeniedException) when calling the GetCostAndUsage "
                                     "operation: User is not authorized to perform: ce:GetCostAndUsage")
        if svc == "apprunner" and region == "eu-west-1":
            raise CliError(cmd, 255, "Could not connect to the endpoint URL: https://apprunner.eu-west-1.amazonaws.com/")
        if (svc, op) == ("cloudwatch", "get-metric-data"):
            path = cmd[cmd.index("--metric-data-queries") + 1]
            assert path.startswith("file://")
            self.metric_files.append(path)
            return {"MetricDataResults": []}
        if (svc, op) == ("s3api", "list-buckets"):
            return {"Buckets": [{"Name": "acme-app-logs", "CreationDate": "2024-03-01T00:00:00Z"}]}
        if svc == "s3api":
            if op == "get-bucket-location":
                return {"LocationConstraint": None}
            raise CliError(cmd, 254, "An error occurred (AccessDenied) when calling the GetBucketLifecycleConfiguration operation")
        name_map = {"describe-instances": "instances", "describe-volumes": "volumes", "describe-addresses": "addresses",
                    "describe-snapshots": "snapshots", "describe-nat-gateways": "nat_gateways",
                    "describe-vpn-connections": "vpn_connections", "describe-vpc-endpoints": "vpc_endpoints",
                    "describe-db-instances": "db_instances", "describe-load-balancers": "load_balancers",
                    "describe-auto-scaling-groups": "asgs"}
        if op in name_map:
            data = json.loads((SAMPLE / "raw/inventory" / f"{region}_{name_map[op]}.json").read_text())
            if save_as:
                path = self.raw_dir / save_as
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data))
            return data
        return {}

    def save_json(self, rel, data):
        path = self.raw_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path


def test_collect_is_read_only_and_reports_gaps_once(tmp_path):
    runner = FakeAws(tmp_path)
    cfg = Config(aws_regions=["us-east-1", "eu-west-1"])
    manifest = AwsProvider().collect(runner, [{"id": "444455556666", "name": "acme-workloads"}], cfg, log=lambda *_: None)
    ce_calls = [c for c in runner.calls if c[1] == "ce"]
    assert len(ce_calls) == 1  # denied once, other Cost Explorer queries skipped
    access = [e for e in manifest["errors"] if e["step"] == "access"]
    assert [e["message"].split(":")[0] for e in access] == ["Cost Explorer (needs AWSBillingReadOnlyAccess / ce"]
    assert not any("apprunner" in e["scope"] or "apprunner" in e["message"].lower() for e in manifest["errors"])
    assert runner.metric_files  # metrics batched through query files
    buckets = json.loads((tmp_path / "raw/inventory/s3_buckets.json").read_text())
    assert buckets[0]["has_lifecycle"] is None  # access denied -> unknown, never "no policy"


def test_estate_map_does_not_double_count_service_level_billing(tmp_path):
    result = assess(AwsProvider().load(SAMPLE, Config()), Config(), tmp_path)
    account = result.tree.children[0]
    assert not any(c.kind == "unmapped" for c in account.children)
    assert result.tree.cost == pytest.approx(sum(r.monthly_cost or 0 for r in result.dataset.resources))


def test_not_configured_answers_are_not_failures(tmp_path, monkeypatch):
    """NoSuchTagSet / NoSuchLifecycleConfiguration are normal answers: logged as exit 0, returned as None."""
    import os
    import stat
    import sys
    from cloudfinops.harness.runner import CliRunner
    if sys.platform == "win32":
        pytest.skip("uses a POSIX shell script as a fake aws")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "aws"
    fake.write_text("#!/bin/sh\necho 'An error occurred (NoSuchTagSet) when calling the GetBucketTagging operation' >&2\nexit 254\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    runner = CliRunner(tmp_path / "run")
    assert runner.run(["aws", "s3api", "get-bucket-tagging", "--bucket", "b"], absent_ok=("NoSuchTagSet",)) is None
    entry = json.loads((tmp_path / "run/commands.log").read_text().splitlines()[0])
    assert entry["exit"] == 0 and entry["result"] == "not configured"
    with pytest.raises(CliError):  # without absent_ok it is still an error
        runner.run(["aws", "s3api", "get-bucket-tagging", "--bucket", "b"])


def test_billing_note_when_cost_explorer_denied(tmp_path):
    runner = FakeAws(tmp_path)
    manifest = AwsProvider().collect(runner, [{"id": "444455556666", "name": "acme-workloads"}],
                                     Config(aws_regions=["us-east-1"]), log=lambda *_: None)
    assert manifest["billing_note"].startswith("Cost Explorer access denied")
