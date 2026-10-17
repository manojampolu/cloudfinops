"""Approximate AWS us-east-1 on-demand list prices (Linux, USD).

Used only as a fallback when billing data has no resource-level cost.
Real billing data always wins; estimates are labelled as such in reports.
"""

from __future__ import annotations

import re

EBS_GB_MONTH = {
    "gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125,
    "st1": 0.045, "sc1": 0.015, "standard": 0.05,
}
SNAPSHOT_GB_MONTH = 0.05
PUBLIC_IPV4_HOURLY = 0.005
ALB_HOURLY = 0.0225
S3_STANDARD_GB_MONTH = 0.023
S3_INFREQUENT_GB_MONTH = 0.0125

EC2_HOURLY = {
    "t2.micro": 0.0116, "t2.small": 0.023, "t2.medium": 0.0464, "t2.large": 0.0928,
    "t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416, "t3.large": 0.0832, "t3.xlarge": 0.1664,
    "t4g.micro": 0.0084, "t4g.small": 0.0168, "t4g.medium": 0.0336, "t4g.large": 0.0672, "t4g.xlarge": 0.1344,
    "m4.large": 0.10, "m4.xlarge": 0.20, "m4.2xlarge": 0.40,
    "m5.large": 0.096, "m5.xlarge": 0.192, "m5.2xlarge": 0.384, "m5.4xlarge": 0.768,
    "m6i.large": 0.096, "m6i.xlarge": 0.192, "m6i.2xlarge": 0.384, "m6i.4xlarge": 0.768,
    "m7g.medium": 0.0408, "m7g.large": 0.0816, "m7g.xlarge": 0.1632, "m7g.2xlarge": 0.3264, "m7g.4xlarge": 0.6528,
    "c4.large": 0.10, "c4.xlarge": 0.199,
    "c5.large": 0.085, "c5.xlarge": 0.17, "c5.2xlarge": 0.34, "c5.4xlarge": 0.68,
    "c7g.medium": 0.0363, "c7g.large": 0.0725, "c7g.xlarge": 0.145, "c7g.2xlarge": 0.29, "c7g.4xlarge": 0.58,
    "r4.large": 0.133, "r4.xlarge": 0.266,
    "r5.large": 0.126, "r5.xlarge": 0.252, "r5.2xlarge": 0.504,
    "r7g.medium": 0.0536, "r7g.large": 0.1071, "r7g.xlarge": 0.2142, "r7g.2xlarge": 0.4284,
}

RDS_HOURLY = {
    "db.t3.micro": 0.017, "db.t3.small": 0.034, "db.t3.medium": 0.068, "db.t3.large": 0.136,
    "db.m5.large": 0.171, "db.m5.xlarge": 0.342, "db.m5.2xlarge": 0.684,
    "db.r5.large": 0.25, "db.r5.xlarge": 0.50, "db.r5.2xlarge": 1.00,
}

SIZES = ["nano", "micro", "small", "medium", "large", "xlarge", "2xlarge", "4xlarge",
         "8xlarge", "12xlarge", "16xlarge", "24xlarge", "32xlarge", "48xlarge"]

PREVIOUS_GEN_FAMILIES = {"t1", "t2", "m1", "m2", "m3", "m4", "c1", "c3", "c4",
                         "r3", "r4", "i2", "d2", "g2", "p2", "x1"}

# x86 family -> Graviton equivalent
GRAVITON_EQUIVALENT = {
    "t2": "t4g", "t3": "t4g", "t3a": "t4g",
    "m4": "m7g", "m5": "m7g", "m5a": "m7g", "m6i": "m7g", "m6a": "m7g",
    "c4": "c7g", "c5": "c7g", "c5a": "c7g", "c6i": "c7g", "c6a": "c7g",
    "r4": "r7g", "r5": "r7g", "r5a": "r7g", "r6i": "r7g", "r6a": "r7g",
}
CURRENT_GEN_X86 = {"t2": "t3", "m4": "m6i", "m3": "m6i", "c4": "c6i", "c3": "c6i", "r4": "r6i", "r3": "r6i"}

_TYPE_RE = re.compile(r"^(?P<prefix>db\.)?(?P<family>[a-z0-9-]+)\.(?P<size>[a-z0-9]+)$")


def split_type(instance_type: str) -> tuple[str, str, str] | None:
    """'db.m5.large' -> ('db.', 'm5', 'large'); 'm5.large' -> ('', 'm5', 'large')."""
    m = _TYPE_RE.match(instance_type or "")
    if not m:
        return None
    return m.group("prefix") or "", m.group("family"), m.group("size")


def downsize(instance_type: str) -> str | None:
    parts = split_type(instance_type)
    if not parts:
        return None
    prefix, family, size = parts
    if size not in SIZES:
        return None
    idx = SIZES.index(size)
    # Burstable families bottom out around micro; others at large/medium.
    floor = SIZES.index("micro") if family.startswith("t") else SIZES.index("medium")
    if idx <= floor:
        return None
    return f"{prefix}{family}.{SIZES[idx - 1]}"


def ec2_monthly(instance_type: str, hours: float = 730.0) -> float | None:
    hourly = EC2_HOURLY.get(instance_type)
    return hourly * hours if hourly is not None else None


def rds_monthly(instance_class: str, multi_az: bool = False, hours: float = 730.0) -> float | None:
    hourly = RDS_HOURLY.get(instance_class)
    if hourly is None:
        return None
    return hourly * hours * (2 if multi_az else 1)


def ebs_monthly(volume_type: str, size_gb: float) -> float:
    return EBS_GB_MONTH.get(volume_type, EBS_GB_MONTH["gp2"]) * size_gb
