"""Load / save resource inventory snapshots (JSON).

Format: {"generated_at": "...", "resources": [{"id": ..., "type": ..., ...}, ...]}
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from ..models import Resource

_FIELDS = {f.name for f in fields(Resource)}


def load_inventory(path: str | Path) -> list[Resource]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    items = data["resources"] if isinstance(data, dict) else data
    resources = []
    for item in items:
        kwargs = {k: v for k, v in item.items() if k in _FIELDS}
        kwargs["tags"] = {str(k).lower(): str(v) for k, v in (kwargs.get("tags") or {}).items()}
        if kwargs.get("monthly_cost") is not None:
            kwargs.setdefault("cost_source", "inventory")
        resources.append(Resource(**kwargs))
    return resources


def write_inventory(resources: list[Resource], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "resources": [asdict(r) for r in resources],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
