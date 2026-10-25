"""GCP Cloud SQL, Cloud NAT, load balancer, VPN, managed instance group and security-spend rules."""

import json
from datetime import date, timedelta

import pytest

from cloudfinops.analysis import enrich_resource_costs
from cloudfinops.config import Config
from cloudfinops.models import CostRecord
from cloudfinops.providers.gcp import GcpProvider
from cloudfinops.rules import run_rules

P = "acme-dev-data"


def series(labels, values, metric_labels=None):
    return {"resource": {"labels": labels}, "metric": {"labels": metric_labels or {}},
            "points": [{"value": {"doubleValue": v}} for v in values]}


def write(root, rel, data):
    path = root / "raw" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    root = tmp_path_factory.mktemp("gcprun")
    (root / "scope.json").write_text(json.dumps({"provider": "gcp", "subscriptions": [{"id": P, "name": P, "billing": True}]}))
    write(root, f"services/{P}.json", [{"config": {"name": n}} for n in
                                        ("sqladmin.googleapis.com", "compute.googleapis.com", "monitoring.googleapis.com")])
    sql = lambda name, tier, ha, version: {"name": name, "region": "us-central1", "state": "RUNNABLE",  # noqa: E731
                                          "databaseVersion": version, "settings": {
                                              "tier": tier, "availabilityType": ha, "activationPolicy": "ALWAYS",
                                              "edition": "ENTERPRISE", "userLabels": {"environment": "dev"}}}
    write(root, f"inventory/{P}_sql_instances.json", [
        sql("orders-dev", "db-custom-4-15360", "REGIONAL", "POSTGRES_16"),
        sql("old-reports", "db-custom-2-7680", "ZONAL", "POSTGRES_11")])
    write(root, f"inventory/{P}_compute_routers.json", [{"name": "rt-1", "id": "77", "region": "x/regions/us-central1",
                                                         "nats": [{"name": "nat-1"}]}])
    base = "https://www.googleapis.com/compute/v1/projects/" + P
    write(root, f"inventory/{P}_forwarding_rules.json", [
        {"name": "fr-old-site", "selfLink": f"{base}/global/forwardingRules/fr-old-site",
         "target": f"{base}/global/targetHttpsProxies/px", "loadBalancingScheme": "EXTERNAL_MANAGED"},
        {"name": "fr-live", "selfLink": f"{base}/global/forwardingRules/fr-live",
         "target": f"{base}/global/targetHttpsProxies/px2", "loadBalancingScheme": "EXTERNAL_MANAGED"}])
    write(root, f"inventory/{P}_vpn_tunnels.json", [
        {"name": "tun-down", "selfLink": f"{base}/regions/us-central1/vpnTunnels/tun-down", "region": "x/regions/us-central1",
         "status": "NO_INCOMING_PACKETS", "detailedStatus": "No incoming packets from peer"},
        {"name": "tun-up", "selfLink": f"{base}/regions/us-central1/vpnTunnels/tun-up", "region": "x/regions/us-central1",
         "status": "ESTABLISHED"}])
    write(root, f"inventory/{P}_migs.json", [{"name": "mig-dev-web", "selfLink": f"{base}/zones/us-central1-a/instanceGroupManagers/mig-dev-web",
                                             "zone": "x/zones/us-central1-a", "targetSize": 2, "instanceTemplate": "x/tpl-web"}])
    write(root, f"inventory/{P}_compute_instances.json", [
        {"name": f"mig-dev-web-{i}", "id": str(100 + i), "selfLink": f"{base}/zones/us-central1-a/instances/mig-dev-web-{i}",
         "zone": "x/zones/us-central1-a", "machineType": "x/machineTypes/e2-standard-4", "status": "RUNNING",
         "metadata": {"items": [{"key": "created-by", "value": f"projects/1/zones/us-central1-a/instanceGroupManagers/mig-dev-web"}]}}
        for i in (1, 2)])
    db = lambda n: {"database_id": f"{P}:{n}"}  # noqa: E731
    metric = lambda name, ts: write(root, f"metrics/{P}_{name}.json", {"project": P, "metric": name, "days": 14, "timeSeries": ts})  # noqa: E731
    metric("sql_cpu", [series(db("orders-dev"), [0.05, 0.08, 0.10] * 112), series(db("old-reports"), [0.01] * 336)])
    metric("sql_mem", [series(db("orders-dev"), [0.30] * 336)])
    metric("sql_conn_pg", [series(db("orders-dev"), [12, 15], {"database": "orders"}),
                           series(db("orders-dev"), [1], {"database": "cloudsqladmin"}),
                           series(db("old-reports"), [1, 1], {"database": "cloudsqladmin"})])
    metric("nat_bytes", [series({"gateway_name": "nat-1", "region": "us-central1", "router_id": "77"}, [30e9] * 30)])
    metric("lb_requests", [series({"forwarding_rule_name": "fr-live"}, [500] * 30)])
    metric("vm_cpu_mean", [series({"instance_id": str(100 + i)}, [0.3] * 336) for i in (1, 2)])
    metric("vm_cpu_max", [series({"instance_id": str(100 + i)}, [0.6] * 336) for i in (1, 2)])
    return root


@pytest.fixture(scope="module")
def recs(run_dir):
    cfg = Config()
    ds = GcpProvider().load(run_dir, cfg)
    ds.records = [CostRecord(date(2026, 10, 1) + timedelta(days=i), 20.0, "Security Command Center", account="acme-dev-sec",
                             provider="gcp") for i in range(14)]
    enrich_resource_costs(ds.resources, ds.records, cfg)
    return ds, run_rules(ds.records, ds.resources, cfg, provider="gcp", extras=ds.extras, relationships=ds.relationships)


def names(recs, rule):
    return {r.resource_name for r in recs if r.rule_id == rule}


def test_cloud_sql(recs):
    ds, out = recs
    orders = next(r for r in ds.resources if r.name == "orders-dev")
    assert orders.metrics["connections_max"] == 15  # admin connections excluded
    assert orders.monthly_cost and orders.cost_source == "estimate"
    assert names(out, "gcp-sql-idle") == {"old-reports"}  # only admin connections
    assert names(out, "gcp-sql-rightsize") == {"orders-dev"}
    assert "db-custom-2-7680" in next(r.title for r in out if r.rule_id == "gcp-sql-rightsize")
    assert names(out, "gcp-sql-nonprod-ha") == {"orders-dev"}
    assert "orders-dev" in names(out, "gcp-sql-nonprod-stop")
    assert names(out, "gcp-sql-extended-support") == {"old-reports"}


def test_network_mig_and_security(recs):
    ds, out = recs
    assert names(out, "gcp-nat-heavy") == {"nat-1"}  # 900 GB/month
    assert names(out, "gcp-lb-idle") == {"fr-old-site"}  # fr-live has requests
    assert names(out, "gcp-vpn-down") == {"tun-down"}
    assert names(out, "gcp-mig-nonprod-schedule") == {"mig-dev-web"}
    assert not names(out, "gcp-nonprod-schedule")  # MIG members are scheduled at group level
    sec = [r for r in out if r.rule_id == "gcp-security-nonprod"]
    assert len(sec) == 1 and sec[0].savings_high > 0
