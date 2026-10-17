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
| AWS | `collect aws` + `analyze` (boto3); moving to `assess` next |
| GCP | `assess --provider gcp`: gcloud sign-in, Cloud Asset + per-service inventory, BigQuery billing export, Cloud Monitoring usage, Recommender |
| Any | `analyze --billing` on a FOCUS / CUR / simple CSV export |

Core has zero dependencies (Python 3.11+). Azure needs the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli);
AWS collection needs `boto3`.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[aws,dev]'

cloudfinops demo --provider azure --out demo-azure   # full Azure assessment on bundled sample data
open demo-azure/report.html
cloudfinops demo                                     # AWS sample (text report)
```

### Azure assessment

```bash
brew install azure-cli                 # once
cloudfinops assess                     # opens browser sign-in if needed, then lets you pick subscriptions
cloudfinops assess --subscriptions acme-prod --device-code --zip
cloudfinops assess --from runs/azure-20261017-1030 --config cloudfinops.toml   # re-analyze offline
```

Required roles: Reader, Cost Management Reader, Log Analytics Reader. See [docs/permissions.md](docs/permissions.md)
for the exact commands. [docs/assessment-playbook.md](docs/assessment-playbook.md) covers how to run the
engagement end to end.

**Report contents**: KPIs and the savings range by module, an estate map (subscription → service → SKU →
utilization → resources, with large services rolled up), service profiles, filterable opportunities, owner review
sheets, spend trends and anomalies, data quality and gaps, and the method.

Every finding has evidence, assumptions, missing evidence, validation steps, connected resources, a suggested
owner and a confidence level. Findings without key evidence are marked **needs validation** and given a
savings range starting at zero.

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
| Logging | `az-logging` (`az-log-basic-plan`, `az-log-containerlog-v2`, `az-log-retention`, `az-log-commitment-tier`, `az-log-review-ingestion`) | Expensive tables, plans, retention, commitment tiers |
| Commitments | `az-commitment`, `az-advisor` | Stable compute baseline *after* other savings; Advisor recommendations as inputs |
| Governance | `az-untagged` | Spend without owner/environment/cost-center tags |

### AWS

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

## Project layout

```
src/cloudfinops/
  cli.py              commands: assess, register, demo, analyze, collect, rules
  harness/runner.py   read-only CLI runner (allowlist, retries, commands.log, raw capture)
  harness/wizard.py   interactive sign-in and scope selection
  providers/azure.py  Azure collect (CLI) + load (raw JSON -> common model)
  providers/aws.py    AWS collector (boto3)
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

- AWS on the `assess` harness (aws CLI: Resource Explorer/describe, `ce`, CloudWatch, Compute Optimizer)
- More Azure modules: SQL/Cosmos DB, storage account tiering, Azure Firewall/NAT, Defender plans
- Optional importer for Azure Optimization Engine output
- Web review UI (FastAPI) on top of the register; AI-written summaries of evidence (never deciding)

## Tests

```bash
pytest
```
