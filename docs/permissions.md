# Access required for a cloudfinops assessment

cloudfinops is **read-only**. It runs on the assessor's machine through the official cloud CLI, signs in
with the client's normal sign-in flow (browser, device code, MFA, SSO), and makes no changes to any resource.
Every command is checked against a read-only allowlist before it runs and is written to `commands.log`
in the run folder, so the client can audit exactly what was called.

## Azure

Assign these built-in roles to the assessing identity at **subscription** scope (or management-group scope
to cover several subscriptions):

| Role | Why |
|---|---|
| Reader | Resource inventory (Azure Resource Graph), Azure Monitor metrics, Azure Advisor |
| Cost Management Reader | Amortized cost by resource and pricing model (Cost Management Query API) |
| Log Analytics Reader | Billable ingestion per table (`Usage` table) and table plan/retention settings |

The tool does not need Contributor, Owner, Key Vault data access, storage data access or any write permission.
It reads no application data: only configuration, metrics, cost and log-volume metadata.

### Commands that run

| Purpose | Command |
|---|---|
| Sign-in check | `az account show`, `az login` (only if not signed in), `az account list` |
| Inventory | `az graph query -q "Resources \| project ..."` and `ResourceContainers`, paged |
| Costs | `az rest --method post --url .../providers/Microsoft.CostManagement/query` (a query API; read-only) |
| Native recommendations | `az advisor recommendation list --category Cost` |
| Utilization | `az monitor metrics list` (CPU, available memory, disk IOPS, App Service CPU/memory; 14 days hourly) |
| Logging | `az monitor log-analytics query` (Usage table only), `az monitor log-analytics workspace table list` |
| Prices | HTTPS GET to the public Azure Retail Prices API (no credentials) |

The only local change is installing the `resource-graph` CLI extension if it is missing, which needs the user's confirmation.

### Notes on evidence quality

- **Guest memory**: VM memory utilization comes from the platform metric "Available Memory Bytes". Where
  it is unavailable, rightsizing findings are marked *needs validation* rather than guessed.
- **Cost Management** data for the current day can lag by 8–24 hours; the collection window ends yesterday.
- **Throttling**: Cost Management and Monitor APIs rate-limit; the runner retries with backoff.

## AWS (via `cloudfinops collect aws`)

`ReadOnlyAccess` + `AWSBillingReadOnlyAccess` managed policies (or the minimal action list in
`src/cloudfinops/providers/aws.py`). Cost Explorer charges $0.01 per API request.

## GCP (`cloudfinops assess --provider gcp`)

| Role (per project) | Why |
|---|---|
| Viewer (or Browser + Monitoring Viewer + Cloud Asset Viewer + Recommender Viewer) | Inventory, usage metrics, recommendations |
| Billing Account Viewer | Which projects are linked to billing |
| BigQuery Data Viewer + Job User on the export dataset | Costs from the billing export |

The Cloud Asset, Recommender and Monitoring APIs are called with a quota project where they are already enabled.
All gcloud prompts are disabled (`CLOUDSDK_CORE_DISABLE_PROMPTS=1`), so the tool can never enable an API.
`bq query` accepts only a single SELECT statement. Cloud Monitoring is read with HTTPS GET requests, which are
logged like CLI commands.
