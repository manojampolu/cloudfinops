"""Core data models shared by ingestion, analysis, rules and reporting."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass
class CostRecord:
    """One line of billing data (typically a daily aggregate)."""

    day: date
    cost: float
    service: str
    account: str = ""
    region: str = ""
    resource_id: str = ""
    provider: str = "aws"
    pricing_category: str = ""  # On-Demand / Commitment-Based / Spot / "" (unknown)
    usage_type: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    currency: str = "USD"


@dataclass
class Resource:
    """A cloud resource from an inventory snapshot.

    `type` is a normalized kind, e.g. ec2_instance, ebs_volume (AWS) or azure.vm, azure.disk (Azure).
    `metrics` holds utilization stats, e.g. cpu_avg_14d, cpu_max_14d, cpu_p95.
    """

    id: str
    type: str
    provider: str = "aws"
    region: str = ""
    account: str = ""
    attributes: dict = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    monthly_cost: float | None = None
    cost_source: str = ""  # "billing" | "estimate" | "inventory"
    name: str = ""
    service: str = ""  # human-readable service, e.g. "Virtual Machines"
    group: str = ""  # resource group / project / VPC-ish grouping
    sku: str = ""
    related: list[str] = field(default_factory=list)  # ids of directly connected resources

    @property
    def display_name(self) -> str:
        return self.name or self.id.rsplit("/", 1)[-1]


@dataclass
class Relationship:
    src: str
    dst: str
    kind: str  # attached_to | uses_plan | nic_of | ip_of | node_pool_of | backend_of


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
CONFIDENCE_RANGE = {"high": (0.9, 1.0), "medium": (0.6, 1.0), "low": (0.0, 1.0)}


@dataclass
class Recommendation:
    rule_id: str
    title: str
    category: str  # waste | rightsizing | modernization | commitment | scheduling | logging | governance | advisor
    action: str
    detail: str = ""
    resource_id: str = ""
    resource_type: str = ""
    region: str = ""
    account: str = ""
    est_monthly_savings: float = 0.0
    current_monthly_cost: float | None = None
    effort: str = "low"  # low | medium | high
    risk: str = "low"  # low | medium | high
    # Evidence model
    confidence: str = "medium"  # high | medium | low
    evidence: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    validation_steps: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)  # dependent / connected resources
    owner: str = ""  # suggested reviewer (owner tag, else resource group)
    resource_name: str = ""
    service: str = ""
    scenario_group: str = ""  # findings sharing a group are alternatives, not additive
    savings_low: float | None = None
    savings_high: float | None = None

    def __post_init__(self):
        if self.missing_evidence and self.confidence == "high":
            self.confidence = "medium"
        if not self.scenario_group:
            self.scenario_group = self.resource_id or f"__{self.rule_id}"
        lo, hi = CONFIDENCE_RANGE.get(self.confidence, (0.0, 1.0))
        if self.savings_low is None:
            self.savings_low = self.est_monthly_savings * lo
        if self.savings_high is None:
            self.savings_high = self.est_monthly_savings * hi

    @property
    def key(self) -> str:
        return f"{self.rule_id}|{self.resource_id}"

    @property
    def needs_validation(self) -> bool:
        return bool(self.missing_evidence) or self.confidence == "low"

    @property
    def severity(self) -> str:
        if self.est_monthly_savings >= 100:
            return "high"
        if self.est_monthly_savings >= 20:
            return "medium"
        return "low"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity
        d["needs_validation"] = self.needs_validation
        d["key"] = self.key
        return d


@dataclass
class Dataset:
    """Everything one assessment run knows, independent of provider."""

    provider: str
    records: list[CostRecord] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    scope: dict = field(default_factory=dict)  # subscriptions/accounts, period, collection errors
    extras: dict = field(default_factory=dict)  # provider-specific: pricing_model_daily, log_usage, advisor...
    currency: str = "USD"
