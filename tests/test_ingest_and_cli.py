import json
from datetime import date

from cloudfinops.cli import main
from cloudfinops.ingest import load_billing_csv


def test_load_focus_csv(tmp_path):
    p = tmp_path / "focus.csv"
    p.write_text(
        "ChargePeriodStart,ServiceName,BilledCost,SubAccountId,RegionId,ResourceId,PricingCategory,Tags\n"
        '2026-10-01T00:00:00Z,Amazon EC2,12.5,123,us-east-1,i-1,On-Demand,"{""Owner"": ""team-a""}"\n'
    )
    [r] = load_billing_csv(p)
    assert r.day == date(2026, 10, 1)
    assert r.cost == 12.5 and r.resource_id == "i-1" and r.pricing_category == "On-Demand"
    assert r.tags == {"owner": "team-a"}


def test_load_cur_csv_with_tag_columns(tmp_path):
    p = tmp_path / "cur.csv"
    p.write_text(
        "lineItem/UsageStartDate,product/ProductName,lineItem/UnblendedCost,resourceTags/user:Environment\n"
        "2026-10-01T00:00:00Z,Amazon S3,3.0,prod\n"
    )
    [r] = load_billing_csv(p)
    assert r.service == "Amazon S3" and r.tags == {"environment": "prod"}


def test_missing_columns_is_clear_error(tmp_path, capsys):
    p = tmp_path / "bad.csv"
    p.write_text("foo,bar\n1,2\n")
    assert main(["analyze", "--billing", str(p)]) == 2
    assert "missing required column" in capsys.readouterr().err


def test_demo_json(tmp_path):
    out = tmp_path / "r.json"
    assert main(["demo", "--format", "json", "-o", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["summary"]["est_monthly_savings"] > 0
    rule_ids = {r["rule_id"] for r in data["recommendations"]}
    assert {"ebs-unattached", "ec2-idle", "rds-idle", "savings-plan", "data-transfer"} <= rule_ids
    assert data["anomalies"]


def test_demo_markdown(capsys):
    assert main(["demo", "--format", "markdown", "--top", "5"]) == 0
    assert "| # | Sev |" in capsys.readouterr().out


def test_assess_from_uses_run_folder_provider(tmp_path):
    import json
    import shutil
    from importlib import resources as pkg_resources
    src = pkg_resources.files("cloudfinops") / "samples" / "azure"
    run = tmp_path / "run"
    shutil.copytree(str(src), run)
    scope = json.loads((run / "scope.json").read_text())
    assert scope["provider"] == "azure"
    assert main(["assess", "--from", str(run), "--register", str(tmp_path / "r.db")]) == 0
    assert json.loads((run / "findings.json").read_text())["provider"] == "azure"
