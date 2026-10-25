"""Classify and shorten errors from cloud CLIs / APIs (gcloud, az, aws/boto3, HTTP)."""

from __future__ import annotations

import re

PERMISSION_MARKERS = (
    "permission", "(403)", "forbidden", "authorizationfailed", "does not have authorization", "not authorized",
    "accessdenied", "access denied", "unauthorizedoperation", "(401)", "authorizationpermissionmismatch",
    "insufficient privileges",
)


def is_permission_error(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in PERMISSION_MARKERS)


def short_error(msg: str) -> str:
    """One readable line: drops the echoed command, JSON bodies and stack-like noise."""
    msg = msg or ""
    m = re.search(r'"message":\s*"([^"]+)"', msg)
    if m:
        return m.group(1)[:200]
    m = re.search(r"Message:\s*(.+)", msg)  # az: "Code: X\nMessage: ..."
    if m:
        code = re.search(r"Code:\s*(\S+)", msg)
        return " ".join(((f"{code.group(1)}: " if code else "") + m.group(1)).split())[:200]
    m = re.search(r"ERROR:\s*\(([^)]*)\)\s*(.*)", msg, re.S)  # gcloud: (gcloud.x.y) text / az: (AuthorizationFailed) text
    if m:
        prefix = "" if m.group(1).startswith("gcloud.") else f"{m.group(1)}: "
        text = re.sub(r"\s*Code:\s*\S+\s*$", "", m.group(2).strip())
        return " ".join((prefix + text).split())[:200]
    m = re.search(r"An error occurred \((\w+)\) when calling the (\w+) operation: (.*)", msg, re.S)  # boto3
    if m:
        return " ".join(f"{m.group(1)} ({m.group(2)}): {m.group(3)}".split())[:200]
    m = re.search(r"failed \(-?\d+\):\s*(.*)", msg, re.S)
    return " ".join((m.group(1) if m else msg).split())[:200]
