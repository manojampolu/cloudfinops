"""Render analysis + recommendations as text, Markdown or JSON."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass

from .analysis import CostSummary
from .models import Recommendation
from .rules import total_savings


@dataclass
class Report:
    summary: CostSummary
    recommendations: list[Recommendation]
    lookback_days: int

    @property
    def savings(self) -> float:
        return total_savings(self.recommendations)

    def by_category(self) -> list[tuple[str, int, float]]:
        agg = defaultdict(lambda: [0, 0.0])
        for r in self.recommendations:
            agg[r.category][0] += 1
            agg[r.category][1] += r.est_monthly_savings
        return sorted(((k, v[0], v[1]) for k, v in agg.items()), key=lambda x: -x[2])


def money(v: float | None) -> str:
    if v is None:
        return "-"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}" if abs(v) < 1000 else f"{sign}${abs(v):,.0f}"


def pct(v: float | None) -> str:
    return "-" if v is None else f"{v:+.0%}" if v else "0%"


def _table(headers: list[str], rows: list[list[str]], md: bool) -> str:
    if md:
        out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
        out += ["| " + " | ".join(c.replace("|", "\\|") for c in row) + " |" for row in rows]
        return "\n".join(out)
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    widths = [min(w, 60) for w in widths]
    fmt = lambda row: "  ".join(c[:widths[i]].ljust(widths[i]) for i, c in enumerate(row))  # noqa: E731
    return "\n".join([fmt(headers), fmt(["-" * w for w in widths])] + [fmt(r) for r in rows])


def render(report: Report, fmt: str = "text", top: int = 25) -> str:
    if fmt == "json":
        return render_json(report)
    md = fmt == "markdown"
    s = report.summary
    h1 = (lambda t: f"# {t}") if md else (lambda t: f"{t}\n{'=' * len(t)}")
    h2 = (lambda t: f"\n## {t}\n") if md else (lambda t: f"\n{t}\n{'-' * len(t)}")
    window_share = report.savings / s.window_total * 30 / report.lookback_days if s.window_total else 0
    out = [
        h1("Cloud Cost Optimization Report"),
        f"\nBilling data: {s.start} to {s.end}  |  analysis window: last {report.lookback_days} days\n",
        _table(["Metric", "Value"], [
            [f"Spend (last {report.lookback_days}d)", money(s.window_total)],
            ["Daily average", money(s.daily_avg)],
            ["30-day run-rate (from last 7d)", money(s.run_rate_30d)],
            ["Fully tagged spend", f"{s.fully_tagged_share:.0%}"],
            ["Recommendations", str(len(report.recommendations))],
            ["Est. monthly savings", f"{money(report.savings)} (~{window_share:.0%} of spend)"],
            ["Est. annual savings", money(report.savings * 12)],
        ], md),
    ]

    if report.recommendations:
        out.append(h2("Savings by category"))
        out.append(_table(["Category", "Count", "Est. monthly"],
                          [[c, str(n), money(v)] for c, n, v in report.by_category()], md))

        out.append(h2(f"Top recommendations"))
        rows = [[str(i), r.severity, r.category, r.title, r.resource_id or "(account)", r.region or "-",
                 money(r.est_monthly_savings) if r.est_monthly_savings else "-", r.effort]
                for i, r in enumerate(report.recommendations[:top], 1)]
        out.append(_table(["#", "Sev", "Category", "Recommendation", "Resource", "Region", "Save/mo", "Effort"],
                          rows, md))

        out.append(h2("Recommendation details"))
        for i, r in enumerate(report.recommendations[:top], 1):
            target = f" `{r.resource_id}`" if md and r.resource_id else (f" [{r.resource_id}]" if r.resource_id else "")
            bullet = "-" if md else " "
            out.append(f"{'**' if md else ''}{i}. {r.title}{'**' if md else ''}{target}")
            if r.detail:
                out.append(f"   {bullet} Why: {r.detail}")
            out.append(f"   {bullet} Action: {r.action}")
            out.append(f"   {bullet} Current: {money(r.current_monthly_cost)}/mo, "
                       f"saving: {money(r.est_monthly_savings)}/mo, risk: {r.risk}\n")
        if len(report.recommendations) > top:
            out.append(f"... {len(report.recommendations) - top} more (use --top to show more)")

    out.append(h2("Spend by service"))
    out.append(_table(["Service", "Cost", "Share"],
                      [[k, money(v), f"{v / s.window_total:.1%}"] for k, v in s.by_service], md))

    if s.changes:
        out.append(h2(f"Biggest changes vs previous {report.lookback_days} days"))
        out.append(_table(["Service", "Previous", "Current", "Change", "%"],
                          [[c.service, money(c.previous), money(c.current), money(c.delta), pct(c.pct)]
                           for c in s.changes], md))

    if s.anomalies:
        out.append(h2("Cost anomalies"))
        out.append(_table(["Date", "Service", "Actual", "Expected", "z-score"],
                          [[str(a.day), a.service, money(a.cost), money(a.expected), f"{a.z_score:.1f}"]
                           for a in s.anomalies], md))

    if len(s.by_account) > 1:
        out.append(h2("Spend by account"))
        out.append(_table(["Account", "Cost"], [[k, money(v)] for k, v in s.by_account], md))
    if s.by_region:
        out.append(h2("Spend by region"))
        out.append(_table(["Region", "Cost"], [[k, money(v)] for k, v in s.by_region], md))
    if s.tag_coverage:
        out.append(h2("Tag coverage (share of spend)"))
        out.append(_table(["Tag", "Coverage"], [[k, f"{v:.0%}"] for k, v in s.tag_coverage.items()], md))

    out.append("\nSavings are estimates; overlapping recommendations on the same resource are "
               "counted once (the largest). Validate with resource owners before acting.")
    return "\n".join(out) + "\n"


def render_json(report: Report) -> str:
    s = report.summary
    payload = {
        "period": {"start": str(s.start), "end": str(s.end), "lookback_days": report.lookback_days},
        "summary": {
            "window_spend": round(s.window_total, 2), "daily_avg": round(s.daily_avg, 2),
            "run_rate_30d": round(s.run_rate_30d, 2), "fully_tagged_share": round(s.fully_tagged_share, 4),
            "est_monthly_savings": round(report.savings, 2), "est_annual_savings": round(report.savings * 12, 2),
        },
        "by_service": [{"service": k, "cost": round(v, 2)} for k, v in s.by_service],
        "by_account": [{"account": k, "cost": round(v, 2)} for k, v in s.by_account],
        "by_region": [{"region": k, "cost": round(v, 2)} for k, v in s.by_region],
        "changes": [{"service": c.service, "previous": round(c.previous, 2), "current": round(c.current, 2)}
                    for c in s.changes],
        "anomalies": [{"date": str(a.day), "service": a.service, "cost": round(a.cost, 2),
                       "expected": round(a.expected, 2), "z_score": round(a.z_score, 2)} for a in s.anomalies],
        "tag_coverage": {k: round(v, 4) for k, v in s.tag_coverage.items()},
        "recommendations": [r.to_dict() for r in report.recommendations],
    }
    return json.dumps(payload, indent=2, default=str)
