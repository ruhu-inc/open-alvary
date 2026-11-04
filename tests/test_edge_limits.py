"""The edge's limits and the alerts behind them, as each cloud's Terraform has them (D23): every path has
a per-address limit, the API's stands above the largest plan's own rate, Anthropic's hosts are never held
back from the MCP address, and the operator hears of an attack before the site is down."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOOGLE = ROOT / "deployment/gcp-terraform/env"
AZURE = ROOT / "deployment/azure/env"
ANTHROPIC = "160.79.104.0/21"


def default(directory: Path, name: str) -> int:
    source = (directory / "variables.tf").read_text()
    block = re.search(rf'variable "{name}" \{{(.*?)\n\}}', source, re.S).group(1)
    return int(re.search(r"default\s*=\s*(\d+)", block).group(1))


def armor_rules() -> list[str]:
    source = (GOOGLE / "edge.tf").read_text()
    policy = source[source.index('resource "google_compute_security_policy" "edge"') :]
    policy = policy[: policy.index('resource "google_compute_region_network_endpoint_group"')]
    return re.split(r"\n  rule \{", policy)[1:]


def test_every_path_on_google_cloud_has_a_per_address_limit():
    rules = armor_rules()
    priorities = [int(re.search(r"priority\s*=\s*(\d+)", rule).group(1)) for rule in rules]
    assert priorities == sorted(priorities)
    last_limit = rules[-2]  # just before the default rule, so nothing reaches that unlimited
    assert '"throttle"' in last_limit and 'src_ip_ranges = ["*"]' in last_limit
    assert "edge_page_requests_per_minute" in last_limit
    assert rules[-1].count("2147483647") == 1 and '"allow"' in rules[-1]
    for rule in rules[:-1]:
        if '"throttle"' in rule:
            assert 'exceed_action  = "deny(429)"' in rule and 'enforce_on_key = "IP"' in rule


def test_cloud_armors_rules_stay_within_its_own_bounds():
    source = (GOOGLE / "edge.tf").read_text()
    for rule in armor_rules():
        # Longest the description can come to, with the largest number filled in.
        description = re.search(r'description = "(.*)"', rule).group(1)
        assert len(re.sub(r"\$\{[^}]*\}", "000000", description)) <= 64
        written = re.search(r'expression = "(.*)"', rule)
        expression = written.group(1) if written else ""
        if "local.api_paths" in rule:
            expression = re.search(r'api_paths\s*=\s*"(.*)"', source).group(1)
        assert len(re.findall(r"startsWith|inIpRange", expression)) <= 5  # at most five subexpressions


def test_every_path_on_azure_has_a_per_address_limit():
    source = (AZURE / "frontdoor.tf").read_text()
    rules = re.findall(r'custom_rule \{\n    name\s*=\s*"(\w+)"(.*?)\n  \}\n', source, re.S)
    assert [name for name, _ in rules] == [
        "certifiedhosts",
        "websitethrottle",
        "apithrottle",
        "everythingthrottle",
    ]
    assert 'operator       = "Any"' in dict(rules)["everythingthrottle"]
    assert "edge_page_requests_per_minute" in dict(rules)["everythingthrottle"]
    assert "edge_api_requests_per_minute" in dict(rules)["apithrottle"]


def test_the_api_limit_stands_above_the_largest_plans_rate():
    largest = max(
        tier["allowance"]["requests_per_minute"]
        for tier in json.loads((ROOT / "accounts/tiers.json").read_text())
    )
    for directory in (GOOGLE, AZURE):
        assert default(directory, "edge_api_requests_per_minute") > largest


def test_both_clouds_set_the_same_limits_and_alert_levels():
    for name in (
        "edge_requests_per_minute",
        "edge_page_requests_per_minute",
        "edge_api_requests_per_minute",
        "alert_requests_per_minute",
        "alert_refused_per_minute",
        "alert_errors_per_minute",
    ):
        assert default(GOOGLE, name) == default(AZURE, name), name


def test_anthropics_hosts_are_never_held_back_from_the_mcp_address():
    google = next(rule for rule in armor_rules() if "inIpRange" in rule)
    assert '"allow"' in google and "/mcp" in google and "/.well-known/" in google
    assert ANTHROPIC in (GOOGLE / "edge.tf").read_text()
    assert int(re.search(r"priority\s*=\s*(\d+)", google).group(1)) == 900  # ahead of every limit
    azure = (AZURE / "frontdoor.tf").read_text()
    assert ANTHROPIC in azure and 'name     = "certifiedhosts"' in azure


def test_the_operator_hears_of_an_attack_on_each_cloud():
    google = (GOOGLE / "monitoring.tf").read_text()
    assert "loadbalancing.googleapis.com/https/request_count" in google
    assert "metric.labels.response_code = 429" in google and "response_code_class = 500" in google
    assert 'resource "google_monitoring_alert_policy" "edge"' in google
    azure = (AZURE / "monitoring.tf").read_text()
    assert 'resource "azurerm_monitor_metric_alert" "edge"' in azure
    assert '"HttpStatus"' in azure and '"HttpStatusGroup"' in azure
