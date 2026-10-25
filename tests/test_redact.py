import csv
import re
from importlib import resources as pkg_resources
from pathlib import Path

import pytest

from cloudfinops.assessment import assess
from cloudfinops.config import Config
from cloudfinops.providers.aws_cli import AwsProvider
from cloudfinops.providers.azure import AzureProvider

SAMPLES = Path(str(pkg_resources.files("cloudfinops") / "samples"))
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def run(provider, level, tmp_path):
    loader = AzureProvider() if provider == "azure" else AwsProvider()
    result = assess(loader.load(SAMPLES / provider, Config()), Config(), tmp_path, redact=level)
    files = {k: v for k, v in result.outputs.items() if k.startswith("redacted_")}
    return result, {k: v.read_text(encoding="utf-8") for k, v in files.items()}


def test_azure_ids_level_hides_subscriptions_but_keeps_names(tmp_path):
    result, red = run("azure", "ids", tmp_path)
    original = result.outputs["report"].read_text()
    assert "11111111-aaaa-4aaa-8aaa-111111111111" in original and "acme-prod" in original
    for text in red.values():
        assert not GUID.search(text)
        # the subscription names themselves are gone; resource names that *contain* them are kept at this level
        assert not re.search(r"(?<![\w-])acme-(non)?prod(?![\w-])", text)
    assert "vm-web-01" in red["redacted_report"]  # resource names kept at ids level
    assert "Redacted copy (ids)" in red["redacted_report"]
    assert "redaction-map.csv" not in " ".join(red)  # mapping is not part of the shareable set


def test_all_level_hides_resource_names_and_groups(tmp_path):
    result, red = run("azure", "all", tmp_path)
    report = red["redacted_report"]
    for name in ("vm-web-01", "rg-web", "law-prod", "asp-prod-web", "web-team"):
        assert not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", report), name
    # still a usable report: numbers and findings survive
    assert "Estimated savings / month" in report and report.count('class="opp"') == result.outputs["report"].read_text().count('class="opp"')


def test_aws_account_ips_and_mapping(tmp_path):
    result, red = run("aws", "all", tmp_path)
    for text in red.values():
        assert "444455556666" not in text and "54.10.0." not in text
        assert "acme" not in text.lower()  # client name gone from aliases, buckets, tags
    rows = list(csv.DictReader(open(result.outputs["redaction_map"], encoding="utf-8")))
    assert {"account-1", "account-1-name"} <= {r["pseudonym"] for r in rows}
    pseudos = [r["pseudonym"] for r in rows]
    assert len(pseudos) == len(set(pseudos))  # one pseudonym per original


def test_invalid_level(tmp_path):
    with pytest.raises(ValueError):
        run("aws", "everything", tmp_path)
