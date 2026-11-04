"""The restricted logins' plan (rights.logins) and the Terraform that uses it stay in step, with no database.
tests/test_postgres_logins.py signs in as each; deployment/gcp-terraform/env/tests plans each step of the move."""

import re
from pathlib import Path

from rights import logins

ROOT = Path(__file__).resolve().parents[1]
GOOGLE = ROOT / "deployment/gcp-terraform/env"
AZURE = ROOT / "deployment/azure/env"


def roles_in(source: str) -> dict[str, str]:
    """The keys and roles of a Terraform map naming open_alvary_ logins."""
    return dict(re.findall(r"^\s*(\w+)\s*=\s*\"(open_alvary_\w+)\"", source, re.M))


def test_terraform_makes_each_login_the_plan_names_with_the_setting_the_command_reads():
    named = roles_in((GOOGLE / "secrets.tf").read_text())
    assert set(named.values()) == set(logins.LOGINS)
    for key, role in named.items():
        assert logins.LOGINS[role].environment == f"ALVARY_LOGIN_{key.upper()}_URL"
    jobs = (GOOGLE / "jobs.tf").read_text()
    assert '"ALVARY_LOGIN_${upper(login)}_URL" => "database-url-${login}"' in jobs


def test_azure_makes_those_of_its_workloads_the_same_way():
    named = roles_in((AZURE / "keyvault.tf").read_text())
    assert set(named.values()) == set(logins.LOGINS) - {"open_alvary_publish"}  # no publish job there yet
    for key, role in named.items():
        assert logins.LOGINS[role].environment == f"ALVARY_LOGIN_{key.upper()}_URL"
    assert '"ALVARY_LOGIN_${upper(login)}_URL" => "database-url-${login}"' in (AZURE / "jobs.tf").read_text()


def test_the_secrets_a_moved_job_keeps_are_settings_the_service_is_given():
    settings = (GOOGLE / "cloud_run.tf").read_text()
    for name in re.findall(r"reads\s*=\s*\[([^\]]*)\]", (GOOGLE / "jobs.tf").read_text()):
        for setting in re.findall(r'"(\w+)"', name):
            assert setting in settings, setting


def test_every_grant_is_on_a_kind_of_schema_the_namespace_knows_and_every_function_names_its_schema():
    for login in logins.LOGINS.values():
        assert set(login.tables) <= {"base", "accounts", "changes", "corpus"}
        for function in login.functions:
            kind, signature = function.split(".", 1)
            assert kind in {"accounts", "policy"} and signature.endswith(")")


def test_no_login_is_written_into_a_grant_of_a_guarded_column():
    for login in logins.LOGINS.values():
        grants = str(login.tables.get("accounts", {}).get("api_key", ""))
        assert "secret_hash" not in grants and not re.search(r"\bSELECT\b(?! \()", grants)
    jobs = logins.JOBS.tables["accounts"]
    assert "token_hash" not in jobs["console_session"] and "SELECT (" in jobs["console_session"]
    assert "nonce" not in jobs["sign_in_flow"] and "SELECT (" in jobs["sign_in_flow"]
    for login in (logins.RETRIEVAL, logins.PUBLISH):
        assert "accounts" not in login.tables and not any(f.startswith("accounts.") for f in login.functions)
