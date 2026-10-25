#!/usr/bin/env bash
# Run a cloudfinops assessment from a cloud shell (Azure Cloud Shell, Google Cloud Shell, AWS CloudShell).
# Nothing is installed outside your home directory, nothing is deployed, and every cloud call is read-only.
#
#   bash <(curl -sSL https://raw.githubusercontent.com/manojampolu/cloudfinops/master/cloudshell.sh)
#   bash <(curl -sSL .../cloudshell.sh) --redact all           # also write a shareable redacted copy
#   bash <(curl -sSL .../cloudshell.sh) demo                   # try it on sample data first
#
# Use `bash <(curl ...)` rather than `curl ... | bash` so the subscription/project picker can read your keyboard.
# Any extra options are passed to `cloudfinops assess` (e.g. --subscriptions, --projects, --regions, --resource-groups).
set -euo pipefail

REPO="${CLOUDFINOPS_REPO:-https://github.com/manojampolu/cloudfinops.git}"
REF="${CLOUDFINOPS_REF:-master}"
APP_DIR="${CLOUDFINOPS_HOME:-$HOME/cloudfinops}"
RUNS_DIR="${CLOUDFINOPS_RUNS:-$HOME/cloudfinops-runs}"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- which cloud shell?
SHELL_KIND="local"
if [ -n "${AZUREPS_HOST_ENVIRONMENT:-}" ] || [ -n "${ACC_CLOUD:-}" ] || [ -n "${ACC_TERM_ID:-}" ]; then
  SHELL_KIND="azure"
elif [ "${CLOUD_SHELL:-}" = "true" ] || [ -n "${DEVSHELL_PROJECT_ID:-}" ]; then
  SHELL_KIND="gcp"
elif [ "${AWS_EXECUTION_ENV:-}" = "CloudShell" ]; then
  SHELL_KIND="aws"
fi

SUBCMD="assess"
if [ "${1:-}" = "demo" ]; then SUBCMD="demo"; shift; fi
PROVIDER=""
for arg in "$@"; do
  if [ "${prev:-}" = "--provider" ]; then PROVIDER="$arg"; fi
  prev="$arg"
done
if [ -z "$PROVIDER" ]; then
  case "$SHELL_KIND" in
    azure|gcp|aws) PROVIDER="$SHELL_KIND"; set -- --provider "$PROVIDER" "$@" ;;
    *) echo "Not in a recognised cloud shell; pass --provider azure|gcp|aws." >&2; exit 2 ;;
  esac
fi
say "cloudfinops · $SHELL_KIND shell · provider $PROVIDER · $SUBCMD"

# ---------------------------------------------------------------- Python 3.11+
PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && \
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done

# ---------------------------------------------------------------- code
if [ -n "${CLOUDFINOPS_NO_UPDATE:-}" ]; then
  say "Using the existing copy in $APP_DIR"  # CI / development: test the checked-out code as is
elif [ -d "$APP_DIR/.git" ]; then
  say "Updating $APP_DIR"
  git -C "$APP_DIR" fetch -q --depth 1 origin "$REF" && git -C "$APP_DIR" checkout -q FETCH_HEAD
else
  say "Downloading cloudfinops into $APP_DIR"
  git clone -q --depth 1 --branch "$REF" "$REPO" "$APP_DIR"
fi

# ---------------------------------------------------------------- virtual environment
if [ ! -x "$APP_DIR/.venv/bin/cloudfinops" ]; then
  if [ -n "$PY" ]; then
    "$PY" -m venv "$APP_DIR/.venv"
  else
    # e.g. AWS CloudShell ships Python 3.9: get a private 3.12 with uv (no sudo, stays in $HOME)
    say "Python 3.11+ not found; installing a private Python 3.12 with uv"
    python3 -m pip install -q --user uv
    "$HOME/.local/bin/uv" venv -q --python 3.12 "$APP_DIR/.venv"
    "$HOME/.local/bin/uv" pip install -q --python "$APP_DIR/.venv/bin/python" pip
  fi
  "$APP_DIR/.venv/bin/python" -m pip install -q --upgrade pip
  "$APP_DIR/.venv/bin/python" -m pip install -q -e "$APP_DIR"
else
  "$APP_DIR/.venv/bin/python" -m pip install -q -e "$APP_DIR"  # pick up updates
fi
CFO="$APP_DIR/.venv/bin/cloudfinops"
mkdir -p "$RUNS_DIR"

# ---------------------------------------------------------------- run
if [ "$SUBCMD" = "demo" ]; then
  [ "$PROVIDER" = "gcp" ] && { echo "No GCP sample yet; showing the Azure sample." >&2; PROVIDER="azure"; }
  OUT="$RUNS_DIR/demo-$PROVIDER"
  ARGS=()
  for arg in "$@"; do [ "$arg" = "--provider" ] || [ "$arg" = "$PROVIDER" ] || ARGS+=("$arg"); done
  "$CFO" demo --provider "$PROVIDER" --out "$OUT" "${ARGS[@]+"${ARGS[@]}"}"
  (cd "$RUNS_DIR" && rm -f "demo-$PROVIDER.zip" && "$APP_DIR/.venv/bin/python" -m zipfile -c "demo-$PROVIDER.zip" "demo-$PROVIDER" >/dev/null)
  BUNDLE="$RUNS_DIR/demo-$PROVIDER.zip"
else
  EXTRA=()
  [ -t 0 ] || EXTRA+=(--yes)  # no keyboard (piped): accept defaults instead of waiting
  "$CFO" assess --out "$RUNS_DIR" --zip "${EXTRA[@]+"${EXTRA[@]}"}" "$@"
  BUNDLE="$(ls -t "$RUNS_DIR"/*.zip 2>/dev/null | head -1)"
fi

# ---------------------------------------------------------------- how to get the results
echo
say "Results: $BUNDLE"
case "$SHELL_KIND" in
  azure) echo "Download: Cloud Shell toolbar > Manage files > Download, then enter: ${BUNDLE#"$HOME"/}" ;;
  gcp)   echo "Download: run  cloudshell download $BUNDLE" ;;
  aws)   echo "Download: Actions (top right) > Download file, then enter: $BUNDLE" ;;
  *)     echo "Open report.html inside the run folder." ;;
esac
echo "The zip holds report.html, findings.csv, inventory.csv and the raw evidence. Re-run any time; nothing was changed in your cloud."
