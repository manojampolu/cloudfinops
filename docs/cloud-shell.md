# Run cloudfinops from a cloud shell

The quickest way for a client to run an assessment: no installation, no credentials to hand over, and the data stays
in the client's own cloud shell until they decide to download it.

| Cloud shell | Open it from | Sign-in |
|---|---|---|
| Azure Cloud Shell (Bash) | portal.azure.com → `>_` icon | already signed in |
| Google Cloud Shell | console.cloud.google.com → `>_` icon | already signed in |
| AWS CloudShell | console.aws.amazon.com → CloudShell icon | uses the console role |

## Run

```bash
bash <(curl -sSL https://raw.githubusercontent.com/manojampolu/cloudfinops/master/cloudshell.sh)
```

The script:
1. detects which cloud shell it is in (override with `--provider azure|gcp|aws`)
2. downloads cloudfinops into `~/cloudfinops`; on AWS CloudShell, which ships Python 3.9, it also fetches a private
   Python 3.12 with `uv` (no sudo)
3. runs `cloudfinops assess` (pick subscriptions/projects when asked) and zips the run folder into `~/cloudfinops-runs/`
4. prints how to download the zip from that shell

Use `bash <(curl …)`, not `curl … | bash`; otherwise the picker cannot read the keyboard and defaults are used.

### Useful options

Options after the URL are passed to `cloudfinops assess`:

```bash
bash <(curl -sSL …/cloudshell.sh) demo                        # sample data first, no cloud calls
bash <(curl -sSL …/cloudshell.sh) --redact all                # adds a shareable redacted copy
bash <(curl -sSL …/cloudshell.sh) --subscriptions prod-sub    # Azure: skip the picker
bash <(curl -sSL …/cloudshell.sh) --projects all              # GCP: every visible project
bash <(curl -sSL …/cloudshell.sh) --regions eu-west-1 us-east-1  # AWS: limit regions
```

## Getting the results out

| Shell | How |
|---|---|
| Azure | Toolbar → Manage files → Download → `cloudfinops-runs/<run>.zip` |
| Google | `cloudshell download ~/cloudfinops-runs/<run>.zip` |
| AWS | Actions → Download file → `/home/cloudshell-user/cloudfinops-runs/<run>.zip` |

The zip contains `report.html` (open it in any browser), `findings.csv`, `inventory.csv`, the mind map and the raw
evidence (`raw/`, `commands.log`). With `--redact`, the `redacted/` folder is the copy to share outside the team.
`redaction-map.csv` translates pseudonyms back and should stay with the assessor.

## Notes

- Cloud shells time out after about 20 minutes idle. Large estates can take longer, but collection keeps running
  while the tab is open.
- Azure Cloud Shell keeps `$HOME` between sessions, so later runs reuse the installed copy and update it.
- Remove everything with `rm -rf ~/cloudfinops ~/cloudfinops-runs`.
