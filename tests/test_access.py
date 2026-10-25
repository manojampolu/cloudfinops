"""Access gaps are detected once, skipped, and reported once - for Azure and AWS."""

import json
from datetime import datetime, timezone
from importlib import resources as pkg_resources
from pathlib import Path

import pytest

from cloudfinops.config import Config
from cloudfinops.harness.errors import is_permission_error, short_error
from cloudfinops.harness.runner import CliError, check_read_only

SAMPLE_RAW = Path(str(pkg_resources.files("cloudfinops") / "samples" / "azure" / "raw"))
PROD, NONPROD, EMPTY = ("11111111-aaaa-4aaa-8aaa-111111111111", "22222222-bbbb-4bbb-8bbb-222222222222",
                        "33333333-cccc-4ccc-8ccc-333333333333")
AZ_DENIED = ("ERROR: (AuthorizationFailed) The client 'me@x.com' with object id '1' does not have authorization "
             "to perform action 'Microsoft.Insights/metrics/read' over scope '/subscriptions/2'\nCode: AuthorizationFailed")


def test_error_helpers_understand_all_three_clouds():
    assert is_permission_error(AZ_DENIED)
    assert short_error(AZ_DENIED).startswith("AuthorizationFailed: The client") and "Code:" not in short_error(AZ_DENIED)
    aws = ("An error occurred (AccessDeniedException) when calling the GetCostAndUsage operation: "
           "User: arn:aws:iam::1:user/x is not authorized to perform: ce:GetCostAndUsage")
    assert is_permission_error(aws) and short_error(aws).startswith("AccessDeniedException (GetCostAndUsage)")
    assert is_permission_error("ERROR: (gcloud.services.list) [me] does not have permission to access projects")
    assert not is_permission_error("ERROR: (ResourceNotFound) The Resource 'x' was not found")


class FakeAzRunner:
    def __init__(self, tmp_path):
        self.run_dir = Path(tmp_path)
        self.raw_dir = self.run_dir / "raw"
        self.raw_dir.mkdir(parents=True)
        self.posts, self.metric_calls = [], []
        self.resources = json.loads((SAMPLE_RAW / "inventory/resources.json").read_text())

    def run(self, cmd, save_as=None, parse_json=True, interactive=False):
        check_read_only(cmd)
        joined = " ".join(cmd)
        if cmd[:3] == ["az", "graph", "query"]:
            data = [] if "ResourceContainers" in joined else self.resources
            return {"data": data, "skip_token": None}
        if "get-access-token" in cmd:
            return {"accessToken": "t"}
        if cmd[:2] in (["az", "advisor"], ["az", "security"]):
            return []
        if cmd[:3] == ["az", "monitor", "metrics"]:
            self.metric_calls.append(joined)
            if NONPROD in joined:
                raise CliError(cmd, 1, AZ_DENIED)
            return {"interval": "PT1H", "value": []}
        if cmd[:3] == ["az", "monitor", "log-analytics"]:
            if NONPROD in joined or joined.endswith("-o json") and "0002" in joined:
                raise CliError(cmd, 1, AZ_DENIED)
            return []
        raise AssertionError(f"unexpected command {joined}")

    def http_post(self, url, token, body, headers=None, save_as=None):
        self.posts.append(url)
        if NONPROD in url:
            raise CliError(["POST", url], 403, '{"error": {"code": "AuthorizationFailed", "message": "No access to cost data"}}')
        return {"properties": {"columns": [{"name": c} for c in ("Cost", "UsageDate", "ResourceId", "ServiceName", "Currency")],
                               "rows": [], "nextLink": None}}

    def save_json(self, rel, data):
        path = self.raw_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path


def test_azure_access_gaps_are_reported_once_per_subscription(tmp_path):
    from cloudfinops.providers.azure import AzureProvider
    cfg = Config(cost_query_pause_s=0, price_lookup=False)
    runner = FakeAzRunner(tmp_path)
    scopes = [{"id": PROD, "name": "acme-prod"}, {"id": NONPROD, "name": "acme-nonprod"}, {"id": EMPTY, "name": "acme-empty"}]
    manifest = AzureProvider().collect(runner, scopes, cfg, log=lambda *_: None)

    access = [e for e in manifest["errors"] if e["step"] == "access"]
    by_scope = {}
    for e in access:
        by_scope.setdefault(e["scope"], []).append(e["message"])
    assert len(by_scope["acme-nonprod"]) == 3  # costs, logging, metrics - one each
    assert any(m.startswith("Cost Management") for m in by_scope["acme-nonprod"])
    assert any("Resource Graph" in m and "no resources visible" in m for m in by_scope["acme-empty"])
    assert "acme-prod" not in by_scope
    assert manifest["access_gaps"]["costs"] == [NONPROD]

    assert sum(1 for u in runner.posts if NONPROD in u) == 1  # pricing-model query skipped after the denial
    nonprod_metric_calls = sum(1 for c in runner.metric_calls if NONPROD in c)
    nonprod_targets = sum(1 for r in runner.resources if r["subscriptionId"] == NONPROD) // 2
    assert 1 <= nonprod_metric_calls <= 16 < nonprod_targets  # stopped after the first denials (8 worker threads)
    assert not [e for e in manifest["errors"] if e["step"] == "metrics"]  # no per-resource noise
    assert all("\n" not in e["message"] for e in manifest["errors"])


# ---------------------------------------------------------------- AWS

botocore = pytest.importorskip("botocore")


def _denied(op):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "not authorized"}}, op)


class _Pages:
    def __init__(self, pages=None, error=None, counter=None, key=None):
        self.pages, self.error, self.counter, self.key = pages or [], error, counter, key

    def paginate(self, **_):
        if self.counter is not None:
            self.counter[self.key] = self.counter.get(self.key, 0) + 1
        if self.error:
            raise self.error
        return self.pages


class FakeAwsSession:
    region_name = "r1"

    def __init__(self, ce_error=None):
        self.calls = {}
        self.ce_error = ce_error

    def client(self, name, region_name=None):
        calls, ce_error = self.calls, self.ce_error

        class C:
            def get_caller_identity(self):
                return {"Account": "123456789012"}

            def describe_regions(self):
                return {"Regions": [{"RegionName": r} for r in ("r1", "r2", "r3")]}

            def describe_addresses(self):
                return {"Addresses": []}

            def get_paginator(self, op):
                if name == "rds":
                    return _Pages(error=_denied("DescribeDBInstances"), counter=calls, key="rds")
                pages = {
                    "describe_instances": [{"Reservations": [{"Instances": [{
                        "InstanceId": f"i-{region_name}", "State": {"Name": "running"}, "InstanceType": "m5.large",
                        "LaunchTime": datetime(2026, 1, 1, tzinfo=timezone.utc), "BlockDeviceMappings": []}]}]}],
                    "describe_volumes": [{"Volumes": []}], "describe_snapshots": [{"Snapshots": []}],
                    "describe_load_balancers": [{"LoadBalancers": []}],
                }[op]
                return _Pages(pages)

            def get_metric_statistics(self, **_):
                calls["cloudwatch"] = calls.get("cloudwatch", 0) + 1
                raise _denied("GetMetricStatistics")

            def list_buckets(self):
                return {"Buckets": []}

            def get_cost_and_usage(self, **_):
                raise ce_error

        return C()


def test_aws_denied_services_skipped_after_first_region(monkeypatch):
    from cloudfinops.providers import aws
    session = FakeAwsSession()
    monkeypatch.setattr(aws, "_session", lambda profile: session)
    gaps = {}
    resources = aws.collect_inventory(gaps=gaps)
    assert sorted(r.id for r in resources) == ["i-r1", "i-r2", "i-r3"]  # EC2 inventory kept despite CloudWatch denial
    assert gaps["denied"] == ["cloudwatch", "rds"]
    assert session.calls == {"rds": 1, "cloudwatch": 1}


def test_aws_cost_explorer_errors_are_readable(monkeypatch):
    from botocore.exceptions import ClientError
    from cloudfinops.providers import aws
    monkeypatch.setattr(aws, "_session", lambda profile: FakeAwsSession(ce_error=_denied("GetCostAndUsage")))
    with pytest.raises(ValueError, match="Cost Explorer access denied"):
        aws.collect_costs()
    unavailable = ClientError({"Error": {"Code": "DataUnavailableException", "Message": "x"}}, "GetCostAndUsage")
    monkeypatch.setattr(aws, "_session", lambda profile: FakeAwsSession(ce_error=unavailable))
    with pytest.raises(ValueError, match="Enable Cost Explorer"):
        aws.collect_costs()


def test_interactive_login_timeout_is_a_clean_error(tmp_path, monkeypatch):
    import subprocess
    from cloudfinops.harness.runner import CliRunner

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=1)

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(CliError, match="sign-in was not completed"):
        CliRunner(tmp_path, timeout=1).run(["gcloud", "auth", "login"], interactive=True)
