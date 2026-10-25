# Validating on real data

Use this on the first real run in each cloud, before showing results to a client.

## 0. Before the run

- [ ] `cloudfinops check --provider <aws|azure|gcp>` reports *Ready*. Fix every MISSING item you want covered
      (usually a role/policy) before collecting.

## 1. Collection is complete

- [ ] The report's **Data quality** section shows no `error` rows. Every `access` gap is understood: either expected
      (e.g. no Log Analytics Reader) or fixed and re-run.
- [ ] `commands.log` contains only reads (`list`, `show`, `get`, `describe`, `query`, `GET`, Cost Management query `POST`).
- [ ] Resource counts look right. Compare `inventory.csv` with the portal for one subscription/account/project.

## 2. Spend reconciles (within ~2%)

| Cloud | Compare the report's "Monthly spend (last 30 days)" with |
|---|---|
| Azure | Cost Management → Cost analysis → **Amortized cost**, same 30 days, same subscriptions |
| AWS | Cost Explorer → **Amortized costs**, same 30 days (report window ends the day before collection) |
| GCP | Billing → Reports, same projects and dates (needs the BigQuery billing export) |

If the gap is larger than ~2%: check currencies (Data quality flags mixed currencies), credits and refunds, marketplace
charges, and whether every subscription/project was selected.

## 3. Findings hold up

Pick the top 10 findings by savings and check each with the owner:
- [ ] The evidence is true: open the resource in the portal and confirm state, size and utilization.
- [ ] The current cost matches the bill. If it says "list-price estimate", compare it with the portal.
- [ ] Missing evidence is real. Answer the open questions (operating hours, retention policy, licences).

Record each decision so the register learns:

```bash
cloudfinops register update <id> --status approved --validated <monthly $> --db runs/register.db
cloudfinops register update <id> --status rejected --notes "<why>" --db runs/register.db
```

## 4. Calibrate

```bash
cloudfinops register stats --db runs/register.db
```

shows, per rule, how many findings owners accepted or rejected. Rules marked *often rejected* need a threshold change
in `cloudfinops.toml` (e.g. `idle_cpu_max_pct`, `rightsize_cpu_p95_pct`, `aca_low_requests_30d`, `nat_heavy_gb_30d`)
or `--disable <rule>` for that client. Re-analyze without calling the cloud again:

```bash
cloudfinops assess --from runs/<run-folder> --config cloudfinops.toml
```

The **acceptance rate** and **validated savings** from `register stats` are the numbers to use in presales
("owners accepted 78% of findings, worth $X/month validated").

## 5. After changes are made

Set `--status implemented --implemented-on YYYY-MM-DD`. Re-run after at least 7 days of new billing, then run
`cloudfinops register verify`. Realized savings compare the 30 days before with the days after. They don't adjust for
workload growth, so say so when presenting them.
