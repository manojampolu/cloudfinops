# Cloud savings assessment – playbook

A bounded, read-only engagement: **Assess → Review with owners → (client implements) → Verify**.

## 1. Before the session

- Agree scope: which subscriptions/environments, and who the application owners are.
- Ask the client to grant the roles in [permissions.md](permissions.md) to the person who will sign in.
- Collect what the tool cannot see:
  - required operating hours for non-production environments
  - log retention and audit requirements
  - known peaks (month-end, campaigns) and upcoming migrations
  - recommendations they already have, and why they were not implemented
- Discovery question: *"Show me the last cost-saving change you made. How did you check it was safe, and did the bill go down?"*

## 2. Run the assessment (30–60 minutes on a typical estate)

Easiest for clients: run it in their own cloud shell ([cloud-shell.md](cloud-shell.md)), so nothing is installed and
no credentials are shared. Send [data-handling.md](data-handling.md) to their security team beforehand. On the
first real run in a new cloud, follow [validation-checklist.md](validation-checklist.md).

```bash
pip install -e '.[dev]'          # once
brew install azure-cli           # once; or see https://learn.microsoft.com/cli/azure/install-azure-cli
cloudfinops assess               # sign in, pick subscriptions, collect, analyze
```

Options: `--subscriptions <id|name> ...`, `--tenant <id>`, `--device-code` (no browser on the machine),
`--zip` (bundle the run folder for hand-over), `--config cloudfinops.toml` (thresholds such as
operating hours or required tags).

Each run creates `runs/azure-<timestamp>/`:

| File | Use |
|---|---|
| `report.html` | The discussion document (self-contained; email or present it) |
| `findings.csv` | Findings for spreadsheets / ticketing |
| `inventory.csv` | Every resource with cost, utilization bucket and links |
| `estate.mmd` | Mermaid mind map of the estate for slides |
| `raw/`, `commands.log`, `scope.json` | Evidence and audit trail |

To re-run the analysis after changing thresholds, without calling the cloud again:
`cloudfinops assess --from runs/azure-<timestamp> --config cloudfinops.toml`.

## 3. Review with owners

Walk through the report in this order:

1. **Summary**: spend, estimated savings *range*, and the share needing validation.
2. **Data quality & gaps**: say up front what the estimate cannot see.
3. **Owner review sheets**: one per team. For each finding, read out the evidence, the missing
   evidence and the validation steps.

Record decisions:

```bash
cloudfinops register list --db runs/register.db
cloudfinops register update 12 --status approved --owner web-team --validated 480 --db runs/register.db
cloudfinops register update 12 --status rejected --notes "needed for DR" --db runs/register.db
```

Statuses: `open`, `in-review`, `approved`, `rejected`, `deferred`, `implemented`, `verified`.

## 4. Verify realized savings

After the client implements a change, set `--status implemented --implemented-on YYYY-MM-DD`. Run the
assessment again after at least 7 days of new billing, then:

```bash
cloudfinops register verify --db runs/register.db
```

Realized savings compare the resource's daily cost in the 30 days before and the days after the change. They do
not adjust for workload growth, so mention that when presenting them.

## Talking points

- "We separate **estimated**, **validated** and **realized** savings, and we never add up alternatives
  for the same resource."
- "Every finding shows its evidence, assumptions and what we could *not* see."
- "Nothing was changed. Your teams decide and implement; we measure the result."
- Don't promise a savings percentage before seeing the data.
