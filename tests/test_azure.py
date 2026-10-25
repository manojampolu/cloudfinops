import json
import os
import stat
import sys
from datetime import date, timedelta
from importlib import resources as pkg_resources
from pathlib import Path

import pytest

from cloudfinops.analysis import enrich_resource_costs
from cloudfinops.assessment import assess
from cloudfinops.config import Config
from cloudfinops.harness.runner import CliRunner, NotReadOnlyError, check_read_only
from cloudfinops.models import CostRecord, Recommendation
from cloudfinops.pricing_azure import disk_tier_price, parse_vm_size, vm_downsize, vm_memory_gb
from cloudfinops.providers.azure import AzureProvider
from cloudfinops.register import Register
from cloudfinops.rules import run_rules, scenario_total

SAMPLE = Path(str(pkg_resources.files("cloudfinops") / "samples" / "azure"))


@pytest.fixture(scope="module")
def dataset():
    return AzureProvider().load(SAMPLE, Config())


@pytest.fixture(scope="module")
def findings(dataset):
    cfg = Config()
    enrich_resource_costs(dataset.resources, dataset.records, cfg)
    return run_rules(dataset.records, dataset.resources, cfg, provider="azure", extras=dataset.extras,
                     relationships=dataset.relationships)


def by_rule(recs, rule_id):
    return [r for r in recs if r.rule_id == rule_id]


def names(recs):
    return {r.resource_name for r in recs}


# ---------------------------------------------------------------- runner

@pytest.mark.parametrize("cmd", [
    ["az", "graph", "query", "-q", "Resources", "-o", "json"],
    ["az", "monitor", "metrics", "list", "--resource", "x"],
    ["az", "advisor", "recommendation", "list", "--category", "Cost"],
    ["az", "monitor", "log-analytics", "workspace", "table", "list", "-g", "rg"],
    ["az", "rest", "--method", "post", "--url",
     "https://management.azure.com/subscriptions/s/providers/Microsoft.CostManagement/query?api-version=1"],
    ["az", "rest", "--url", "https://management.azure.com/subscriptions/s/resources"],
    ["az", "login", "--use-device-code"],
    ["aws", "ec2", "describe-instances"],
    ["aws", "ce", "get-cost-and-usage"],
    ["gcloud", "asset", "search-all-resources"],
])
def test_read_only_allowed(cmd):
    check_read_only(cmd)


@pytest.mark.parametrize("cmd", [
    ["az", "vm", "delete", "--ids", "x"],
    ["az", "vm", "deallocate", "-n", "x"],
    ["az", "disk", "update", "--sku", "Standard_LRS"],
    ["az", "rest", "--method", "put", "--url", "https://management.azure.com/x"],
    ["az", "rest", "--method", "post", "--url", "https://management.azure.com/x/start?api-version=1"],
    ["aws", "ec2", "terminate-instances", "--instance-ids", "i-1"],
])
def test_write_commands_rejected(cmd):
    with pytest.raises(NotReadOnlyError):
        check_read_only(cmd)


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as a fake az")
def test_runner_logs_and_saves_raw(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "az"
    fake.write_text('#!/bin/sh\necho \'{"data": [{"id": "a"}], "skip_token": null}\'\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    runner = CliRunner(tmp_path / "run")
    out = runner.run(["az", "graph", "query", "-q", "Resources"], save_as="inventory/page1.json")
    assert out["data"][0]["id"] == "a"
    assert (tmp_path / "run/raw/inventory/page1.json").exists()
    entry = json.loads((tmp_path / "run/commands.log").read_text().splitlines()[0])
    assert entry["cmd"].startswith("az graph query") and entry["exit"] == 0
    with pytest.raises(NotReadOnlyError):
        runner.run(["az", "vm", "delete", "--ids", "x"])
    assert len((tmp_path / "run/commands.log").read_text().splitlines()) == 1  # rejected before running


# ---------------------------------------------------------------- pricing helpers

def test_vm_size_helpers():
    assert parse_vm_size("Standard_D8s_v5") == {"family": "d", "vcpu": 8, "suffix": "s", "ver": "v5"}
    assert vm_downsize("Standard_D8s_v5") == "Standard_D4s_v5"
    assert vm_downsize("Standard_E16as_v5") == "Standard_E8as_v5"
    assert vm_downsize("Standard_D2s_v5") is None
    assert vm_downsize("Standard_B4ms") == "Standard_B2ms"
    assert vm_memory_gb("Standard_E8s_v5") == 64
    assert vm_memory_gb("Standard_B2s") == 4
    assert disk_tier_price(100, "Premium_LRS") == disk_tier_price(128, "Premium_LRS")
    assert disk_tier_price(512, "StandardSSD_LRS") < disk_tier_price(512, "Premium_LRS")


# ---------------------------------------------------------------- normalization

def test_load_inventory_and_relationships(dataset):
    by_id = {r.id: r for r in dataset.resources}
    vm = next(r for r in dataset.resources if r.name == "vm-web-01")
    assert vm.type == "azure.vm" and vm.sku == "Standard_D4s_v5" and vm.attributes["state"] == "running"
    assert vm.id == vm.id.lower()
    disks = [by_id[i] for i in vm.related if i in by_id and by_id[i].type == "azure.disk"]
    assert len(disks) == 3  # OS + 2 data
    assert sum(1 for r in dataset.resources if r.type == "azure.disk") > 200
    dealloc = next(r for r in dataset.resources if r.name == "vm-legacy-01")
    assert dealloc.attributes["power_state"] == "deallocated"
    plan = next(r for r in dataset.resources if r.name == "asp-prod-web")
    assert sum(1 for i in plan.related if by_id.get(i) and by_id[i].type == "azure.web_app") == 6


def test_load_costs_metrics_and_extras(dataset):
    assert dataset.currency == "USD"
    assert {r.provider for r in dataset.records} == {"azure"}
    assert any(r.resource_id == "" and r.service == "Microsoft Defender for Cloud" for r in dataset.records)
    api1 = next(r for r in dataset.resources if r.name == "vm-api-01")
    api9 = next(r for r in dataset.resources if r.name == "vm-api-09")
    assert api1.metrics["cpu_p95"] < 40 and "mem_used_p95" in api1.metrics
    assert "mem_used_p95" not in api9.metrics  # sample omits memory for api-07..10
    assert api1.metrics["uptime_share"] == pytest.approx(1.0)
    assert any(a["monthly_savings"] for a in dataset.extras["advisor"])
    assert any("containerlog" in {t.lower() for t in v["tables"]} for v in dataset.extras["log_usage"].values())


# ---------------------------------------------------------------- rules

def test_waste_rules(findings):
    assert len(by_rule(findings, "az-disk-unattached")) == 28
    assert len(by_rule(findings, "az-pip-unassociated")) == 5
    assert names(by_rule(findings, "az-lb-no-backend")) == {"lb-old-partner", "agw-legacy-portal"}
    assert names(by_rule(findings, "az-plan-empty")) == {"asp-campaign-2025"}
    assert names(by_rule(findings, "az-vm-idle")) == {"vm-batch-01", "vm-batch-02", "vm-batch-03"}
    assert names(by_rule(findings, "az-vm-stopped-billing")) == {"vm-legacy-report"}
    assert names(by_rule(findings, "az-vm-deallocated")) == {"vm-legacy-01", "vm-legacy-02"}
    old = by_rule(findings, "az-snapshot-old")
    assert old and all(int(r.title.split()[2]) > 90 for r in old)


def test_rightsizing_respects_memory_and_confidence(findings):
    rs = {r.resource_name: r for r in by_rule(findings, "az-vm-rightsize")}
    assert not any(n.startswith("vm-sql") for n in rs)  # memory-bound
    assert not any(n.startswith("vm-web") for n in rs)  # CPU p95 too high
    assert rs["vm-api-01"].confidence == "high" and not rs["vm-api-01"].needs_validation
    assert rs["vm-api-09"].confidence == "low" and rs["vm-api-09"].needs_validation
    assert "Standard_D4s_v5" in rs["vm-api-01"].title
    assert rs["vm-api-01"].est_monthly_savings == pytest.approx(rs["vm-api-01"].current_monthly_cost / 2, rel=0.01)


def test_scheduling(findings):
    sched = names(by_rule(findings, "az-nonprod-schedule"))
    assert {f"vm-dev-{i:02d}" for i in range(1, 15)} <= sched
    assert {f"vm-erp-uat-{i:02d}" for i in range(1, 5)} <= sched  # classified by name token
    assert not any(n.startswith(("vm-web", "vm-api")) for n in sched)
    aks = by_rule(findings, "az-aks-nonprod-stop")
    assert names(aks) == {"aks-dev"} and aks[0].est_monthly_savings > 0
    plans = {r.resource_name: r for r in by_rule(findings, "az-plan-scale-in")}
    assert "2 instances" in plans["asp-prod-web"].title  # production keeps 2
    assert "to 1 instances" in plans["asp-dev"].title


def test_logging_and_commitments(findings):
    basic = names(by_rule(findings, "az-log-basic-plan"))
    assert basic == {"law-prod", "law-dev"}
    assert by_rule(findings, "az-log-containerlog-v2")
    assert by_rule(findings, "az-log-retention")
    # after moving ContainerLog/AppTraces off Analytics, law-prod falls below the commitment threshold
    assert not by_rule(findings, "az-log-commitment-tier")
    commit = by_rule(findings, "az-commitment")[0]
    assert any("reduced" in a for a in commit.assumptions)
    advisor_ri = [r for r in by_rule(findings, "az-advisor") if r.category == "commitment"][0]
    assert advisor_ri.scenario_group == commit.scenario_group == "__commitment"
    assert advisor_ri.est_monthly_savings <= max(commit.est_monthly_savings, advisor_ri.savings_high)


def test_owner_and_evidence_present(findings):
    for r in findings:
        if r.est_monthly_savings > 0:
            assert r.evidence, r.rule_id
            assert r.action
    unattached = by_rule(findings, "az-disk-unattached")[0]
    assert unattached.owner  # tag or rg:<name>


def test_scenario_total_counts_alternatives_once():
    a = Recommendation("r1", "a", "rightsizing", "x", resource_id="vm1", est_monthly_savings=100, confidence="high")
    b = Recommendation("r2", "b", "scheduling", "x", resource_id="vm1", est_monthly_savings=60, confidence="medium")
    c = Recommendation("r3", "c", "waste", "x", resource_id="disk1", est_monthly_savings=10, confidence="low")
    t = scenario_total([a, b, c])
    assert t["estimate"] == 110
    assert t["low"] == pytest.approx(90 + 0)
    assert t["high"] == pytest.approx(110)


# ---------------------------------------------------------------- pipeline, estate, report, register

def test_assess_outputs_and_estate(tmp_path, dataset):
    ds = AzureProvider().load(SAMPLE, Config())
    result = assess(ds, Config(), tmp_path, run_dir=SAMPLE, register_path=tmp_path / "register.db")
    for key in ("report", "findings_csv", "inventory_csv", "mindmap", "findings_json", "register"):
        assert result.outputs[key].exists()
    html = result.outputs["report"].read_text()
    assert "<script src" not in html and "<link" not in html and "http://" not in html  # self-contained
    for section in ("summary", "map", "services", "opportunities", "owners", "quality", "method"):
        assert f'id="{section}"' in html
    disks = next(s for a in result.tree.children for s in a.children if s.label == "Managed Disks" and s.count > 100)
    assert all(c.kind == "sku" for c in disks.children)
    assert any(n.kind == "rollup" for sku in disks.children for b in sku.children for n in (b.children or [b]))
    assert result.outputs["mindmap"].read_text().startswith("mindmap")
    assert any(q.area == "utilization" and q.level == "warn" for q in result.quality)
    la = next(p for p in result.profiles if p.service == "Log Analytics")
    assert la.findings > 0


def test_register_keeps_decisions_and_verifies(tmp_path):
    db = tmp_path / "reg.db"
    day0 = date(2026, 9, 1)
    rec = Recommendation("az-disk-unattached", "Unattached disk", "waste", "delete", resource_id="d1",
                         est_monthly_savings=30)
    before = [CostRecord(day0 + timedelta(days=i), 1.0, "Storage", resource_id="d1") for i in range(30)]
    reg = Register(db)
    reg.record_run("azure", "run1", ("2026-09-01", "2026-09-30"), 100, [rec], 30, before)
    reg.update(rec.key, status="implemented", implemented_on="2026-09-30", validated_savings=30)
    reg.close()

    after = [CostRecord(day0 + timedelta(days=30 + i), 0.0, "Storage", resource_id="d1") for i in range(1, 15)]
    reg = Register(db)
    reg.record_run("azure", "run2", ("2026-10-01", "2026-10-14"), 70, [], 0, after)  # finding gone
    row = reg.rows(active_only=False)[0]
    assert row["status"] == "implemented" and row["active"] == 0
    [res] = reg.verify()
    assert res["status"] == "verified" and res["realized_monthly"] == pytest.approx(30.0)
    assert reg.totals()["realized"] == pytest.approx(30.0)
    with pytest.raises(ValueError):
        reg.update(rec.key, status="done")
    reg.close()


def test_metrics_fall_back_to_one_metric_at_a_time(tmp_path):
    from datetime import datetime, timezone
    from cloudfinops.harness.runner import CliError

    class Runner:
        def __init__(self):
            self.calls = []

        def run(self, cmd, **kw):
            names = cmd[cmd.index("--metrics") + 1:cmd.index("--aggregation")]
            self.calls.append(names)
            if "Available Memory Bytes" in names:
                raise CliError(cmd, 1, "Failed to find metric configuration for provider")
            return {"interval": "PT1H", "value": [{"name": {"value": n}, "timeseries": []} for n in names]}

    r = Runner()
    now = datetime.now(timezone.utc)
    out = AzureProvider()._metrics(r, "/vm", ["Percentage CPU", "Available Memory Bytes"], now, now)
    assert [v["name"]["value"] for v in out["value"]] == ["Percentage CPU"]  # CPU kept despite memory failing
    assert r.calls == [["Percentage CPU", "Available Memory Bytes"], ["Percentage CPU"], ["Available Memory Bytes"]]


def test_preflight_requires_extensions(monkeypatch, tmp_path):
    class Runner:
        @staticmethod
        def available(_):
            return True

        def run(self, cmd, **kw):
            return {"azure-cli": "2.70.0", "extensions": {"resource-graph": "2.1.0"}}

    assert AzureProvider().preflight(Runner()) == ["missing-extension:log-analytics"]


# ---------------------------------------------------------------- container apps, cost pagination, runner

def test_container_app_usage_and_rules(dataset, findings):
    apps = {r.name: r for r in dataset.resources if r.type == "azure.container_app"}
    assert set(apps) == {"ca-orders-worker", "ca-legacy-pdf", "ca-poc-ocr", "ca-dev-portal"}
    worker = apps["ca-orders-worker"].metrics
    assert 20 < worker["cpu_avg"] < 40 and worker["cpu_max"] <= 100 and worker["replicas_p95"] >= 3
    assert apps["ca-legacy-pdf"].attributes["min_replicas"] == 1 and apps["ca-legacy-pdf"].sku == "1 vCPU / 2 GiB"
    by_rule = lambda rid: {r.resource_name for r in findings if r.rule_id == rid}  # noqa: E731
    assert by_rule("az-aca-idle") == {"ca-poc-ocr"}
    assert by_rule("az-aca-low-traffic") == {"ca-legacy-pdf"}
    assert "ca-dev-portal" in by_rule("az-aca-nonprod-min-replicas")
    assert not any(r.resource_name == "ca-orders-worker" for r in findings if r.rule_id.startswith("az-aca"))


def test_container_app_cpu_is_capped_at_allocation():
    from cloudfinops.models import Resource
    from cloudfinops.providers.azure import _container_app_usage
    res = Resource(id="a", type="azure.container_app", attributes={"vcpu": 0.5})
    series = {"UsageNanoCores": [{"timeStamp": "t1", "average": 2e8, "maximum": 9e9}],
              "Replicas": [{"timeStamp": "t1", "average": 1, "maximum": 1}],
              "Requests": [{"timeStamp": "t1", "total": 7}]}
    _container_app_usage(res, series, expected_points=1, cfg=Config())
    assert res.metrics["cpu_avg"] == pytest.approx(40.0) and res.metrics["cpu_max"] == 100.0
    assert res.metrics["requests_window"] == 7 and res.metrics["active_share"] == 1.0


def test_cost_query_follows_next_link_over_https():
    calls = []

    class Runner:
        def http_post(self, url, token, body):
            calls.append(url)
            if "skiptoken" in url:
                return {"properties": {"columns": [{"name": "Cost"}], "rows": [[2.0]], "nextLink": None}}
            return {"properties": {"columns": [{"name": "Cost"}], "rows": [[1.0]],
                                   "nextLink": url + "&$skiptoken=abc"}}

    out = AzureProvider()._cost_query(Runner(), "tok", "sub-1", date(2026, 9, 1), date(2026, 9, 30), ["ResourceId"])
    assert out["rows"] == [[1.0], [2.0]] and len(calls) == 2 and "&$skiptoken=abc" in calls[1]


def test_http_post_is_limited_to_query_apis(tmp_path):
    runner = CliRunner(tmp_path)
    with pytest.raises(NotReadOnlyError):
        runner.http_post("https://management.azure.com/subscriptions/s/resourceGroups/rg?api-version=1", "t", {})


def test_windows_batch_cmdline_quotes_special_characters():
    from cloudfinops.harness.runner import windows_batch_cmdline
    line = windows_batch_cmdline(["C:\\az.cmd", "rest", "--url", "https://x/q?a=1&$skiptoken=Z", "-q", "R | project id"])
    assert '"https://x/q?a=1&$skiptoken=Z"' in line and '"R | project id"' in line and line.startswith("C:\\az.cmd rest")


def test_retry_after_headers():
    from cloudfinops.harness.runner import _retry_after
    assert _retry_after({"x-ms-ratelimit-microsoft.costmanagement-entity-retry-after": "37"}) == 37.0
    assert _retry_after({"Retry-After": "5"}) == 5.0
    assert _retry_after({}) is None
