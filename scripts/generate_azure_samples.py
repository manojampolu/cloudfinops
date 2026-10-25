"""Generate a deterministic Azure sample run folder in CLI output shapes.

    python scripts/generate_azure_samples.py

Writes src/cloudfinops/samples/azure/{scope.json, raw/...} which `cloudfinops demo --provider azure` loads
through the same normalization code as a live run.
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cloudfinops.pricing_azure import (  # noqa: E402
    APP_SERVICE_HOURLY, APPGW_V2_HOURLY, LB_STANDARD_HOURLY, PUBLIC_IP_HOURLY, SNAPSHOT_GB_MONTH, VM_HOURLY,
    disk_tier_price, vm_downsize, vm_memory_gb,
)

OUT = ROOT / "src" / "cloudfinops" / "samples" / "azure"
END = date(2026, 10, 15)
DAYS = 60
METRIC_DAYS = 14
REGION = "eastus"
PROD = ("11111111-aaaa-4aaa-8aaa-111111111111", "acme-prod")
NONPROD = ("22222222-bbbb-4bbb-8bbb-222222222222", "acme-nonprod")
rnd = random.Random(7)

resources: list[dict] = []
daily_cost: dict[str, tuple[str, str, float]] = {}  # resource id -> (sub, service, cost/day)
metrics: dict[str, dict] = {}  # resource id -> metric spec
pricing_model_reserved: set[str] = set()

T_VM, T_DISK, T_SNAP = "microsoft.compute/virtualmachines", "microsoft.compute/disks", "microsoft.compute/snapshots"
T_NIC, T_PIP = "microsoft.network/networkinterfaces", "microsoft.network/publicipaddresses"
T_LB, T_AGW = "microsoft.network/loadbalancers", "microsoft.network/applicationgateways"
T_PLAN, T_SITE = "microsoft.web/serverfarms", "microsoft.web/sites"
T_VMSS, T_AKS = "microsoft.compute/virtualmachinescalesets", "microsoft.containerservice/managedclusters"
T_LAW, T_SA = "microsoft.operationalinsights/workspaces", "microsoft.storage/storageaccounts"
T_KV = "microsoft.keyvault/vaults"
T_ACA, T_ENV = "microsoft.app/containerapps", "microsoft.app/managedenvironments"
T_FW, T_BAS, T_VGW = "microsoft.network/azurefirewalls", "microsoft.network/bastionhosts", "microsoft.network/virtualnetworkgateways"
T_CONN, T_DDOS, T_NAT = "microsoft.network/connections", "microsoft.network/ddosprotectionplans", "microsoft.network/natgateways"
T_VNET, T_PG, T_MY = "microsoft.network/virtualnetworks", "microsoft.dbforpostgresql/flexibleservers", "microsoft.dbformysql/flexibleservers"
T_SQLVM, T_SQLSRV, T_SQLDB = "microsoft.sqlvirtualmachine/sqlvirtualmachines", "microsoft.sql/servers", "microsoft.sql/servers/databases"

ARM_TYPE = {T_VM: "Microsoft.Compute/virtualMachines", T_DISK: "Microsoft.Compute/disks",
            T_SNAP: "Microsoft.Compute/snapshots", T_NIC: "Microsoft.Network/networkInterfaces",
            T_PIP: "Microsoft.Network/publicIPAddresses", T_LB: "Microsoft.Network/loadBalancers",
            T_AGW: "Microsoft.Network/applicationGateways", T_PLAN: "Microsoft.Web/serverfarms",
            T_SITE: "Microsoft.Web/sites", T_VMSS: "Microsoft.Compute/virtualMachineScaleSets",
            T_AKS: "Microsoft.ContainerService/managedClusters", T_LAW: "Microsoft.OperationalInsights/workspaces",
            T_SA: "Microsoft.Storage/storageAccounts", T_KV: "Microsoft.KeyVault/vaults",
            T_ACA: "Microsoft.App/containerApps", T_ENV: "Microsoft.App/managedEnvironments",
            T_FW: "Microsoft.Network/azureFirewalls", T_BAS: "Microsoft.Network/bastionHosts",
            T_VGW: "Microsoft.Network/virtualNetworkGateways", T_CONN: "Microsoft.Network/connections",
            T_DDOS: "Microsoft.Network/ddosProtectionPlans", T_NAT: "Microsoft.Network/natGateways",
            T_VNET: "Microsoft.Network/virtualNetworks", T_PG: "Microsoft.DBforPostgreSQL/flexibleServers",
            T_MY: "Microsoft.DBforMySQL/flexibleServers", T_SQLVM: "Microsoft.SqlVirtualMachine/sqlVirtualMachines",
            T_SQLSRV: "Microsoft.Sql/servers", T_SQLDB: "Microsoft.Sql/servers/databases"}


def rid(sub, rg, rtype, name):
    return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/{ARM_TYPE[rtype]}/{name}"


def add(sub, rg, rtype, name, props=None, sku=None, tags=None, managed_by=None, kind=None, service=None, cost_day=0.0):
    r = {"id": rid(sub[0], rg, rtype, name), "name": name, "type": rtype, "location": REGION, "resourceGroup": rg,
         "subscriptionId": sub[0], "sku": sku, "kind": kind, "managedBy": managed_by, "tags": tags or None,
         "properties": props or {}}
    resources.append(r)
    if cost_day:
        daily_cost[r["id"].lower()] = (sub[0], service, cost_day)
    return r["id"]


def disk(sub, rg, name, gb, sku="Premium_LRS", attached_to=None, tags=None, iops=None, created="2025-03-01"):
    state = "Attached" if attached_to else "Unattached"
    props = {"diskSizeGB": gb, "diskState": state, "timeCreated": f"{created}T10:00:00Z",
             "diskIOPSReadWrite": 500 if sku.startswith("Premium") else 500}
    if not attached_to:
        props["LastOwnershipUpdateTime"] = f"{created}T10:00:00Z"
    d = add(sub, rg, T_DISK, name, props, {"name": sku, "tier": sku.split("_")[0]}, tags, attached_to,
            service="Storage", cost_day=disk_tier_price(gb, sku) / 30)
    if iops is not None and attached_to and sku.startswith("Premium"):
        metrics[d.lower()] = {"kind": "disk", "iops": iops}
    return d


def vm(sub, rg, name, size, cpu_avg=None, mem_used=None, power="running", tags=None, data_disks=(), disk_sku="Premium_LRS",
       data_iops=None, os_gb=128, reserved=False, pip=False, os_type="Linux", license=None, extra_hourly=0.0):
    vm_id = rid(sub[0], rg, T_VM, name)
    os_disk = disk(sub, rg, f"{name}_OsDisk_1", os_gb, disk_sku, vm_id, tags)
    datas = [disk(sub, rg, f"{name}-data{i + 1}", gb, disk_sku, vm_id, tags, iops=data_iops)
             for i, gb in enumerate(data_disks)]
    nic_id = rid(sub[0], rg, T_NIC, f"{name}-nic")
    ip_cfg = {"name": "ipconfig1", "properties": {}}
    if pip:
        pip_id = add(sub, rg, T_PIP, f"{name}-pip", {"ipConfiguration": {"id": nic_id + "/ipConfigurations/ipconfig1"},
                                                    "publicIPAllocationMethod": "Static"}, {"name": "Standard"}, tags,
                     service="Virtual Network", cost_day=PUBLIC_IP_HOURLY * 24)
        ip_cfg["properties"]["publicIPAddress"] = {"id": pip_id}
    add(sub, rg, T_NIC, f"{name}-nic", {"virtualMachine": {"id": vm_id}, "ipConfigurations": [ip_cfg]}, tags=tags)
    hourly = VM_HOURLY[size.lower()] + extra_hourly
    compute = 0.0 if power == "deallocated" else hourly * 24 * (0.6 if reserved else 1.0)
    add(sub, rg, T_VM, name, {
        "hardwareProfile": {"vmSize": size},
        **({"licenseType": license} if license else {}),
        "storageProfile": {"osDisk": {"osType": os_type, "managedDisk": {"id": os_disk}},
                           "dataDisks": [{"lun": i, "managedDisk": {"id": d}} for i, d in enumerate(datas)]},
        "networkProfile": {"networkInterfaces": [{"id": nic_id}]},
        "extended": {"instanceView": {"powerState": {"code": f"PowerState/{power}"}}},
        "timeCreated": "2024-06-01T09:00:00Z"}, tags=tags, service="Virtual Machines", cost_day=compute)
    if reserved:
        pricing_model_reserved.add(vm_id.lower())
    if power == "running" and cpu_avg is not None:
        metrics[vm_id.lower()] = {"kind": "vm", "cpu": cpu_avg, "mem": mem_used, "mem_gb": vm_memory_gb(size)}
    return vm_id


def container_app(sub, rg, env, name, cpu, mem_gi, min_r, max_r, cost_day, tags, metric):
    app = add(sub, rg, T_ACA, name, {
        "managedEnvironmentId": env, "workloadProfileName": "Consumption", "runningStatus": "Running",
        "configuration": {"ingress": {"external": True, "targetPort": 8080}},
        "template": {"containers": [{"name": "app", "image": f"acmeacr.azurecr.io/{name}:1.4",
                                     "resources": {"cpu": cpu, "memory": f"{mem_gi}Gi"}}],
                     "scale": {"minReplicas": min_r, "maxReplicas": max_r,
                               "rules": [{"name": "http", "http": {"metadata": {"concurrentRequests": "50"}}}]}}},
        tags=tags, service="Azure Container Apps", cost_day=cost_day)
    metrics[app.lower()] = {"kind": "aca", "vcpu": cpu, **metric}
    return app


def build():
    prod_tags = lambda app, owner: {"owner": owner, "environment": "prod", "cost-center": "cc-100", "app": app}  # noqa
    dev_tags = {"owner": "platform-team", "environment": "dev", "cost-center": "cc-200"}

    # ---------------------------------------------------------------- prod
    web_backends = []
    for i in range(1, 21):
        v = vm(PROD, "rg-web", f"vm-web-{i:02d}", "Standard_D4s_v5", cpu_avg=35, mem_used=55,
               tags=prod_tags("storefront", "web-team"), data_disks=(256, 256), data_iops=420)
        web_backends.append(v)
    lb_pip = add(PROD, "rg-web", T_PIP, "pip-lb-web", {"ipConfiguration": {"id": "lbfe"}, "publicIPAllocationMethod": "Static"},
                 {"name": "Standard"}, prod_tags("storefront", "web-team"), service="Virtual Network", cost_day=PUBLIC_IP_HOURLY * 24)
    add(PROD, "rg-web", T_LB, "lb-web", {"backendAddressPools": [{"name": "web", "properties": {
        "backendIPConfigurations": [{"id": b + "-nic/ipconfig1"} for b in web_backends]}}],
        "loadBalancingRules": [{"name": "https"}], "frontendIPConfigurations": [{"properties": {"publicIPAddress": {"id": lb_pip}}}]},
        {"name": "Standard"}, prod_tags("storefront", "web-team"), service="Load Balancer", cost_day=LB_STANDARD_HOURLY * 24 + 4.0)

    for i in range(1, 11):
        vm(PROD, "rg-api", f"vm-api-{i:02d}", "Standard_D8s_v5", cpu_avg=9, mem_used=35 if i <= 6 else None,
           tags=prod_tags("orders-api", "api-team"), data_disks=(512,) * 4, data_iops=35)
    for i in range(1, 9):
        vm(PROD, "rg-db", f"vm-sql-{i:02d}", "Standard_E8s_v5", cpu_avg=12, mem_used=86,
           tags={"owner": "data-team", "environment": "prod"}, data_disks=(1024,) * 6, data_iops=2200, reserved=True)
    for i in range(1, 4):
        vm(PROD, "rg-batch", f"vm-batch-{i:02d}", "Standard_D16s_v5", cpu_avg=1.0, mem_used=8)
    for i in range(1, 3):
        vm(PROD, "rg-legacy", f"vm-legacy-{i:02d}", "Standard_D4s_v3", power="deallocated", data_disks=(512,),
           tags={"owner": "finance-it"})
    vm(PROD, "rg-legacy", "vm-legacy-report", "Standard_D2s_v3", power="stopped", tags={"owner": "finance-it"})

    for i in range(1, 19):  # orphaned disks
        sku = "Premium_LRS" if i % 3 else "StandardSSD_LRS"
        disk(PROD, rnd.choice(["rg-web", "rg-api", "rg-legacy", "rg-migration"]), f"disk-orphan-{i:02d}",
             rnd.choice([128, 256, 512, 1024]), sku, created=f"2026-0{rnd.randint(1, 7)}-1{rnd.randint(0, 9)}")
    for i in range(1, 6):
        add(PROD, "rg-network", T_PIP, f"pip-unused-{i:02d}", {"publicIPAllocationMethod": "Static"}, {"name": "Standard"},
            service="Virtual Network", cost_day=PUBLIC_IP_HOURLY * 24)
    add(PROD, "rg-network", T_LB, "lb-old-partner", {"backendAddressPools": [{"name": "pool", "properties": {}}],
        "loadBalancingRules": [{"name": "r"}]}, {"name": "Standard"}, {"owner": "network-team"},
        service="Load Balancer", cost_day=LB_STANDARD_HOURLY * 24)
    add(PROD, "rg-network", T_AGW, "agw-legacy-portal", {"backendAddressPools": [{"name": "be", "properties": {"backendAddresses": []}}],
        "sku": {"name": "Standard_v2"}}, {"name": "Standard_v2"}, {"owner": "network-team"},
        service="Application Gateway", cost_day=APPGW_V2_HOURLY * 24)

    for i in range(1, 31):
        created = END - timedelta(days=rnd.choice([20, 45, 75, 120, 200, 400]) if i > 6 else 10)
        src = rid(PROD[0], "rg-db", T_DISK, f"vm-sql-0{(i % 8) + 1}-data1") if i % 4 else rid(PROD[0], "rg-old", T_DISK, f"disk-deleted-{i}")
        add(PROD, "rg-backup", T_SNAP, f"snap-sql-{i:02d}", {"diskSizeGB": 1024 if i % 4 else 256,
            "timeCreated": f"{created.isoformat()}T02:00:00Z", "creationData": {"sourceResourceId": src}, "incremental": False},
            {"name": "Standard_LRS"}, {"owner": "data-team"} if i % 2 else None,
            service="Storage", cost_day=SNAPSHOT_GB_MONTH * (1024 if i % 4 else 256) / 30)

    plan = add(PROD, "rg-app", T_PLAN, "asp-prod-web", {"numberOfSites": 6, "status": "Ready"},
               {"name": "P2v3", "tier": "PremiumV3", "capacity": 4}, prod_tags("portal", "web-team"), kind="linux",
               service="Azure App Service", cost_day=APP_SERVICE_HOURLY["p2v3"] * 24 * 4)
    metrics[plan.lower()] = {"kind": "plan", "cpu": 10, "mem": 40}
    for i in range(1, 7):
        add(PROD, "rg-app", T_SITE, f"app-portal-{i}", {"serverFarmId": plan, "state": "Running"}, tags=prod_tags("portal", "web-team"))
    add(PROD, "rg-app", T_PLAN, "asp-campaign-2025", {"numberOfSites": 0, "status": "Ready"},
        {"name": "P1v3", "tier": "PremiumV3", "capacity": 1}, {"owner": "marketing"}, kind="linux",
        service="Azure App Service", cost_day=APP_SERVICE_HOURLY["p1v3"] * 24)

    ng = "MC_rg-aks_aks-prod_eastus"
    add(PROD, "rg-aks", T_AKS, "aks-prod", {"nodeResourceGroup": ng, "powerState": {"code": "Running"},
        "agentPoolProfiles": [{"name": "system", "count": 3, "vmSize": "Standard_D4s_v5", "mode": "System"},
                              {"name": "user", "count": 6, "vmSize": "Standard_D8s_v5", "mode": "User",
                               "enableAutoScaling": True, "minCount": 4, "maxCount": 10}]},
        {"name": "Base", "tier": "Standard"}, prod_tags("platform", "platform-team"), service="Azure Kubernetes Service", cost_day=2.4)
    for pool, size, n in (("system", "Standard_D4s_v5", 3), ("user", "Standard_D8s_v5", 6)):
        add(PROD, ng, T_VMSS, f"aks-{pool}-12345678-vmss", {}, {"name": size, "capacity": n},
            {"aks-managed-poolName": pool}, service="Virtual Machines", cost_day=VM_HOURLY[size.lower()] * 24 * n)

    add(PROD, "rg-monitoring", T_LAW, "law-prod", {"customerId": "aaaaaaaa-0000-0000-0000-000000000001",
        "retentionInDays": 180, "sku": {"name": "PerGB2018"}}, tags=prod_tags("observability", "platform-team"),
        service="Log Analytics", cost_day=0)  # cost set from usage below
    for i, name in enumerate(["stacmeprodlogs", "stacmeproddata", "stacmebackup", "stacmeweb"]):
        add(PROD, "rg-storage", T_SA, name, {}, {"name": "Standard_GRS" if i == 2 else "Standard_LRS"},
            prod_tags("shared", "platform-team") if i != 1 else None, kind="StorageV2",
            service="Storage", cost_day=[18.0, 42.0, 31.0, 4.5][i])
    add(PROD, "rg-security", T_KV, "kv-acme-prod", {}, {"name": "standard"}, prod_tags("shared", "security-team"),
        service="Key Vault", cost_day=0.4)

    # ---------------------------------------------------------------- non-prod
    for i in range(1, 15):
        vm(NONPROD, "rg-dev-app", f"vm-dev-{i:02d}", "Standard_D4s_v5", cpu_avg=26, mem_used=45, tags=dev_tags)
    for i in range(1, 7):
        vm(NONPROD, "rg-test", f"vm-test-{i:02d}", "Standard_D2s_v5", cpu_avg=10, mem_used=30,
           tags={**dev_tags, "environment": "test"})
    for i in range(1, 5):
        vm(NONPROD, "rg-uat-erp", f"vm-erp-uat-{i:02d}", "Standard_B4ms", cpu_avg=22, mem_used=50, disk_sku="StandardSSD_LRS")
    for i in range(1, 11):
        disk(NONPROD, "rg-dev-app", f"disk-dev-scratch-{i:02d}", rnd.choice([64, 128, 256]),
             rnd.choice(["Premium_LRS", "StandardSSD_LRS"]), created="2026-02-0" + str(rnd.randint(1, 9)))

    ng = "MC_rg-dev-aks_aks-dev_eastus"
    add(NONPROD, "rg-dev-aks", T_AKS, "aks-dev", {"nodeResourceGroup": ng, "powerState": {"code": "Running"},
        "agentPoolProfiles": [{"name": "system", "count": 2, "vmSize": "Standard_D4s_v5", "mode": "System"},
                              {"name": "user", "count": 4, "vmSize": "Standard_D4s_v5", "mode": "User"}]},
        {"name": "Base", "tier": "Free"}, dev_tags, service="Azure Kubernetes Service")
    for pool, n in (("system", 2), ("user", 4)):
        add(NONPROD, ng, T_VMSS, f"aks-{pool}-87654321-vmss", {}, {"name": "Standard_D4s_v5", "capacity": n},
            {"aks-managed-poolName": pool}, service="Virtual Machines", cost_day=VM_HOURLY["standard_d4s_v5"] * 24 * n)

    plan = add(NONPROD, "rg-dev-app", T_PLAN, "asp-dev", {"numberOfSites": 3, "status": "Ready"},
               {"name": "S2", "tier": "Standard", "capacity": 2}, dev_tags, kind="linux",
               service="Azure App Service", cost_day=APP_SERVICE_HOURLY["s2"] * 24 * 2)
    metrics[plan.lower()] = {"kind": "plan", "cpu": 6, "mem": 35}
    for i in range(1, 4):
        add(NONPROD, "rg-dev-app", T_SITE, f"app-dev-{i}", {"serverFarmId": plan, "state": "Running"}, tags=dev_tags)
    env = add(PROD, "rg-apps", T_ENV, "cae-prod", {"workloadProfiles": [{"name": "Consumption"}]},
              tags=prod_tags("platform", "platform-team"))
    container_app(PROD, "rg-apps", env, "ca-orders-worker", 1.0, 2, 2, 10, 5.6, prod_tags("orders", "api-team"),
                  {"cpu": 30, "replicas": 3, "requests_per_h": 900})
    container_app(PROD, "rg-apps", env, "ca-legacy-pdf", 1.0, 2, 1, 3, 1.4, prod_tags("billing", "finance-it"),
                  {"cpu": 6, "replicas": 1, "requests_per_h": 0.1})
    container_app(PROD, "rg-apps", env, "ca-poc-ocr", 2.0, 4, 1, 2, 2.2, {"owner": "data-team"},
                  {"cpu": 0.4, "replicas": 1, "requests_per_h": 0.02})
    dev_env = add(NONPROD, "rg-dev-app", T_ENV, "cae-dev", {}, tags=dev_tags)
    container_app(NONPROD, "rg-dev-app", dev_env, "ca-dev-portal", 0.5, 1, 1, 4, 1.1, dev_tags,
                  {"cpu": 15, "replicas": 1, "requests_per_h": 40})

    # ---- licensing: Windows + SQL Server VMs, Azure SQL Database
    for i in (1, 2):
        vm(PROD, "rg-erp", f"vm-win-erp-{i:02d}", "Standard_D4s_v5", cpu_avg=45, mem_used=60, os_type="Windows",
           tags=prod_tags("erp", "erp-team"), extra_hourly=4 * 0.046)
    sqlwin = vm(PROD, "rg-db", "vm-sqlwin-01", "Standard_E4s_v5", cpu_avg=40, mem_used=70, os_type="Windows",
                license="Windows_Server", tags={"owner": "data-team", "environment": "prod"}, extra_hourly=4 * 0.375)
    add(PROD, "rg-db", T_SQLVM, "vm-sqlwin-01", {"virtualMachineResourceId": sqlwin, "sqlServerLicenseType": "PAYG",
        "sqlImageSku": "Enterprise", "sqlImageOffer": "SQL2022-WS2022"}, tags={"owner": "data-team"})
    sqldev = vm(NONPROD, "rg-dev-data", "vm-sql-dev-01", "Standard_D4s_v5", cpu_avg=25, mem_used=50, os_type="Windows",
                tags=dev_tags, extra_hourly=4 * (0.046 + 0.10))
    add(NONPROD, "rg-dev-data", T_SQLVM, "vm-sql-dev-01", {"virtualMachineResourceId": sqldev,
        "sqlServerLicenseType": "PAYG", "sqlImageSku": "Standard"}, tags=dev_tags)
    srv = add(PROD, "rg-db", T_SQLSRV, "sql-acme-prod", {}, tags={"owner": "data-team"})
    add(PROD, "rg-db", T_SQLDB, "sql-acme-prod/sqldb-orders", {"licenseType": "LicenseIncluded"},
        {"name": "GP_Gen5", "tier": "GeneralPurpose", "family": "Gen5", "capacity": 8}, {"owner": "api-team"},
        service="SQL Database", cost_day=8 * 0.505 * 24)
    add(PROD, "rg-db", T_SQLDB, "sql-acme-prod/sqldb-reports", {"licenseType": ""},
        {"name": "GP_S_Gen5", "tier": "GeneralPurpose", "family": "Gen5", "capacity": 2}, {"owner": "data-team"},
        service="SQL Database", cost_day=6.0)

    # ---- network appliances
    hub = add(PROD, "rg-network", T_VNET, "vnet-hub-prod", {}, tags={"owner": "network-team"})
    add(PROD, "rg-network", T_DDOS, "ddos-plan-acme", {"virtualNetworks": [{"id": hub}]}, tags={"owner": "network-team"},
        service="Azure DDOS Protection", cost_day=2944 / 30)
    fw = add(PROD, "rg-network", T_FW, "afw-legacy-dmz", {"sku": {"name": "AZFW_VNet", "tier": "Standard"},
             "ipConfigurations": [{"name": "ip"}]}, tags={"owner": "network-team"}, service="Azure Firewall", cost_day=1.25 * 24)
    metrics[fw.lower()] = {"kind": "fw", "gb_per_6h": 0.004}
    fw = add(NONPROD, "rg-hub-nonprod", T_FW, "afw-hub-nonprod", {"sku": {"name": "AZFW_VNet", "tier": "Standard"},
             "ipConfigurations": [{"name": "ip"}]}, tags={"owner": "network-team"}, service="Azure Firewall", cost_day=1.25 * 24)
    metrics[fw.lower()] = {"kind": "fw", "gb_per_6h": 6}
    bas = add(PROD, "rg-network", T_BAS, "bas-prod", {}, {"name": "Basic"}, {"owner": "network-team"},
              service="Azure Bastion", cost_day=0.19 * 24)
    metrics[bas.lower()] = {"kind": "bastion", "sessions": 0}
    bas = add(NONPROD, "rg-hub-nonprod", T_BAS, "bas-dev", {}, {"name": "Standard"}, {"owner": "network-team"},
              service="Azure Bastion", cost_day=0.29 * 24)
    metrics[bas.lower()] = {"kind": "bastion", "sessions": 2}
    add(PROD, "rg-network", T_VGW, "vgw-old-branch", {"gatewayType": "Vpn", "vpnType": "RouteBased",
        "sku": {"name": "VpnGw1", "tier": "VpnGw1"}}, tags={"owner": "network-team"}, service="VPN Gateway", cost_day=0.19 * 24)
    vgw = add(PROD, "rg-network", T_VGW, "vgw-hub", {"gatewayType": "Vpn", "vpnType": "RouteBased",
              "sku": {"name": "VpnGw1", "tier": "VpnGw1"}}, tags={"owner": "network-team"}, service="VPN Gateway", cost_day=0.19 * 24)
    add(PROD, "rg-network", T_CONN, "conn-hq", {"virtualNetworkGateway1": {"id": vgw}, "connectionStatus": "Connected"},
        tags={"owner": "network-team"})
    add(PROD, "rg-network", T_NAT, "ng-unused", {"subnets": []}, {"name": "Standard"}, {"owner": "network-team"},
        service="NAT Gateway", cost_day=0.045 * 24)

    # ---- PostgreSQL / MySQL flexible servers
    pg = add(NONPROD, "rg-dev-data", T_PG, "psql-dev-orders", {"state": "Ready", "version": "16",
             "storage": {"storageSizeGB": 256}, "highAvailability": {"mode": "ZoneRedundant"}},
             {"name": "Standard_D4ds_v4", "tier": "GeneralPurpose"}, dev_tags, service="Azure Database for PostgreSQL",
             cost_day=0.355 * 24 * 2 + 1.0)
    metrics[pg.lower()] = {"kind": "dbflex", "cpu": 8, "mem": 30, "connections": 12}
    my = add(PROD, "rg-legacy", T_MY, "mysql-legacy-cms", {"state": "Ready", "version": "8.0.21",
             "storage": {"storageSizeGB": 128}, "highAvailability": {"mode": "Disabled"}},
             {"name": "Standard_D2ds_v4", "tier": "GeneralPurpose"}, {"owner": "finance-it"},
             service="Azure Database for MySQL", cost_day=0.171 * 24 + 0.5)
    metrics[my.lower()] = {"kind": "dbflex", "cpu": 2, "mem": 20, "connections": 0}

    # ---- standalone VM scale set
    vmss = add(PROD, "rg-render", T_VMSS, "vmss-render", {}, {"name": "Standard_D8s_v5", "capacity": 4},
               prod_tags("render", "media-team"), service="Virtual Machines", cost_day=VM_HOURLY["standard_d8s_v5"] * 24 * 4)
    metrics[vmss.lower()] = {"kind": "vm", "cpu": 7, "mem": None, "mem_gb": None}

    add(NONPROD, "rg-dev-monitoring", T_LAW, "law-dev", {"customerId": "aaaaaaaa-0000-0000-0000-000000000002",
        "retentionInDays": 30, "sku": {"name": "PerGB2018"}}, tags=dev_tags, service="Log Analytics")


LOG_USAGE = {
    "law-prod": {"ContainerLog": 2400, "AzureDiagnostics": 900, "AppTraces": 620, "Perf": 260,
                 "SecurityEvent": 180, "Heartbeat": 12, "AzureMetrics": 30},
    "law-dev": {"ContainerLogV2": 310, "AppTraces": 150, "Perf": 40, "Heartbeat": 4},
}


def cost_rows():
    start = END - timedelta(days=DAYS - 1)
    by_sub = {PROD[0]: [], NONPROD[0]: []}
    pm = {PROD[0]: [], NONPROD[0]: []}
    law_ids = {r["name"]: (r["subscriptionId"], r["id"].lower()) for r in resources if r["type"] == T_LAW}
    for n in range(DAYS):
        day = start + timedelta(days=n)
        ud = int(day.strftime("%Y%m%d"))
        agg = {}
        for rid_l, (sub, service, c) in daily_cost.items():
            cost = round(c * rnd.uniform(0.97, 1.03), 4)
            by_sub[sub].append([cost, ud, rid_l, service, "USD"])
            model = "Reservation" if rid_l in pricing_model_reserved else "OnDemand"
            agg[(sub, service, model)] = agg.get((sub, service, model), 0) + cost
        for name, tables in LOG_USAGE.items():
            sub, wid = law_ids[name]
            gb = sum(tables.values())
            cost = gb / 30 * 2.30 * rnd.uniform(0.93, 1.07)
            if name == "law-prod":
                cost += gb * 5 / 30 * 0.10  # retention beyond 31 days
                cost *= 0.92 + 0.08 * n / DAYS  # steady growth
            if name == "law-dev" and day == END - timedelta(days=6):
                cost += 640.0  # debug logging left on
            by_sub[sub].append([round(cost, 4), ud, wid, "Log Analytics", "USD"])
            agg[(sub, "Log Analytics", "OnDemand")] = agg.get((sub, "Log Analytics", "OnDemand"), 0) + cost
        if n < 20:  # resource deleted mid-period -> unmapped in inventory
            gone = f"/subscriptions/{PROD[0]}/resourcegroups/rg-migration/providers/microsoft.compute/virtualmachines/vm-migrate-temp"
            by_sub[PROD[0]].append([round(0.384 * 24, 4), ud, gone, "Virtual Machines", "USD"])
        nonprod_def = round(14.0 * rnd.uniform(0.98, 1.02), 4)
        by_sub[NONPROD[0]].append([nonprod_def, ud, "", "Microsoft Defender for Cloud", "USD"])
        agg[(NONPROD[0], "Microsoft Defender for Cloud", "OnDemand")] = nonprod_def
        defender = round(28.0 * rnd.uniform(0.98, 1.02), 4)
        by_sub[PROD[0]].append([defender, ud, "", "Microsoft Defender for Cloud", "USD"])
        agg[(PROD[0], "Microsoft Defender for Cloud", "OnDemand")] = defender
        for (sub, service, model), cost in agg.items():
            pm[sub].append([round(cost, 4), ud, service, model, "USD"])
    cols_r = [{"name": "Cost", "type": "Number"}, {"name": "UsageDate", "type": "Number"},
              {"name": "ResourceId", "type": "String"}, {"name": "ServiceName", "type": "String"},
              {"name": "Currency", "type": "String"}]
    cols_p = [{"name": "Cost", "type": "Number"}, {"name": "UsageDate", "type": "Number"},
              {"name": "ServiceName", "type": "String"}, {"name": "PricingModel", "type": "String"},
              {"name": "Currency", "type": "String"}]
    return ({s: {"columns": cols_r, "rows": rows} for s, rows in by_sub.items()},
            {s: {"columns": cols_p, "rows": rows} for s, rows in pm.items()})


def metric_payload(spec):
    end = datetime(END.year, END.month, END.day, tzinfo=timezone.utc)
    start = end - timedelta(days=METRIC_DAYS)
    stamps = [start + timedelta(hours=6 * i) for i in range(METRIC_DAYS * 4)]

    def series(name, unit, base, spread=(0.5, 1.3), peak=2.0, cap=100.0):
        data = []
        for ts in stamps:
            avg = min(cap, base * rnd.uniform(*spread))
            data.append({"timeStamp": ts.isoformat().replace("+00:00", "Z"), "average": round(avg, 3),
                         "maximum": round(min(cap, avg * peak), 3)})
        return {"name": {"value": name}, "unit": unit, "timeseries": [{"data": data}]}

    value = []
    if spec["kind"] == "vm":
        value.append(series("Percentage CPU", "Percent", spec["cpu"], peak=2.5 if spec["cpu"] > 2 else 2.4))
        if spec.get("mem") is not None and spec.get("mem_gb"):
            free = spec["mem_gb"] * (1 - spec["mem"] / 100) * 1024 ** 3
            value.append(series("Available Memory Bytes", "Bytes", free, spread=(0.95, 1.05), peak=1.0, cap=1e15))
    elif spec["kind"] == "disk":
        value.append(series("Composite Disk Read Operations/sec", "CountPerSecond", spec["iops"] * 0.6, cap=1e6))
        value.append(series("Composite Disk Write Operations/sec", "CountPerSecond", spec["iops"] * 0.4, cap=1e6))
    elif spec["kind"] == "fw":
        data = [{"timeStamp": ts.isoformat().replace("+00:00", "Z"),
                 "total": round(spec["gb_per_6h"] * 1e9 * rnd.uniform(0.5, 1.5))} for ts in stamps]
        value.append({"name": {"value": "DataProcessed"}, "unit": "Bytes", "timeseries": [{"data": data}]})
    elif spec["kind"] == "bastion":
        data = [{"timeStamp": ts.isoformat().replace("+00:00", "Z"), "average": spec["sessions"] * 0.4,
                 "maximum": spec["sessions"] if rnd.random() < 0.3 else 0} for ts in stamps]
        value.append({"name": {"value": "sessions"}, "unit": "Count", "timeseries": [{"data": data}]})
    elif spec["kind"] == "dbflex":
        value.append(series("cpu_percent", "Percent", spec["cpu"]))
        value.append(series("memory_percent", "Percent", spec["mem"], spread=(0.9, 1.1), peak=1.1))
        conn = [{"timeStamp": ts.isoformat().replace("+00:00", "Z"), "average": spec["connections"] * 0.6,
                 "maximum": spec["connections"]} for ts in stamps]
        value.append({"name": {"value": "active_connections"}, "unit": "Count", "timeseries": [{"data": conn}]})
    elif spec["kind"] == "aca":
        data_nano, data_req, data_rep = [], [], []
        for ts in stamps:
            t = ts.isoformat().replace("+00:00", "Z")
            reps = spec["replicas"]
            nano = spec["cpu"] / 100 * spec["vcpu"] * reps * 1e9 * rnd.uniform(0.6, 1.3)
            data_nano.append({"timeStamp": t, "average": round(nano), "maximum": round(nano * 1.8)})
            data_req.append({"timeStamp": t, "total": round(spec["requests_per_h"] * 6 * rnd.uniform(0, 2))})
            data_rep.append({"timeStamp": t, "average": reps, "maximum": reps + (1 if spec["requests_per_h"] > 100 else 0)})
        value += [{"name": {"value": "UsageNanoCores"}, "unit": "NanoCores", "timeseries": [{"data": data_nano}]},
                  {"name": {"value": "Requests"}, "unit": "Count", "timeseries": [{"data": data_req}]},
                  {"name": {"value": "Replicas"}, "unit": "Count", "timeseries": [{"data": data_rep}]}]
    elif spec["kind"] == "plan":
        value.append(series("CpuPercentage", "Percent", spec["cpu"]))
        value.append(series("MemoryPercentage", "Percent", spec["mem"], spread=(0.9, 1.1), peak=1.1))
    return {"cost": 0, "interval": "PT6H", "timespan": f"{start.isoformat()}/{end.isoformat()}", "value": value}


def main():
    from cloudfinops.providers.azure import _safe_name

    build()
    if OUT.exists():
        shutil.rmtree(OUT)
    raw = OUT / "raw"
    for d in ("inventory", "costs", "metrics", "advisor", "logging", "prices", "defender"):
        (raw / d).mkdir(parents=True, exist_ok=True)

    w = lambda p, data: (raw / p).write_text(json.dumps(data, separators=(",", ":")))  # noqa: E731
    w("inventory/resources.json", resources)
    w("inventory/containers.json", [
        {"id": f"/subscriptions/{s}", "name": n, "type": "microsoft.resources/subscriptions", "subscriptionId": s, "tags": None}
        for s, n in (PROD, NONPROD)])
    by_res, by_pm = cost_rows()
    for sub, data in by_res.items():
        w(f"costs/{sub}_by_resource.json", data)
    for sub, data in by_pm.items():
        w(f"costs/{sub}_by_pricing_model.json", data)
    for rid_l, spec in metrics.items():
        w(f"metrics/{_safe_name(rid_l)}.json", {"resource_id": rid_l, "response": metric_payload(spec)})

    api = [r for r in resources if r["type"] == T_VM and r["name"].startswith("vm-api-")][:3]
    advisor = [{
        "id": f"/subscriptions/{PROD[0]}/providers/Microsoft.Advisor/recommendations/{i}", "category": "Cost",
        "impact": "Medium", "impactedField": "Microsoft.Compute/virtualMachines", "impactedValue": r["name"],
        "recommendationTypeId": "e10b1381-5f0a-47ff-8c7b-37bd13d7c974",
        "resourceMetadata": {"resourceId": r["id"]},
        "shortDescription": {"problem": "Right-size or shutdown underutilized virtual machines",
                             "solution": "Right-size or shutdown underutilized virtual machines"},
        "extendedProperties": {"currentSku": "Standard_D8s_v5", "targetSku": "Standard_D4s_v5",
                               "annualSavingsAmount": "1681", "savingsCurrency": "USD"}} for i, r in enumerate(api)]
    advisor.append({
        "id": f"/subscriptions/{PROD[0]}/providers/Microsoft.Advisor/recommendations/ri-1", "category": "Cost",
        "impact": "High", "impactedField": "Microsoft.Subscriptions/subscriptions", "impactedValue": PROD[0],
        "recommendationTypeId": "84b1a508-fc21-49da-979e-96894f1665df",
        "resourceMetadata": {"resourceId": f"/subscriptions/{PROD[0]}"},
        "shortDescription": {"problem": "Buy virtual machine reserved instances to save money over pay-as-you-go costs",
                             "solution": "Buy virtual machine reserved instances to save money over pay-as-you-go costs"},
        "extendedProperties": {"annualSavingsAmount": "21480", "savingsCurrency": "USD", "term": "P1Y",
                               "vmSize": "Standard_D4s_v5", "qty": "20"}})
    w(f"advisor/{PROD[0]}.json", advisor)
    w(f"advisor/{NONPROD[0]}.json", [])
    (raw / "defender").mkdir(exist_ok=True)
    w(f"defender/{PROD[0]}.json", [{"name": "VirtualMachines", "pricingTier": "Standard", "subPlan": "P2"},
                                   {"name": "StorageAccounts", "pricingTier": "Standard", "subPlan": "DefenderForStorageV2"},
                                   {"name": "KeyVaults", "pricingTier": "Free"}])
    w(f"defender/{NONPROD[0]}.json", [{"name": "VirtualMachines", "pricingTier": "Standard", "subPlan": "P2"},
                                      {"name": "KeyVaults", "pricingTier": "Standard", "subPlan": "PerKeyVault"},
                                      {"name": "AppServices", "pricingTier": "Standard"}])

    for r in resources:
        if r["type"] != T_LAW:
            continue
        base = _safe_name(r["id"].lower())
        rows = [{"DataType": t, "BillableGB": str(gb * rnd.uniform(0.97, 1.03)), "TableName": "PrimaryResult"}
                for t, gb in LOG_USAGE[r["name"]].items()]
        w(f"logging/{base}_usage.json", {"workspace_id": r["id"].lower(), "rows": rows})
        w(f"logging/{base}_tables.json", {"workspace_id": r["id"].lower(), "tables": [
            {"name": t, "plan": "Analytics", "retentionInDays": r["properties"]["retentionInDays"],
             "totalRetentionInDays": r["properties"]["retentionInDays"]} for t in LOG_USAGE[r["name"]]]})

    sizes = {r["properties"]["hardwareProfile"]["vmSize"] for r in resources if r["type"] == T_VM}
    sizes |= {vm_downsize(s) for s in sizes if vm_downsize(s)}
    w("prices/vm_prices.json", {f"{REGION}|{s.lower()}": VM_HOURLY.get(s.lower()) for s in sorted(sizes)})

    (OUT / "scope.json").write_text(json.dumps({
        "provider": "azure", "sample": True,
        "subscriptions": [{"id": s, "name": n, "tenant": "00000000-0000-0000-0000-00000000acme"} for s, n in (PROD, NONPROD)],
        "period": {"start": str(END - timedelta(days=DAYS - 1)), "end": str(END)},
        "metric_days": METRIC_DAYS, "collected_at": f"{END}T06:00:00+00:00", "errors": [],
    }, indent=2))
    n_disks = sum(1 for r in resources if r["type"] == T_DISK)
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"Wrote {len(resources)} resources ({n_disks} disks), {len(metrics)} metric files, "
          f"{size / 1e6:.1f} MB to {OUT}")


if __name__ == "__main__":
    main()
