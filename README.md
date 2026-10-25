# cloudfinops

A read-only **cloud savings assessment** accelerator. Sign in to the client's cloud, collect inventory, costs
and utilization through the official CLI, map the estate service by service, and produce a discussion-ready
report of quantified, evidence-backed optimization opportunities. **Nothing is changed in the cloud**: teams
review the findings and implement them manually, and a savings register tracks decisions and realized savings.

```
cloudfinops assess
  sign in (az login) -> pick subscriptions -> collect (read-only, audited) -> normalize
  -> estate map + service profiles -> rules (scheduling, rightsizing, storage/orphans, logging, commitments)
  -> report.html + findings.csv + inventory.csv + estate.mmd + savings register
```

| Provider | Status |
|---|---|
| Azure | `assess`: interactive sign-in, Resource Graph, Cost Management, Monitor, Advisor, Log Analytics |
| AWS | `assess --provider aws`: aws CLI (SSO/profiles), Cost Explorer (+ optional CUR), EC2/EBS/RDS/ELB/NAT/VPN/ASG/ECS/App Runner/Network Firewall/S3, CloudWatch, Compute Optimizer |
| GCP | `assess --provider gcp`: gcloud sign-in, Cloud Asset + per-service inventory, BigQuery billing export, Cloud Monitoring usage, Recommender |
| Any | `analyze --billing` on a FOCUS / CUR / simple CSV export |

Core has zero dependencies (Python 3.11+). Azure needs the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli);
AWS collection needs `boto3`.

## Install on another machine

```bash
git clone https://github.com/manojampolu/cloudfinops.git && cd cloudfinops
python3 install.py --cloud azure       # or gcp / aws / all; Windows: py install.py --cloud azure
```

`install.py` (standard library only) does the whole setup:
1. checks for Python 3.11+
2. creates `.venv` and installs cloudfinops with its packages
3. checks the cloud CLI and offers to install it (Homebrew on macOS, winget on Windows, instructions on Linux)
4. adds the `resource-graph` and `log-analytics` Azure CLI extensions
5. runs the test suite

Use `python3 install.py --check` to see what is missing without changing anything, and `--yes` to install without prompts.

### First real Azure run: checklist

1. `az login` works and `az account list -o table` shows the subscriptions you expect.
2. Roles on those subscriptions: Reader, Cost Management Reader, Log Analytics Reader.
3. Run `cloudfinops assess`. It offers to install the `resource-graph` and `log-analytics` CLI extensions if missing.
4. Check `runs/azure-*/commands.log` (reads only) and the report's **Data quality** section for failed steps.
5. Compare the report's 30-day spend with Cost Management → Cost analysis (amortized, same dates).
6. If something fails, re-run the analysis offline after a fix, without calling Azure again:
   `cloudfinops assess --from runs/azure-<timestamp>`. Include `scope.json` (errors) and `commands.log`
   when reporting issues. Both contain subscription IDs, so share them privately.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[aws,dev]'

cloudfinops demo --provider azure --out demo-azure   # full Azure assessment on bundled sample data
open demo-azure/report.html
cloudfinops demo --provider aws --out demo-aws       # full AWS assessment on bundled sample data
cloudfinops demo                                     # classic AWS text report (collect/analyze flow)
```

### Azure assessment

```bash
brew install azure-cli                 # once
cloudfinops assess                     # opens browser sign-in if needed, then lets you pick subscriptions
cloudfinops assess --subscriptions acme-prod --device-code --zip
cloudfinops assess --from runs/azure-20261017-1030 --config cloudfinops.toml   # re-analyze offline
cloudfinops assess --from runs/azure-20261017-1030 --resource-groups rg-dev rg-test   # report on some RGs only
```

`--resource-groups` (or `resource_groups = [...]` in the config) narrows the report, findings and inventory to those
resource groups; AKS node resource groups follow their cluster. Collection stays subscription-wide, so one collection
can produce a DEV report and a TEST report. Each goes to its own `rg-<names>/` folder inside the run folder, and
subscription-level commitment analysis is left out. Container Apps thresholds: `aca_low_requests_30d`,
`aca_idle_cpu_max_pct`, `aca_idle_requests_30d`, `aca_min_monthly_cost`.

Required roles: Reader, Cost Management Reader, Log Analytics Reader. See [docs/permissions.md](docs/permissions.md)
for the exact commands. [docs/assessment-playbook.md](docs/assessment-playbook.md) covers how to run the
engagement end to end.

**Report contents**: KPIs and the savings range by module, an estate map (subscription → service → SKU →
utilization → resources, with large services rolled up), service profiles, filterable opportunities, owner review
sheets, spend trends and anomalies, data quality and gaps, and the method.

Every finding has evidence, assumptions, missing evidence, validation steps, connected resources, a suggested
owner and a confidence level. Findings without key evidence are marked **needs validation** and given a
savings range starting at zero.

### AWS assessment

```bash
cloudfinops assess --provider aws                        # current credentials (runs `aws sso login` if the SSO session expired)
cloudfinops assess --provider aws --profile prod --regions us-east-1 eu-west-1
cloudfinops assess --provider aws --billing-csv cur-2026-10.csv   # resource-level costs from a CUR / FOCUS export
```

Cost Explorer gives service-level costs (3 requests at $0.01 each), so resource costs are list-price estimates unless
you pass a CUR export. Services that are access-denied are skipped in every region and reported once. Run once per
account or profile. The classic `collect aws` + `analyze` flow still works.

### GCP assessment

```bash
cloudfinops assess --provider gcp                        # billing-linked projects are pre-selected
cloudfinops assess --provider gcp --projects my-proj-1 my-proj-2 --billing-table p.dataset.gcp_billing_export_resource_v1_XXXX
```

GCP exposes costs only through the BigQuery billing export, which the tool auto-discovers. Without an export, the
report shows spend as unknown and prices findings from list prices and Recommender projections. API request volume
per service (for example Gemini / generativelanguage calls) comes from Cloud Monitoring.

### Savings register

```bash
cloudfinops register list   --db runs/register.db
cloudfinops register update 12 --status approved --validated 480 --owner web-team --db runs/register.db
cloudfinops register update 12 --status implemented --implemented-on 2026-11-02 --db runs/register.db
cloudfinops register verify --db runs/register.db       # realized savings from later billing
cloudfinops register export --csv register.csv --db runs/register.db
```

Three separate numbers: **estimated** (model), **validated** (owner-confirmed) and **realized** (measured).

### Against your AWS account

```bash
# read-only credentials (ReadOnlyAccess + AWSBillingReadOnlyAccess)
cloudfinops collect aws --profile myprofile --regions us-east-1 eu-west-1 --days 60
cloudfinops analyze --billing data/billing.csv --inventory data/inventory.json --format markdown -o report.md
```

`--group-by` picks the second Cost Explorer dimension: `account` (default), `region`, or
`purchase_type` (recommended if you already hold Savings Plans/RIs, so the commitment rule sees real coverage).
Cost Explorer charges $0.01 per API request.

### From a billing export (any cloud)

```bash
cloudfinops analyze --billing my_focus_export.csv
```

Column names are auto-detected (see [ingest/billing.py](src/cloudfinops/ingest/billing.py)). Without an
inventory you still get breakdowns, anomalies, tag coverage, Savings Plan and data-transfer findings;
resource-level rules need the inventory.

## Rules

Run `cloudfinops rules` for the live list.

### Azure

| Module | Rule | Finds |
|---|---|---|
| Idle & orphaned | `az-disk-unattached`, `az-pip-unassociated`, `az-snapshot-old`, `az-lb-no-backend`, `az-plan-empty` | Resources nothing uses |
| Idle & orphaned | `az-vm-idle`, `az-vm-stopped-billing`, `az-vm-deallocated` | Idle VMs, VMs stopped but still billed, deallocated VMs still paying for disks/IPs |
| Rightsizing | `az-vm-rightsize`, `az-plan-scale-in` | Low CPU p95 (and memory where available) → one size down, priced via the Retail Prices API |
| Scheduling | `az-nonprod-schedule`, `az-aks-nonprod-stop` | Non-prod VMs/VMSS/AKS (by tag or name) running 24/7 |
| Storage | `az-disk-premium-lowiops` | Premium SSD with low IOPS → Standard SSD |
| Container Apps | `az-aca-idle` | CPU p95 < 2% of allocated vCPU × replicas and almost no HTTP requests |
| Container Apps | `az-aca-low-traffic` | Meaningful cost but < 500 requests/30 days (min replicas ≥ 1 → scale to zero) |
| Container Apps | `az-aca-nonprod-min-replicas` | Non-prod (tag/name/RG) apps keeping ≥ 1 replica warm 24/7 |
| Licensing | `az-ahb-windows`, `az-ahb-sql-vm`, `az-ahb-sql-paas` | Azure Hybrid Benefit not applied (Windows VMs, SQL Server VMs, vCore SQL DB / pools / MI); SQL Developer edition for non-prod SQL VMs |
| Licensing | `az-defender-nonprod` | Paid Defender for Cloud plans on non-prod subscriptions (Servers P2 → P1) |
| Network | `az-firewall`, `az-bastion` | Idle (no traffic / sessions) or non-prod Firewall and Bastion (deallocate off-hours, Developer SKU) |
| Network | `az-vnet-gateway-unused`, `az-nat-idle`, `az-ddos-plan` | VPN/ER gateways without connections, NAT gateways with no subnets or traffic, DDoS plans (none protected / IP Protection cheaper) |
| Databases | `az-db-flex-idle`, `az-db-flex-rightsize`, `az-db-flex-nonprod-ha`, `az-db-flex-nonprod-stop` | PostgreSQL / MySQL Flexible Server: no connections, low CPU, HA in non-prod, non-prod running 24/7 |
| Compute | `az-vmss-idle`, `az-vmss-rightsize` | Standalone VM scale sets (AKS node pools excluded) |
| Logging | `az-logging` (`az-log-basic-plan`, `az-log-containerlog-v2`, `az-log-retention`, `az-log-commitment-tier`, `az-log-review-ingestion`) | Expensive tables, plans, retention, commitment tiers |
| Commitments | `az-commitment`, `az-advisor` | Stable compute baseline *after* other savings; Advisor recommendations as inputs |
| Governance | `az-untagged` | Spend without owner/environment/cost-center tags |

### AWS

`assess --provider aws` adds these rules to the classic ones in the table that follows:

| Area | Rules | Finds |
|---|---|---|
| Network | `aws-nat-idle`, `aws-nat-heavy`, `aws-vpn-down`, `aws-network-firewall-nonprod` | NAT gateways with no traffic or heavy processing (missing S3/DynamoDB gateway endpoints), VPNs with all tunnels down, Network Firewall in non-prod |
| Compute | `aws-asg-idle`, `aws-asg-rightsize`, `aws-asg-nonprod-schedule` | Auto Scaling groups judged as a group (members are not double counted) |
| Containers | `aws-ecs-rightsize`, `aws-ecs-nonprod-schedule`, `aws-apprunner-unused` | Fargate services with low CPU or non-prod running 24/7; App Runner services with no requests |
| Databases | `aws-rds-nonprod-multiaz`, `aws-rds-extended-support` | Multi-AZ in non-prod; engines in paid RDS Extended Support (confirmed from Cost Explorer usage types) |
| Native | `aws-compute-optimizer` | AWS Compute Optimizer EC2 recommendations (counted once with cloudfinops rightsizing) |
| Governance | `aws-security-nonprod`, `aws-untagged` | Security service spend in non-prod accounts; untagged resources rolled up per account |

| Category | Rule | Finds |
|---|---|---|
| waste | `ebs-unattached` | Volumes in `available` state |
| waste | `eip-unassociated` | Elastic IPs not attached to anything |
| waste | `ebs-snapshot-old` | Snapshots older than 90 days (skips `retention=permanent/legal-hold`) |
| waste | `ec2-stopped-long` | Instances stopped > 30 days still paying for EBS |
| waste | `ec2-idle` | Running instances with max CPU < 5% |
| waste | `rds-idle` | Databases with zero connections in 14 days |
| waste | `lb-idle` | ALBs with zero requests in 14 days |
| rightsizing | `ec2-rightsize` | Avg CPU < 20% and max < 50% → one size down |
| rightsizing | `rds-rightsize` | Same for RDS |
| modernization | `ebs-gp2-to-gp3` | gp2 → gp3 (20% cheaper) |
| modernization | `ec2-graviton` | x86 / previous-gen → Graviton equivalent |
| modernization | `s3-lifecycle` | Large Standard buckets without lifecycle / Intelligent-Tiering |
| scheduling | `nonprod-schedule` | dev/test/staging running 24/7 |
| commitment | `savings-plan` | Steady on-demand compute baseline (p10 of daily spend) |
| governance | `data-transfer` | Data transfer + NAT > 10% of spend |
| governance | `untagged` | Resources missing required cost-allocation tags |

Tune thresholds with `--config` (see [cloudfinops.example.toml](cloudfinops.example.toml)); skip rules with `--disable rule-id ...`.

### How savings are estimated

1. Resource cost comes from (amortized) billing when the resource appears there (last 30 days → monthly).
   Otherwise list prices are used ([pricing.py](src/cloudfinops/pricing.py), [pricing_azure.py](src/cloudfinops/pricing_azure.py)),
   and the finding is labelled as an estimate.
2. Each rule estimates the share of that cost it removes. The confidence level sets the range: high 90–100%,
   medium 60–100%, low 0–100%.
3. **No double counting**: findings in the same scenario group (by default, the same resource) are alternatives,
   so only the largest counts. Commitment recommendations are sized on the compute that remains after the
   other findings. Logging commitment tiers and retention are sized on volume that stays on the Analytics plan.

### GCP

| Area | Rules | Finds |
|---|---|---|
| Compute | `gcp-vm-idle`, `gcp-vm-rightsize`, `gcp-nonprod-schedule`, `gcp-vm-stopped`, `gcp-mig-nonprod-schedule` | Compute Engine VMs and managed instance groups |
| Storage | `gcp-disk-unattached`, `gcp-snapshot-old`, `gcp-bucket-lifecycle`, `gcp-artifact-cleanup` | Orphaned disks, old snapshots, buckets and Artifact Registry repos without lifecycle/cleanup |
| Serverless | `gcp-run-min-instances`, `gcp-run-unused`, `gcp-function-unused` | Cloud Run warm instances for little traffic, unused services and functions |
| Cloud SQL | `gcp-sql-idle`, `gcp-sql-rightsize`, `gcp-sql-nonprod-ha`, `gcp-sql-nonprod-stop`, `gcp-sql-extended-support` | No connections, oversized tiers, HA in non-prod, non-prod running 24/7, versions in extended support |
| Network | `gcp-ip-unused`, `gcp-nat-heavy`, `gcp-lb-idle`, `gcp-vpn-down` | Reserved IPs, heavy Cloud NAT processing (Private Google Access), HTTPS LBs with no requests, down VPN tunnels |
| Logging / native | `gcp-logging`, `gcp-recommender` | Ingestion above the free tier, long retention; Google Recommender findings |
| Governance | `gcp-idle-billed-project`, `gcp-unused-apis`, `gcp-unlabelled`, `gcp-security-nonprod` | Billing-linked projects with no activity, unused APIs, missing labels, security spend in non-prod |

## Project layout

```
src/cloudfinops/
  cli.py              commands: assess, register, demo, analyze, collect, rules
  harness/runner.py   read-only CLI runner (allowlist, retries, commands.log, raw capture)
  harness/wizard.py   interactive sign-in and scope selection
  providers/azure.py  Azure collect (CLI) + load (raw JSON -> common model)
  providers/aws_cli.py AWS collect (aws CLI) + load for `assess`
  providers/gcp.py    GCP collect (gcloud / bq / Monitoring API) + load
  providers/aws.py    classic AWS collector (boto3) for `collect aws`
  models.py           CostRecord, Resource, Relationship, Recommendation, Dataset
  analysis.py         breakdowns, trends, anomalies, cost enrichment
  rules/              registry (base.py), aws.py, azure.py, scenario totals (__init__.py)
  estate.py           estate tree, large-service roll-ups, service profiles, Mermaid mind map
  quality.py          data-quality and coverage checks
  register.py         SQLite savings register (decisions, validated, realized)
  report_html.py      self-contained HTML report
  assessment.py       pipeline tying it together
  samples/            AWS CSV/JSON samples; azure/ = a recorded-shape Azure run folder
scripts/              sample data generators
docs/                 permissions, assessment playbook
tests/
```

### Adding a rule

```python
# src/cloudfinops/rules/aws.py
@resource_rule("nat-idle", "NAT gateways with no traffic", "nat_gateway")
def nat_idle(res, ctx):
    if res.metrics.get("bytes_out_14d", 1) > 0:
        return None
    return make("nat-idle", res, title="Idle NAT gateway", category="waste",
                est_monthly_savings=res.monthly_cost or 0, action="Delete it.")
```

## Roadmap

- AWS Organizations: assess several member accounts in one run (assume-role)
- More Azure modules: SQL/Cosmos DB, storage account tiering, Azure Firewall/NAT, Defender plans
- Optional importer for Azure Optimization Engine output
- Web review UI (FastAPI) on top of the register; AI-written summaries of evidence (never deciding)

## Tests

```bash
pytest
```
