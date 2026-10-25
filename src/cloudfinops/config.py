"""Tunable thresholds. Override any field via a TOML file (see cloudfinops.example.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Config:
    required_tags: list[str] = field(default_factory=lambda: ["owner", "environment", "cost-center"])
    lookback_days: int = 30
    hours_per_month: float = 730.0

    # Utilization thresholds (percent)
    idle_cpu_max_pct: float = 5.0
    rightsize_cpu_avg_pct: float = 20.0
    rightsize_cpu_max_pct: float = 50.0

    # Age thresholds (days)
    snapshot_max_age_days: int = 90
    stopped_instance_max_days: int = 30

    # Scheduling non-prod resources: hours per week they need to be on
    non_prod_env_values: list[str] = field(
        default_factory=lambda: ["dev", "development", "test", "qa", "staging", "sandbox"]
    )
    non_prod_on_hours_per_week: float = 60.0  # 12h x 5 weekdays

    # Anomaly detection
    anomaly_z_threshold: float = 3.0
    anomaly_min_delta: float = 50.0

    # Commitments: conservative blended discount for 1-yr no-upfront Compute Savings Plans
    savings_plan_discount: float = 0.27

    # Storage
    s3_large_bucket_gb: float = 1024.0
    data_transfer_share_warn: float = 0.10

    # Azure-specific
    rightsize_cpu_p95_pct: float = 40.0
    rightsize_mem_p95_pct: float = 60.0  # skip rightsizing when memory p95 is above this
    scheduling_min_uptime_share: float = 0.9  # running >= 90% of hours -> effectively 24/7
    premium_disk_low_iops: float = 100.0  # p95 IOPS below this -> Standard SSD candidate
    plan_scale_in_cpu_p95_pct: float = 30.0
    non_prod_name_tokens: list[str] = field(
        default_factory=lambda: ["dev", "test", "qa", "uat", "stg", "staging", "sandbox", "nonprod", "poc"]
    )
    log_analytics_gb_price: float = 2.30
    log_basic_gb_price: float = 0.50
    log_retention_gb_month_price: float = 0.10
    log_archive_gb_month_price: float = 0.02
    log_max_interactive_retention_days: int = 90
    log_commitment_min_gb_day: float = 100.0
    log_commitment_discount: float = 0.15
    log_large_table_gb_month: float = 50.0
    log_basic_eligible_tables: list[str] = field(default_factory=lambda: [
        "ContainerLogV2", "AppTraces", "ContainerAppConsoleLogs", "AppServiceConsoleLogs",
        "AppServiceHTTPLogs", "StorageBlobLogs", "AKSControlPlane", "ACSCallAutomationIncomingOperations",
    ])

    # Azure Container Apps
    aca_low_requests_30d: float = 500.0  # below this (with min replicas >= 1) -> scale-to-zero candidate
    aca_idle_cpu_max_pct: float = 2.0  # CPU p95 below this ...
    aca_idle_requests_30d: float = 50.0  # ... and requests below this -> idle
    aca_min_monthly_cost: float = 5.0  # ignore apps cheaper than this

    # Scope: limit the report to these resource groups (collection stays subscription-wide)
    resource_groups: list[str] = field(default_factory=list)

    # Collection / mapping
    cost_query_pause_s: float = 45.0  # pause between Cost Management queries (limits 429 throttling)
    collection_days: int = 60
    metric_days: int = 14
    max_metric_resources: int = 500
    max_items_per_node: int = 25
    owner_tag_keys: list[str] = field(default_factory=lambda: ["owner", "team", "application-owner", "contact"])

    # GCP-specific
    gcp_billing_table: str = ""  # project.dataset.gcp_billing_export_resource_v1_XXXX (auto-discovered if empty)
    gcp_bucket_large_gb: float = 100.0
    gcp_artifact_large_gb: float = 1.0
    gcp_run_low_requests_30d: float = 1000.0
    fx_to_usd: dict = field(default_factory=dict)  # e.g. {"INR": 0.012}; overrides built-in rates

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        cfg = cls()
        if not path:
            return cfg
        if not Path(path).exists():
            raise FileNotFoundError(f"Config file '{path}' not found. Create one with: "
                                    f"cp cloudfinops.example.toml {path}  (or drop --config to use defaults)")
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown config keys: {', '.join(sorted(unknown))}")
        for key, value in data.items():
            setattr(cfg, key, value)
        return cfg
