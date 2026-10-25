"""`cloudfinops check` classifies probes without signing in or changing anything."""

import pytest

from cloudfinops.config import Config
from cloudfinops.harness import check as check_mod
from cloudfinops.harness.runner import CliError, CliRunner, check_read_only

DENY = "An error occurred (AccessDeniedException) when calling the {op} operation: User: arn:aws:iam::1:user/x is not authorized to perform: {action}"


def fake_run(answers):
    def run(self, cmd, save_as=None, parse_json=True, interactive=False, **kw):
        check_read_only(cmd)  # every probe must be a read
        assert not interactive  # check never signs in
        key = " ".join(cmd[1:3])
        ans = answers.get(key, {})
        if isinstance(ans, Exception):
            raise ans
        return ans
    return run


def test_aws_check_reports_gaps_and_stays_ready(monkeypatch):
    monkeypatch.setattr(CliRunner, "available", staticmethod(lambda b: True))
    monkeypatch.setattr(CliRunner, "run", fake_run({
        "sts get-caller-identity": {"Arn": "arn:aws:iam::1:user/x", "Account": "1"},
        "configure get": "eu-west-1",
        "ce get-cost-and-usage": CliError(["aws"], 254, DENY.format(op="GetCostAndUsage", action="ce:GetCostAndUsage")),
        "rds describe-db-instances": CliError(["aws"], 254, DENY.format(op="DescribeDBInstances", action="rds:DescribeDBInstances")),
        "ec2 describe-regions": {"Regions": [{"RegionName": "eu-west-1"}]},
        "compute-optimizer get-enrollment-status": CliError(["aws"], 254, DENY.format(
            op="GetEnrollmentStatus", action="compute-optimizer:GetEnrollmentStatus")),
    }))
    report = check_mod.run_check("aws", Config())
    by = {p.name: p for p in report.probes}
    assert by["Cost Explorer (spend)"].status == "MISSING" and "ce:GetCostAndUsage" in by["Cost Explorer (spend)"].detail
    assert by["RDS"].status == "MISSING" and by["EC2 / EBS / VPC"].status == "OK"
    assert by["Compute Optimizer"].status == "WARN"  # optional
    assert report.ready  # gaps, but nothing critical
    text = report.render()
    assert "Ready, with gaps" in text and "AWSBillingReadOnlyAccess" in text


def test_not_signed_in_is_not_ready(monkeypatch):
    monkeypatch.setattr(CliRunner, "available", staticmethod(lambda b: True))
    monkeypatch.setattr(CliRunner, "run", fake_run({
        "sts get-caller-identity": CliError(["aws"], 255, "Unable to locate credentials. You can configure credentials by running \"aws configure\"."),
    }))
    report = check_mod.run_check("aws", Config())
    assert not report.ready
    assert "Not ready" in report.render() and "aws configure" in report.render()


def test_missing_cli(monkeypatch):
    monkeypatch.setattr(CliRunner, "available", staticmethod(lambda b: False))
    for provider in ("aws", "azure", "gcp"):
        report = check_mod.run_check(provider, Config())
        assert not report.ready and any(p.status == "MISSING" and p.critical for p in report.probes)


def test_cli_exit_code(monkeypatch, capsys):
    from cloudfinops.cli import main
    monkeypatch.setattr(CliRunner, "available", staticmethod(lambda b: False))
    assert main(["check", "--provider", "gcp"]) == 1
    assert "Not ready" in capsys.readouterr().out
