"""Limit an assessment to selected Azure resource groups.

Collection stays subscription-wide (same Azure calls); this narrows the Dataset before analysis so one
collection can produce separate reports per team or environment, e.g. DEV and TEST.
"""

from __future__ import annotations

from .models import Dataset


def filter_resource_groups(ds: Dataset, groups: list[str]) -> Dataset:
    if ds.provider != "azure":
        raise ValueError("--resource-groups applies to Azure assessments only")
    wanted = {g.strip().lower() for g in groups if g.strip()}
    available = sorted({r.group for r in ds.resources if r.group}, key=str.lower)
    if not wanted & {g.lower() for g in available}:
        shown = ", ".join(available[:25]) + (" ..." if len(available) > 25 else "")
        raise ValueError(f"None of the resource groups {sorted(wanted)} exist in this run. Available: {shown}")

    # AKS nodes live in the cluster's node resource group (MC_...): keep them with their cluster
    for r in ds.resources:
        if r.type == "azure.aks" and r.group.lower() in wanted and r.attributes.get("node_resource_group"):
            wanted.add(r.attributes["node_resource_group"].lower())

    resources = [r for r in ds.resources if r.group.lower() in wanted]
    kept = {r.id for r in resources}
    markers = tuple(f"/resourcegroups/{g}/" for g in wanted)
    records = [c for c in ds.records if c.resource_id in kept or (c.resource_id and any(m in c.resource_id for m in markers))]
    for r in resources:
        r.related = [i for i in r.related if i in kept]
    relationships = [rel for rel in ds.relationships if rel.src in kept and rel.dst in kept]

    extras = dict(ds.extras)
    extras["advisor"] = [a for a in ds.extras.get("advisor") or [] if a["resource_id"] in kept]
    extras["log_usage"] = {ws: v for ws, v in (ds.extras.get("log_usage") or {}).items() if ws in kept}
    extras["pricing_model_daily"] = []  # subscription-level; cannot be split by resource group

    scope = dict(ds.scope)
    scope["resource_groups"] = sorted(wanted)
    scope["info_notes"] = [*scope.get("info_notes", []),
                           f"Report limited to resource group(s): {', '.join(sorted(wanted))}. Collection was "
                      "subscription-wide; subscription-level commitment analysis is omitted."]
    return Dataset(provider=ds.provider, records=records, resources=resources, relationships=relationships,
                   scope=scope, extras=extras, currency=ds.currency)
