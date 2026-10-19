#!/usr/bin/env python3
"""One-step setup for cloudfinops (macOS, Linux, Windows). Standard library only.

    python3 install.py                  # set up Python env + check all cloud CLIs, ask before installing any
    python3 install.py --cloud azure    # only what an Azure assessment needs
    python3 install.py --cloud gcp --yes
    python3 install.py --check          # report what is missing, change nothing

Steps:
  1. Check Python >= 3.11
  2. Create .venv and install cloudfinops with its Python dependencies (boto3, pytest)
  3. Check the cloud CLIs for the chosen clouds and offer to install missing ones
     (Homebrew on macOS, winget on Windows; printed instructions on Linux)
  4. Azure: install the az CLI extensions resource-graph and log-analytics
  5. Run the test suite and print next steps
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

CLIS = {
    "azure": {
        "binary": "az", "name": "Azure CLI",
        "brew": ["brew", "install", "azure-cli"],
        "winget": ["winget", "install", "-e", "--id", "Microsoft.AzureCLI"],
        "docs": "https://learn.microsoft.com/cli/azure/install-azure-cli-linux",
    },
    "gcp": {
        "binary": "gcloud", "name": "Google Cloud CLI (gcloud + bq)",
        "brew": ["brew", "install", "--cask", "google-cloud-sdk"],
        "winget": ["winget", "install", "-e", "--id", "Google.CloudSDK"],
        "docs": "https://cloud.google.com/sdk/docs/install",
    },
    "aws": {
        "binary": "aws", "name": "AWS CLI",
        "brew": ["brew", "install", "awscli"],
        "winget": ["winget", "install", "-e", "--id", "Amazon.AWSCLI"],
        "docs": "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
    },
}
AZ_EXTENSIONS = ("resource-graph", "log-analytics")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    say(f"  [ok]   {msg}")


def warn(msg: str) -> None:
    say(f"  [todo] {msg}")


def ask(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"  {question} [y/N]: ").strip().lower().startswith("y")
    except EOFError:
        return False


def run(cmd: list[str], **kw) -> int:
    exe = shutil.which(cmd[0]) or cmd[0]
    say(f"  $ {' '.join(cmd)}")
    return subprocess.call([exe, *cmd[1:]], **kw)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def step_python(check: bool) -> bool:
    say("1. Python")
    if sys.version_info < (3, 11):
        warn(f"Python {platform.python_version()} found; cloudfinops needs 3.11 or newer "
             "(https://www.python.org/downloads/).")
        return False
    ok(f"Python {platform.python_version()}")
    return True


def step_venv(check: bool) -> bool:
    say("2. Virtual environment and Python packages")
    if not venv_python().exists():
        if check:
            warn(f"{VENV.name} not created yet")
            return False
        venv.EnvBuilder(with_pip=True, upgrade_deps=False).create(VENV)
        ok(f"created {VENV}")
    else:
        ok(f"{VENV.name} exists")
    if check:
        return True
    py = str(venv_python())
    if run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"]) != 0:
        warn("could not upgrade pip (continuing)")
    if run([py, "-m", "pip", "install", "--quiet", "-e", ".[aws,dev]"], cwd=ROOT) != 0:
        warn("pip install failed - see the error above")
        return False
    ok("installed cloudfinops + boto3 + pytest")
    return True


def step_clis(clouds: list[str], check: bool, assume_yes: bool) -> list[str]:
    say("3. Cloud CLIs")
    missing = []
    for cloud in clouds:
        spec = CLIS[cloud]
        if shutil.which(spec["binary"]):
            ok(f"{spec['name']} found ({shutil.which(spec['binary'])})")
            continue
        installer = spec["brew"] if IS_MAC and shutil.which("brew") else spec["winget"] if IS_WIN and shutil.which("winget") else None
        if check or not installer:
            warn(f"{spec['name']} not found. Install: {' '.join(installer) if installer else spec['docs']}")
            missing.append(cloud)
            continue
        if ask(f"{spec['name']} is missing. Install it with `{' '.join(installer)}`?", assume_yes):
            if run(installer) == 0 and (shutil.which(spec["binary"]) or IS_WIN):
                ok(f"installed {spec['name']}" + (" (open a new terminal so PATH picks it up)" if IS_WIN else ""))
            else:
                warn(f"installation did not complete; see {spec['docs']}")
                missing.append(cloud)
        else:
            warn(f"skipped {spec['name']}; install later from {spec['docs']}")
            missing.append(cloud)
    return missing


def step_az_extensions(check: bool) -> None:
    say("4. Azure CLI extensions")
    if not shutil.which("az"):
        warn("Azure CLI not installed; skipping extensions")
        return
    try:
        out = subprocess.run([shutil.which("az"), "extension", "list", "--query", "[].name", "-o", "tsv"],
                             capture_output=True, text=True, timeout=120).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        out = []
    for ext in AZ_EXTENSIONS:
        if ext in out:
            ok(f"az extension {ext}")
        elif check:
            warn(f"az extension {ext} missing (az extension add --name {ext})")
        elif run(["az", "extension", "add", "--name", ext, "--only-show-errors"]) == 0:
            ok(f"installed az extension {ext}")
        else:
            warn(f"could not install az extension {ext}")


def step_tests(check: bool) -> bool:
    say("5. Self-test")
    if check:
        say("  (skipped in --check mode)")
        return True
    if run([str(venv_python()), "-m", "pytest", "-q"], cwd=ROOT) == 0:
        ok("all tests passed")
        return True
    warn("some tests failed - see output above")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Set up cloudfinops and the cloud CLIs it uses.")
    ap.add_argument("--cloud", choices=["azure", "gcp", "aws", "all", "none"], default="all",
                    help="which cloud CLIs to check/install (default: all)")
    ap.add_argument("--yes", action="store_true", help="install missing CLIs without asking")
    ap.add_argument("--check", action="store_true", help="only report what is missing; change nothing")
    ap.add_argument("--skip-tests", action="store_true")
    args = ap.parse_args()
    clouds = [] if args.cloud == "none" else list(CLIS) if args.cloud == "all" else [args.cloud]

    say(f"cloudfinops setup · {platform.system()} {platform.machine()} · {ROOT}\n")
    if not step_python(args.check):
        return 1
    venv_ok = step_venv(args.check)
    missing = step_clis(clouds, args.check, args.yes)
    if "azure" in clouds:
        step_az_extensions(args.check)
    tests_ok = True if args.skip_tests or not venv_ok else step_tests(args.check)

    activate = r".venv\Scripts\activate" if IS_WIN else "source .venv/bin/activate"
    say("\nNext steps:")
    say(f"  {activate}")
    say("  cloudfinops demo --provider azure --out demo-azure      # sample report, no cloud access needed")
    if "azure" in clouds:
        say("  cloudfinops assess                                       # real Azure assessment (signs you in)")
    if "gcp" in clouds:
        say("  cloudfinops assess --provider gcp")
    if missing:
        say(f"\nStill to install: {', '.join(CLIS[c]['name'] for c in missing)}")
    return 0 if venv_ok and tests_ok else 1


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
