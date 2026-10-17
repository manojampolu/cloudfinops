"""Provider adapter contract.

An adapter has two halves:
  * collect(): talks to the cloud (through CliRunner, read-only) and writes raw JSON to run_dir/raw
  * load():    turns a run directory's raw files into a provider-neutral Dataset (works offline)
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..config import Config
from ..harness.runner import CliRunner
from ..models import Dataset


class Provider(Protocol):
    name: str
    cli: str

    def preflight(self, runner: CliRunner) -> list[str]:
        """Return a list of problems (empty if ready)."""

    def ensure_login(self, runner: CliRunner, device_code: bool = False) -> dict:
        """Make sure the user is logged in; returns identity info."""

    def list_scopes(self, runner: CliRunner) -> list[dict]:
        """Subscriptions / accounts / projects the identity can read: [{id, name, ...}]."""

    def collect(self, runner: CliRunner, scopes: list[dict], cfg: Config, log=print) -> dict:
        """Collect raw data for the given scopes. Returns the scope manifest (incl. errors)."""

    def load(self, run_dir: Path, cfg: Config) -> Dataset:
        """Build a Dataset from raw files in run_dir."""


def get_provider(name: str) -> Provider:
    if name == "azure":
        from .azure import AzureProvider
        return AzureProvider()
    if name == "gcp":
        from .gcp import GcpProvider
        return GcpProvider()
    raise ValueError(f"Provider '{name}' is not supported by `assess` yet (supported: azure, gcp). "
                     "For AWS use `cloudfinops collect aws` + `analyze`.")
