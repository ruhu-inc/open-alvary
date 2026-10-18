"""Data a day (MAC F11, OA-A1) through the public service against PostgreSQL: every answer's bytes are
reserved in the BUD ledger (migration 0017) against the organisation's bytes_per_day, shared by REST
and MCP; a tier change recalculates the capacity without resetting what was spent."""

import pytest
from sqlalchemy import text
from test_postgres import pg_engine as pg_engine
from test_postgres_api_v1 import TIER, api, grant, key

from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres
assert api  # the fixture, imported for use here


def bucket(accounts, organisation):
    with accounts.engine.connect() as conn:
        namespace = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        table = namespace.qualified(conn, "policy", "byte_bucket")
        return (
            conn.execute(
                text(f"SELECT capacity, refill_per_minute, held, spent FROM {table} WHERE scope_key = :k"),
                {"k": "org:" + organisation.removeprefix("org_")},
            )
            .mappings()
            .one()
        )


def test_answers_are_charged_to_the_data_a_day_and_refused_beyond_it_across_rest_and_mcp(api):
    client, accounts, person, organisation = api
    measured = {**TIER, "id": "measured", "allowance": {**TIER["allowance"], "bytes_per_day": 10_000_000}}
    accounts.set_tiers([TIER, measured])
    grant(accounts, organisation, tier="measured")
    headers = key(accounts, person, organisation)
    path = "/api/v1/sources/ng-foi-act-2011"
    first = client.get(path, headers=headers)
    size = len(first.content)
    assert first.status_code == 200 and size > 100
    charged = bucket(accounts, organisation)
    assert charged["capacity"] == 10_000_000 and charged["held"] == 0 and charged["spent"] == size
    assert float(charged["refill_per_minute"]) == pytest.approx(10_000_000 / 1440)

    # Room for one more answer and a half: the next fits, the one after doesn't.
    smaller = {**measured, "allowance": {**measured["allowance"], "bytes_per_day": 2 * size + size // 2}}
    accounts.set_tiers([TIER, smaller])
    assert client.get(path, headers=headers).status_code == 200
    refused = client.get(path, headers=headers)
    assert refused.status_code == 429 and refused.json()["code"] == "rate_limited"
    assert "data a day" in refused.json()["detail"] and int(refused.headers["retry-after"]) > 60
    after = bucket(accounts, organisation)
    assert after["capacity"] == 2 * size + size // 2 and after["held"] == 0
    assert size * 2 - 1 <= after["spent"] <= size * 2  # not reset by the tier change; refill is slow

    # MCP draws on the same allowance.
    listing = {"jsonrpc": "2.0", "method": "tools/list", "id": 1}
    mcp = client.post(
        "/mcp", json=listing, headers={**headers, "Accept": "application/json, text/event-stream"}
    )
    assert mcp.status_code == 429 and mcp.json()["error"]["data"]["code"] == "rate_limited"
    days = accounts.usage(person, organisation)
    assert sum(d["requests"] for d in days) == 2 and sum(d["bytes"] for d in days) == 2 * size
    assert any(d["refusals"] for d in days)
