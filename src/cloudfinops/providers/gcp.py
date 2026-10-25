"""GCP adapter: read-only collection through gcloud / bq, and offline normalization.

Data sources
  scope       gcloud projects list, gcloud billing projects list          -> billing-linked projects
  APIs        gcloud services list --enabled                              -> raw/services/<project>.json
  inventory   gcloud asset search-all-resources (if Cloud Asset API usable) + per-service lists
              (compute, run, functions, storage, artifacts, logging, sql, container) -> raw/inventory/
  costs       BigQuery billing export via `bq query` (SELECT only)        -> raw/costs/billing_export.json
  usage       Cloud Monitoring API timeSeries (HTTPS GET)                 -> raw/metrics/<project>_<name>.json
  native recs gcloud recommender recommendations list                     -> raw/recommender/*.json

Prompts are disabled for every gcloud call, so the tool can never enable an API or change a setting.
Required roles per project: Viewer (or Browser + Monitoring Viewer + Cloud Asset Viewer + Recommender Viewer),
plus BigQuery Data Viewer / Job User on the billing export dataset.
"""

from __future__ import annotations

import json
import re
import statistics
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..config import Config
from ..harness.errors import is_permission_error, short_error  # noqa: F401  (re-exported)
from ..harness.runner import CliError, CliRunner
from ..models import CostRecord, Dataset, Relationship, Resource
from ..pricing_gcp import FX_TO_USD, machine_memory_gb, machine_type_short, parse_cpu, parse_mem_gib

MONITORING = "https://monitoring.googleapis.com/v3"
EXPORT_RE = re.compile(r"^gcp_billing_export_(resource_)?v1_[0-9A-F_]+$", re.I)

DETAIL_LISTS = [  # (required API, raw name, command)
    ("compute.googleapis.com", "compute_instances", ["gcloud", "compute", "instances", "list"]),
    ("compute.googleapis.com", "compute_disks", ["gcloud", "compute", "disks", "list"]),
    ("compute.googleapis.com", "compute_addresses", ["gcloud", "compute", "addresses", "list"]),
    ("compute.googleapis.com", "compute_snapshots", ["gcloud", "compute", "snapshots", "list"]),
    ("run.googleapis.com", "run_services", ["gcloud", "run", "services", "list"]),
    ("cloudfunctions.googleapis.com", "functions", ["gcloud", "functions", "list"]),
    ("storage-api.googleapis.com", "buckets", ["gcloud", "storage", "buckets", "list"]),
    ("artifactregistry.googleapis.com", "artifact_repos", ["gcloud", "artifacts", "repositories", "list"]),
    ("logging.googleapis.com", "log_buckets", ["gcloud", "logging", "buckets", "list"]),
    ("sqladmin.googleapis.com", "sql_instances", ["gcloud", "sql", "instances", "list"]),
    ("container.googleapis.com", "gke_clusters", ["gcloud", "container", "clusters", "list"]),
    ("compute.googleapis.com", "compute_routers", ["gcloud", "compute", "routers", "list"]),
    ("compute.googleapis.com", "forwarding_rules", ["gcloud", "compute", "forwarding-rules", "list"]),
    ("compute.googleapis.com", "vpn_tunnels", ["gcloud", "compute", "vpn-tunnels", "list"]),
    ("compute.googleapis.com", "migs", ["gcloud", "compute", "instance-groups", "managed", "list"]),
]
STORAGE_APIS = {"storage-api.googleapis.com", "storage.googleapis.com", "storage-component.googleapis.com"}

METRICS = {  # name: (filter, days, alignment seconds, aligner, reducer, group-by, required API or None)
    "api_requests": ('metric.type="serviceruntime.googleapis.com/api/request_count" AND resource.type="consumed_api"',
                     30, 86400, "ALIGN_DELTA", "REDUCE_SUM", ["resource.label.service", "resource.label.method"], None),
    "vm_cpu_mean": ('metric.type="compute.googleapis.com/instance/cpu/utilization"', 14, 3600, "ALIGN_MEAN", None, [],
                    "compute.googleapis.com"),
    "vm_cpu_max": ('metric.type="compute.googleapis.com/instance/cpu/utilization"', 14, 3600, "ALIGN_MAX", None, [],
                   "compute.googleapis.com"),
    "vm_mem": ('metric.type="agent.googleapis.com/memory/percent_used" AND metric.label.state="used"', 14, 3600,
               "ALIGN_MEAN", None, [], "compute.googleapis.com"),
    "run_requests": ('metric.type="run.googleapis.com/request_count"', 30, 86400, "ALIGN_DELTA", "REDUCE_SUM",
                     ["resource.label.service_name", "resource.label.location"], "run.googleapis.com"),
    "run_instances": ('metric.type="run.googleapis.com/container/instance_count"', 14, 3600, "ALIGN_MEAN", "REDUCE_SUM",
                      ["resource.label.service_name", "resource.label.location"], "run.googleapis.com"),
    "function_execs": ('metric.type="cloudfunctions.googleapis.com/function/execution_count"', 30, 86400, "ALIGN_DELTA",
                       "REDUCE_SUM", ["resource.label.function_name", "resource.label.region"],
                       "cloudfunctions.googleapis.com"),
    "gcs_bytes": ('metric.type="storage.googleapis.com/storage/total_bytes"', 3, 86400, "ALIGN_MEAN", "REDUCE_SUM",
                  ["resource.label.bucket_name", "metric.label.storage_class"], "storage"),
    "log_ingest": ('metric.type="logging.googleapis.com/billing/bytes_ingested"', 30, 86400, "ALIGN_DELTA",
                   "REDUCE_SUM", ["metric.label.resource_type"], "logging.googleapis.com"),
    "sql_cpu": ('metric.type="cloudsql.googleapis.com/database/cpu/utilization"', 14, 3600, "ALIGN_MEAN", None, [],
                "sqladmin.googleapis.com"),
    "sql_mem": ('metric.type="cloudsql.googleapis.com/database/memory/utilization"', 14, 3600, "ALIGN_MEAN", None, [],
                "sqladmin.googleapis.com"),
    "sql_conn_mysql": ('metric.type="cloudsql.googleapis.com/database/network/connections"', 14, 86400, "ALIGN_MAX",
                       None, [], "sqladmin.googleapis.com"),
    "sql_conn_pg": ('metric.type="cloudsql.googleapis.com/database/postgresql/num_backends"', 14, 86400, "ALIGN_MAX",
                    None, [], "sqladmin.googleapis.com"),
    "nat_bytes": ('metric.type="router.googleapis.com/nat/sent_bytes_count"', 30, 86400, "ALIGN_DELTA", "REDUCE_SUM",
                  ["resource.label.gateway_name", "resource.label.region", "resource.label.router_id"],
                  "compute.googleapis.com"),
    "lb_requests": ('metric.type="loadbalancing.googleapis.com/https/request_count"', 30, 86400, "ALIGN_DELTA",
                    "REDUCE_SUM", ["resource.label.forwarding_rule_name"], "compute.googleapis.com"),
}
SQL_ADMIN_DATABASES = {"cloudsqladmin", "template0", "template1"}

ASSET_SERVICE = {
    "compute.googleapis.com": "Compute Engine", "run.googleapis.com": "Cloud Run",
    "cloudfunctions.googleapis.com": "Cloud Functions", "storage.googleapis.com": "Cloud Storage",
    "artifactregistry.googleapis.com": "Artifact Registry", "logging.googleapis.com": "Cloud Logging",
    "sqladmin.googleapis.com": "Cloud SQL", "container.googleapis.com": "GKE", "pubsub.googleapis.com": "Pub/Sub",
    "iam.googleapis.com": "IAM", "apikeys.googleapis.com": "API Keys", "cloudscheduler.googleapis.com": "Cloud Scheduler",
    "cloudtasks.googleapis.com": "Cloud Tasks", "cloudbuild.googleapis.com": "Cloud Build",
    "secretmanager.googleapis.com": "Secret Manager", "bigquery.googleapis.com": "BigQuery",
    "firestore.googleapis.com": "Firestore", "cloudresourcemanager.googleapis.com": "Resource Manager",
    "monitoring.googleapis.com": "Cloud Monitoring", "aiplatform.googleapis.com": "Vertex AI",
    "containerregistry.googleapis.com": "Container Registry", "cloudbilling.googleapis.com": "Cloud Billing",
}
# Generic assets whose kind is the main thing in that service keep the plain service name
PRIMARY_KINDS = {"Service", "Bucket", "Function", "Repository", "Instance", "Disk", "Cluster", "Topic", "Queue",
                 "Job", "Key", "ServiceAccount", "LogBucket", "Secret", "Dataset"}
SKIP_ASSET_TYPES = {"serviceusage.googleapis.com/Service", "cloudresourcemanager.googleapis.com/Project",
                    "compute.googleapis.com/Project"}
TYPE_SERVICE = {
    "gcp.vm": "Compute Engine VMs", "gcp.disk": "Persistent Disks", "gcp.address": "Static IP Addresses",
    "gcp.snapshot": "Disk Snapshots", "gcp.run_service": "Cloud Run", "gcp.function": "Cloud Functions",
    "gcp.bucket": "Cloud Storage", "gcp.artifact_repo": "Artifact Registry", "gcp.log_bucket": "Cloud Logging",
    "gcp.sql": "Cloud SQL", "gcp.gke": "GKE", "gcp.api": "APIs & services",
    "gcp.cloud_nat": "Cloud NAT", "gcp.forwarding_rule": "Load Balancer Forwarding Rules",
    "gcp.vpn_tunnel": "Cloud VPN Tunnels", "gcp.mig": "Managed Instance Groups",
}
FREE_TYPES = {"gcp.api", "gcp.log_bucket", "gcp.function", "gcp.mig", "gcp.cloud_nat"}


def _short(url: str) -> str:
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


def _from_self_link(link: str) -> str:
    """https://www.googleapis.com/compute/v1/projects/p/zones/z/disks/d -> //compute.googleapis.com/projects/p/zones/z/disks/d"""
    m = re.match(r"https://(?:www|compute)\.googleapis\.com/compute/(?:v1|beta|alpha)/(.*)", link or "")
    return f"//compute.googleapis.com/{m.group(1)}".lower() if m else (link or "").lower()


def _region_of(location: str) -> str:
    loc = _short(location)
    return loc[:-2] if re.match(r".*-[a-z]\d?-[a-z]$", loc) or re.match(r"^[a-z]+-[a-z]+\d+-[a-z]$", loc) else loc


def _ts(value) -> str | None:
    return str(value)[:10] if value else None


def _point_value(p: dict) -> float:
    v = p.get("value", {})
    for k in ("doubleValue", "int64Value"):
        if k in v:
            return float(v[k])
    if "distributionValue" in v:
        return float(v["distributionValue"].get("mean", 0))
    return 0.0


def _p95(values: list[float]) -> float:
    vals = sorted(values)
    return vals[min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))] if vals else 0.0


class GcpProvider:
    name = "gcp"
    cli = "gcloud"
    scope_label = "projects"

    # ------------------------------------------------------------------ session

    def preflight(self, runner: CliRunner) -> list[str]:
        if not runner.available("gcloud"):
            return ["Google Cloud CLI (gcloud) not found. Install: https://cloud.google.com/sdk/docs/install "
                    "(macOS: brew install --cask google-cloud-sdk)"]
        return []

    def ensure_login(self, runner: CliRunner, device_code: bool = False, tenant: str | None = None) -> dict:
        def who():
            accts = runner.run(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=json"]) or []
            runner.run(["gcloud", "auth", "print-access-token"], parse_json=False)  # fails if re-auth needed
            return accts
        try:
            accts = who()
            if not accts:
                raise CliError(["gcloud", "auth", "list"], 1, "no active account")
        except CliError:
            runner.run(["gcloud", "auth", "login"] + (["--no-launch-browser"] if device_code else []), interactive=True)
            accts = who()
        return {"user": {"name": accts[0]["account"] if accts else "?"}, "tenantId": "gcp"}

    def list_scopes(self, runner: CliRunner) -> list[dict]:
        projects = runner.run(["gcloud", "projects", "list", "--format=json"]) or []
        billed: dict[str, str] = {}
        try:
            for acct in runner.run(["gcloud", "billing", "accounts", "list", "--format=json"]) or []:
                if not acct.get("open"):
                    continue
                acct_id = _short(acct["name"])
                for bp in runner.run(["gcloud", "billing", "projects", "list", f"--billing-account={acct_id}",
                                      "--format=json"]) or []:
                    if bp.get("billingEnabled"):
                        billed[bp["projectId"]] = f"{acct_id} ({acct.get('currencyCode', '')})"
        except CliError:
            pass  # no billing-account visibility; projects are still listed
        return [{"id": p["projectId"], "name": p["projectId"], "display_name": p.get("name", ""),
                 "number": p.get("projectNumber", ""), "billing": p["projectId"] in billed,
                 "billing_account": billed.get(p["projectId"], "")}
                for p in projects if p.get("lifecycleState") == "ACTIVE"]

    def default_scopes(self, scopes: list[dict]) -> list[dict]:
        billed = [s for s in scopes if s.get("billing")]
        return billed or scopes

    def describe_scope(self, s: dict) -> str:
        tag = "billing linked" if s.get("billing") else "no billing linked"
        return f"{s['id']}  ({s.get('display_name', '')}; {tag})"

    # ------------------------------------------------------------------ collection

    def collect(self, runner: CliRunner, scopes: list[dict], cfg: Config, log=print) -> dict:
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=cfg.collection_days - 1)
        projects = [s["id"] for s in scopes]
        manifest = {"provider": "gcp", "subscriptions": scopes, "period": {"start": str(start), "end": str(end)},
                    "metric_days": cfg.metric_days, "errors": [],
                    "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

        def guard(step, scope, fn):
            try:
                return fn()
            except Exception as exc:
                msg = str(exc)
                log(f"  ! {step} ({scope}): {short_error(msg)}")
                manifest["errors"].append({"step": step, "scope": scope, "message": short_error(msg),
                                           "permission": is_permission_error(msg)})
                return None

        log("Listing enabled APIs per project...")
        services: dict[str, set[str]] = {}
        no_access: list[str] = []
        for p in projects:
            try:
                data = runner.run(["gcloud", "services", "list", "--enabled", "--project", p, "--format=json"],
                                  save_as=f"services/{p}.json")
                services[p] = {s["config"]["name"] for s in data or []}
            except CliError as exc:
                if is_permission_error(str(exc)):
                    no_access.append(p)  # can list the project but not read it: skip every other step
                else:
                    services[p] = set()
                    manifest["errors"].append({"step": "services", "scope": p, "message": short_error(str(exc))})
        if no_access:
            log(f"  skipping {len(no_access)} project(s) this account cannot read: {', '.join(no_access)}")
            manifest["no_access"] = no_access
            manifest["errors"].append({"step": "access", "scope": ", ".join(no_access), "permission": True,
                                       "message": f"{len(no_access)} project(s) visible but not readable, not assessed "
                                                  "(grant roles/viewer to include them)"})
            projects = [p for p in projects if p not in no_access]
            manifest["subscriptions"] = [s for s in scopes if s["id"] not in no_access]
        cfg_project = guard("config", "core/project", lambda: runner.run(
            ["gcloud", "config", "get", "core/project"], parse_json=False).strip()) or ""
        if cfg_project and cfg_project not in services:
            data = guard("services", cfg_project, lambda: runner.run(
                ["gcloud", "services", "list", "--enabled", "--project", cfg_project, "--format=json"]))
            services[cfg_project] = {s["config"]["name"] for s in data or []}

        def quota_project(api: str) -> str | None:
            for p in [*projects, cfg_project]:
                if p and api in services.get(p, set()):
                    return p
            return None

        qp_asset, qp_rec, qp_mon = (quota_project("cloudasset.googleapis.com"), quota_project("recommender.googleapis.com"),
                                    quota_project("monitoring.googleapis.com"))
        manifest["quota_projects"] = {"cloudasset": qp_asset, "recommender": qp_rec, "monitoring": qp_mon}

        log("Collecting inventory...")
        for p in projects:
            if qp_asset:
                guard("inventory", f"{p}:assets", lambda: runner.run(
                    ["gcloud", "asset", "search-all-resources", f"--scope=projects/{p}", f"--billing-project={qp_asset}",
                     "--format=json"], save_as=f"inventory/{p}_assets.json"))
            for api, name, cmd in DETAIL_LISTS:
                enabled = services.get(p, set())
                if api in enabled or (api in STORAGE_APIS and enabled & STORAGE_APIS):
                    guard("inventory", f"{p}:{name}", lambda: runner.run(
                        cmd + ["--project", p, "--format=json"], save_as=f"inventory/{p}_{name}.json"))
        if not qp_asset:
            manifest["errors"].append({"step": "inventory", "scope": "cloudasset",
                                       "message": "Cloud Asset API not enabled in any selected project; "
                                                  "inventory limited to per-service lists of enabled APIs"})

        log("Looking for a BigQuery billing export...")
        table = cfg.gcp_billing_table or guard("costs", "discover", lambda: self._find_export(runner, projects, services))
        manifest["billing_export"] = table
        if table:
            log(f"  using {table}")
            guard("costs", table, lambda: self._query_export(runner, table, projects, cfg.collection_days))
        else:
            manifest["billing_note"] = ("No BigQuery billing export found. GCP exposes costs only through the billing "
                                        "export; enable 'Detailed usage cost' export in Billing > Billing export "
                                        "(data starts from the day it is enabled), or pass --billing-table.")
            log("  none found - costs will be list-price estimates only")

        log("Collecting usage metrics (Cloud Monitoring)...")
        token = guard("metrics", "token", lambda: runner.run(["gcloud", "auth", "print-access-token"],
                                                              parse_json=False).strip())
        if token:
            for p in projects:
                enabled = services.get(p, set())
                for name, (flt, days, period, aligner, reducer, group, needs) in METRICS.items():
                    if needs == "storage":
                        if not enabled & STORAGE_APIS:
                            continue
                    elif needs and needs not in enabled:
                        continue
                    guard("metrics", f"{p}:{name}", lambda: self._timeseries(
                        runner, token, p, name, flt, days, period, aligner, reducer, group, qp_mon))

        if qp_rec:
            log("Collecting Google Recommender findings...")
            for p in projects:
                for loc, recommender in self._recommender_targets(runner.raw_dir, p):
                    data = guard("recommender", f"{p}:{loc}:{recommender}", lambda: runner.run(
                        ["gcloud", "recommender", "recommendations", "list", "--project", p, "--location", loc,
                         "--recommender", recommender, f"--billing-project={qp_rec}", "--format=json"]))
                    if data:
                        runner.save_json(f"recommender/{p}_{loc}_{recommender.split('.')[-2]}_"
                                         f"{recommender.split('.')[-1]}.json", data)
        else:
            manifest["errors"].append({"step": "recommender", "scope": "recommender",
                                       "message": "Recommender API not enabled in any selected project"})

        (runner.run_dir / "scope.json").write_text(json.dumps(manifest, indent=2))
        return manifest

    def _find_export(self, runner: CliRunner, projects: list[str], services: dict) -> str | None:
        candidates = []
        for p in projects:
            if "bigquery.googleapis.com" not in services.get(p, set()):
                continue
            for ds in runner.run(["bq", "ls", f"--project_id={p}", "--format=json"]) or []:
                ds_id = ds["datasetReference"]["datasetId"]
                for t in runner.run(["bq", "ls", "--format=json", "--max_results=1000", f"{p}:{ds_id}"]) or []:
                    tid = t["tableReference"]["tableId"]
                    if EXPORT_RE.match(tid):
                        candidates.append((0 if "resource" in tid else 1, f"{p}.{ds_id}.{tid}"))
        return sorted(candidates)[0][1] if candidates else None

    def _query_export(self, runner: CliRunner, table: str, projects: list[str], days: int) -> None:
        resource_col = "IFNULL(resource.name, '')" if "resource_v1" in table.lower() else "''"
        plist = ", ".join(f"'{p}'" for p in projects if re.fullmatch(r"[a-z0-9-]+", p))
        sql = (f"SELECT DATE(usage_start_time) AS day, project.id AS project, service.description AS service, "
               f"{resource_col} AS resource, IFNULL(location.region, '') AS region, currency, "
               f"SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS cost "
               f"FROM `{table}` WHERE DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL {int(days)} DAY) "
               f"AND project.id IN ({plist}) GROUP BY 1, 2, 3, 4, 5, 6")
        bq_project = table.split(".")[0]
        runner.run(["bq", "query", "--nouse_legacy_sql", "--format=json", "--max_rows=1000000",
                    f"--project_id={bq_project}", sql], save_as="costs/billing_export.json")

    def _timeseries(self, runner, token, project, name, flt, days, period, aligner, reducer, group, quota_project):
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        params = [("filter", flt), ("interval.startTime", (end - timedelta(days=days)).isoformat().replace("+00:00", "Z")),
                  ("interval.endTime", end.isoformat().replace("+00:00", "Z")),
                  ("aggregation.alignmentPeriod", f"{period}s"), ("aggregation.perSeriesAligner", aligner)]
        if reducer:
            params.append(("aggregation.crossSeriesReducer", reducer))
            params += [("aggregation.groupByFields", g) for g in group]
        headers = {"x-goog-user-project": quota_project} if quota_project else {}
        series, token_page = [], None
        while True:
            q = params + ([("pageToken", token_page)] if token_page else [])
            url = f"{MONITORING}/projects/{project}/timeSeries?{urllib.parse.urlencode(q)}"
            data = runner.http_get(url, token, headers)
            series += data.get("timeSeries", [])
            token_page = data.get("nextPageToken")
            if not token_page:
                break
        runner.save_json(f"metrics/{project}_{name}.json", {"project": project, "metric": name, "days": days,
                                                          "alignment_s": period, "timeSeries": series})

    def _recommender_targets(self, raw: Path, project: str) -> list[tuple[str, str]]:
        targets = [("global", "google.resourcemanager.projectUtilization.Recommender")]
        zones, regions, sql_regions = set(), set(), set()
        for rel in ("compute_instances", "compute_disks"):
            for r in json.loads((raw / f"inventory/{project}_{rel}.json").read_text()) if (raw / f"inventory/{project}_{rel}.json").exists() else []:
                zones.add(_short(r.get("zone", "")))
        f = raw / f"inventory/{project}_compute_addresses.json"
        for r in json.loads(f.read_text()) if f.exists() else []:
            regions.add(_short(r.get("region", "")) or "global")
        f = raw / f"inventory/{project}_sql_instances.json"
        for r in json.loads(f.read_text()) if f.exists() else []:
            sql_regions.add(r.get("region", ""))
        for z in sorted(zones - {""}):
            targets += [(z, "google.compute.instance.IdleResourceRecommender"),
                        (z, "google.compute.instance.MachineTypeRecommender"),
                        (z, "google.compute.disk.IdleResourceRecommender")]
        targets += [(r, "google.compute.address.IdleResourceRecommender") for r in sorted(regions - {""})]
        for r in sorted(sql_regions - {""}):
            targets += [(r, "google.cloudsql.instance.IdleRecommender"),
                        (r, "google.cloudsql.instance.OverprovisionedRecommender")]
        return targets

    # ------------------------------------------------------------------ normalization

    def load(self, run_dir: Path, cfg: Config) -> Dataset:
        run_dir = Path(run_dir)
        raw = run_dir / "raw"
        scope = json.loads((run_dir / "scope.json").read_text()) if (run_dir / "scope.json").exists() else {}
        projects = [s["id"] for s in scope.get("subscriptions", [])]
        names = {s["id"]: s["id"] for s in scope.get("subscriptions", [])}

        resources: dict[str, Resource] = {}

        def add(res: Resource):
            res.service = res.service or TYPE_SERVICE.get(res.type, res.type)
            resources[res.id] = res
            return res

        for p in projects:
            for s in _read(raw / f"services/{p}.json", []):
                api = s["config"]["name"]
                add(Resource(id=f"//serviceusage.googleapis.com/projects/{p}/services/{api}", type="gcp.api",
                             provider="gcp", account=p, name=api, sku=api, group="global",
                             attributes={"title": s["config"].get("title", api), "api": api}))
            self._detail(raw, p, add)
            for a in _read(raw / f"inventory/{p}_assets.json", []):
                rid = a["name"].lower()
                if a.get("assetType") in SKIP_ASSET_TYPES or rid in resources:
                    continue
                domain, kind = a["assetType"].split("/", 1) if "/" in a["assetType"] else (a["assetType"], "")
                add(Resource(id=rid, type=f"gcp.asset.{kind.lower()}", provider="gcp", account=p,
                             name=a.get("displayName") or _short(a["name"]), group=a.get("location", ""),
                             region=_region_of(a.get("location", "")), sku=kind,
                             service=(ASSET_SERVICE.get(domain, domain.split('.')[0].title())
                                      + ("" if kind in PRIMARY_KINDS else f" · {kind}s")),
                             tags={k.lower(): v for k, v in (a.get("labels") or {}).items()},
                             attributes={"asset_type": a["assetType"], "state": (a.get("state") or "").lower(),
                                         "created": _ts(a.get("createTime"))}))

        by_id = resources
        extras = {"subscription_names": names, "free_types": FREE_TYPES, "log_ingest": {}, "recommendations": [],
                  "api_metrics_projects": set(), "billing_export": scope.get("billing_export"),
                  "billed_projects": {s["id"] for s in scope.get("subscriptions", []) if s.get("billing")}}
        self._attach_metrics(raw, by_id, extras)
        relationships = self._relationships(list(by_id.values()), by_id)
        records = self._costs(raw, by_id, cfg)
        extras["recommendations"] = self._recommendations(raw, cfg)
        if records:
            scope.setdefault("period", {})
            scope["period"]["start"] = str(min(r.day for r in records))
            scope["period"]["end"] = str(max(r.day for r in records))
        if not records and scope.get("billing_note"):
            scope.setdefault("notes", []).append(scope["billing_note"])
        return Dataset(provider="gcp", records=records, resources=list(by_id.values()), relationships=relationships,
                       scope=scope, extras=extras, currency="USD")

    def _detail(self, raw: Path, p: str, add) -> None:
        def rows(name):
            return _read(raw / f"inventory/{p}_{name}.json", [])

        for r in rows("compute_instances"):
            zone = _short(r.get("zone", ""))
            mt = machine_type_short(r.get("machineType", ""))
            status = r.get("status", "")
            add(Resource(
                id=_from_self_link(r["selfLink"]), type="gcp.vm", provider="gcp", account=p, name=r["name"], sku=mt,
                region=_region_of(zone), group=zone, tags={k.lower(): v for k, v in (r.get("labels") or {}).items()},
                attributes={"state": "running" if status == "RUNNING" else "stopped" if status in ("TERMINATED", "STOPPED", "SUSPENDED") else status.lower(),
                            "status": status, "instance_id": str(r.get("id", "")), "zone": zone,
                            "memory_gb": machine_memory_gb(mt),
                            "spot": (r.get("scheduling") or {}).get("provisioningModel") == "SPOT",
                            "disk_ids": [_from_self_link(d.get("source", "")) for d in r.get("disks", [])],
                            "created": _ts(r.get("creationTimestamp")), "stopped_since": _ts(r.get("lastStopTimestamp")),
                            "mig": _short(next((i.get("value", "") for i in (r.get("metadata") or {}).get("items", [])
                                                if i.get("key") == "created-by" and "instanceGroupManagers" in i.get("value", "")), ""))}))
        for r in rows("compute_disks"):
            zone = _short(r.get("zone", "") or r.get("region", ""))
            add(Resource(
                id=_from_self_link(r["selfLink"]), type="gcp.disk", provider="gcp", account=p, name=r["name"],
                sku=_short(r.get("type", "")), region=_region_of(zone), group=zone,
                tags={k.lower(): v for k, v in (r.get("labels") or {}).items()},
                attributes={"size_gb": float(r.get("sizeGb", 0)), "users": [_from_self_link(u) for u in r.get("users", [])],
                            "attached": bool(r.get("users")), "created": _ts(r.get("creationTimestamp")),
                            "last_detached": _ts(r.get("lastDetachTimestamp"))}))
        for r in rows("compute_addresses"):
            region = _short(r.get("region", "")) or "global"
            add(Resource(
                id=_from_self_link(r["selfLink"]), type="gcp.address", provider="gcp", account=p, name=r["name"],
                sku=r.get("networkTier", ""), region=region, group=region,
                tags={k.lower(): v for k, v in (r.get("labels") or {}).items()},
                attributes={"status": r.get("status", ""), "address_type": r.get("addressType", "EXTERNAL"),
                            "users": [_from_self_link(u) for u in r.get("users", [])], "address": r.get("address", "")}))
        for r in rows("compute_snapshots"):
            add(Resource(
                id=_from_self_link(r["selfLink"]), type="gcp.snapshot", provider="gcp", account=p, name=r["name"],
                sku=r.get("snapshotType", "STANDARD"), region=",".join(r.get("storageLocations", [])), group="global",
                tags={k.lower(): v for k, v in (r.get("labels") or {}).items()},
                attributes={"size_gb": float(r.get("diskSizeGb", 0)),
                            "storage_gb": float(r.get("storageBytes", 0)) / 1024 ** 3,
                            "created": _ts(r.get("creationTimestamp")), "source_id": _from_self_link(r.get("sourceDisk", ""))}))
        for r in rows("run_services"):
            meta, spec = r.get("metadata", {}), r.get("spec", {})
            region = (meta.get("labels") or {}).get("cloud.googleapis.com/location", "")
            tmpl = spec.get("template", {})
            ann = (tmpl.get("metadata") or {}).get("annotations") or {}
            containers = (tmpl.get("spec") or {}).get("containers") or [{}]
            limits = (containers[0].get("resources") or {}).get("limits") or {}
            labels = {k.lower(): v for k, v in (meta.get("labels") or {}).items() if not k.startswith(("cloud.googleapis", "goog-", "run.googleapis"))}
            add(Resource(
                id=f"//run.googleapis.com/projects/{p}/locations/{region}/services/{meta.get('name')}".lower(),
                type="gcp.run_service", provider="gcp", account=p, name=meta.get("name", ""), region=region, group=region,
                sku=f"{limits.get('cpu', '1')} vCPU / {limits.get('memory', '512Mi')}", tags=labels,
                attributes={"min_instances": int(ann.get("autoscaling.knative.dev/minScale", 0) or 0),
                            "max_instances": int(ann.get("autoscaling.knative.dev/maxScale", 0) or 0),
                            "cpu": parse_cpu(limits.get("cpu", "1")), "memory_gib": parse_mem_gib(limits.get("memory", "512Mi")),
                            "cpu_always_allocated": ann.get("run.googleapis.com/cpu-throttling") == "false",
                            "image": containers[0].get("image", ""),
                            "created": _ts(meta.get("creationTimestamp")),
                            "gen2_function": "goog-managed-by" in (meta.get("labels") or {}) and
                                             (meta.get("labels") or {}).get("goog-managed-by") == "cloudfunctions"}))
        for r in rows("functions"):
            name = r.get("name", "")
            m = re.match(r"projects/[^/]+/locations/([^/]+)/functions/(.+)", name)
            region, fname = (m.group(1), m.group(2)) if m else ("", _short(name))
            svc = r.get("serviceConfig") or {}
            add(Resource(
                id=f"//cloudfunctions.googleapis.com/projects/{p}/locations/{region}/functions/{fname}".lower(),
                type="gcp.function", provider="gcp", account=p, name=fname, region=region, group=region,
                sku=r.get("environment") or ("GEN_2" if svc else "GEN_1"),
                tags={k.lower(): v for k, v in (r.get("labels") or {}).items() if not k.startswith("deployment")},
                attributes={"state": (r.get("state") or r.get("status") or "").lower(),
                            "runtime": (r.get("buildConfig") or {}).get("runtime") or r.get("runtime", ""),
                            "min_instances": int(svc.get("minInstanceCount") or r.get("minInstances") or 0),
                            "memory": svc.get("availableMemory") or r.get("availableMemoryMb"),
                            "run_service": svc.get("service", ""),
                            "updated": _ts(r.get("updateTime"))}))
        for r in rows("buckets"):
            name = r.get("name", "")
            lifecycle = r.get("lifecycle_config") or r.get("lifecycle") or {}
            add(Resource(
                id=f"//storage.googleapis.com/{name}".lower(), type="gcp.bucket", provider="gcp", account=p, name=name,
                sku=(r.get("default_storage_class") or r.get("storageClass") or "STANDARD").upper(),
                region=(r.get("location") or "").lower(), group=(r.get("location") or "").lower(),
                tags={k.lower(): v for k, v in (r.get("labels") or {}).items()},
                attributes={"has_lifecycle": bool((lifecycle or {}).get("rule")),
                            "soft_delete_days": int((r.get("soft_delete_policy") or {}).get("retentionDurationSeconds", 0) or 0) // 86400,
                            "versioning": bool((r.get("versioning") or {}).get("enabled") or r.get("versioning_enabled")),
                            "created": _ts(r.get("creation_time") or r.get("timeCreated"))}))
        for r in rows("artifact_repos"):
            name = r.get("name", "")
            m = re.match(r"projects/[^/]+/locations/([^/]+)/repositories/(.+)", name)
            region, rname = (m.group(1), m.group(2)) if m else ("", _short(name))
            add(Resource(
                id=f"//artifactregistry.googleapis.com/projects/{p}/locations/{region}/repositories/{rname}".lower(),
                type="gcp.artifact_repo", provider="gcp", account=p, name=rname, region=region, group=region,
                sku=r.get("format", ""), tags={k.lower(): v for k, v in (r.get("labels") or {}).items()},
                attributes={"size_gb": float(r.get("sizeBytes", 0) or 0) / 1024 ** 3,
                            "cleanup_policies": len(r.get("cleanupPolicies") or {}),
                            "cleanup_dry_run": bool(r.get("cleanupPolicyDryRun")),
                            "mode": r.get("mode", ""), "description": r.get("description", ""),
                            "created": _ts(r.get("createTime"))}))
        for r in rows("log_buckets"):
            name = r.get("name", "")
            m = re.match(r"projects/[^/]+/locations/([^/]+)/buckets/(.+)", name)
            region, bname = (m.group(1), m.group(2)) if m else ("global", _short(name))
            add(Resource(
                id=f"//logging.googleapis.com/projects/{p}/locations/{region}/buckets/{bname}".lower(),
                type="gcp.log_bucket", provider="gcp", account=p, name=bname, region=region, group=region,
                sku=f"{r.get('retentionDays', 30)}d retention",
                attributes={"retention_days": int(r.get("retentionDays", 30) or 30), "locked": bool(r.get("locked")),
                            "analytics": bool(r.get("analyticsEnabled"))}))
        for r in rows("sql_instances"):
            settings = r.get("settings") or {}
            add(Resource(
                id=f"//cloudsql.googleapis.com/projects/{p}/instances/{r['name']}".lower(), type="gcp.sql",
                provider="gcp", account=p, name=r["name"], sku=settings.get("tier", ""), region=r.get("region", ""),
                group=r.get("region", ""), tags={k.lower(): v for k, v in (settings.get("userLabels") or {}).items()},
                attributes={"state": "running" if r.get("state") == "RUNNABLE" and settings.get("activationPolicy") != "NEVER" else "stopped",
                            "version": r.get("databaseVersion", ""), "availability": settings.get("availabilityType", ""),
                            "disk_gb": float(settings.get("dataDiskSizeGb", 0) or 0), "edition": settings.get("edition", ""),
                            "activation_policy": settings.get("activationPolicy", "ALWAYS"),
                            "database_id": f"{p}:{r['name']}"}))
        for r in rows("compute_routers"):
            region = _short(r.get("region", ""))
            for nat in r.get("nats") or []:
                add(Resource(
                    id=f"//compute.googleapis.com/projects/{p}/regions/{region}/routers/{r['name']}/nats/{nat['name']}".lower(),
                    type="gcp.cloud_nat", provider="gcp", account=p, name=nat["name"], region=region, group=region,
                    sku=nat.get("natIpAllocateOption", ""),
                    attributes={"router": r["name"], "router_id": str(r.get("id", "")),
                                "ranges": nat.get("sourceSubnetworkIpRangesToNat", "")}))
        for r in rows("forwarding_rules"):
            region = _short(r.get("region", "")) or "global"
            target = _short(r.get("target", ""))
            add(Resource(
                id=_from_self_link(r["selfLink"]), type="gcp.forwarding_rule", provider="gcp", account=p, name=r["name"],
                region=region, group=region, sku=r.get("loadBalancingScheme", ""),
                tags={k.lower(): v for k, v in (r.get("labels") or {}).items()},
                attributes={"target": target, "port_range": r.get("portRange", ""), "ip": r.get("IPAddress", ""),
                            "http": "targetHttp" in r.get("target", "")}))
        for r in rows("vpn_tunnels"):
            region = _short(r.get("region", ""))
            add(Resource(
                id=_from_self_link(r["selfLink"]), type="gcp.vpn_tunnel", provider="gcp", account=p, name=r["name"],
                region=region, group=region, sku=r.get("status", ""),
                attributes={"status": r.get("status", ""), "detail": r.get("detailedStatus", ""), "peer_ip": r.get("peerIp", "")}))
        for r in rows("migs"):
            loc = _short(r.get("zone", "") or r.get("region", ""))
            add(Resource(
                id=_from_self_link(r["selfLink"]), type="gcp.mig", provider="gcp", account=p, name=r["name"],
                region=_region_of(loc), group=loc, sku=_short(r.get("instanceTemplate", "")),
                attributes={"target_size": int(r.get("targetSize") or 0),
                            "autoscaled": bool((r.get("status") or {}).get("autoscaler"))}))
        for r in rows("gke_clusters"):
            add(Resource(
                id=f"//container.googleapis.com/projects/{p}/locations/{r.get('location')}/clusters/{r['name']}".lower(),
                type="gcp.gke", provider="gcp", account=p, name=r["name"], region=_region_of(r.get("location", "")),
                group=r.get("location", ""), sku="autopilot" if (r.get("autopilot") or {}).get("enabled") else "standard",
                tags={k.lower(): v for k, v in (r.get("resourceLabels") or {}).items()},
                attributes={"node_count": r.get("currentNodeCount", 0), "status": r.get("status", ""),
                            "node_pools": [{"name": np.get("name"), "machine_type": (np.get("config") or {}).get("machineType"),
                                            "count": np.get("initialNodeCount"),
                                            "autoscale": (np.get("autoscaling") or {}).get("enabled", False)}
                                           for np in r.get("nodePools", [])]}))

    def _attach_metrics(self, raw: Path, by_id: dict[str, Resource], extras: dict) -> None:
        vms = {(r.account, r.attributes.get("zone"), r.name): r for r in by_id.values() if r.type == "gcp.vm"}
        vms_by_iid = {r.attributes.get("instance_id"): r for r in by_id.values() if r.type == "gcp.vm"}
        runs = {(r.account, r.region, r.name): r for r in by_id.values() if r.type == "gcp.run_service"}
        funcs = {(r.account, r.name): r for r in by_id.values() if r.type == "gcp.function"}
        buckets = {r.name: r for r in by_id.values() if r.type == "gcp.bucket"}
        apis = {(r.account, r.name): r for r in by_id.values() if r.type == "gcp.api"}
        sqls = {r.attributes.get("database_id"): r for r in by_id.values() if r.type == "gcp.sql"}
        nats = {(r.account, r.region, r.name): r for r in by_id.values() if r.type == "gcp.cloud_nat"}
        rules = {(r.account, r.name): r for r in by_id.values() if r.type == "gcp.forwarding_rule"}
        sql_series: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        vm_series: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

        for f in sorted((raw / "metrics").glob("*.json")) if (raw / "metrics").exists() else []:
            payload = json.loads(f.read_text())
            p, name = payload["project"], payload["metric"]
            for ts in payload.get("timeSeries", []):
                rl = (ts.get("resource") or {}).get("labels", {})
                ml = (ts.get("metric") or {}).get("labels", {})
                values = [_point_value(pt) for pt in ts.get("points", [])]
                if name == "api_requests":
                    extras["api_metrics_projects"].add(p)
                    api = apis.get((p, rl.get("service")))
                    if api:
                        api.metrics["requests_30d"] = api.metrics.get("requests_30d", 0) + sum(values)
                        methods = api.attributes.setdefault("methods", {})
                        method = rl.get("method", "").rsplit(".", 1)[-1]
                        methods[method] = methods.get(method, 0) + sum(values)
                elif name in ("vm_cpu_mean", "vm_cpu_max", "vm_mem"):
                    vm = vms_by_iid.get(rl.get("instance_id")) or vms.get((p, rl.get("zone"), ml.get("instance_name")))
                    if vm:
                        vm_series[vm.id][name] += values
                elif name in ("run_requests", "run_instances"):
                    svc = runs.get((p, rl.get("location"), rl.get("service_name")))
                    if svc:
                        key = "requests_30d" if name == "run_requests" else "avg_instances"
                        svc.metrics[key] = sum(values) if name == "run_requests" else (statistics.fmean(values) if values else 0.0)
                elif name == "function_execs":
                    fn = funcs.get((p, rl.get("function_name")))
                    if fn:
                        fn.metrics["executions_30d"] = fn.metrics.get("executions_30d", 0) + sum(values)
                elif name == "gcs_bytes":
                    b = buckets.get(rl.get("bucket_name"))
                    if b and values:
                        gb = values[0] / 1024 ** 3  # newest point first
                        b.metrics["stored_gb"] = b.metrics.get("stored_gb", 0) + gb
                        b.attributes.setdefault("classes", {})[ml.get("storage_class", "")] = round(gb, 3)
                elif name == "log_ingest":
                    extras["log_ingest"].setdefault(p, {})[ml.get("resource_type", "other")] = sum(values) / 1024 ** 3
                elif name.startswith("sql_"):
                    if rl.get("database_id") not in sqls:
                        continue
                    if name == "sql_conn_pg" and ml.get("database") in SQL_ADMIN_DATABASES:
                        sql_series[rl["database_id"]].setdefault(name, [])  # measured; admin connections don't count
                        continue
                    sql_series[rl["database_id"]][name] += values
                elif name == "nat_bytes":
                    nat = nats.get((p, rl.get("region"), rl.get("gateway_name")))
                    if nat:
                        nat.metrics["processed_gb_30d"] = nat.metrics.get("processed_gb_30d", 0) + sum(values) / 1e9
                elif name == "lb_requests":
                    fr = rules.get((p, rl.get("forwarding_rule_name")))
                    if fr:
                        fr.metrics["requests_30d"] = fr.metrics.get("requests_30d", 0) + sum(values)
            # metric collected but nothing returned -> zero usage is real evidence
            if name == "run_requests":
                for (rp, _, _), svc in runs.items():
                    if rp == p:
                        svc.metrics.setdefault("requests_30d", 0.0)
            elif name == "function_execs":
                for (fp, _), fn in funcs.items():
                    if fp == p:
                        fn.metrics.setdefault("executions_30d", 0.0)
            elif name == "lb_requests":
                for (fp, _), fr in rules.items():
                    if fp == p and fr.attributes.get("http"):
                        fr.metrics.setdefault("requests_30d", 0.0)
            elif name == "nat_bytes":
                for (np_, _, _), nat in nats.items():
                    if np_ == p:
                        nat.metrics.setdefault("processed_gb_30d", 0.0)
            elif name == "api_requests":
                extras["api_metrics_projects"].add(p)
                for (ap, _), api in apis.items():
                    if ap == p:
                        api.metrics.setdefault("requests_30d", 0.0)

        for db_id, series in sql_series.items():
            m = sqls[db_id].metrics
            cpu = [v * 100 for v in series.get("sql_cpu", [])]
            if cpu:
                m.update(cpu_avg=statistics.fmean(cpu), cpu_p95=_p95(cpu), cpu_max=max(cpu),
                         uptime_share=min(1.0, len(cpu) / (14 * 24)), metrics_window_days=14)
            if series.get("sql_mem"):
                m["mem_used_p95"] = _p95([v * 100 for v in series["sql_mem"]])
            conns = series.get("sql_conn_mysql", []) + series.get("sql_conn_pg", [])
            if "sql_conn_mysql" in series or "sql_conn_pg" in series:
                m["connections_max"] = max(conns, default=0.0)
        for rid, series in vm_series.items():
            vm, m = by_id[rid], by_id[rid].metrics
            mean = [v * 100 for v in series.get("vm_cpu_mean", [])]
            mx = [v * 100 for v in series.get("vm_cpu_max", [])] or mean
            if mean:
                m.update(cpu_avg=statistics.fmean(mean), cpu_p95=_p95(mean), cpu_max=max(mx),
                         uptime_share=min(1.0, len(mean) / (14 * 24)), metrics_window_days=14)
            if series.get("vm_mem"):
                m["mem_used_p95"] = _p95(series["vm_mem"])

    def _relationships(self, resources: list[Resource], by_id: dict[str, Resource]) -> list[Relationship]:
        rels = []
        repos = {r.id: r for r in resources if r.type == "gcp.artifact_repo"}
        for r in resources:
            a = r.attributes
            if r.type == "gcp.vm":
                rels += [Relationship(d, r.id, "attached_to") for d in a.get("disk_ids", [])]
                if a.get("mig"):
                    mig = next((m for m in resources if m.type == "gcp.mig" and m.account == r.account
                                and m.name == a["mig"]), None)
                    if mig:
                        rels.append(Relationship(r.id, mig.id, "member_of"))
            elif r.type == "gcp.address":
                rels += [Relationship(r.id, u, "ip_of") for u in a.get("users", [])]
            elif r.type == "gcp.snapshot" and a.get("source_id"):
                rels.append(Relationship(r.id, a["source_id"], "snapshot_of"))
            elif r.type == "gcp.function" and a.get("run_service"):
                m = re.match(r"projects/([^/]+)/locations/([^/]+)/services/(.+)", a["run_service"])
                if m:
                    rels.append(Relationship(r.id, f"//run.googleapis.com/projects/{r.account}/locations/{m.group(2)}/services/{m.group(3)}".lower(), "runs_on"))
            elif r.type == "gcp.run_service" and a.get("image"):
                m = re.match(r"([a-z0-9-]+)-docker\.pkg\.dev/([^/]+)/([^/]+)/", a["image"])
                if m:
                    rid = f"//artifactregistry.googleapis.com/projects/{m.group(2)}/locations/{m.group(1)}/repositories/{m.group(3)}".lower()
                    if rid in repos:
                        rels.append(Relationship(r.id, rid, "image_from"))
        out, seen = [], set()
        for rel in rels:
            if (rel.src, rel.dst, rel.kind) in seen:
                continue
            seen.add((rel.src, rel.dst, rel.kind))
            out.append(rel)
            for a, b in ((rel.src, rel.dst), (rel.dst, rel.src)):
                if a in by_id and b not in by_id[a].related:
                    by_id[a].related.append(b)
        return out

    def _costs(self, raw: Path, by_id: dict[str, Resource], cfg: Config) -> list[CostRecord]:
        rows = _read(raw / "costs/billing_export.json", [])
        if not rows:
            return []
        by_name = defaultdict(list)
        for r in by_id.values():
            by_name[(r.account, r.name.lower())].append(r)
        fx = {**FX_TO_USD, **(cfg.fx_to_usd or {})}
        records = []
        for row in rows:
            p, res_name = row.get("project") or "", (row.get("resource") or "").lower()
            res_name = res_name.rsplit("/", 1)[-1]
            matches = by_name.get((p, res_name), []) if res_name else []
            res = matches[0] if len(matches) == 1 else None
            rate = fx.get(row.get("currency", "USD"), None)
            if rate is None:
                continue
            records.append(CostRecord(
                day=date.fromisoformat(row["day"][:10]), cost=float(row["cost"]) * rate, service=row["service"],
                account=p, region=row.get("region", ""), resource_id=res.id if res else (f"{p}/{res_name}" if res_name else ""),
                provider="gcp", tags=res.tags if res else {}, currency="USD"))
        return records

    def _recommendations(self, raw: Path, cfg: Config) -> list[dict]:
        out = []
        fx = {**FX_TO_USD, **(cfg.fx_to_usd or {})}
        for f in sorted((raw / "recommender").glob("*.json")) if (raw / "recommender").exists() else []:
            project = f.name.split("_", 1)[0]
            for rec in json.loads(f.read_text()) or []:
                if (rec.get("stateInfo") or {}).get("state", "ACTIVE") != "ACTIVE":
                    continue
                impact = (rec.get("primaryImpact") or {})
                cp = impact.get("costProjection") or {}
                cost = cp.get("cost") or {}
                amount = float(cost.get("units", 0) or 0) + float(cost.get("nanos", 0) or 0) / 1e9
                dur = float(str(cp.get("duration", "2592000s")).rstrip("s") or 2592000)
                monthly = -amount * (2592000 / dur) * fx.get(cost.get("currencyCode", "USD"), 0.0) if amount else 0.0
                overview = (rec.get("content") or {}).get("overview") or {}
                target = overview.get("resourceName") or overview.get("resource") or ""
                if not target:
                    for grp in (rec.get("content") or {}).get("operationGroups", []):
                        for op in grp.get("operations", []):
                            target = target or op.get("resource", "")
                out.append({"project": project, "recommender": rec.get("recommenderSubtype", ""),
                            "name": rec.get("name", ""), "description": rec.get("description", ""),
                            "resource_id": target.lower(), "monthly_savings": max(0.0, monthly),
                            "currency": cost.get("currencyCode", "USD"), "priority": rec.get("priority", ""),
                            "category": impact.get("category", "")})
        return out


def _read(path: Path, default):
    if not path.exists():
        return default
    text = path.read_text().strip()
    return json.loads(text) if text else default
