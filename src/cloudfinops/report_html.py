"""Self-contained HTML assessment report (no external assets; works offline and as an email attachment)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape

from .analysis import CostSummary
from .config import Config
from .estate import Node, ServiceProfile
from .models import Dataset, Recommendation
from .quality import QualityIssue, period_label
from .rules import scenario_total

CATEGORY_LABELS = {
    "waste": "Idle & orphaned", "rightsizing": "Rightsizing", "scheduling": "Non-prod scheduling",
    "modernization": "Storage & SKU modernization", "logging": "Logging", "commitment": "Commitments",
    "advisor": "Azure Advisor", "governance": "Governance", "licensing": "Licensing (Hybrid Benefit, Defender)",
}


def m(v: float | None, cur: str = "$") -> str:
    if v is None:
        return "–"
    sign = "-" if v < 0 else ""
    v = abs(v)
    return f"{sign}{cur}{v:,.0f}" if v >= 100 else f"{sign}{cur}{v:,.2f}"


def e(s) -> str:
    return escape(str(s if s is not None else ""))


CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1f23;--muted:#62666d;--line:#e3e3df;--accent:#2357d9;--accent-soft:#e8eefc;
--good:#18794e;--good-soft:#e6f4ec;--warn:#9a5b00;--warn-soft:#fdf1dc;--bad:#b42318;--bad-soft:#fdecea;--bar:#2357d9;
color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#131416;--panel:#1c1d20;--ink:#ecedee;--muted:#9a9ea6;--line:#2c2e33;
--accent:#7aa2ff;--accent-soft:#1f2a44;--good:#5fd19a;--good-soft:#173326;--warn:#f0b357;--warn-soft:#3a2c12;
--bad:#ff8a80;--bad-soft:#3d1c1a;--bar:#7aa2ff;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 80px}
header h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}header p{margin:0;color:var(--muted)}
nav{display:flex;flex-wrap:wrap;gap:6px;margin:20px 0 8px}nav a{color:var(--accent);text-decoration:none;
padding:4px 10px;border:1px solid var(--line);border-radius:999px;background:var(--panel);font-size:13px}
section{margin-top:36px}h2{font-size:18px;margin:0 0 12px}h3{font-size:15px;margin:0 0 6px}
.muted{color:var(--muted)}.small{font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:20px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi .v{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}.kpi .l{color:var(--muted);font-size:12px}
.kpi.hl{border-color:var(--accent);background:var(--accent-soft)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600}td.n,th.n{text-align:right;white-space:nowrap}
.scroll{overflow-x:auto}
.bars .row{display:grid;grid-template-columns:200px 1fr 150px;gap:10px;align-items:center;margin:6px 0}
.track{background:var(--line);border-radius:4px;height:10px;overflow:hidden}.fill{background:var(--bar);height:100%}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;white-space:nowrap}
.c-high{background:var(--good-soft);color:var(--good)}.c-medium{background:var(--warn-soft);color:var(--warn)}
.c-low{background:var(--bad-soft);color:var(--bad)}.tag{background:var(--accent-soft);color:var(--accent)}
.q-ok{color:var(--good)}.q-info{color:var(--muted)}.q-warn{color:var(--warn)}.q-error{color:var(--bad)}
.tree details{margin-left:18px}.tree>details{margin-left:0}
.tree summary{cursor:pointer;padding:3px 0;list-style:none;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.tree summary::-webkit-details-marker{display:none}
.tree summary:before{content:"▸";color:var(--muted);width:10px}.tree details[open]>summary:before{content:"▾"}
.tree .leaf{margin-left:36px;padding:2px 0;display:flex;gap:8px;flex-wrap:wrap;align-items:baseline}
.tree .cost{font-variant-numeric:tabular-nums;font-weight:600}.tree .mini{display:inline-block;width:60px;height:6px;
background:var(--line);border-radius:3px;overflow:hidden;vertical-align:middle}.tree .mini i{display:block;height:100%;background:var(--bar)}
.k-service>summary{font-weight:600}.k-unmapped>summary,.k-rollup{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.card dl{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;margin:8px 0 0;font-size:13px}
.card dt{color:var(--muted)}.card dd{margin:0}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.filters select,.filters input{padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
.opp{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:8px}
.opp>summary{display:grid;grid-template-columns:38px 1fr 150px 190px;gap:10px;padding:10px 14px;cursor:pointer;list-style:none;align-items:center}
.opp>summary::-webkit-details-marker{display:none}
.opp .t{font-weight:600}.opp .sub{color:var(--muted);font-size:12px}.opp .s{text-align:right;font-weight:650;font-variant-numeric:tabular-nums}
.opp .body{padding:0 14px 14px 62px;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.opp .body h4{margin:6px 0 4px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.opp ul{margin:0;padding-left:18px}.opp .act{grid-column:1/-1;background:var(--accent-soft);border-radius:8px;padding:8px 12px}
.owner{break-inside:avoid;margin-bottom:12px}
@media (max-width:640px){.bars .row{grid-template-columns:1fr;gap:2px}.opp>summary{grid-template-columns:30px 1fr}
.opp .s,.opp .cf{text-align:left}.opp .body{padding-left:14px}}
@media print{nav,.filters{display:none}.opp{break-inside:avoid}details{open:true}}
"""

JS = """
const q=s=>document.querySelector(s),qa=s=>[...document.querySelectorAll(s)];
function f(){const c=q('#fc').value,k=q('#fk').value,o=q('#fo').value,t=q('#ft').value.toLowerCase();let n=0;
qa('.opp').forEach(d=>{const ok=(!c||d.dataset.c===c)&&(!k||d.dataset.k===k)&&(!o||d.dataset.o===o)&&(!t||d.textContent.toLowerCase().includes(t));
d.hidden=!ok;if(ok)n++});q('#fn').textContent=n+' shown'}
qa('.filters select,.filters input').forEach(el=>el.addEventListener('input',f));
q('#expand').addEventListener('click',()=>qa('.tree details').forEach(d=>d.open=true));
q('#collapse').addEventListener('click',()=>qa('.tree details').forEach(d=>d.open=false));
"""


def _tree(node: Node, parent_cost: float, depth: int = 0) -> str:
    pct = (node.cost / parent_cost * 100) if parent_cost else 0
    badges = "".join(f'<span class="pill tag">{e(b)}</span>' for b in node.badges if b)
    if node.findings and node.kind != "root":
        badges += f'<span class="pill c-medium">{node.findings} finding{"s" if node.findings != 1 else ""} · {m(node.savings)}/mo</span>'
    count = f'<span class="muted small">{node.count}</span>' if node.count and node.kind not in ("resource",) else ""
    detail = f'<span class="muted small">{e(node.detail)}</span>' if node.detail else ""
    head = (f'<span>{e(node.label)}</span>{count}<span class="cost">{m(node.cost)}</span>'
            f'<span class="mini"><i style="width:{min(100, pct):.0f}%"></i></span>{badges}{detail}')
    if not node.children:
        return f'<div class="leaf k-{node.kind}">{head}</div>'
    opened = " open" if depth < 2 else ""
    inner = "".join(_tree(c, node.cost, depth + 1) for c in node.children)
    return f'<details class="k-{node.kind}"{opened}><summary>{head}</summary>{inner}</details>'


def _profile_card(p: ServiceProfile) -> str:
    change = ""
    if p.previous_cost:
        d = (p.monthly_cost - p.previous_cost) / p.previous_cost
        txt = "no change" if abs(d) < 0.005 else f"{d:+.0%}"
        change = f' <span class="muted small">({txt} vs prev. period)</span>'
    u = p.utilization
    util_bits = [f"{int(u[k])} {k}" for k in ("idle", "low", "normal", "high", "no metrics", "stopped") if u.get(k)]
    rows = [
        ("Resources", f"{p.count}"),
        ("Cost / month", f"{m(p.monthly_cost)} · {p.share:.0%} of mapped spend{change}"),
        ("Top SKUs", ", ".join(f"{e(s)} ×{n} ({m(c)})" for s, n, c in p.skus[:4])),
        ("Regions", ", ".join(f"{e(r or '–')} ×{n}" for r, n in p.regions)),
    ]
    if util_bits:
        rows.append(("Utilization", " · ".join(util_bits)))
    if "median_cpu_avg" in u:
        rows.append(("CPU", f"median avg {u['median_cpu_avg']:.0f}% · highest p95 {u['max_cpu_p95']:.0f}%"))
    if p.metrics_coverage is not None:
        rows.append(("Metrics coverage", f"{p.metrics_coverage:.0%} of running resources"))
    rows.append(("Missing tags", f"{p.untagged_share:.0%} of resources"))
    if p.findings:
        rows.append(("Findings", f"{p.findings} · est. {m(p.est_savings)}/mo"))
    if p.advisor_recs:
        rows.append(("Azure Advisor", f"{p.advisor_recs} cost recommendation(s)"))
    rows.append(("Largest", ", ".join(f"{e(n)} ({m(c)})" for n, c, _ in p.top_resources[:3])))
    dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    return f'<div class="card"><h3>{e(p.service)}</h3><dl>{dl}</dl></div>'


def _list(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{e(i)}</li>" for i in items) + "</ul>" if items else '<p class="muted small">None</p>'


def _opp(i: int, r: Recommendation, decision: dict | None, by_name: dict[str, str], counted: bool = True) -> str:
    rng = f"{m(r.savings_low)}–{m(r.savings_high)}" if r.savings_high and r.savings_low != r.savings_high else ""
    nv = ' · <span class="pill c-low">needs validation</span>' if r.needs_validation else ""
    status = f' · <span class="pill tag">{e(decision["status"])}</span>' if decision and decision.get("status") != "open" else ""
    if not counted and r.est_monthly_savings:
        status += ' · <span class="pill tag" title="Another finding on the same resource/scenario is larger; only one is counted">alternative, not counted in total</span>'
    where = " · ".join(x for x in (r.service, r.resource_name or ("account-level" if not r.resource_id else ""),
                                   r.region, f"owner: {r.owner}" if r.owner else "") if x)
    related = [by_name.get(x, x.rsplit("/", 1)[-1]) for x in r.related[:12]]
    cur = f"Current cost {m(r.current_monthly_cost)}/mo · " if r.current_monthly_cost is not None else ""
    savings_text = m(r.est_monthly_savings) + "/mo" if r.est_monthly_savings else (f"up to {m(r.savings_high)}" if r.savings_high else "–")
    return f"""
<details class="opp" data-c="{e(r.category)}" data-k="{e(r.confidence)}" data-o="{e(r.owner)}">
<summary><span class="muted">#{i}</span><span><span class="t">{e(r.title)}</span><br>
<span class="sub">{e(CATEGORY_LABELS.get(r.category, r.category))} · {e(where)}{nv}{status}</span></span>
<span class="s">{savings_text}<br><span class="sub">{rng}</span></span>
<span class="cf"><span class="pill c-{e(r.confidence)}">{e(r.confidence)} confidence</span><br><span class="sub">effort {e(r.effort)} · risk {e(r.risk)}</span></span>
</summary>
<div class="body">
<div><h4>Evidence</h4>{_list(r.evidence or ([r.detail] if r.detail else []))}</div>
<div><h4>Assumptions</h4>{_list(r.assumptions)}</div>
<div><h4>Missing evidence</h4>{_list(r.missing_evidence)}</div>
<div><h4>Validate before changing</h4>{_list(r.validation_steps)}</div>
<div><h4>Connected resources</h4>{_list(related)}</div>
<div><h4>Reference</h4><p class="small muted" style="word-break:break-all">{e(r.resource_id or 'account-level')}<br>rule {e(r.rule_id)}{' · ' + cur.rstrip(' · ') if cur else ''}</p></div>
<div class="act"><strong>Proposed action:</strong> {e(r.action)}</div>
</div></details>"""


def render_html(ds: Dataset, summary: CostSummary, recs: list[Recommendation], quality: list[QualityIssue],
                tree: Node, profiles: list[ServiceProfile], cfg: Config, register_totals: dict | None = None,
                decisions: dict | None = None, command_stats: dict | None = None) -> str:
    decisions = decisions or {}
    totals = scenario_total(recs)
    spend = summary.window_total / max(1, min(cfg.lookback_days, (summary.end - summary.start).days + 1)) * 30
    nv = sum(1 for r in recs if r.needs_validation)
    subs = ds.scope.get("subscriptions") or []
    scope_txt = ", ".join(s.get("name", s.get("id", "")) for s in subs) or "uploaded data"
    if ds.scope.get("resource_groups"):
        scope_txt += " · resource groups: " + ", ".join(ds.scope["resource_groups"])
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    by_name = {r.id: r.display_name for r in ds.resources}

    # savings by category (scenario-deduped within category)
    by_cat = defaultdict(list)
    for r in recs:
        by_cat[r.category].append(r)
    cat_rows = sorted(((c, scenario_total(v), len(v)) for c, v in by_cat.items()), key=lambda x: -x[1]["estimate"])
    max_cat = max((t["high"] for _, t, _ in cat_rows), default=1) or 1
    bars = "".join(
        f'<div class="row"><span>{e(CATEGORY_LABELS.get(c, c))} <span class="muted small">({n})</span></span>'
        f'<span class="track"><span class="fill" style="display:block;width:{t["estimate"] / max_cat * 100:.1f}%"></span></span>'
        f'<span class="n" style="text-align:right">{m(t["estimate"])} <span class="muted small">{m(t["low"])}–{m(t["high"])}</span></span></div>'
        for c, t, n in cat_rows)

    reg = register_totals or {}
    has_bill = bool(ds.records)
    kpis = [
        ("Monthly spend (last 30 days)", m(spend) if has_bill else "unknown", ""),
        ("30-day run-rate", m(summary.run_rate_30d) if has_bill else "no billing data", ""),
        ("Estimated savings / month", m(totals["estimate"]), "hl"),
        ("Savings range", f"{m(totals['low'])} – {m(totals['high'])}", ""),
        ("Share of spend", f"{(totals['estimate'] / spend):.0%}" if spend else "–", ""),
        ("Findings", f"{len(recs)} ({nv} need validation)", ""),
        ("Validated savings", m(reg.get("validated")) if reg.get("validated") else "–", ""),
        ("Realized savings", m(reg.get("realized")) if reg.get("realized") else "–", ""),
    ]
    kpi_html = "".join(f'<div class="kpi {c}"><div class="v">{v}</div><div class="l">{e(l)}</div></div>' for l, v, c in kpis)

    top = [r for r in recs if r.est_monthly_savings > 0][:10]
    top_rows = "".join(
        f"<tr><td>{i}</td><td>{e(r.title)}<div class='muted small'>{e(' · '.join(x for x in (r.resource_name or 'account-level', r.owner) if x))}</div></td>"
        f"<td>{e(CATEGORY_LABELS.get(r.category, r.category))}</td><td><span class='pill c-{e(r.confidence)}'>{e(r.confidence)}</span></td>"
        f"<td class='n'>{m(r.est_monthly_savings)}</td></tr>" for i, r in enumerate(top, 1))

    cats = sorted({r.category for r in recs})
    owners = sorted({r.owner for r in recs if r.owner})
    opts = lambda vals, lab: "".join(f'<option value="{e(v)}">{e(lab(v))}</option>' for v in vals)  # noqa: E731
    best: dict[str, Recommendation] = {}
    for r in recs:
        if r.scenario_group not in best or r.est_monthly_savings > best[r.scenario_group].est_monthly_savings:
            best[r.scenario_group] = r
    counted = {id(r) for r in best.values()}
    opp_html = "".join(_opp(i, r, decisions.get(r.key), by_name, id(r) in counted) for i, r in enumerate(recs, 1))

    by_owner = defaultdict(list)
    for r in recs:
        if r.est_monthly_savings > 0 or r.savings_high:
            by_owner[r.owner or "(unassigned)"].append(r)
    owner_html = ""
    for owner, items in sorted(by_owner.items(), key=lambda kv: -scenario_total(kv[1])["estimate"]):
        t = scenario_total(items)
        rows = "".join(
            f"<tr><td>{e(r.title)}<div class='muted small'>{e(r.resource_name or 'account-level')}</div></td>"
            f"<td><span class='pill c-{e(r.confidence)}'>{e(r.confidence)}</span></td>"
            f"<td>{e((decisions.get(r.key) or {}).get('status', 'open'))}</td><td class='n'>{m(r.est_monthly_savings)}</td></tr>"
            for r in items)
        owner_html += (f'<div class="panel owner"><h3>{e(owner)} <span class="muted small">{len(items)} findings · '
                       f'est. {m(t["estimate"])}/mo</span></h3><div class="scroll"><table><tr><th>Finding</th><th>Confidence</th>'
                       f'<th>Status</th><th class="n">Est./mo</th></tr>{rows}</table></div></div>')

    q_html = "".join(f'<tr><td class="q-{e(q.level)}">{e(q.level.upper())}</td><td>{e(q.area)}</td>'
                     f'<td>{e(q.message)}<div class="muted small">{e(q.impact)}</div></td></tr>' for q in quality)

    svc_rows = "".join(f"<tr><td>{e(k)}</td><td class='n'>{m(v)}</td><td class='n'>{v / summary.window_total:.1%}</td></tr>"
                       for k, v in summary.by_service) if summary.window_total else ""
    anomalies = "".join(f"<tr><td>{a.day}</td><td>{e(a.service)}</td><td class='n'>{m(a.cost)}</td><td class='n'>{m(a.expected)}</td>"
                        f"<td class='n'>{a.z_score:.1f}</td></tr>" for a in summary.anomalies)
    changes = "".join(f"<tr><td>{e(c.service)}</td><td class='n'>{m(c.previous)}</td><td class='n'>{m(c.current)}</td>"
                      f"<td class='n'>{m(c.delta)}</td><td class='n'>{'' if c.pct is None else f'{c.pct:+.0%}'}</td></tr>"
                      for c in summary.changes[:8])

    cmd = command_stats or {}
    cmd_html = ""
    if cmd:
        cmd_html = (f"<p>{cmd.get('total', 0)} CLI calls, all read-only (allowlist-enforced); "
                    f"{cmd.get('failed', 0)} failed. Full log: <code>commands.log</code> in the run folder.</p><ul>"
                    + "".join(f"<li><code>{e(k)}</code> × {v}</li>" for k, v in cmd.get("by_command", {}).items()) + "</ul>")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Cloud Cost Assessment – {e(scope_txt)}</title>
<style>{CSS}</style></head><body><div class="wrap">
<header><h1>Cloud Cost Assessment</h1>
<p>{e(ds.provider.upper())} · {e(scope_txt)} · {('billing ' + e(period_label(summary, cfg))) if ds.records else 'no billing data'} · generated {generated}</p>
{''.join(f'<p class="q-error small" style="margin-top:6px">{e(n)}</p>' for n in ds.scope.get('notes', []))}
{''.join(f'<p class="muted small" style="margin-top:6px">{e(n)}</p>' for n in ds.scope.get('info_notes', []))}</header>
<nav><a href="#summary">Summary</a><a href="#map">Estate map</a><a href="#services">Services</a>
<a href="#opportunities">Opportunities</a><a href="#owners">Owner review</a><a href="#spend">Spend trends</a>
<a href="#quality">Data quality</a><a href="#method">Method</a></nav>

<section id="summary"><div class="kpis">{kpi_html}</div>
<div class="panel" style="margin-top:14px"><h3>Savings by module</h3><div class="bars">{bars or '<p class="muted">No findings.</p>'}</div>
<p class="muted small">Estimates are not additive across alternatives: findings on the same resource count once (largest).
Ranges reflect confidence. Nothing has been changed in the environment.</p></div>
<div class="panel" style="margin-top:14px"><h3>Top opportunities</h3><div class="scroll"><table>
<tr><th>#</th><th>Finding</th><th>Module</th><th>Confidence</th><th class="n">Est./mo</th></tr>{top_rows}</table></div></div></section>

<section id="map"><h2>Estate map</h2><p class="muted small">Provider → subscription/project → service → SKU → resources, sized by monthly cost.
Large services are grouped by SKU and utilization; only the top {10} items per group are listed (full list in findings.csv / inventory).
<button id="expand">Expand all</button> <button id="collapse">Collapse all</button></p>
<div class="panel tree">{_tree(tree, tree.cost)}</div></section>

<section id="services"><h2>Service profiles</h2><div class="cards">{''.join(_profile_card(p) for p in profiles if p.count)}</div></section>

<section id="opportunities"><h2>Opportunities ({len(recs)})</h2>
<div class="filters"><select id="fc"><option value="">All modules</option>{opts(cats, lambda c: CATEGORY_LABELS.get(c, c))}</select>
<select id="fk"><option value="">Any confidence</option>{opts(['high', 'medium', 'low'], str)}</select>
<select id="fo"><option value="">All owners</option>{opts(owners, str)}</select>
<input id="ft" placeholder="Search…"><span id="fn" class="muted small" style="align-self:center"></span></div>
{opp_html}</section>

<section id="owners"><h2>Owner review sheets</h2><p class="muted small">Suggested reviewer = owner tag, otherwise the resource group.
Use these in team discussions; record decisions with <code>cloudfinops register update</code>.</p>{owner_html}</section>

<section id="spend"><h2>Spend trends</h2><div class="cards">
<div class="card"><h3>Spend by billing service (last {cfg.lookback_days} days)</h3><div class="scroll"><table><tr><th>Service</th><th class="n">Cost</th><th class="n">Share</th></tr>{svc_rows}</table></div></div>
<div class="card"><h3>Biggest changes vs previous period</h3><div class="scroll"><table><tr><th>Service</th><th class="n">Prev.</th><th class="n">Current</th><th class="n">Δ</th><th class="n">%</th></tr>{changes or '<tr><td colspan=5 class="muted">Not enough history</td></tr>'}</table></div></div>
<div class="card"><h3>Cost anomalies</h3><div class="scroll"><table><tr><th>Date</th><th>Service</th><th class="n">Actual</th><th class="n">Expected</th><th class="n">z</th></tr>{anomalies or '<tr><td colspan=5 class="muted">None detected</td></tr>'}</table></div></div>
</div></section>

<section id="quality"><h2>Data quality &amp; gaps</h2><div class="panel scroll"><table><tr><th>Level</th><th>Area</th><th>Finding</th></tr>{q_html}</table></div></section>

<section id="method"><h2>Method &amp; scope</h2><div class="cards">
<div class="card"><h3>Three savings numbers</h3><p><strong>Estimated</strong>: calculated from collected billing, configuration and
utilization under stated assumptions. <strong>Validated</strong>: confirmed by the owner after review (savings register).
<strong>Realized</strong>: measured from billing after implementation (does not adjust for workload growth).</p>
<p class="small muted">Confidence: high = configuration fact (e.g. unattached disk); medium = utilization-based with complete metrics;
low = key evidence missing (marked "needs validation").</p></div>
<div class="card"><h3>How costs are derived</h3><p>Resource cost = amortized billing for the last {cfg.lookback_days} days, normalized to 30 days.
Where billing has no resource line, list prices are used and labelled as estimates. Alternative SKUs are priced from the
Azure Retail Prices API or a fallback table.</p></div>
<div class="card"><h3>Collection</h3><p>Read-only access ({'Viewer-level roles; prompts disabled so no API is ever enabled' if ds.provider == 'gcp' else 'Reader, Cost Management Reader, Log Analytics Reader'}). No changes were made.</p>{cmd_html}</div>
</div></section>
</div><script>{JS}f();</script></body></html>"""


def command_stats(log_path) -> dict:
    stats = {"total": 0, "failed": 0, "by_command": Counter()}
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                stats["total"] += 1
                stats["failed"] += entry.get("exit", 0) != 0
                words = []
                for tok in entry["cmd"].split():
                    if tok.startswith("-"):
                        break
                    words.append(tok)
                stats["by_command"][" ".join(words[:5])] += 1
    except FileNotFoundError:
        return {}
    stats["by_command"] = dict(stats["by_command"].most_common())
    return stats
