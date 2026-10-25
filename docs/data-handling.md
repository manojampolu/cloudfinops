# Data handling

A short statement to share with a client's security or procurement team.

**What it reads.** Configuration metadata (resource types, sizes, SKUs, tags, relationships), cost totals from the
provider's billing APIs, utilization metrics (CPU, memory, connections, request counts, bytes processed), native
cost recommendations, and log *volumes* (GB per table, not log contents). [permissions.md](permissions.md) lists
every command.

**What it never reads.** Application data, database contents, storage objects, log contents, secrets, keys or
certificates. No role grants write access, and the tool refuses any command that is not on its read-only allowlist
(enforced in code and covered by tests).

**What it changes.** Nothing in the cloud. CLI prompts are disabled, so it cannot even enable an API. Local changes are
limited to installing CLI extensions (with the user's confirmation) and the files in its own folders.

**Where data goes.** Everything stays where the tool runs: the client's cloud shell or the assessor's laptop. There is
no telemetry and no upload. The only outbound calls besides the cloud's own APIs are optional public price lookups
(Azure Retail Prices API), which send a VM size and region and no client data. You can switch them off with
`price_lookup = false`.

**What is produced.** A run folder with the raw API responses (`raw/`), an audit log of every call (`commands.log`), the
report and CSVs, and a local SQLite savings register. These contain subscription/account IDs, resource names and tags.
Treat them as internal.

**Sharing.** `--redact ids` replaces subscription/account/project IDs and names, tenant IDs, GUIDs, IP addresses and
emails with stable pseudonyms. `--redact all` also replaces resource names, resource groups and owner tags. The
redacted copy goes to `redacted/`, and the lookup table (`redaction-map.csv`) stays with the assessor.

**Retention.** Delete the run folders when the engagement ends. In a cloud shell: `rm -rf ~/cloudfinops ~/cloudfinops-runs`.
