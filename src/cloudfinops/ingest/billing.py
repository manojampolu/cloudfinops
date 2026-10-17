"""Load billing CSVs.

Accepts three column conventions (auto-detected per column):
  * simple:  date, service, cost, account, region, resource_id, tags (JSON)
  * FOCUS:   ChargePeriodStart, ServiceName, BilledCost, SubAccountId, RegionId, ResourceId, Tags
  * AWS CUR: lineItem/UsageStartDate, product/ProductName, lineItem/UnblendedCost, resourceTags/user:*
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from ..models import CostRecord

ALIASES: dict[str, list[str]] = {
    "day": ["date", "day", "usage_date", "ChargePeriodStart", "lineItem/UsageStartDate"],
    "cost": ["cost", "unblended_cost", "BilledCost", "EffectiveCost", "lineItem/UnblendedCost"],
    "service": ["service", "ServiceName", "product/ProductName", "lineItem/ProductCode"],
    "account": ["account", "account_id", "SubAccountId", "lineItem/UsageAccountId"],
    "region": ["region", "RegionId", "Region", "product/region"],
    "resource_id": ["resource_id", "ResourceId", "lineItem/ResourceId"],
    "provider": ["provider", "ProviderName"],
    "pricing_category": ["pricing_category", "PricingCategory", "pricing/term"],
    "usage_type": ["usage_type", "lineItem/UsageType", "ChargeDescription"],
    "tags": ["tags", "Tags"],
}
TAG_COLUMN_PREFIXES = ("resourceTags/user:", "tag:", "tag/")
REQUIRED = ("day", "cost", "service")
SIMPLE_COLUMNS = ["date", "provider", "account", "region", "service", "resource_id",
                  "pricing_category", "usage_type", "cost", "tags"]


def _resolve_columns(header: list[str]) -> dict[str, str]:
    lookup = {h.lower(): h for h in header}
    resolved = {}
    for field, names in ALIASES.items():
        for name in names:
            if name.lower() in lookup:
                resolved[field] = lookup[name.lower()]
                break
    missing = [f for f in REQUIRED if f not in resolved]
    if missing:
        raise ValueError(
            f"Billing CSV is missing required column(s) {missing}. "
            f"Accepted names: " + "; ".join(f"{f}: {ALIASES[f]}" for f in missing)
        )
    return resolved


def _parse_tags(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: "k1=v1;k2=v2"
        data = dict(p.split("=", 1) for p in raw.split(";") if "=" in p)
    return {str(k).lower(): str(v) for k, v in data.items() if v not in (None, "")}


def _normalize_pricing(raw: str) -> str:
    v = (raw or "").strip().lower().replace("_", "-")
    if not v:
        return ""
    if v in ("ondemand", "on-demand", "standard"):
        return "On-Demand"
    if v in ("reserved", "commitment-based", "savingsplan", "savings-plan"):
        return "Commitment-Based"
    if v == "spot":
        return "Spot"
    return raw.strip()


def load_billing_csv(path: str | Path) -> list[CostRecord]:
    records: list[CostRecord] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return records
        cols = _resolve_columns(reader.fieldnames)
        tag_cols = [h for h in reader.fieldnames if h.startswith(TAG_COLUMN_PREFIXES)]

        def get(row: dict, field: str) -> str:
            col = cols.get(field)
            return (row.get(col) or "").strip() if col else ""

        for lineno, row in enumerate(reader, start=2):
            cost_raw = get(row, "cost")
            if not cost_raw:
                continue
            try:
                cost = float(cost_raw)
                day = date.fromisoformat(get(row, "day")[:10])
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: {exc}") from exc

            tags = _parse_tags(get(row, "tags"))
            for col in tag_cols:
                if row.get(col):
                    key = col
                    for prefix in TAG_COLUMN_PREFIXES:
                        key = key.removeprefix(prefix)
                    tags[key.lower()] = row[col]

            records.append(CostRecord(
                day=day,
                cost=cost,
                service=get(row, "service") or "Unknown",
                account=get(row, "account"),
                region=get(row, "region"),
                resource_id=get(row, "resource_id"),
                provider=(get(row, "provider") or "aws").lower(),
                pricing_category=_normalize_pricing(get(row, "pricing_category")),
                usage_type=get(row, "usage_type"),
                tags=tags,
            ))
    return records


def write_billing_csv(records: list[CostRecord], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SIMPLE_COLUMNS)
        writer.writeheader()
        for r in records:
            writer.writerow({
                "date": r.day.isoformat(), "provider": r.provider, "account": r.account,
                "region": r.region, "service": r.service, "resource_id": r.resource_id,
                "pricing_category": r.pricing_category, "usage_type": r.usage_type,
                "cost": f"{r.cost:.6f}", "tags": json.dumps(r.tags) if r.tags else "",
            })
