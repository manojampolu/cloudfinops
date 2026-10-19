import pytest

from cloudfinops.harness.runner import NotReadOnlyError, check_read_only
from cloudfinops.models import Resource
from cloudfinops.pricing_gcp import machine_downsize, machine_memory_gb, parse_cpu, parse_mem_gib, run_idle_monthly
from cloudfinops.providers.gcp import _from_self_link, _region_of


def test_gcp_read_only_guard():
    check_read_only(["gcloud", "asset", "search-all-resources", "--scope=projects/p"])
    check_read_only(["gcloud", "recommender", "recommendations", "list", "--project", "p"])
    check_read_only(["gcloud", "config", "get", "core/project"])
    check_read_only(["bq", "query", "--nouse_legacy_sql", "SELECT day FROM `p.d.t` GROUP BY 1"])
    for bad in (["gcloud", "services", "enable", "compute.googleapis.com"],
                ["gcloud", "run", "services", "delete", "x"],
                ["bq", "query", "--nouse_legacy_sql", "DELETE FROM `p.d.t` WHERE true"],
                ["bq", "query", "SELECT 1; DROP TABLE t"],
                ["bq", "rm", "-f", "p:d.t"]):
        with pytest.raises(NotReadOnlyError):
            check_read_only(bad)


def test_gcp_helpers():
    assert _from_self_link("https://www.googleapis.com/compute/v1/projects/P/zones/us-central1-a/disks/D") == \
        "//compute.googleapis.com/projects/p/zones/us-central1-a/disks/d"
    assert _region_of("us-central1-a") == "us-central1"
    assert _region_of("us-central1") == "us-central1"
    assert machine_downsize("n2-standard-8") == "n2-standard-4"
    assert machine_downsize("e2-medium") == "e2-small"
    assert machine_memory_gb("e2-standard-4") == 16
    assert parse_cpu("1000m") == 1.0 and parse_cpu("2") == 2.0
    assert parse_mem_gib("2Gi") == 2.0 and parse_mem_gib("512Mi") == 0.5
    assert run_idle_monthly(1, 0.5, 1) == pytest.approx(730 * 3600 * 0.00000375)


def test_gcp_owner_fallback_is_project():
    from cloudfinops.config import Config
    from cloudfinops.rules import owner_of
    res = Resource(id="x", type="gcp.run_service", provider="gcp", account="proj-1", group="us-central1")
    assert owner_of(res, Config()) == "project:proj-1"


class FakeRunner:
    """Answers gcloud calls in-process; project 'locked' denies everything."""

    def __init__(self, tmp_path):
        from pathlib import Path
        self.run_dir = Path(tmp_path)
        self.raw_dir = self.run_dir / "raw"
        self.raw_dir.mkdir(parents=True)
        self.calls = []

    def run(self, cmd, save_as=None, parse_json=True, interactive=False):
        from cloudfinops.harness.runner import CliError, check_read_only
        check_read_only(cmd)
        self.calls.append(cmd)
        if "locked" in " ".join(cmd):
            raise CliError(cmd, 1, "ERROR: (gcloud.services.list) [me@x.com] does not have permission to access "
                                   "projects instance [locked] (or it may not exist)")
        if cmd[:3] == ["gcloud", "services", "list"]:
            data = [{"config": {"name": "run.googleapis.com", "title": "Cloud Run"}}]
        elif cmd[:3] == ["gcloud", "config", "get"]:
            return "open"
        elif cmd[:3] == ["gcloud", "auth", "print-access-token"]:
            return "token"
        else:
            data = []
        if save_as:
            self.save_json(save_as, data)
        return data

    def http_get(self, url, token, headers=None, save_as=None):
        from cloudfinops.harness.runner import CliError
        if "/projects/locked/" in url:
            raise CliError(["GET", url], 403, '{"error": {"code": 403, "message": "Permission denied"}}')
        return {"timeSeries": []}

    def save_json(self, rel, data):
        import json
        path = self.raw_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path


def test_unreadable_projects_are_skipped_once(tmp_path):
    from cloudfinops.config import Config
    from cloudfinops.providers.gcp import GcpProvider
    runner = FakeRunner(tmp_path)
    scopes = [{"id": "open", "name": "open", "billing": True}, {"id": "locked", "name": "locked", "billing": False}]
    manifest = GcpProvider().collect(runner, scopes, Config(), log=lambda *_: None)
    assert manifest["no_access"] == ["locked"]
    assert [s["id"] for s in manifest["subscriptions"]] == ["open"]
    assert sum(1 for c in runner.calls if "locked" in " ".join(c)) == 1  # only the access probe
    locked_errors = [e for e in manifest["errors"] if "locked" in e["scope"]]
    assert [e["step"] for e in locked_errors] == ["access"]  # one entry, not one per step
    assert all("\n" not in e["message"] for e in manifest["errors"])


def test_missing_config_message(tmp_path):
    from cloudfinops.config import Config
    with pytest.raises(FileNotFoundError, match="cp cloudfinops.example.toml"):
        Config.load(tmp_path / "nope.toml")


def test_short_error():
    from cloudfinops.providers.gcp import short_error
    assert short_error('failed (403): {\n "error": {\n "code": 403,\n "message": "Permission denied (or the resource may not exist)."') \
        == "Permission denied (or the resource may not exist)."
    assert short_error("`gcloud x` failed (1): ERROR: (gcloud.x) [a@b] does not have\npermission").startswith("[a@b] does not have permission")
