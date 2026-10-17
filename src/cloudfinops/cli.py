"""Command-line interface: `cloudfinops analyze | collect | demo | rules`."""

from __future__ import annotations

import argparse
import logging
import sys
from importlib import resources as pkg_resources
from pathlib import Path

from . import __version__
from .analysis import enrich_resource_costs, summarize
from .config import Config
from .ingest import load_billing_csv, load_inventory, write_billing_csv, write_inventory
from .report import Report, render
from .rules import RULES, run_rules


def _analyze(billing: Path, inventory: Path | None, args) -> int:
    cfg = Config.load(args.config)
    if args.lookback_days:
        cfg.lookback_days = args.lookback_days
    records = load_billing_csv(billing)
    resources = load_inventory(inventory) if inventory else []
    enrich_resource_costs(resources, records, cfg)
    recs = run_rules(records, resources, cfg, disabled=set(args.disable or []))
    report = Report(summarize(records, cfg, top_n=args.top), recs, cfg.lookback_days)
    text = render(report, args.format, top=args.top)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Report written to {args.output} - est. savings ${report.savings:,.2f}/month "
              f"across {len(recs)} recommendations", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_analyze(args) -> int:
    return _analyze(Path(args.billing), Path(args.inventory) if args.inventory else None, args)


def cmd_demo(args) -> int:
    samples = pkg_resources.files("cloudfinops") / "samples"
    if args.provider == "azure":
        with pkg_resources.as_file(samples / "azure") as run_dir:
            out = Path(args.out or "demo-azure")
            return _assess_run_dir(Path(run_dir), out, args, register=out / "register.db")
    with pkg_resources.as_file(samples / "billing.csv") as b, pkg_resources.as_file(samples / "inventory.json") as i:
        return _analyze(b, i, args)


def _assess_run_dir(run_dir: Path, out_dir: Path, args, register: Path | None) -> int:
    from .assessment import assess, bundle
    from .providers.base import get_provider

    cfg = Config.load(args.config)
    if getattr(args, "lookback_days", None):
        cfg.lookback_days = args.lookback_days
    if getattr(args, "billing_table", None):
        cfg.gcp_billing_table = args.billing_table
    provider = get_provider(getattr(args, "provider", None) or _provider_of(run_dir))
    ds = provider.load(run_dir, cfg)
    result = assess(ds, cfg, out_dir, run_dir=run_dir, register_path=register, disabled=set(args.disable or []))
    t = result.totals
    nv = sum(1 for r in result.recommendations if r.needs_validation)
    spend = f"${result.summary.window_total:,.0f}" if ds.records else "unknown (no billing data)"
    print(f"\nResources: {len(ds.resources)} · billing rows: {len(ds.records)} · "
          f"spend (last {cfg.lookback_days}d): {spend}")
    print(f"Findings: {len(result.recommendations)} ({nv} need validation) · est. savings "
          f"${t['estimate']:,.0f}/month (range ${t['low']:,.0f}-${t['high']:,.0f})")
    for q in result.quality:
        if q.level in ("warn", "error"):
            print(f"  [{q.level}] {q.message}")
    print("\nOutputs:")
    for name, path in result.outputs.items():
        print(f"  {name:<14} {path}")
    if getattr(args, "zip", False):
        print(f"  {'bundle':<14} {bundle(run_dir)}")
    return 0


def _provider_of(run_dir: Path) -> str:
    import json
    scope = run_dir / "scope.json"
    return json.loads(scope.read_text()).get("provider", "azure") if scope.exists() else "azure"


def cmd_assess(args) -> int:
    cfg = Config.load(args.config)
    if getattr(args, "billing_table", None):
        cfg.gcp_billing_table = args.billing_table
    if args.from_run:
        run_dir = Path(args.from_run)
        if not (run_dir / "raw").is_dir():
            raise ValueError(f"{run_dir} is not a run folder (no raw/ directory)")
    else:
        from .harness.wizard import collect_interactive
        args.provider = args.provider or "azure"
        run_dir = collect_interactive(args.provider, Path(args.out), cfg, subscriptions=args.subscriptions,
                                      device_code=args.device_code, tenant=args.tenant, assume_yes=args.yes)
    register = Path(args.register) if args.register else run_dir.parent / "register.db"
    return _assess_run_dir(run_dir, run_dir, args, register=register)


def cmd_register(args) -> int:
    from .register import Register

    reg = Register(args.db)
    try:
        if args.action == "list":
            rows = reg.rows(status=args.status, active_only=not args.all)
            print(f"{'id':>4}  {'status':<11} {'conf':<6} {'est/mo':>10} {'valid/mo':>10}  {'owner':<18} title")
            for r in rows:
                print(f"{r['id']:>4}  {r['status']:<11} {r['confidence']:<6} {r['est_monthly_savings']:>10,.2f} "
                      f"{(r['validated_savings'] or 0):>10,.2f}  {(r['decision_owner'] or r['owner'] or '')[:18]:<18} "
                      f"{r['title'][:70]}{'' if r['active'] else '  (no longer detected)'}")
            t = reg.totals()
            print(f"\nopen {t['open']} · approved {t['approved']} · implemented {t['implemented']} · "
                  f"rejected {t['rejected']} · validated ${t['validated']:,.2f}/mo · realized ${t['realized']:,.2f}/mo")
        elif args.action == "update":
            fields = {k: v for k, v in (("status", args.status), ("owner", args.owner),
                                        ("validated_savings", args.validated), ("implemented_on", args.implemented_on),
                                        ("notes", args.notes)) if v is not None}
            if not fields:
                raise ValueError("Nothing to update (use --status/--owner/--validated/--implemented-on/--notes)")
            reg.update(args.id, **fields)
            print(f"Updated {args.id}: {fields}")
        elif args.action == "verify":
            for res in reg.verify():
                print(f"{res['key']}: {res['status']}"
                      + (f" realized ${res['realized_monthly']:,.2f}/mo vs est. ${res['estimated_monthly']:,.2f}/mo"
                         if "realized_monthly" in res else ""))
        elif args.action == "export":
            import csv
            rows = reg.rows(active_only=False)
            with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                cols = ["id", "key", "status", "owner", "decision_owner", "category", "title", "resource_id",
                        "confidence", "est_monthly_savings", "validated_savings", "realized_savings", "implemented_on",
                        "notes", "active"]
                w.writerow(cols)
                for r in rows:
                    w.writerow([r[c] for c in cols])
            print(f"Exported {len(rows)} findings to {args.csv}")
    finally:
        reg.close()
    return 0


def cmd_collect(args) -> int:
    from .providers import aws

    out = Path(args.out_dir)
    if not args.skip_costs:
        records = aws.collect_costs(days=args.days, group_by=args.group_by, profile=args.profile)
        write_billing_csv(records, out / "billing.csv")
        print(f"Wrote {len(records)} cost records to {out / 'billing.csv'}", file=sys.stderr)
    if not args.skip_inventory:
        resources = aws.collect_inventory(regions=args.regions, profile=args.profile, include_s3=not args.skip_s3)
        write_inventory(resources, out / "inventory.json")
        print(f"Wrote {len(resources)} resources to {out / 'inventory.json'}", file=sys.stderr)
    print(f"Next: cloudfinops analyze --billing {out / 'billing.csv'} --inventory {out / 'inventory.json'}",
          file=sys.stderr)
    return 0


def cmd_rules(args) -> int:
    for r in RULES:
        print(f"{r.id:<26} {r.scope[:44]:<45} {r.description}")
    return 0


def _report_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="TOML file overriding thresholds")
    p.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    p.add_argument("-o", "--output", help="write report to file instead of stdout")
    p.add_argument("--top", type=int, default=25, help="number of rows per section (default 25)")
    p.add_argument("--lookback-days", type=int, help="analysis window in days (default 30)")
    p.add_argument("--disable", nargs="*", metavar="RULE_ID", help="rule ids to skip")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cloudfinops", description="Analyze cloud costs and find savings.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="analyze billing CSV (+ optional inventory JSON)")
    p.add_argument("--billing", required=True, help="billing CSV (simple, FOCUS or AWS CUR columns)")
    p.add_argument("--inventory", help="resource inventory JSON (from `collect`)")
    _report_args(p)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("demo", help="run analysis on bundled sample data")
    p.add_argument("--provider", choices=["aws", "azure"], default="aws")
    p.add_argument("--out", help="azure: output folder (default ./demo-azure)")
    _report_args(p)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("assess", help="read-only assessment: login, collect, analyze, report")
    p.add_argument("--provider", choices=["azure", "gcp"], help="default azure; with --from, read from the run folder")
    p.add_argument("--subscriptions", "--projects", dest="subscriptions", nargs="*",
                   help="subscription/project ids or names (default: pick interactively)")
    p.add_argument("--billing-table", help="gcp: BigQuery billing export table project.dataset.table")
    p.add_argument("--tenant", help="tenant id/domain to sign in to")
    p.add_argument("--device-code", action="store_true", help="sign in with a device code instead of a browser")
    p.add_argument("--yes", action="store_true", help="non-interactive: accept defaults, all visible subscriptions")
    p.add_argument("--from", dest="from_run", metavar="RUN_DIR", help="re-analyze an existing run folder offline")
    p.add_argument("--out", default="runs", help="folder for run folders (default ./runs)")
    p.add_argument("--register", help="savings register database (default <out>/register.db)")
    p.add_argument("--zip", action="store_true", help="also zip the run folder for hand-over")
    p.add_argument("--config", help="TOML file overriding thresholds")
    p.add_argument("--lookback-days", type=int)
    p.add_argument("--disable", nargs="*", metavar="RULE_ID")
    p.set_defaults(func=cmd_assess)

    p = sub.add_parser("register", help="savings register: track decisions, validated and realized savings")
    p.add_argument("action", choices=["list", "update", "verify", "export"])
    p.add_argument("id", nargs="?", help="finding id (number from `list`) or key, for update")
    p.add_argument("--db", default="runs/register.db")
    p.add_argument("--status", help="filter (list) or new status (update)")
    p.add_argument("--owner")
    p.add_argument("--validated", type=float, help="validated monthly savings")
    p.add_argument("--implemented-on", help="YYYY-MM-DD the change was made")
    p.add_argument("--notes")
    p.add_argument("--all", action="store_true", help="include findings no longer detected")
    p.add_argument("--csv", default="register.csv", help="export target")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("collect", help="pull costs + inventory from a cloud provider")
    p.add_argument("provider", choices=["aws"])
    p.add_argument("--profile", help="AWS named profile")
    p.add_argument("--regions", nargs="*", help="regions to scan (default: all enabled)")
    p.add_argument("--days", type=int, default=60, help="days of cost history (default 60)")
    p.add_argument("--group-by", choices=["account", "region", "purchase_type"], default="account",
                   help="second Cost Explorer grouping besides service")
    p.add_argument("--out-dir", default="data")
    p.add_argument("--skip-costs", action="store_true")
    p.add_argument("--skip-inventory", action="store_true")
    p.add_argument("--skip-s3", action="store_true")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("rules", help="list available optimization rules")
    p.set_defaults(func=cmd_rules)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
