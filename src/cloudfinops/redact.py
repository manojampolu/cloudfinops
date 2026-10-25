"""Redacted copies of assessment outputs, for sharing outside the client's team.

Levels
  ids  subscription / account / project IDs and names, tenant IDs, GUIDs, AWS account numbers, IP addresses, emails
  all  ids + resource names, resource groups, full resource IDs and owner tag values

Pseudonyms are stable within a run (the same ID always maps to the same pseudonym), so the redacted report still
reads coherently. The lookup table is written next to the originals (redaction-map.csv), never into the redacted copy.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

from .models import Dataset

LEVELS = ("ids", "all")
GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
AWS_ACCOUNT = re.compile(r"(?<![\d.])\d{12}(?![\d.])")
IPV4 = re.compile(r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Words too common to replace even if a resource happens to be named after them
STOPWORDS = {"default", "prod", "production", "test", "main", "data", "logs", "app", "apps", "web", "api", "dev",
             "staging", "backup", "backups", "shared", "global", "public", "private", "internal", "storage",
             "network", "monitoring", "security", "platform", "system", "user", "none", "true", "false"}
NAME_EDGE = r"A-Za-z0-9_.\-"


class Redactor:
    def __init__(self, ds: Dataset, level: str = "ids"):
        if level not in LEVELS:
            raise ValueError(f"redaction level must be one of {LEVELS}")
        self.level = level
        self.mapping: dict[str, str] = {}  # lowercase original -> pseudonym
        self.kinds: dict[str, str] = {}
        self._counters: dict[str, int] = defaultdict(int)
        self._collect(ds)
        tokens = sorted(self.mapping, key=len, reverse=True)
        self._token_re = re.compile(
            rf"(?<![{NAME_EDGE}])(" + "|".join(re.escape(t) for t in tokens) + rf")(?![{NAME_EDGE}])", re.I
        ) if tokens else None

    # ------------------------------------------------------------------ mapping

    def _pseudo(self, kind: str, value: str) -> str:
        key = (value or "").strip().lower()
        if not key:
            return value
        if key not in self.mapping:
            self._counters[kind] += 1
            self.mapping[key] = f"{kind}-{self._counters[kind]}"
            self.kinds[key] = kind
        return self.mapping[key]

    def _add(self, kind: str, value) -> None:
        v = str(value or "").strip()
        if len(v) >= 3 and v.lower() not in STOPWORDS:
            self._pseudo(kind, v)

    def _collect(self, ds: Dataset) -> None:
        scope_kind = {"azure": "subscription", "aws": "account", "gcp": "project"}.get(ds.provider, "scope")
        for s in ds.scope.get("subscriptions") or []:
            pseudo = self._pseudo(scope_kind, s.get("id", ""))
            for alias in (s.get("name"), s.get("display_name")):  # names often contain the client's name
                if alias and alias.strip().lower() not in self.mapping:
                    self.mapping[alias.strip().lower()] = f"{pseudo}-name"
                    self.kinds[alias.strip().lower()] = scope_kind
            self._add("tenant", s.get("tenant"))
        for sid, name in (ds.extras.get("subscription_names") or {}).items():
            self._add(scope_kind, sid)
            if name and name.strip().lower() not in self.mapping:
                self.mapping[name.strip().lower()] = f"{self._pseudo(scope_kind, sid)}-name"
        self._add("account", ds.extras.get("account_alias"))
        if self.level != "all":
            return
        for r in sorted(ds.resources, key=lambda r: r.id):
            kind = r.type.split(".")[-1].replace("_", "-")[:14] or "resource"
            if r.id and len(r.id) > 20:
                self._add("resource-id", r.id)
            self._add(kind, r.name or r.display_name)
            self._add("group", r.group)
            for key in ("owner", "team", "application-owner", "contact", "app", "cost-center"):
                self._add("tag", r.tags.get(key))
            if r.type == "s3_bucket" or r.type == "gcp.bucket":
                self._add("bucket", r.name)

    # ------------------------------------------------------------------ apply

    def apply(self, text: str) -> str:
        if self._token_re is not None:
            text = self._token_re.sub(lambda m: self.mapping.get(m.group(1).lower(), m.group(1)), text)
        text = GUID.sub(lambda m: self._pseudo("guid", m.group(0)), text)
        text = AWS_ACCOUNT.sub(lambda m: self._pseudo("account", m.group(0)), text)
        text = IPV4.sub(lambda m: self._pseudo("ip", m.group(0)), text)
        text = EMAIL.sub(lambda m: self._pseudo("user", m.group(0)) + "@redacted", text)
        return text

    def write_map(self, path: Path) -> None:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["kind", "pseudonym", "original"])
            for original, pseudo in sorted(self.mapping.items(), key=lambda kv: kv[1]):
                w.writerow([self.kinds.get(original, ""), pseudo, original])


BANNER = ('<p class="muted small" style="margin-top:6px;padding:6px 10px;border:1px dashed var(--line);border-radius:6px">'
          'Redacted copy ({level}): identifiers are replaced with stable pseudonyms. The mapping stays with the assessor.</p>')


def write_redacted(outputs: dict[str, Path], ds: Dataset, level: str, out_dir: Path) -> dict[str, Path]:
    """Write redacted copies of the shareable outputs into <out_dir>/redacted/ plus a private mapping file."""
    red = Redactor(ds, level)
    target = Path(out_dir) / "redacted"
    target.mkdir(parents=True, exist_ok=True)
    written = {}
    for key in ("report", "findings_csv", "inventory_csv", "mindmap", "findings_json"):
        src = outputs.get(key)
        if not src or not Path(src).exists():
            continue
        text = red.apply(Path(src).read_text(encoding="utf-8"))
        if key == "report":
            text = text.replace("</h1>", "</h1>" + BANNER.format(level=level), 1)
        dst = target / Path(src).name
        dst.write_text(text, encoding="utf-8")
        written[f"redacted_{key}"] = dst
    written["redaction_map"] = Path(out_dir) / "redaction-map.csv"
    red.write_map(written["redaction_map"])
    return written
