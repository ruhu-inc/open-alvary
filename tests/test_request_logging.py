"""Request logs never carry what a caller sent: queries reveal what someone is researching (RSV-01,
ACS-06, OA-A2). The legacy API's own request log holds no input; every image runs uvicorn without its
access log; Front Door's request logs go nowhere on Azure, and Google Cloud drops REST v1's and MCP's
before they are stored. tests/test_postgres_request_logging.py runs the public service itself."""

import http.client
import json
import logging
import re
import shlex
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest
import uvicorn
from click.core import ParameterSource
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api.main import create_app
from api.store import engine_for

ROOT = Path(__file__).resolve().parents[1]
# A query no text holds. Its first seven characters survive every analyser (case, stemming), so they are
# what a leak would show.
MARKER = "Qzv7731kq"
TRACE = MARKER[:7].lower()


def carried(record: logging.LogRecord) -> bool:
    """Whether a log record holds the marker anywhere: message, arguments, exception text or extras."""
    shown = logging.Formatter().format(record) + repr(vars(record))
    return TRACE in shown.lower()


@contextmanager
def every_log():
    """Every logger at DEBUG into one list: the root, uvicorn's, the service's, SQLAlchemy's, the HTTP
    client's and every other logger already made, including those that do not propagate."""
    records: list[logging.LogRecord] = []
    seen: set[int] = set()

    class Keep(logging.Handler):
        def emit(self, record):
            if id(record) not in seen:  # a record reaches each logger it propagates through
                seen.add(id(record))
                records.append(record)

    keep = Keep(logging.DEBUG)
    names = {"uvicorn", "uvicorn.error", "uvicorn.access", "open_alvary", "sqlalchemy", "httpx", "httpcore"}
    names |= set(logging.Logger.manager.loggerDict)
    loggers = [logging.getLogger(), *(logging.getLogger(name) for name in sorted(names))]
    saved = [(logger, logger.level, logger.disabled) for logger in loggers]
    disabled = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
        logger.disabled = False
        logger.addHandler(keep)
    try:
        yield records
    finally:
        for logger, level, was_disabled in saved:
            logger.removeHandler(keep)
            logger.setLevel(level)
            logger.disabled = was_disabled
        logging.disable(disabled)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def uvicorn_arguments(command: list[str]) -> list[str]:
    """What a container command passes uvicorn, through `sh -c`, `exec` and any setup before it."""
    if command[:2] == ["sh", "-c"]:
        # The shell's defaults stand in for the platform's variables (${PORT:-8080}).
        command = shlex.split(re.sub(r"\$\{\w+:-([^}]*)\}", r"\1", command[2]))
    start = command.index("uvicorn") + 1
    ends = [i for i, word in enumerate(command) if i > start and word in ("&&", "||", ";")]
    return command[start : ends[0] if ends else len(command)]


def image_options(dockerfile: str) -> dict:
    """The options an image's CMD gives uvicorn, read by uvicorn's own command line, less its address."""
    lines = (ROOT / dockerfile).read_text().splitlines()
    [command] = [json.loads(line.removeprefix("CMD")) for line in lines if line.startswith("CMD ")]
    parsed = uvicorn.main.make_context("uvicorn", uvicorn_arguments(command))
    given = [n for n in parsed.params if parsed.get_parameter_source(n) is ParameterSource.COMMANDLINE]
    return {name: parsed.params[name] for name in given if name not in ("app", "host", "port")}


@contextmanager
def served(app, options: dict):
    """The app under uvicorn on a free local port with these options, and no logging set-up of uvicorn's
    own, so every record reaches every_log."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, **options))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(10)


def ask(port: int, method: str, path: str, body=None, headers: dict | None = None) -> tuple[int, str]:
    """One request over HTTP. http.client logs nothing, so every record is the server's."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        payload = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
        connection.request(
            method, path, body=payload, headers={"Content-Type": "application/json", **(headers or {})}
        )
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


@pytest.mark.parametrize("path,status", [("/project", 200), ("/private-name-not-a-route", 404)])
def test_request_log_correlates_without_sensitive_input(store, caplog, path, status):
    with caplog.at_level(logging.INFO, logger="open_alvary.requests"):
        response = TestClient(create_app(store)).get(
            path + "?q=private-query",
            headers={"Authorization": "Bearer private-token", "X-Request-ID": "caller-controlled"},
        )
    assert response.status_code == status
    event = json.loads(next(r.message for r in caplog.records if r.name == "open_alvary.requests"))
    assert UUID(event["request_id"]).version == 4
    assert response.headers["X-Request-ID"] == event["request_id"]
    assert event["status"] == status
    assert event["response_headers_ms"] >= 0
    assert set(event) == {"event", "request_id", "method", "status", "response_headers_ms"}
    assert not any(x in json.dumps(event) for x in ["private", "caller-controlled", "Bearer"])
    assert response.headers["Cache-Control"] == "no-store"


def test_failed_request_logs_status_without_exception_text(store, caplog):
    app = create_app(store)

    @app.get("/test-failure")
    def fail():
        raise RuntimeError("private-error-content")

    with caplog.at_level(logging.INFO, logger="open_alvary.requests"):
        response = TestClient(app, raise_server_exceptions=False).get("/test-failure")
    assert response.status_code == 500
    events = [json.loads(r.message) for r in caplog.records if r.name == "open_alvary.requests"]
    assert len(events) == 1 and events[0]["status"] == 500
    assert "private-error-content" not in json.dumps(events)


def test_the_services_engine_logs_no_statement_value_and_no_row_at_any_level(tmp_path, monkeypatch):
    # A search's words come back as rows (its terms in each analyser), which SQLAlchemy logs at DEBUG.
    monkeypatch.delenv("ALVARY_RUNTIME_MODE", raising=False)
    engine = engine_for(f"sqlite:///{tmp_path / 'logs.db'}")
    with every_log() as records, engine.connect() as connection:
        assert connection.scalar(text("SELECT :q"), {"q": MARKER}) == MARKER
        with pytest.raises(SQLAlchemyError) as failed:
            connection.execute(text("SELECT :q FROM no_such_table"), {"q": MARKER})
    engine.dispose()
    assert TRACE not in str(failed.value).lower()
    # Statements are still logged, with their values hidden.
    assert any(r.name.startswith("sqlalchemy.engine") and "SELECT ?" in r.getMessage() for r in records)
    assert [r.getMessage() for r in records if carried(r)] == []


# An app uvicorn serves, wherever a command names one.
SERVES = re.compile(r"\b(?:api\.service|api\.main|workbench\.app):app\b")


def test_every_image_compose_file_terraform_and_deploy_script_runs_uvicorn_without_its_access_log():
    commands = {}
    for path in [ROOT / "Dockerfile", ROOT / "compose.yaml", *sorted((ROOT / "deployment").rglob("*"))]:
        if path.is_file() and (path.name.endswith("Dockerfile") or path.suffix in (".yaml", ".tf", ".sh")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if SERVES.search(line) and not line.lstrip().startswith("#"):
                    commands[f"{path.relative_to(ROOT)}:{number}"] = line
    assert {where.split(":")[0] for where in commands} >= {
        "Dockerfile",
        "compose.yaml",
        "deployment/public-service.Dockerfile",
        "deployment/compose.yaml",
        "deployment/admission.yaml",
        "deployment/workbench.Dockerfile",
        "deployment/workbench.yaml",
        "deployment/azure/env/workbench.tf",
        "deployment/gcp-terraform/env/workbench.tf",
        "deployment/gcp/workbench.sh",
    }
    assert [where for where, line in commands.items() if "--no-access-log" not in line] == []


def test_uvicorn_as_the_images_run_it_logs_no_request_where_its_access_log_would_carry_the_query():
    app = FastAPI()

    @app.get("/search")
    def search(q: str):
        return {"words": len(q.split())}

    for options, logged in (
        (image_options("deployment/public-service.Dockerfile"), False),
        (image_options("Dockerfile"), False),
        ({}, True),  # uvicorn's default: the access log, with each request's address
    ):
        with every_log() as records, served(app, options) as port:
            assert ask(port, "GET", f"/search?q=data+protection+{MARKER}")[0] == 200
        assert any(carried(r) for r in records) is logged, options
        assert any(r.name == "uvicorn.access" for r in records) is logged, options


def resources(kind: str, source: str) -> dict[str, str]:
    """Each `resource "<kind>" "<name>" { ... }` in Terraform source: its name and its body."""
    found = {}
    for match in re.finditer(rf'resource\s+"{kind}"\s+"([\w-]+)"\s*\{{', source):
        depth, end = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(source[end], 0)
            end += 1
        found[match.group(1)] = source[match.end() : end - 1]
    return found


# Front Door records each request's full address, query string included, in its access and WAF logs; a
# category group (allLogs) includes both.
FRONT_DOOR_REQUEST_LOGS = ("FrontDoorAccessLog", "FrontDoorWebApplicationFirewallLog")
WITHOUT_ADDRESSES = ('"FrontDoorHealthProbeLog"', '"AllMetrics"')


def front_door_request_logs(source: str) -> list[str]:
    """The diagnostic settings that could send Front Door's request logs anywhere: on its profile or WAF
    policy (or a target that cannot be read), any category group, and any category but health probes
    and metrics, including one Terraform only knows when it runs."""
    flagged = []
    for name, body in resources("azurerm_monitor_diagnostic_setting", source).items():
        target = re.search(r"\btarget_resource_id\s*=\s*(.+)", body)
        if target and not re.search(r"frontdoor|front_door|Microsoft\.Cdn/profiles", target.group(1), re.I):
            continue
        categories = [c.strip() for c in re.findall(r"\bcategory\s*=\s*(.+)", body)]
        if re.search(r"\bcategory_group\s*=", body) or set(categories) - set(WITHOUT_ADDRESSES):
            flagged.append(name)
    return flagged


def test_no_diagnostic_setting_sends_front_doors_access_or_waf_logs_anywhere():
    source = "\n".join(p.read_text() for p in sorted((ROOT / "deployment" / "azure").rglob("*.tf")))
    assert 'resource "azurerm_cdn_frontdoor_profile"' in source
    assert front_door_request_logs(source) == []
    # The check sees a setting that would send them, and passes one with health probes and metrics only.
    setting = """
resource "azurerm_monitor_diagnostic_setting" "edge" {
  name                       = "edge"
  target_resource_id         = azurerm_cdn_frontdoor_profile.edge.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id

  enabled_log {
    category = "CATEGORY"
  }
  enabled_metric {
    category = "AllMetrics"
  }
}
"""
    for category in FRONT_DOOR_REQUEST_LOGS:
        assert front_door_request_logs(setting.replace("CATEGORY", category)) == ["edge"]
    grouped = setting.replace('category = "CATEGORY"', 'category_group = "allLogs"')
    assert front_door_request_logs(grouped) == ["edge"]
    assert front_door_request_logs(setting.replace('"CATEGORY"', "each.value")) == ["edge"]
    assert front_door_request_logs(setting.replace("CATEGORY", "FrontDoorHealthProbeLog")) == []
    elsewhere = setting.replace("azurerm_cdn_frontdoor_profile.edge.id", "azurerm_key_vault.main.id")
    assert front_door_request_logs(elsewhere.replace("CATEGORY", "AuditEvent")) == []


# A double-quoted string, its escapes kept (\" in both HCL and the shell).
STRING = r'"((?:[^"\\]|\\.)*)"'


def exclusion_groups(text: str) -> list[set[str]]:
    """A logging filter of the form (A OR B) AND (C OR D OR E), as its two groups of clauses."""
    matched = re.fullmatch(r"\((.+)\) AND \((.+)\)", " ".join(text.split()))
    assert matched, text
    return [set(group.split(" OR ")) for group in matched.groups()]


def test_google_clouds_exclusion_still_drops_the_request_logs_of_rest_v1_mcp_and_the_api_host():
    # Cloud Run and the load balancer log each request's full address: these entries are never stored.
    monitoring = (ROOT / "deployment" / "gcp-terraform" / "env" / "monitoring.tf").read_text()
    body = resources("google_logging_project_exclusion", monitoring)["api_request_urls"]
    assert not re.search(r"\bdisabled\s*=\s*true", body)
    [listed] = re.findall(r'\bfilter\s*=\s*join\(" ",\s*\[(.*?)\]\s*\)', body, re.S)
    terraform = " ".join(json.loads(f'"{part}"') for part in re.findall(STRING, listed))
    logs, addresses = exclusion_groups(terraform)
    assert logs >= {'log_id("run.googleapis.com/requests")', 'resource.type="http_load_balancer"'}
    assert addresses >= {
        'httpRequest.requestUrl:"/api/v1"',
        'httpRequest.requestUrl:"/mcp"',
        'httpRequest.requestUrl:"//${var.api_domain}/"',
    }
    # The script that set it on the live project (public_service.sh request_logs) drops the same.
    script = (ROOT / "deployment" / "gcp" / "public_service.sh").read_text()
    function = script.split("\nrequest_logs() {", 1)[1].split("\n}\n", 1)[0]
    live = "".join(json.loads(f'"{part}"') for part in re.findall(r"\bfilter\+?=" + STRING, function))
    assert exclusion_groups(live.replace("$API_DOMAIN", "${var.api_domain}")) == [logs, addresses]
