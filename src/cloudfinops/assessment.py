"""Assessment pipeline: Dataset -> analysis -> findings -> register -> report files."""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .analysis import CostSummary, enrich_resource_costs, summarize
from .config import Config
from .estate import Node, ServiceProfile, build, to_mermaid
from .models import Dataset, Recommendation
from .quality import QualityIssue, check
from .register import Register
from .report_html import command_stats, render_html
from .rules import run_rules, scenario_total


@dataclass
class Assessment:
    dataset: Dataset
    summary: CostSummary
    recommendations: list[Recommendation]
    quality: list[QualityIssue]
    tree: Node
    profiles: list[ServiceProfile]
    outputs: dict[str, Path]

    @property
    def totals(self) -> dict[str, float]:
        return scenario_total(self.recommendations)


def assess(ds: Dataset, cfg: Config, out_dir: Path, run_dir: Path | None = None,
           register_path: Path | None = None, disabled: set[str] | None = None) -> Assessment:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    enrich_resource_costs(ds.resources, ds.records, cfg)
    summary = summarize(ds.records, cfg, top_n=12)
    as_of = summary.end if ds.records else None
    recs = run_rules(ds.records, ds.resources, cfg, as_of=as_of, disabled=disabled, provider=ds.provider,
                     extras=ds.extras, relationships=ds.relationships)
    quality = check(ds, summary, cfg)
    tree, profiles = build(ds, recs, cfg)
    totals = scenario_total(recs)

    reg_totals, decisions = None, {}
    if register_path:
        reg = Register(register_path)
        try:
            reg.record_run(ds.provider, str(run_dir or out_dir), (str(summary.start), str(summary.end)),
                           summary.window_total, recs, totals["estimate"], ds.records,
                           mark_missing=not ds.scope.get("resource_groups"))  # partial scope: don't retire others
            reg_totals, decisions = reg.totals(), reg.decisions()
        finally:
            reg.close()

    outputs = {}
    html = render_html(ds, summary, recs, quality, tree, profiles, cfg, reg_totals, decisions,
                       command_stats(Path(run_dir or out_dir) / "commands.log"))
    outputs["report"] = out_dir / "report.html"
    outputs["report"].write_text(html, encoding="utf-8")

    outputs["findings_csv"] = out_dir / "findings.csv"
    _write_findings_csv(recs, outputs["findings_csv"], decisions)
    outputs["inventory_csv"] = out_dir / "inventory.csv"
    _write_inventory_csv(ds, outputs["inventory_csv"], cfg)
    outputs["mindmap"] = out_dir / "estate.mmd"
    outputs["mindmap"].write_text(to_mermaid(tree), encoding="utf-8")
    outputs["findings_json"] = out_dir / "findings.json"
    outputs["findings_json"].write_text(json.dumps({
        "provider": ds.provider, "period": {"start": str(summary.start), "end": str(summary.end)},
        "window_spend": round(summary.window_total, 2), "savings": {k: round(v, 2) for k, v in totals.items()},
        "quality": [q.__dict__ for q in quality], "findings": [r.to_dict() for r in recs],
    }, indent=2, default=str), encoding="utf-8")
    if register_path:
        outputs["register"] = Path(register_path)
    return Assessment(ds, summary, recs, quality, tree, profiles, outputs)


def _write_findings_csv(recs: list[Recommendation], path: Path, decisions: dict) -> None:
    cols = ["key", "rule_id", "category", "title", "service", "resource_name", "resource_id", "account", "region",
            "owner", "confidence", "needs_validation", "current_monthly_cost", "est_monthly_savings", "savings_low",
            "savings_high", "effort", "risk", "action", "evidence", "assumptions", "missing_evidence",
            "validation_steps", "status", "validated_savings", "realized_savings"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in recs:
            d = r.to_dict()
            for k in ("evidence", "assumptions", "missing_evidence", "validation_steps"):
                d[k] = " | ".join(d[k])
            dec = decisions.get(r.key) or {}
            d.update(status=dec.get("status", "open"), validated_savings=dec.get("validated_savings"),
                     realized_savings=dec.get("realized_savings"))
            for k in ("current_monthly_cost", "est_monthly_savings", "savings_low", "savings_high"):
                if isinstance(d.get(k), float):
                    d[k] = round(d[k], 2)
            w.writerow(d)


def _write_inventory_csv(ds: Dataset, path: Path, cfg: Config) -> None:
    from .estate import utilization_bucket

    cols = ["id", "name", "service", "type", "sku", "account", "group", "region", "monthly_cost", "cost_source",
            "utilization", "cpu_avg", "cpu_p95", "mem_used_p95", "iops_p95", "requests_30d", "replicas_p95",
            "related", "tags"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in sorted(ds.resources, key=lambda r: -(r.monthly_cost or 0)):
            w.writerow([r.id, r.display_name, r.service, r.type, r.sku, r.account, r.group, r.region,
                        round(r.monthly_cost, 2) if r.monthly_cost is not None else "", r.cost_source,
                        utilization_bucket(r, cfg), *(round(r.metrics[k], 1) if k in r.metrics else ""
                                                       for k in ("cpu_avg", "cpu_p95", "mem_used_p95", "iops_p95",
                                                                 "requests_30d", "replicas_p95")),
                        len(r.related), json.dumps(r.tags) if r.tags else ""])


def bundle(run_dir: Path) -> Path:
    """Zip the run folder (raw evidence + report) for hand-over."""
    return Path(shutil.make_archive(str(run_dir), "zip", root_dir=run_dir))
