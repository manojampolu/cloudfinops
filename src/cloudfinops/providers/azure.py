"""Azure adapter: read-only collection through the Azure CLI, and offline normalization.

Data sources
  inventory   az graph query (Azure Resource Graph)            -> raw/inventory/*.json
  costs       Cost Management Query API via `az rest` (POST)    -> raw/costs/<sub>_*.json
  metrics     az monitor metrics list                           -> raw/metrics/*.json
  advisor     az advisor recommendation list --category Cost    -> raw/advisor/<sub>.json
  logging     az monitor log-analytics query / workspace table  -> raw/logging/*.json
  prices      Azure Retail Prices API (public)                  -> raw/prices/vm_prices.json

Required roles at subscription scope: Reader, Cost Management Reader, Log Analytics Reader.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..config import Config
from ..harness.runner import CliError, CliRunner
from ..models import CostRecord, Dataset, Relationship, Resource
from ..pricing_azure import PriceBook, vm_downsize, vm_memory_gb

COST_API = "2023-11-01"
INVENTORY_QUERY = ("Resources | project id, name, type, location, resourceGroup, subscriptionId, "
                   "sku, kind, managedBy, tags, properties")
CONTAINERS_QUERY = "ResourceContainers | project id, name, type, subscriptionId, tags"
LOG_USAGE_QUERY = ("Usage | where TimeGenerated > ago(30d) | where IsBillable == true "
                   "| summarize BillableGB = sum(Quantity) / 1000.0 by DataType")

# az graph -> resource-graph; az monitor log-analytics query -> log-analytics
REQUIRED_EXTENSIONS = ("resource-graph", "log-analytics")

TYPE_MAP = {
    "microsoft.compute/virtualmachines": ("azure.vm", "Virtual Machines"),
    "microsoft.compute/disks": ("azure.disk", "Managed Disks"),
    "microsoft.compute/snapshots": ("azure.snapshot", "Snapshots"),
    "microsoft.compute/virtualmachinescalesets": ("azure.vmss", "VM Scale Sets"),
    "microsoft.network/publicipaddresses": ("azure.public_ip", "Public IP Addresses"),
    "microsoft.network/networkinterfaces": ("azure.nic", "Network Interfaces"),
    "microsoft.network/loadbalancers": ("azure.lb", "Load Balancers"),
    "microsoft.network/applicationgateways": ("azure.appgw", "Application Gateways"),
    "microsoft.network/virtualnetworks": ("azure.vnet", "Virtual Networks"),
    "microsoft.network/networksecuritygroups": ("azure.nsg", "Network Security Groups"),
    "microsoft.network/natgateways": ("azure.nat_gateway", "NAT Gateways"),
    "microsoft.web/serverfarms": ("azure.app_service_plan", "App Service Plans"),
    "microsoft.web/sites": ("azure.web_app", "App Services"),
    "microsoft.containerservice/managedclusters": ("azure.aks", "Kubernetes Service (AKS)"),
    "microsoft.operationalinsights/workspaces": ("azure.log_workspace", "Log Analytics"),
    "microsoft.insights/components": ("azure.app_insights", "Application Insights"),
    "microsoft.storage/storageaccounts": ("azure.storage_account", "Storage Accounts"),
    "microsoft.sql/servers": ("azure.sql_server", "SQL Servers"),
    "microsoft.sql/servers/databases": ("azure.sql_db", "SQL Databases"),
    "microsoft.keyvault/vaults": ("azure.key_vault", "Key Vaults"),
    "microsoft.containerregistry/registries": ("azure.acr", "Container Registries"),
}
# Types that normally carry no direct charge (used by data-quality checks)
FREE_TYPES = {"azure.nic", "azure.vnet", "azure.nsg", "azure.web_app", "azure.sql_server"}

METRICS_BY_TYPE = {
    "azure.vm": ["Percentage CPU", "Available Memory Bytes"],
    "azure.vmss": ["Percentage CPU"],
    "azure.disk": ["Composite Disk Read Operations/sec", "Composite Disk Write Operations/sec"],
    "azure.app_service_plan": ["CpuPercentage", "MemoryPercentage"],
}


def _lower(s) -> str:
    return (s or "").lower()


def _safe_name(resource_id: str) -> str:
    short = resource_id.rstrip("/").rsplit("/", 1)[-1][:40]
    return f"{re.sub(r'[^a-zA-Z0-9_.-]', '_', short)}-{hashlib.sha1(resource_id.encode()).hexdigest()[:8]}"


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    return vals[min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))]


def _interval_hours(interval: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", interval or "PT1H")
    if not m:
        return 1.0
    d, h, mins = (int(x) if x else 0 for x in m.groups())
    return d * 24 + h + mins / 60 or 1.0


class AzureProvider:
    name = "azure"
    cli = "az"

    # ------------------------------------------------------------------ session

    def preflight(self, runner: CliRunner) -> list[str]:
        if not runner.available("az"):
            return ["Azure CLI (az) not found. Install it: https://learn.microsoft.com/cli/azure/install-azure-cli "
                    "(macOS: brew install azure-cli)"]
        problems = []
        try:
            ver = runner.run(["az", "version", "-o", "json"])
            for ext in REQUIRED_EXTENSIONS:
                if ext not in (ver.get("extensions") or {}):
                    problems.append(f"missing-extension:{ext}")
        except CliError as exc:
            problems.append(str(exc))
        return problems

    def install_extension(self, runner: CliRunner, name: str) -> None:
        runner.run(["az", "extension", "add", "--name", name, "--only-show-errors"], parse_json=False)

    def ensure_login(self, runner: CliRunner, device_code: bool = False, tenant: str | None = None) -> dict:
        try:
            return runner.run(["az", "account", "show", "-o", "json"])
        except CliError:
            cmd = ["az", "login"] + (["--use-device-code"] if device_code else []) + (["--tenant", tenant] if tenant else [])
            runner.run(cmd, interactive=True)
            return runner.run(["az", "account", "show", "-o", "json"])

    def list_scopes(self, runner: CliRunner) -> list[dict]:
        subs = runner.run(["az", "account", "list", "--all", "-o", "json"]) or []
        return [{"id": s["id"], "name": s["name"], "tenant": s.get("tenantId", "")}
                for s in subs if s.get("state") == "Enabled"]

    # ------------------------------------------------------------------ collection

    def collect(self, runner: CliRunner, scopes: list[dict], cfg: Config, log=print) -> dict:
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=cfg.collection_days - 1)
        sub_ids = [s["id"] for s in scopes]
        manifest = {
            "provider": "azure", "subscriptions": scopes,
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "metric_days": cfg.metric_days,
            "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "errors": [],
        }

        def guard(step: str, scope: str, fn):
            try:
                return fn()
            except Exception as exc:  # record and keep going: a gap is evidence too
                log(f"  ! {step} ({scope}) failed: {str(exc)[:200]}")
                manifest["errors"].append({"step": step, "scope": scope, "message": str(exc)[:1000]})
                return None

        log("Collecting inventory (Azure Resource Graph)...")
        resources = guard("inventory", "all", lambda: self._graph(runner, INVENTORY_QUERY, sub_ids)) or []
        runner.save_json("inventory/resources.json", resources)
        containers = guard("inventory", "containers", lambda: self._graph(runner, CONTAINERS_QUERY, sub_ids)) or []
        runner.save_json("inventory/containers.json", containers)
        log(f"  {len(resources)} resources")

        log("Collecting costs (Cost Management, amortized)...")
        for sub in sub_ids:
            for name, grouping in (("by_resource", ["ResourceId", "ServiceName"]),
                                   ("by_pricing_model", ["ServiceName", "PricingModel"])):
                data = guard("costs", f"{sub}:{name}", lambda: self._cost_query(runner, sub, start, end, grouping))
                if data is not None:
                    runner.save_json(f"costs/{sub}_{name}.json", data)

        log("Collecting Azure Advisor cost recommendations...")
        for sub in sub_ids:
            recs = guard("advisor", sub, lambda: runner.run(
                ["az", "advisor", "recommendation", "list", "--category", "Cost", "--subscription", sub, "-o", "json"]))
            if recs is not None:
                runner.save_json(f"advisor/{sub}.json", recs)

        log("Collecting Log Analytics usage...")
        for ws in (r for r in resources if _lower(r.get("type")) == "microsoft.operationalinsights/workspaces"):
            cid = (ws.get("properties") or {}).get("customerId")
            base = _safe_name(_lower(ws["id"]))
            if cid:
                usage = guard("logging", ws["name"], lambda: runner.run(
                    ["az", "monitor", "log-analytics", "query", "-w", cid, "--analytics-query", LOG_USAGE_QUERY,
                     "-o", "json"]))
                if usage is not None:
                    runner.save_json(f"logging/{base}_usage.json", {"workspace_id": _lower(ws["id"]), "rows": usage})
            tables = guard("logging", ws["name"], lambda: runner.run(
                ["az", "monitor", "log-analytics", "workspace", "table", "list", "-g", ws["resourceGroup"],
                 "--workspace-name", ws["name"], "--subscription", ws["subscriptionId"], "-o", "json"]))
            if tables is not None:
                runner.save_json(f"logging/{base}_tables.json", {"workspace_id": _lower(ws["id"]), "tables": tables})

        log("Collecting utilization metrics (Azure Monitor)...")
        targets = self._metric_targets(resources, runner.raw_dir, cfg)
        m_end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        m_start = m_end - timedelta(days=cfg.metric_days)
        done = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._metrics, runner, rid, names, m_start, m_end): rid
                       for rid, names in targets}
            for fut in as_completed(futures):
                rid = futures[fut]
                try:
                    runner.save_json(f"metrics/{_safe_name(rid)}.json", {"resource_id": rid, "response": fut.result()})
                except Exception as exc:
                    manifest["errors"].append({"step": "metrics", "scope": rid, "message": str(exc)[:500]})
                done += 1
                if done % 50 == 0:
                    log(f"  {done}/{len(targets)}")
        manifest["metrics_requested"] = len(targets)
        log(f"  metrics for {len(targets)} resources")

        log("Looking up VM list prices (Azure Retail Prices API)...")
        book = PriceBook(cache_dir=runner.raw_dir / "prices", online=True)
        sizes = set()
        for r in resources:
            if _lower(r.get("type")) == "microsoft.compute/virtualmachines":
                size = ((r.get("properties") or {}).get("hardwareProfile") or {}).get("vmSize")
                if size:
                    sizes.add((size, r.get("location", "")))
                    if (smaller := vm_downsize(size)):
                        sizes.add((smaller, r.get("location", "")))
        book.prefetch({(size, region) for size, region in sizes})
        if not book.online:
            manifest["errors"].append({"step": "prices", "scope": "prices.azure.com",
                                       "message": "Retail Prices API unreachable; fallback price table used"})

        (runner.run_dir / "scope.json").write_text(json.dumps(manifest, indent=2))
        return manifest

    def _graph(self, runner: CliRunner, query: str, subs: list[str]) -> list[dict]:
        rows, token = [], None
        while True:
            cmd = ["az", "graph", "query", "-q", query, "--first", "1000", "-o", "json"]
            if subs:
                cmd += ["--subscriptions", *subs]
            if token:
                cmd += ["--skip-token", token]
            page = runner.run(cmd) or {}
            rows += page.get("data", [])
            token = page.get("skip_token") or page.get("skipToken")
            if not token:
                return rows

    def _cost_query(self, runner: CliRunner, sub: str, start: date, end: date, grouping: list[str]) -> dict:
        body = {
            "type": "AmortizedCost", "timeframe": "Custom",
            "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
            "dataset": {"granularity": "Daily",
                        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                        "grouping": [{"type": "Dimension", "name": g} for g in grouping]},
        }
        url = (f"https://management.azure.com/subscriptions/{sub}/providers/Microsoft.CostManagement/query"
               f"?api-version={COST_API}")
        columns, rows = None, []
        while url:
            resp = runner.run(["az", "rest", "--method", "post", "--url", url, "--body", json.dumps(body), "-o", "json"])
            props = resp.get("properties", {})
            columns = columns or props.get("columns", [])
            rows += props.get("rows", [])
            url = props.get("nextLink")
        return {"columns": columns, "rows": rows}

    def _metric_targets(self, resources: list[dict], raw_dir: Path, cfg: Config) -> list[tuple[str, list[str]]]:
        cost = defaultdict(float)
        for f in (raw_dir / "costs").glob("*_by_resource.json"):
            data = json.loads(f.read_text())
            idx = {c["name"]: i for i, c in enumerate(data["columns"])}
            for row in data["rows"]:
                cost[_lower(row[idx["ResourceId"]])] += float(row[idx["Cost"]])
        targets = []
        for r in resources:
            rid, rtype = _lower(r["id"]), TYPE_MAP.get(_lower(r.get("type")), ("", ""))[0]
            props = r.get("properties") or {}
            if rtype == "azure.vm":
                power = _lower(((props.get("extended") or {}).get("instanceView") or {}).get("powerState", {}).get("code"))
                if "running" not in power:
                    continue
            elif rtype == "azure.disk":
                if not _lower((r.get("sku") or {}).get("name")).startswith("premium") or \
                        _lower(props.get("diskState")) != "attached":
                    continue
            elif rtype not in METRICS_BY_TYPE:
                continue
            targets.append((cost[rid], rid, METRICS_BY_TYPE[rtype]))
        targets.sort(reverse=True)
        return [(rid, names) for _, rid, names in targets[:cfg.max_metric_resources]]

    def _metrics(self, runner: CliRunner, rid: str, names: list[str], start: datetime, end: datetime):
        def query(metric_names):
            return runner.run(["az", "monitor", "metrics", "list", "--resource", rid, "--metrics", *metric_names,
                               "--aggregation", "Average", "Maximum", "--interval", "PT1H",
                               "--start-time", start.isoformat(), "--end-time", end.isoformat(), "-o", "json"])
        try:
            return query(names)
        except CliError:
            if len(names) == 1:
                raise
        # One unsupported metric (e.g. no guest memory on this VM size) fails the whole call: fetch one by one
        merged, last_error = None, None
        for name in names:
            try:
                part = query([name])
            except CliError as exc:
                last_error = exc
                continue
            if merged is None:
                merged = part
            else:
                merged.setdefault("value", []).extend(part.get("value", []))
        if merged is None:
            raise last_error
        return merged

    # ------------------------------------------------------------------ normalization

    def load(self, run_dir: Path, cfg: Config) -> Dataset:
        run_dir = Path(run_dir)
        raw = run_dir / "raw"
        scope = json.loads((run_dir / "scope.json").read_text()) if (run_dir / "scope.json").exists() else {}
        rows = _read(raw / "inventory/resources.json", [])
        sub_names = {s["id"]: s.get("name", s["id"]) for s in scope.get("subscriptions", [])}
        for c in _read(raw / "inventory/containers.json", []):
            if _lower(c.get("type")) == "microsoft.resources/subscriptions":
                sub_names.setdefault(c["subscriptionId"], c["name"])

        resources = [self._resource(r, sub_names) for r in rows]
        by_id = {r.id: r for r in resources}
        relationships = self._relationships(resources, by_id)

        records, currency = self._costs(raw, by_id)
        pricing_model = self._pricing_model(raw)
        self._attach_metrics(raw, by_id, cfg)
        advisor = self._advisor(raw, by_id)
        log_usage = self._logging(raw, by_id)

        if records:
            scope.setdefault("period", {})
            scope["period"]["start"] = str(min(r.day for r in records))
            scope["period"]["end"] = str(max(r.day for r in records))

        return Dataset(
            provider="azure", records=records, resources=resources, relationships=relationships,
            scope=scope, currency=currency,
            extras={"pricing_model_daily": pricing_model, "advisor": advisor, "log_usage": log_usage,
                    "pricebook": PriceBook(cache_dir=raw / "prices", online=False),
                    "subscription_names": sub_names, "free_types": FREE_TYPES},
        )

    def _resource(self, r: dict, sub_names: dict) -> Resource:
        rtype_raw = _lower(r.get("type"))
        rtype, service = TYPE_MAP.get(rtype_raw, (
            "azure." + rtype_raw.rsplit("/", 1)[-1], rtype_raw.rsplit("/", 1)[-1].title()))
        props = r.get("properties") or {}
        sku = r.get("sku") or {}
        sub = r.get("subscriptionId", "")
        res = Resource(
            id=_lower(r["id"]), type=rtype, provider="azure", region=_lower(r.get("location")),
            account=sub, name=r.get("name", ""), service=service, group=r.get("resourceGroup", ""),
            sku=sku.get("name", "") if isinstance(sku, dict) else str(sku),
            tags={str(k).lower(): str(v) for k, v in (r.get("tags") or {}).items()},
        )
        a = res.attributes
        a["subscription_name"] = sub_names.get(sub, sub)
        a["arm_type"] = rtype_raw
        if r.get("kind"):
            a["kind"] = r["kind"]

        if rtype == "azure.vm":
            size = (props.get("hardwareProfile") or {}).get("vmSize", "")
            power = _lower(((props.get("extended") or {}).get("instanceView") or {}).get("powerState", {}).get("code"))
            storage = props.get("storageProfile") or {}
            disks = [((storage.get("osDisk") or {}).get("managedDisk") or {}).get("id")]
            disks += [(d.get("managedDisk") or {}).get("id") for d in storage.get("dataDisks", [])]
            res.sku = size
            a.update(vm_size=size, power_state=power.split("/")[-1] if power else "unknown",
                     state="running" if "running" in power else (power.split("/")[-1] or "unknown"),
                     os_type=(storage.get("osDisk") or {}).get("osType", ""),
                     priority=props.get("priority", "Regular"),
                     disk_ids=[_lower(d) for d in disks if d],
                     nic_ids=[_lower(n["id"]) for n in (props.get("networkProfile") or {}).get("networkInterfaces", [])],
                     time_created=props.get("timeCreated"), memory_gb=vm_memory_gb(size))
        elif rtype == "azure.disk":
            a.update(size_gb=props.get("diskSizeGB", 0), disk_state=_lower(props.get("diskState")),
                     managed_by=_lower(r.get("managedBy")), time_created=props.get("timeCreated"),
                     last_ownership_update=props.get("LastOwnershipUpdateTime") or props.get("lastOwnershipUpdateTime"),
                     provisioned_iops=props.get("diskIOPSReadWrite"))
        elif rtype == "azure.snapshot":
            a.update(size_gb=props.get("diskSizeGB", 0), time_created=props.get("timeCreated"),
                     source_id=_lower((props.get("creationData") or {}).get("sourceResourceId")),
                     incremental=bool(props.get("incremental")))
        elif rtype == "azure.public_ip":
            a.update(ip_configuration_id=_lower((props.get("ipConfiguration") or {}).get("id")),
                     nat_gateway_id=_lower((props.get("natGateway") or {}).get("id")),
                     allocation=props.get("publicIPAllocationMethod", ""), ip_address=props.get("ipAddress", ""))
            a["associated"] = bool(a["ip_configuration_id"] or a["nat_gateway_id"])
        elif rtype == "azure.nic":
            a.update(vm_id=_lower((props.get("virtualMachine") or {}).get("id")),
                     public_ip_ids=[_lower(((c.get("properties") or {}).get("publicIPAddress") or {}).get("id"))
                                    for c in props.get("ipConfigurations", [])
                                    if ((c.get("properties") or {}).get("publicIPAddress") or {}).get("id")])
        elif rtype == "azure.lb":
            pools = props.get("backendAddressPools", [])
            a["backend_count"] = sum(len((p.get("properties") or {}).get("backendIPConfigurations") or []) +
                                     len((p.get("properties") or {}).get("loadBalancerBackendAddresses") or [])
                                     for p in pools)
            a["rule_count"] = len(props.get("loadBalancingRules", []))
        elif rtype == "azure.appgw":
            pools = props.get("backendAddressPools", [])
            a["backend_count"] = sum(len((p.get("properties") or {}).get("backendAddresses") or []) +
                                     len((p.get("properties") or {}).get("backendIPConfigurations") or [])
                                     for p in pools)
        elif rtype == "azure.app_service_plan":
            a.update(tier=sku.get("tier", ""), capacity=sku.get("capacity", 1),
                     number_of_sites=props.get("numberOfSites", 0), status=props.get("status", ""))
        elif rtype == "azure.web_app":
            a.update(server_farm_id=_lower(props.get("serverFarmId")), state=props.get("state", ""))
        elif rtype == "azure.vmss":
            a.update(capacity=sku.get("capacity", 0), aks_pool=res.tags.get("aks-managed-poolname", ""))
            res.sku = sku.get("name", "")
        elif rtype == "azure.aks":
            pools = props.get("agentPoolProfiles", [])
            a.update(node_resource_group=_lower(props.get("nodeResourceGroup")),
                     power_state=_lower((props.get("powerState") or {}).get("code")),
                     node_pools=[{"name": p.get("name"), "count": p.get("count"), "vm_size": p.get("vmSize"),
                                  "mode": p.get("mode"), "autoscale": p.get("enableAutoScaling", False),
                                  "min": p.get("minCount"), "max": p.get("maxCount")} for p in pools],
                     node_count=sum(p.get("count") or 0 for p in pools))
        elif rtype == "azure.log_workspace":
            res.sku = (props.get("sku") or {}).get("name", "")
            a.update(customer_id=props.get("customerId"), retention_days=props.get("retentionInDays", 30),
                     pricing_sku=res.sku,
                     daily_cap_gb=(props.get("workspaceCapping") or {}).get("dailyQuotaGb"))
        return res

    def _relationships(self, resources: list[Resource], by_id: dict[str, Resource]) -> list[Relationship]:
        rels: list[Relationship] = []
        nic_to_vm = {}
        for r in resources:
            a = r.attributes
            if r.type == "azure.vm":
                rels += [Relationship(d, r.id, "attached_to") for d in a.get("disk_ids", [])]
                rels += [Relationship(n, r.id, "nic_of") for n in a.get("nic_ids", [])]
            elif r.type == "azure.disk" and a.get("managed_by"):
                rels.append(Relationship(r.id, a["managed_by"], "attached_to"))
            elif r.type == "azure.nic":
                if a.get("vm_id"):
                    nic_to_vm[r.id] = a["vm_id"]
                rels += [Relationship(p, r.id, "ip_of") for p in a.get("public_ip_ids", [])]
            elif r.type == "azure.web_app" and a.get("server_farm_id"):
                rels.append(Relationship(r.id, a["server_farm_id"], "uses_plan"))
            elif r.type == "azure.snapshot" and a.get("source_id"):
                rels.append(Relationship(r.id, a["source_id"], "snapshot_of"))
            elif r.type == "azure.aks":
                ng = a.get("node_resource_group")
                for other in resources:
                    if other.type == "azure.vmss" and _lower(other.group) == ng:
                        rels.append(Relationship(other.id, r.id, "node_pool_of"))
        # public IP -> VM via NIC
        for rel in list(rels):
            if rel.kind == "ip_of" and rel.dst in nic_to_vm:
                rels.append(Relationship(rel.src, nic_to_vm[rel.dst], "ip_of"))
        seen = set()
        unique = []
        for rel in rels:
            k = (rel.src, rel.dst, rel.kind)
            if k not in seen:
                seen.add(k)
                unique.append(rel)
        for rel in unique:
            for a, b in ((rel.src, rel.dst), (rel.dst, rel.src)):
                if a in by_id and b not in by_id[a].related:
                    by_id[a].related.append(b)
        return unique

    def _costs(self, raw: Path, by_id: dict[str, Resource]) -> tuple[list[CostRecord], str]:
        records, currencies = [], set()
        for f in sorted((raw / "costs").glob("*_by_resource.json")):
            sub = f.name.split("_by_resource")[0]
            data = json.loads(f.read_text())
            idx = {c["name"]: i for i, c in enumerate(data["columns"])}
            for row in data["rows"]:
                rid = _lower(row[idx["ResourceId"]])
                res = by_id.get(rid)
                cur = row[idx["Currency"]] if "Currency" in idx else "USD"
                currencies.add(cur)
                records.append(CostRecord(
                    day=_usage_date(row[idx["UsageDate"]]), cost=float(row[idx["Cost"]]),
                    service=row[idx["ServiceName"]] or "Unknown", account=sub,
                    region=res.region if res else "", resource_id=rid, provider="azure",
                    tags=res.tags if res else {}, currency=cur,
                ))
        currency = sorted(currencies)[0] if len(currencies) == 1 else ("MIXED" if currencies else "USD")
        return records, currency

    def _pricing_model(self, raw: Path) -> list[dict]:
        out = []
        for f in sorted((raw / "costs").glob("*_by_pricing_model.json")):
            data = json.loads(f.read_text())
            idx = {c["name"]: i for i, c in enumerate(data["columns"])}
            for row in data["rows"]:
                out.append({"day": _usage_date(row[idx["UsageDate"]]), "service": row[idx["ServiceName"]],
                            "pricing_model": row[idx["PricingModel"]], "cost": float(row[idx["Cost"]])})
        return out

    def _attach_metrics(self, raw: Path, by_id: dict[str, Resource], cfg: Config) -> None:
        for f in sorted((raw / "metrics").glob("*.json")):
            payload = json.loads(f.read_text())
            res = by_id.get(_lower(payload.get("resource_id")))
            resp = payload.get("response") or {}
            if not res:
                continue
            hours = _interval_hours(resp.get("interval", "PT1H"))
            series = {}
            for metric in resp.get("value", []):
                name = (metric.get("name") or {}).get("value", "")
                points = [p for ts in metric.get("timeseries", []) for p in ts.get("data", [])]
                series[name] = points
            m = res.metrics
            m["metrics_window_days"] = cfg.metric_days
            expected = max(1, int(cfg.metric_days * 24 / hours))

            cpu_name = "Percentage CPU" if "Percentage CPU" in series else "CpuPercentage"
            if cpu_name in series:
                avgs = [p["average"] for p in series[cpu_name] if p.get("average") is not None]
                maxs = [p.get("maximum", p["average"]) for p in series[cpu_name] if p.get("average") is not None]
                if avgs:
                    m.update(cpu_avg=statistics.fmean(avgs), cpu_p95=_p95(avgs), cpu_max=max(maxs),
                             uptime_share=min(1.0, len(avgs) / expected))
                else:
                    m["uptime_share"] = 0.0
            if "Available Memory Bytes" in series:
                total = res.attributes.get("memory_gb")
                avail = [p["average"] for p in series["Available Memory Bytes"] if p.get("average") is not None]
                if avail and total:
                    used = [max(0.0, 100 * (1 - (a / 1024 ** 3) / total)) for a in avail]
                    m["mem_used_p95"] = _p95(used)
                elif avail:
                    m["mem_available_min_gb"] = min(avail) / 1024 ** 3
            if "MemoryPercentage" in series:
                vals = [p["average"] for p in series["MemoryPercentage"] if p.get("average") is not None]
                if vals:
                    m["mem_used_p95"] = _p95(vals)
            disk_r = series.get("Composite Disk Read Operations/sec")
            disk_w = series.get("Composite Disk Write Operations/sec")
            if disk_r is not None or disk_w is not None:
                totals = defaultdict(float)
                for pts in (disk_r or [], disk_w or []):
                    for p in pts:
                        if p.get("average") is not None:
                            totals[p["timeStamp"]] += p["average"]
                vals = list(totals.values())
                m.update(iops_p95=_p95(vals), iops_max=max(vals, default=0.0))

    def _advisor(self, raw: Path, by_id: dict[str, Resource]) -> list[dict]:
        out = []
        for f in sorted((raw / "advisor").glob("*.json")):
            for rec in json.loads(f.read_text()) or []:
                ext = rec.get("extendedProperties") or {}
                rid = _lower((rec.get("resourceMetadata") or {}).get("resourceId"))
                monthly = None
                if ext.get("annualSavingsAmount"):
                    monthly = float(ext["annualSavingsAmount"]) / 12
                elif ext.get("savingsAmount"):
                    monthly = float(ext["savingsAmount"])
                item = {
                    "id": rec.get("id", ""), "resource_id": rid, "impact": rec.get("impact", ""),
                    "problem": (rec.get("shortDescription") or {}).get("problem", ""),
                    "solution": (rec.get("shortDescription") or {}).get("solution", ""),
                    "monthly_savings": monthly, "currency": ext.get("savingsCurrency", "USD"),
                    "type_id": rec.get("recommendationTypeId", ""),
                    "current_sku": ext.get("currentSku", ""), "target_sku": ext.get("targetSku", ""),
                }
                out.append(item)
                if rid in by_id:
                    by_id[rid].attributes.setdefault("advisor", []).append(item["solution"])
        return out

    def _logging(self, raw: Path, by_id: dict[str, Resource]) -> dict:
        usage: dict[str, dict] = defaultdict(lambda: {"tables": {}, "table_settings": {}})
        for f in sorted((raw / "logging").glob("*_usage.json")):
            payload = json.loads(f.read_text())
            ws = payload["workspace_id"]
            for row in payload.get("rows") or []:
                usage[ws]["tables"][row["DataType"]] = float(row["BillableGB"])
        for f in sorted((raw / "logging").glob("*_tables.json")):
            payload = json.loads(f.read_text())
            ws = payload["workspace_id"]
            for t in payload.get("tables") or []:
                usage[ws]["table_settings"][t["name"]] = {
                    "plan": t.get("plan", "Analytics"), "retention_days": t.get("retentionInDays"),
                    "total_retention_days": t.get("totalRetentionInDays")}
        for ws, data in usage.items():
            if ws in by_id:
                by_id[ws].attributes["ingest_gb_30d"] = sum(data["tables"].values())
        return dict(usage)


def _read(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _usage_date(value) -> date:
    s = str(value)
    if len(s) >= 8 and s[:8].isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return date.fromisoformat(s[:10])
