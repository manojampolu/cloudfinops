"""Interactive `cloudfinops assess` flow: preflight -> login -> pick scope -> collect -> assess."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..providers.base import get_provider
from .runner import CliError, CliRunner


def _ask(prompt: str, default: str = "") -> str:
    try:
        ans = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        ans = ""
    return ans or default


def _yes(prompt: str, default: bool = True, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    ans = _ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
    return default if not ans else ans.startswith("y")


def _pick(scopes: list[dict], provider, defaults: list[dict]) -> list[dict]:
    label = getattr(provider, "scope_label", "subscriptions")
    describe = getattr(provider, "describe_scope", lambda s: f"{s['name']}  ({s['id']})")
    print(f"\n{label.capitalize()} this identity can read:")
    for i, s in enumerate(scopes, 1):
        print(f"  {i:>3}. {describe(s)}")
    default = ",".join(str(scopes.index(d) + 1) for d in defaults) if defaults != scopes else "all"
    raw = _ask("Pick numbers (e.g. 1,3-5) or 'all'", default)
    if raw.lower() == "all":
        return scopes
    picked = set()
    for part in raw.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            picked.update(range(int(a), int(b) + 1))
        elif part:
            picked.add(int(part))
    return [scopes[i - 1] for i in sorted(picked) if 1 <= i <= len(scopes)]


def collect_interactive(provider_name: str, out_root: Path, cfg: Config, subscriptions: list[str] | None = None,
                        device_code: bool = False, tenant: str | None = None, assume_yes: bool = False) -> Path:
    provider = get_provider(provider_name)
    run_dir = Path(out_root) / f"{provider_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    runner = CliRunner(run_dir)
    print(f"cloudfinops read-only assessment · provider: {provider_name} · run folder: {run_dir}")

    problems = provider.preflight(runner)
    for p in list(problems):
        if p.startswith("missing-extension:"):
            ext = p.split(":", 1)[1]
            if _yes(f"The Azure CLI extension '{ext}' is required (local install, no cloud change). Install it now?",
                    assume_yes=assume_yes):
                provider.install_extension(runner, ext)
                problems.remove(p)
    if problems:
        for p in problems:
            print(f"  x {p}", file=sys.stderr)
        raise SystemExit(2)

    print("\nChecking sign-in (a browser window opens if you are not signed in)...")
    try:
        who = provider.ensure_login(runner, device_code=device_code, tenant=tenant)
    except CliError as exc:
        raise SystemExit(f"Login failed: {exc}")
    print(f"  Signed in as {who.get('user', {}).get('name', '?')} (tenant {who.get('tenantId', '?')})")

    scopes = provider.list_scopes(runner)
    label = getattr(provider, "scope_label", "subscriptions")
    if not scopes:
        raise SystemExit(f"This identity cannot read any active {label}.")
    defaults = getattr(provider, "default_scopes", lambda s: s)(scopes)
    if subscriptions and [x.lower() for x in subscriptions] == ["all"]:
        chosen = scopes
    elif subscriptions:
        wanted = {w.lower() for x in subscriptions for w in x.replace(",", " ").split()}
        chosen = [s for s in scopes if s["id"].lower() in wanted or s["name"].lower() in wanted]
        if not chosen:
            raise SystemExit(f"None of {subscriptions} are visible to this identity.")
    elif assume_yes:
        chosen = defaults
    else:
        chosen = _pick(scopes, provider, defaults)
    print(f"\nAssessing {len(chosen)} {label}: {', '.join(s['name'] for s in chosen)}")
    print("All calls are read-only and logged to commands.log.\n")

    manifest = provider.collect(runner, chosen, cfg, log=print)
    if manifest["errors"]:
        print(f"\n{len(manifest['errors'])} collection step(s) failed; see Data quality in the report.")
    return run_dir
