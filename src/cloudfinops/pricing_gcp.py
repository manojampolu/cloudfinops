"""Approximate GCP list prices (USD, us-central1, on-demand) used when no billing export is available.

Billing export data always wins; anything priced here is labelled as an estimate in the report.
"""

from __future__ import annotations

import re

HOURS = 730.0

MACHINE_HOURLY = {
    "e2-micro": 0.0084, "e2-small": 0.0168, "e2-medium": 0.0335,
    "e2-standard-2": 0.067, "e2-standard-4": 0.134, "e2-standard-8": 0.268, "e2-standard-16": 0.536,
    "e2-highmem-2": 0.0904, "e2-highmem-4": 0.1807, "e2-highmem-8": 0.3614,
    "n1-standard-1": 0.0475, "n1-standard-2": 0.095, "n1-standard-4": 0.19, "n1-standard-8": 0.38,
    "n2-standard-2": 0.0971, "n2-standard-4": 0.1942, "n2-standard-8": 0.3885, "n2-standard-16": 0.7769,
    "n2d-standard-2": 0.0845, "n2d-standard-4": 0.169, "n2d-standard-8": 0.338,
    "t2d-standard-1": 0.0422, "t2d-standard-2": 0.0845, "t2d-standard-4": 0.169,
    "c3-standard-4": 0.2014, "c3-standard-8": 0.4028,
}
DISK_GB_MONTH = {"pd-standard": 0.04, "pd-balanced": 0.10, "pd-ssd": 0.17, "pd-extreme": 0.125,
                 "hyperdisk-balanced": 0.08}
SNAPSHOT_GB_MONTH = 0.05
STATIC_IP_UNUSED_HOURLY = 0.01
STATIC_IP_IN_USE_HOURLY = 0.005
GCS_GB_MONTH = {"STANDARD": 0.020, "NEARLINE": 0.010, "COLDLINE": 0.004, "ARCHIVE": 0.0012,
                "MULTI_REGIONAL": 0.026, "REGIONAL": 0.020}
ARTIFACT_GB_MONTH = 0.10
ARTIFACT_FREE_GB = 0.5
RUN_IDLE_VCPU_SEC = 0.0000025
RUN_IDLE_GIB_SEC = 0.0000025
RUN_ACTIVE_VCPU_SEC = 0.000018
RUN_ACTIVE_GIB_SEC = 0.000002
LOGGING_GIB = 0.50
LOGGING_FREE_GIB = 50.0
LOGGING_RETENTION_GIB_MONTH = 0.01
SQL_TIER_HOURLY = {"db-f1-micro": 0.0105, "db-g1-small": 0.035, "db-custom-1-3840": 0.0659,
                   "db-custom-2-7680": 0.1318, "db-custom-4-15360": 0.2636}

# Rough currency conversion to USD for provider-reported amounts (override with config fx_to_usd)
FX_TO_USD = {"USD": 1.0, "INR": 0.012, "EUR": 1.08, "GBP": 1.27, "JPY": 0.0067, "AUD": 0.66, "CAD": 0.73,
             "SGD": 0.74}

_MT_RE = re.compile(r"^(?P<family>[a-z0-9]+)-(?P<kind>[a-z]+)-(?P<cpu>\d+)$")


def machine_type_short(value: str) -> str:
    return (value or "").rstrip("/").rsplit("/", 1)[-1]


def machine_downsize(mt: str) -> str | None:
    if mt == "e2-medium":
        return "e2-small"
    if mt == "e2-small":
        return "e2-micro"
    m = _MT_RE.match(mt or "")
    if not m or m["kind"] not in ("standard", "highmem", "highcpu"):
        return None
    cpu = int(m["cpu"])
    if cpu <= 2:
        return "e2-medium" if m["family"] == "e2" and m["kind"] == "standard" else None
    return f"{m['family']}-{m['kind']}-{cpu // 2}"


def machine_memory_gb(mt: str) -> float | None:
    fixed = {"e2-micro": 1, "e2-small": 2, "e2-medium": 4}
    if mt in fixed:
        return fixed[mt]
    m = _MT_RE.match(mt or "")
    if not m:
        return None
    per = {"standard": 4, "highmem": 8, "highcpu": 1}.get(m["kind"])
    if per is None:
        return None
    if m["family"] == "n1" and m["kind"] == "standard":
        per = 3.75
    return int(m["cpu"]) * per


def run_idle_monthly(cpu: float, mem_gib: float, instances: float) -> float:
    return instances * HOURS * 3600 * (cpu * RUN_IDLE_VCPU_SEC + mem_gib * RUN_IDLE_GIB_SEC)


def parse_cpu(value) -> float:
    s = str(value or "1")
    return float(s[:-1]) / 1000 if s.endswith("m") else float(s)


def parse_mem_gib(value) -> float:
    s = str(value or "512Mi")
    units = {"Ki": 1 / 1024 ** 2, "Mi": 1 / 1024, "Gi": 1.0, "M": 1 / 1024, "G": 1.0}
    for u, f in units.items():
        if s.endswith(u):
            return float(s[: -len(u)]) * f
    try:
        return float(s) / 1024 ** 3
    except ValueError:
        return 0.5


def estimate_monthly(res, hours: float = HOURS) -> float | None:
    a = res.attributes
    match res.type:
        case "gcp.vm":
            if a.get("state") != "running":
                return 0.0
            hourly = MACHINE_HOURLY.get(res.sku)
            return hourly * hours if hourly is not None else None
        case "gcp.disk":
            return DISK_GB_MONTH.get(res.sku, 0.04) * float(a.get("size_gb") or 0)
        case "gcp.snapshot":
            gb = float(a.get("storage_gb") or a.get("size_gb") or 0)
            return SNAPSHOT_GB_MONTH * gb
        case "gcp.address":
            if a.get("address_type") != "EXTERNAL":
                return 0.0
            return (STATIC_IP_IN_USE_HOURLY if a.get("status") == "IN_USE" else STATIC_IP_UNUSED_HOURLY) * hours
        case "gcp.bucket":
            gb = float(res.metrics.get("stored_gb") or 0)
            return GCS_GB_MONTH.get((res.sku or "STANDARD").upper(), 0.02) * gb if gb else None
        case "gcp.artifact_repo":
            gb = float(a.get("size_gb") or 0)
            return max(0.0, gb - ARTIFACT_FREE_GB) * ARTIFACT_GB_MONTH
        case "gcp.run_service":
            min_inst = float(a.get("min_instances") or 0)
            return run_idle_monthly(a.get("cpu", 1.0), a.get("memory_gib", 0.5), min_inst) if min_inst else None
        case "gcp.sql":
            hourly = SQL_TIER_HOURLY.get(res.sku)
            return hourly * hours * (2 if a.get("availability") == "REGIONAL" else 1) if hourly and a.get("state") == "running" else None
    return None
