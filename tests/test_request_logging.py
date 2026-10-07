import json
import logging
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from api.main import create_app


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
