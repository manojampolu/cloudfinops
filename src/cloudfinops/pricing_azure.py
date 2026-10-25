"""Azure price helpers.

Order of preference for a price: billing data (actual) > Retail Prices API cache > fallback table.
Fallback values are approximate East US pay-as-you-go Linux list prices in USD.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

HOURS = 730.0

VM_HOURLY = {
    "standard_b2s": 0.0416, "standard_b2ms": 0.0832, "standard_b4ms": 0.166, "standard_b8ms": 0.333,
    "standard_d2s_v3": 0.096, "standard_d4s_v3": 0.192, "standard_d8s_v3": 0.384, "standard_d16s_v3": 0.768,
    "standard_d2s_v5": 0.096, "standard_d4s_v5": 0.192, "standard_d8s_v5": 0.384, "standard_d16s_v5": 0.768,
    "standard_d32s_v5": 1.536,
    "standard_d2as_v5": 0.086, "standard_d4as_v5": 0.172, "standard_d8as_v5": 0.344, "standard_d16as_v5": 0.688,
    "standard_e2s_v5": 0.126, "standard_e4s_v5": 0.252, "standard_e8s_v5": 0.504, "standard_e16s_v5": 1.008,
    "standard_f2s_v2": 0.0846, "standard_f4s_v2": 0.169, "standard_f8s_v2": 0.338, "standard_f16s_v2": 0.677,
}

# GB of memory per vCPU by family letter (general guidance for current series)
MEM_PER_VCPU = {"d": 4, "e": 8, "f": 2, "m": 24, "a": 2}
B_SERIES_MEM = {"b1s": 1, "b1ms": 2, "b2s": 4, "b2ms": 8, "b4ms": 16, "b8ms": 32, "b12ms": 48, "b16ms": 64}

# Managed disk tiers: (max GB, Premium SSD $/mo, Standard SSD $/mo, Standard HDD $/mo), LRS
DISK_TIERS = [
    (4, 0.78, 0.30, 0.77), (8, 1.54, 0.60, 0.77), (16, 2.95, 1.20, 0.77), (32, 5.28, 2.40, 1.54),
    (64, 10.21, 4.80, 3.01), (128, 19.71, 9.60, 5.89), (256, 38.01, 19.20, 11.33),
    (512, 73.22, 38.40, 21.76), (1024, 135.17, 76.80, 40.96), (2048, 259.05, 153.60, 77.83),
    (4096, 495.57, 307.20, 147.46), (8192, 946.08, 614.40, 283.86), (16384, 1802.24, 1228.80, 545.29),
    (32767, 3604.48, 2457.60, 1090.58),
]
SNAPSHOT_GB_MONTH = 0.05
# Container Apps consumption plan (per second); idle rate applies to min replicas with no traffic
ACA_ACTIVE_VCPU_S, ACA_IDLE_VCPU_S, ACA_GIB_S = 0.000024, 0.000003, 0.000003
PUBLIC_IP_HOURLY = 0.005
LB_STANDARD_HOURLY = 0.025
APPGW_V2_HOURLY = 0.246
APP_SERVICE_HOURLY = {  # Linux, per instance
    "b1": 0.018, "b2": 0.035, "b3": 0.07, "s1": 0.095, "s2": 0.19, "s3": 0.38,
    "p0v3": 0.0897, "p1v3": 0.179, "p2v3": 0.358, "p3v3": 0.716, "p1v2": 0.10, "p2v2": 0.20, "p3v2": 0.40,
}

_VM_RE = re.compile(r"^standard_(?P<family>[a-z]+?)(?P<vcpu>\d+)(?P<suffix>[a-z]*)(?:_(?P<ver>v\d+))?$")


def parse_vm_size(size: str) -> dict | None:
    """'Standard_D8s_v5' -> {'family': 'd', 'vcpu': 8, 'suffix': 's', 'ver': 'v5'}"""
    m = _VM_RE.match((size or "").lower())
    if not m:
        return None
    return {"family": m["family"], "vcpu": int(m["vcpu"]), "suffix": m["suffix"], "ver": m["ver"] or ""}


def vm_memory_gb(size: str) -> float | None:
    s = (size or "").lower().removeprefix("standard_")
    if s in B_SERIES_MEM:
        return B_SERIES_MEM[s]
    p = parse_vm_size(size)
    if not p or p["family"][0] not in MEM_PER_VCPU or p["family"] == "b":
        return None
    return p["vcpu"] * MEM_PER_VCPU[p["family"][0]]


def vm_downsize(size: str) -> str | None:
    """Next smaller size in the same family/version (halve vCPUs), e.g. D8s_v5 -> D4s_v5."""
    cur = (size or "").lower().removeprefix("standard_")
    if cur.startswith("b"):
        order = ["Standard_B8ms", "Standard_B4ms", "Standard_B2ms", "Standard_B2s"]
        names = [o.lower().removeprefix("standard_") for o in order]
        if cur in names and names.index(cur) < len(order) - 1:
            return order[names.index(cur) + 1]
        return None
    p = parse_vm_size(size)
    if not p or p["vcpu"] <= 2:
        return None
    target = p["vcpu"] // 2
    ver = f"_{p['ver']}" if p["ver"] else ""
    return f"Standard_{p['family'].upper()}{target}{p['suffix']}{ver}"


def disk_tier_price(size_gb: float, sku: str) -> float:
    sku = (sku or "").lower()
    col = 1 if sku.startswith("premium") else 2 if sku.startswith("standardssd") else 3
    for tier in DISK_TIERS:
        if size_gb <= tier[0]:
            return tier[col]
    return DISK_TIERS[-1][col]


class PriceBook:
    """VM price lookup with an on-disk cache of Retail Prices API answers.

    online=False (offline replay / tests) uses only the cache and the fallback table.
    """

    API = "https://prices.azure.com/api/retail/prices"

    def __init__(self, cache_dir: Path | None = None, online: bool = False):
        self.cache_dir = cache_dir
        self.online = online
        self._lock = threading.Lock()

    def _cache_file(self) -> Path | None:
        return self.cache_dir / "vm_prices.json" if self.cache_dir else None

    def _load_cache(self) -> dict:
        f = self._cache_file()
        if f and f.exists():
            return json.loads(f.read_text())
        return {}

    def _save_cache(self, data: dict) -> None:
        f = self._cache_file()
        if f:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(data, indent=1, sort_keys=True))

    def vm_hourly(self, size: str, region: str) -> tuple[float | None, str]:
        """Returns (hourly USD, source) where source is retail-api | fallback | unknown."""
        key = f"{region.lower()}|{size.lower()}"
        with self._lock:
            cache = self._load_cache()
        if cache.get(key) is not None:
            return cache[key], "retail-api"
        if self.online and key not in cache:
            price, ok = self._fetch(size, region)
            if ok:  # cache answers (incl. "no price") but never network failures
                with self._lock:
                    cache = self._load_cache()
                    cache[key] = price
                    self._save_cache(cache)
                if price is not None:
                    return price, "retail-api"
        fb = VM_HOURLY.get(size.lower())
        return (fb, "fallback") if fb is not None else (None, "unknown")

    def prefetch(self, pairs: set[tuple[str, str]], workers: int = 6) -> None:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda p: self.vm_hourly(*p), sorted(pairs)))

    def _fetch(self, size: str, region: str) -> tuple[float | None, bool]:
        flt = (f"serviceName eq 'Virtual Machines' and armRegionName eq '{region.lower()}' "
               f"and armSkuName eq '{size}' and priceType eq 'Consumption'")
        url = f"{self.API}?{urllib.parse.urlencode({'$filter': flt})}"
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:  # public, unauthenticated
                items = json.load(resp).get("Items", [])
        except Exception as exc:  # network issues must not break an assessment
            with self._lock:
                if self.online:
                    log.warning("Retail Prices API unreachable (%s); using built-in fallback prices", exc)
                self.online = False  # circuit breaker: don't wait on every SKU
            return None, False
        linux = [i for i in items if "windows" not in i.get("productName", "").lower()
                 and "spot" not in i.get("skuName", "").lower()
                 and "low priority" not in i.get("skuName", "").lower()
                 and i.get("unitOfMeasure") == "1 Hour"]
        return min((i["retailPrice"] for i in linux), default=None), True

def estimate_monthly(res, hours: float = HOURS) -> float | None:
    """List-price estimate for an Azure resource when billing has no resource-level cost."""
    a = res.attributes
    match res.type:
        case "azure.vm":
            if a.get("power_state") == "deallocated":
                return 0.0
            hourly = VM_HOURLY.get((res.sku or "").lower())
            return hourly * hours if hourly is not None else None
        case "azure.disk":
            return disk_tier_price(float(a.get("size_gb") or 0), res.sku)
        case "azure.snapshot":
            return SNAPSHOT_GB_MONTH * float(a.get("size_gb") or 0)
        case "azure.public_ip":
            return PUBLIC_IP_HOURLY * hours
        case "azure.lb":
            return LB_STANDARD_HOURLY * hours if (res.sku or "").lower() == "standard" else 0.0
        case "azure.appgw":
            return APPGW_V2_HOURLY * hours
        case "azure.container_app":
            mins = int(a.get("min_replicas") or 0)
            if not mins or a.get("state") != "running":
                return None  # usage-based; only billing data can tell
            return mins * hours * 3600 * (float(a.get("vcpu") or 0) * ACA_IDLE_VCPU_S + float(a.get("memory_gib") or 0) * ACA_GIB_S)
        case "azure.app_service_plan":
            hourly = APP_SERVICE_HOURLY.get((res.sku or "").lower())
            return hourly * hours * int(a.get("capacity") or 1) if hourly is not None else None
    return None
