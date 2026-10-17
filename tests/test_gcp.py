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
