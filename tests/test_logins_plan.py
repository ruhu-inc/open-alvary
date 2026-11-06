"""The restricted logins' plan (rights.logins) and the Terraform that uses it stay in step, with no database.
tests/test_postgres_logins.py signs in as each; deployment/gcp-terraform/env/tests plans each step of the move."""

import json
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


def test_the_search_path_bundle_covers_what_a_login_can_run_and_changes_only_the_path():
    folder = ROOT / "rights/sql/v0050"
    manifest = json.loads((folder / "manifest.json").read_text())
    pinned = {entry["signature"] for entry in manifest["functions"]}
    runs_as_owner = {
        function.split(".", 1)[1]
        for login in logins.LOGINS.values()
        for function in login.functions
        if function.startswith("policy.")
    } - {"accounts_access(text)"}  # runs as whoever calls it
    assert pinned == runs_as_owner
    for entry in manifest["functions"]:
        assert entry["prior_body_hash"] == entry["current_body_hash"], entry["signature"]
        before = (folder / entry["prior_file"]).read_text().splitlines()
        after = (folder / entry["file"]).read_text().splitlines()
        changed = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
        assert changed == [
            (" SET search_path TO 'pg_catalog'", " SET search_path TO 'pg_catalog', 'pg_temp'")
        ]


def test_no_login_is_written_into_a_grant_of_a_guarded_column():
    for login in logins.LOGINS.values():
        grants = str(login.tables.get("accounts", {}).get("api_key", ""))
        assert "secret_hash" not in grants and not re.search(r"\bSELECT\b(?! \()", grants)
    jobs = logins.JOBS.tables["accounts"]
    assert "token_hash" not in jobs["console_session"] and "SELECT (" in jobs["console_session"]
    assert "nonce" not in jobs["sign_in_flow"] and "SELECT (" in jobs["sign_in_flow"]
    for login in (logins.CATALOGUE, logins.RETRIEVAL, logins.PUBLISH):
        assert "accounts" not in login.tables and not any(f.startswith("accounts.") for f in login.functions)


def test_the_catalogues_login_reads_what_readers_may_see_and_nothing_of_anyones_account():
    catalogue = logins.CATALOGUE
    assert {privileges for tables in catalogue.tables.values() for privileges in tables.values()} == {
        logins.READ
    }
    assert catalogue.tables == logins.READERS_SEE and catalogue.vectors == logins.READ
    # What an anonymous request writes, and nothing that answers about a credential or an account.
    assert catalogue.functions == logins.ANONYMOUS_WRITES
    for function in ("check_credential(jsonb)", "record_usage(jsonb)", "accounts_access(text)"):
        assert f"policy.{function}" not in catalogue.functions


def test_until_the_catalogues_login_is_in_use_the_services_holds_all_it_does():
    """The service serves the catalogue on its own login until database_logins_in_use names the catalogue, so a
    table the catalogue's routes read is the service's too, and what the service reads outside accounts is the
    catalogue's: a table for readers goes in READERS_SEE, which both hold, or this fails."""
    service, catalogue = logins.SERVICE, logins.CATALOGUE
    assert {kind: tables for kind, tables in service.tables.items() if kind != "accounts"} == catalogue.tables
    assert set(catalogue.functions) <= set(service.functions) and catalogue.vectors == service.vectors


def test_the_service_is_given_the_catalogues_login_by_the_setting_it_reads():
    assert 'os.getenv("ALVARY_CATALOGUE_DATABASE_URL")' in (ROOT / "api/service.py").read_text()
    given = 'ALVARY_CATALOGUE_DATABASE_URL = local.database_secret["catalogue"]'
    assert given in (GOOGLE / "cloud_run.tf").read_text() and given in (AZURE / "keyvault.tf").read_text()
    for variables in (GOOGLE / "variables.tf", AZURE / "variables.tf"):
        assert '"service", "catalogue",' in variables.read_text()
