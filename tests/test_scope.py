from importlib import resources as pkg_resources
from pathlib import Path

import pytest

from cloudfinops.assessment import assess
from cloudfinops.cli import main
from cloudfinops.config import Config
from cloudfinops.providers.azure import AzureProvider
from cloudfinops.register import Register
from cloudfinops.scope import filter_resource_groups

SAMPLE = Path(str(pkg_resources.files("cloudfinops") / "samples" / "azure"))


def load():
    return AzureProvider().load(SAMPLE, Config())


def test_filter_keeps_only_named_groups_case_insensitive():
    ds = filter_resource_groups(load(), ["RG-DEV-APP"])
    assert ds.resources and {r.group.lower() for r in ds.resources} == {"rg-dev-app"}
    kept = {r.id for r in ds.resources}
    assert all(c.resource_id in kept or "/resourcegroups/rg-dev-app/" in c.resource_id for c in ds.records)
    assert all(rel.src in kept and rel.dst in kept for rel in ds.relationships)
    assert ds.extras["pricing_model_daily"] == []  # subscription-level data dropped
    assert ds.scope["resource_groups"] == ["rg-dev-app"]


def test_aks_node_resource_group_follows_cluster():
    ds = filter_resource_groups(load(), ["rg-aks"])
    groups = {r.group.lower() for r in ds.resources}
    assert "mc_rg-aks_aks-prod_eastus" in groups and any(r.type == "azure.vmss" for r in ds.resources)


def test_unknown_group_lists_available():
    with pytest.raises(ValueError, match="Available: .*rg-dev-app"):
        filter_resource_groups(load(), ["does-not-exist"])


def test_scoped_run_does_not_retire_other_findings(tmp_path):
    db = tmp_path / "reg.db"
    assess(load(), Config(), tmp_path / "full", register_path=db)
    before = {r["key"] for r in Register(db).rows()}
    assess(filter_resource_groups(load(), ["rg-dev-app"]), Config(), tmp_path / "dev", register_path=db)
    after = {r["key"] for r in Register(db).rows()}
    assert before == after


def test_cli_resource_groups_writes_subfolder(tmp_path, capsys):
    import shutil
    run = tmp_path / "run"
    shutil.copytree(SAMPLE, run)
    assert main(["assess", "--from", str(run), "--resource-groups", "rg-test", "rg-dev-app",
                 "--register", str(tmp_path / "r.db")]) == 0
    out = run / "rg-rg-dev-app-rg-test"
    assert (out / "report.html").exists()
    assert "resource groups: rg-dev-app, rg-test" in (out / "report.html").read_text()
