"""The record page's text in PostgreSQL, where the catalogue's gates are columns (PRD-06): a text is chosen
from the texts' details in one pass, only the chosen text is read, and the check before sending reads none."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from test_postgres import pg_engine as pg_engine
from test_v1_text import UNITS, WORK, publish_act

from api.service import create_service
from api.store import Store

pytestmark = pytest.mark.postgres


def test_a_text_is_chosen_read_and_checked_without_reading_any_other(pg_engine, monkeypatch):
    for name in ("ALVARY_ACCOUNTS", "ALVARY_ADMISSION_CONFIG", "ALVARY_SITE_ADMISSION_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PUBLIC_DOMAIN", "open.example.test")
    store = Store(pg_engine)
    publish_act(store, UNITS)
    read = []

    @event.listens_for(pg_engine, "before_cursor_execute")
    def note(connection, cursor, statement, parameters, context, many):
        if "versions" in statement:
            read.append(" ".join(statement.split()))

    with TestClient(create_service(store=store)) as client:
        answer = client.get(f"/site/v1/sources/{WORK}/text")
    event.remove(pg_engine, "before_cursor_execute", note)
    page = answer.json()
    assert answer.status_code == 200 and page["schema"] == "reader-1" and page["text"]["bytes"] > 0
    assert page["matched"] == "default" and page["missed"] == [] and page["nodes"]

    details = [s for s in read if "json_to_record" in s]
    whole = [s for s in read if s.startswith("SELECT versions.payload FROM versions")]
    # The details of the record's texts: one statement, of texts that pass the gate columns, one pass each.
    assert len(details) == 1 and "gate_text" in details[0] and "gate_content_hash = ANY" in details[0]
    assert "canonical_text" not in details[0] and "->>" not in details[0]
    # One whole text is read: the chosen one.
    assert len(whole) == 1
    # After it, the check before sending asks for the text's gate from its columns, and for no record.
    after = read[read.index(whole[0]) + 1 :]
    checks = [s for s in after if "gate_content_hash" in s]
    assert len(checks) == 1 and "payload" not in checks[0]
    assert not any("payload" in s for s in after if " FROM versions" in s)
