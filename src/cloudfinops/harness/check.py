"""`cloudfinops check`: is this machine and identity ready for an assessment?

Runs a handful of cheap read-only probes (one per data source the assessment uses) and prints what will work, what
will be missing from the report, and how to fix it. It never signs in, never changes anything, and is safe to run
repeatedly.
"""

from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .errors import is_permission_error, short_error
from .runner import CliError, CliRunner

OK, MISSING, WARN, SKIP = "OK", "MISSING", "WARN", "SKIP"


@dataclass
class Probe:
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    critical: bool = False


@dataclass
class CheckReport:
    provider: str
    probes: list[Probe] = field(default_factory=list)

    def add(self, name, status, detail="", fix="", critical=False) -> Probe:
        p = Probe(name, status, detail, fix, critical)
        self.probes.append(p)
        return p

    @property
    def ready(self) -> bool:
        return not any(p.critical and p.status == MISSING for p in self.probes)

    def render(self) -> str:
        lines = [f"cloudfinops check · {self.provider}", ""]
        width = max((len(p.name) for p in self.probes), default=10)
        for p in self.probes:
            lines.append(f"  [{p.status:<7}] {p.name:<{width}}  {p.detail}")
            if p.fix and p.status in (MISSING, WARN):
                lines.append(f"  {'':9} {'':<{width}}  -> {p.fix}")
        missing = [p.name for p in self.probes if p.status == MISSING and not p.critical]
        lines.append("")
        if not self.ready:
            lines.append("Not ready: fix the MISSING items marked above before running an assessment.")
        elif missing:
            lines.append(f"Ready, with gaps: the report will be missing {', '.join(missing)}.")
        else:
            lines.append("Ready: everything the assessment reads is accessible.")
        return "\n".join(lines)


def _denied_action(msg: str) -> str:
    """The permission that was missing, e.g. ce:GetCostAndUsage or Microsoft.CostManagement/query/action."""
    m = (re.search(r"perform(?:\s+action)?[: ]+'?([A-Za-z0-9-]+[:/][A-Za-z0-9*/._-]+)", msg)
         or re.search(r"Permission '([^']+)' denied", msg))
    return m.group(1) if m else short_error(msg)[:90]


def _probe(report: CheckReport, name: str, fn, fix: str, critical: bool = False, ok_detail=None,
           optional: bool = False):
    """Run one read; classify the outcome."""
    try:
        result = fn()
    except CliError as exc:
        msg = str(exc)
        if is_permission_error(msg):
            return report.add(name, WARN if optional else MISSING, f"access denied ({_denied_action(msg)})", fix, critical)
        low = msg.lower()
        if any(k in low for k in ("optinrequired", "not opted in", "not subscribed", "enrollment")):
            return report.add(name, WARN, short_error(msg)[:110], fix)
        return report.add(name, WARN if not critical else MISSING, short_error(msg)[:110], fix, critical)
    detail = ok_detail(result) if ok_detail else ""
    return report.add(name, OK, detail)


def run_check(provider: str, cfg, scopes: list[str] | None = None) -> CheckReport:
    runner = CliRunner(tempfile.mkdtemp(prefix="cloudfinops-check-"), timeout=60, retries=1)
    report = CheckReport(provider)
    report.add("Python", OK if sys.version_info >= (3, 11) else MISSING, sys.version.split()[0],
               "Install Python 3.11+", critical=True)
    {"aws": _check_aws, "azure": _check_azure, "gcp": _check_gcp}[provider](runner, report, cfg, scopes or [])
    return report


# ---------------------------------------------------------------- AWS

def _check_aws(runner: CliRunner, report: CheckReport, cfg, scopes: list[str]) -> None:
    prof = ["--profile", cfg.aws_profile] if cfg.aws_profile else []

    def aws(*args):
        return runner.run(["aws", *args, "--output", "json", *prof])

    if not runner.available("aws"):
        report.add("AWS CLI", MISSING, "not installed", "brew install awscli (or see docs/permissions.md)", critical=True)
        return
    report.add("AWS CLI", OK, "installed")
    ident = _probe(report, "Signed in", lambda: aws("sts", "get-caller-identity"),
                   "Run `aws configure` or `aws sso login` (add --profile <name> for named profiles)", critical=True,
                   ok_detail=lambda r: f"{r.get('Arn')} (account {r.get('Account')})")
    if ident.status != OK:
        return
    region = (cfg.aws_regions or [None])[0]
    if not region:
        try:
            region = (runner.run(["aws", "configure", "get", "region", *prof], parse_json=False) or "").strip()
        except CliError:
            region = ""
    region = region or "us-east-1"
    r = ["--region", region]
    today = date.today()
    fix_ro = "Attach the ReadOnlyAccess managed policy to this identity"
    _probe(report, "Cost Explorer (spend)", lambda: aws(
        "ce", "get-cost-and-usage", "--region", "us-east-1", "--time-period",
        f"Start={today - timedelta(days=2)},End={today}", "--granularity", "DAILY", "--metrics", "AmortizedCost"),
        "Attach AWSBillingReadOnlyAccess, or pass a CUR export with --billing-csv",
        ok_detail=lambda _: "readable (this probe is one $0.01 Cost Explorer request)")
    _probe(report, "Regions", lambda: aws("ec2", "describe-regions", "--region", region),
           fix_ro, critical=True, ok_detail=lambda d: f"{len(d.get('Regions', []))} enabled")
    for name, cmd in (("EC2 / EBS / VPC", ("ec2", "describe-instances", "--max-results", "5")),
                      ("RDS", ("rds", "describe-db-instances", "--max-records", "20")),
                      ("Load balancers", ("elbv2", "describe-load-balancers", "--page-size", "1")),
                      ("Auto Scaling", ("autoscaling", "describe-auto-scaling-groups", "--max-records", "1")),
                      ("ECS", ("ecs", "list-clusters", "--max-results", "1")),
                      ("App Runner", ("apprunner", "list-services", "--max-results", "1")),
                      ("Network Firewall", ("network-firewall", "list-firewalls", "--max-results", "1")),
                      ("S3", ("s3api", "list-buckets"))):
        _probe(report, name, lambda c=cmd: aws(*c, *r), fix_ro, critical=name == "EC2 / EBS / VPC")
    end = datetime.now(timezone.utc).replace(microsecond=0)
    _probe(report, "CloudWatch metrics", lambda: aws(
        "cloudwatch", "get-metric-data", "--start-time", (end - timedelta(hours=1)).isoformat(), "--end-time",
        end.isoformat(), "--metric-data-queries",
        '[{"Id":"q","MetricStat":{"Metric":{"Namespace":"AWS/EC2","MetricName":"CPUUtilization"},'
        '"Period":3600,"Stat":"Average"}}]', *r), "Allow cloudwatch:GetMetricData (included in ReadOnlyAccess)")
    _probe(report, "Compute Optimizer", lambda: aws("compute-optimizer", "get-enrollment-status", *r),
           "Optional: opt in to Compute Optimizer for AWS's own rightsizing recommendations",
           ok_detail=lambda d: f"status {d.get('status', '?')}", optional=True)


# ---------------------------------------------------------------- Azure

def _check_azure(runner: CliRunner, report: CheckReport, cfg, scopes: list[str]) -> None:
    if not runner.available("az"):
        report.add("Azure CLI", MISSING, "not installed", "brew install azure-cli / winget install Microsoft.AzureCLI",
                   critical=True)
        return
    try:
        ver = runner.run(["az", "version", "-o", "json"])
        exts = ver.get("extensions") or {}
        report.add("Azure CLI", OK, f"{ver.get('azure-cli')}")
        for ext in ("resource-graph", "log-analytics"):
            report.add(f"Extension {ext}", OK if ext in exts else MISSING, exts.get(ext, "not installed"),
                       f"az extension add --name {ext} (cloudfinops assess offers to do this)", critical=False)
    except CliError as exc:
        report.add("Azure CLI", MISSING, short_error(str(exc)), critical=True)
        return
    who = _probe(report, "Signed in", lambda: runner.run(["az", "account", "show", "-o", "json"]),
                 "Run `az login` (or let `cloudfinops assess` open the sign-in)", critical=True,
                 ok_detail=lambda a: f"{(a.get('user') or {}).get('name')} (tenant {a.get('tenantId')})")
    if who.status != OK:
        return
    subs = [s for s in runner.run(["az", "account", "list", "--all", "-o", "json"]) or [] if s.get("state") == "Enabled"]
    if scopes:
        wanted = {x.lower() for x in scopes}
        subs = [s for s in subs if s["id"].lower() in wanted or s["name"].lower() in wanted]
    report.add("Subscriptions", OK if subs else MISSING, f"{len(subs)} enabled" + (" (checking the first 5)" if len(subs) > 5 else ""),
               "Ask for Reader on the subscriptions to assess", critical=True)
    for s in subs[:5]:
        sid, label = s["id"], s["name"]
        graph = _probe(report, f"{label}: inventory", lambda: runner.run(
            ["az", "graph", "query", "-q", "Resources | summarize n=count(), w=countif(type =~ 'microsoft.operationalinsights/workspaces')",
             "--subscriptions", sid, "-o", "json"]),
            "Grant Reader at subscription scope", critical=True,
            ok_detail=lambda d: f"{(d.get('data') or [{}])[0].get('n', 0)} resources visible")
        if graph.status == OK and graph.detail.startswith("0 "):
            graph.status, graph.fix = WARN, "Empty subscription, or Reader is missing at subscription scope"

        def cost_probe(sid=sid):
            token = runner.run(["az", "account", "get-access-token", "--resource", "https://management.azure.com/",
                                "--subscription", sid, "-o", "json"])["accessToken"]
            end = date.today()
            return runner.http_post(
                f"https://management.azure.com/subscriptions/{sid}/providers/Microsoft.CostManagement/query?api-version=2023-11-01",
                token, {"type": "AmortizedCost", "timeframe": "Custom",
                        "timePeriod": {"from": f"{end - timedelta(days=2)}T00:00:00Z", "to": f"{end}T00:00:00Z"},
                        "dataset": {"granularity": "None", "aggregation": {"c": {"name": "Cost", "function": "Sum"}}}})
        _probe(report, f"{label}: costs", cost_probe, "Grant Cost Management Reader on the subscription")
        _probe(report, f"{label}: Advisor", lambda sid=sid: runner.run(
            ["az", "advisor", "recommendation", "list", "--category", "Cost", "--subscription", sid, "-o", "json"]),
            "Reader includes Advisor; check the role assignment")
        _probe(report, f"{label}: Defender plans", lambda sid=sid: runner.run(
            ["az", "security", "pricing", "list", "--subscription", sid, "-o", "json"]),
            "Reader includes this; Security Reader also works")


# ---------------------------------------------------------------- GCP

def _check_gcp(runner: CliRunner, report: CheckReport, cfg, scopes: list[str]) -> None:
    if not runner.available("gcloud"):
        report.add("gcloud CLI", MISSING, "not installed", "Install the Google Cloud CLI", critical=True)
        return
    report.add("gcloud CLI", OK, "installed")
    report.add("bq CLI", OK if runner.available("bq") else WARN, "installed" if runner.available("bq") else "not found",
               "Needed only to read a BigQuery billing export (part of the Cloud SDK)")
    acct = _probe(report, "Signed in", lambda: (runner.run(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=json"]),
                                                runner.run(["gcloud", "auth", "print-access-token"], parse_json=False)),
                  "Run `gcloud auth login`", critical=True,
                  ok_detail=lambda r: (r[0] or [{}])[0].get("account", "?"))
    if acct.status != OK:
        return
    from ..providers.gcp import GcpProvider
    try:
        projects = GcpProvider().list_scopes(runner)
    except CliError as exc:
        report.add("Projects", MISSING, short_error(str(exc)), "Grant Browser/Viewer on the projects", critical=True)
        return
    if scopes and [x.lower() for x in scopes] != ["all"]:
        wanted = {x.lower() for x in scopes}
        projects = [p for p in projects if p["id"].lower() in wanted]
    elif not scopes:
        projects = [p for p in projects if p.get("billing")] or projects
    report.add("Projects", OK if projects else MISSING,
               f"{len(projects)} selected ({sum(1 for p in projects if p.get('billing'))} billing-linked)"
               + (" (checking the first 5)" if len(projects) > 5 else ""), critical=True)
    token = runner.run(["gcloud", "auth", "print-access-token"], parse_json=False).strip()
    end = datetime.now(timezone.utc).replace(microsecond=0)
    for p in projects[:5]:
        pid = p["id"]
        svc = _probe(report, f"{pid}: read access", lambda pid=pid: runner.run(
            ["gcloud", "services", "list", "--enabled", "--project", pid, "--format=json"]),
            "Grant roles/viewer on the project", ok_detail=lambda d: f"{len(d or [])} APIs enabled")
        if svc.status != OK:
            continue
        _probe(report, f"{pid}: usage metrics", lambda pid=pid: runner.http_get(
            f"https://monitoring.googleapis.com/v3/projects/{pid}/timeSeries?filter="
            "metric.type%3D%22serviceruntime.googleapis.com%2Fapi%2Frequest_count%22"
            f"&interval.startTime={(end - timedelta(hours=1)).isoformat().replace('+00:00', 'Z')}"
            f"&interval.endTime={end.isoformat().replace('+00:00', 'Z')}&view=HEADERS", token),
            "Grant roles/monitoring.viewer (included in Viewer)")
    report.add("Billing export", WARN if not cfg.gcp_billing_table else OK,
               cfg.gcp_billing_table or "not configured; `assess` searches BigQuery for it",
               "Enable Billing > Billing export > Detailed usage cost to BigQuery, or pass --billing-table")
