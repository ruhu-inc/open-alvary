"""The reference client (RSV-09): every conformance scenario, with a cache in memory and in a SQLite
file, then its rules one at a time: request keys, no cache before a feed position, restarts, resets,
the check operation's effect on the cache, ETag revalidation, Retry-After, profiles and the
application's saved answers."""

import importlib.util
import sys
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from schema.public import CheckPage, EvidencePack

CLIENTS = Path(__file__).resolve().parent.parent / "clients"
sys.path.insert(0, str(CLIENTS / "python"))
oa = importlib.import_module("open_alvary_client")
_spec = importlib.util.spec_from_file_location(
    "open_alvary_conformance", CLIENTS / "conformance" / "runner.py"
)
conformance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conformance)

BODIES = conformance.bodies()
PACK = BODIES["pack_breach"]
U1, U2 = PACK["evidence"]
Q = "when must a controller report a breach"
START = datetime.fromisoformat("2026-10-19T09:00:00+00:00")
ETAG = 'W/"pack-breach-1"'
ITEMS = [{"anchor_id": u["anchor_id"], "span_hash": u["span_hash"]} for u in (U1, U2)]
SCENARIOS = {s["name"]: s for s in conformance.scenarios()}


def caches(kind, tmp_path):
    return oa.MemoryCache() if kind == "memory" else oa.SQLiteCache(tmp_path / "cache.sqlite")


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_conformance_scenario_passes_with_a_cache_in_memory_and_in_sqlite(name, kind, tmp_path):
    def make_client(*, base_url, key, transport, clock):
        return oa.Client(base_url, key, cache=caches(kind, tmp_path), transport=transport, clock=clock)

    conformance.run(SCENARIOS[name], make_client)


def test_the_scenarios_answer_with_valid_public_documents():
    every = [BODIES, *(s.get("bodies", {}) for s in SCENARIOS.values())]
    packs = checks = 0
    for bodies in every:
        for body in bodies.values():
            if body.get("schema_version") == "oa-evidence-1":
                EvidencePack.model_validate(body)
                packs += 1
            elif body.get("schema_version") == "oa-check-1":
                CheckPage.model_validate(body)
                checks += 1
    assert packs >= 3 and checks >= 3
    covered = {step["do"] for scenario in SCENARIOS.values() for step in scenario["steps"]}
    assert covered == {"sync", "retrieve", "check", "advance"}


# --- a scripted Open Alvary ---------------------------------------------------------------------------


def scripted(cache=None, clock=None):
    clock = clock or conformance.FakeClock(START)
    server = conformance.Server(clock, BODIES)
    transport = httpx.MockTransport(server.handle)
    client = oa.Client(conformance.BASE, conformance.KEY, cache=cache, transport=transport, clock=clock)
    return client, server, clock


def exchange(method, path, body=None, status=200, headers=None, **request):
    response = {"status": status} | ({"body": body} if body is not None else {})
    return {
        "request": {"method": method, "path": path, **request},
        "response": response | {"headers": headers or {}},
    }


def step(server, exchanges, action):
    server.expect(exchanges)
    outcome = action()
    assert not server.problems and not server.script, (server.problems, server.script)
    return outcome


def started(client, server):
    """A client with a feed position and the breach pack cached."""
    step(
        server, [exchange("GET", "/changes", "$changes_start", params={"start": "now"})], client.sync_changes
    )
    fresh = [exchange("GET", "/retrieve", "$pack_breach", headers={"ETag": ETAG}, params={"q": Q})]
    return step(server, fresh, lambda: client.retrieve(Q, jurisdiction="ng"))


def changes(*events, token="r1.next", epoch="1"):
    return {"events": list(events), "has_more": False, "feed_epoch": epoch, "resume_token": token}


def event(kind, **fields):
    return {"event_type": kind, "event_id": "e", "event_revision": 1, "feed_epoch": "1", "work": None,
            "version": None, "withdrawal": None, "change_reason": None, "changed_fields": []} | fields  # fmt: skip


def flags(client):
    return {r["anchor_id"]: r["flags"] for _, r in client.cache.scan("unit")}


# --- the rules one at a time ---------------------------------------------------------------------------


def test_requests_that_ask_the_same_thing_share_a_key_and_unknown_filters_are_refused():
    key = oa.client.normalise
    same = key(" when  must a controller\treport a breach ", {"jurisdiction": ["rw", "ng"], "max_units": 10})
    assert same == key(Q, {"jurisdiction": ["ng", "rw"], "include": ["ranking", "context", "expansion"]})
    assert same.params == [("q", Q), ("jurisdiction", "ng"), ("jurisdiction", "rw")]
    assert key(Q, {"as_of": START.date()}).params[-1] == ("as_of", "2026-10-19")
    assert key(Q, {"jurisdiction": "ng"}).key != key(Q, {"jurisdiction": "rw"}).key
    with pytest.raises(TypeError):
        key(Q, {"country": "ng"})
    with pytest.raises(ValueError):
        key("   ", {})


def test_nothing_is_cached_before_the_client_has_a_feed_position():
    client, server, _ = scripted()
    feed_down = exchange("GET", "/changes", status=503, params={"start": "now"}) | {"repeat": 3}
    fresh = exchange("GET", "/retrieve", "$pack_breach", params={"q": Q})
    answer = step(server, [feed_down, fresh], lambda: client.retrieve(Q))
    assert not answer.from_cache and answer.checked
    assert client.cache.scan("pack") == [] and client.cache.scan("unit") == []


def test_a_sqlite_cache_keeps_evidence_and_the_feed_position_across_restarts(tmp_path):
    client, server, clock = scripted(oa.SQLiteCache(tmp_path / "cache.sqlite"))
    started(client, server)
    client.cache.close()
    again, server, _ = scripted(oa.SQLiteCache(tmp_path / "cache.sqlite"), clock)
    answer = step(server, [], lambda: again.retrieve(Q, jurisdiction="ng"))
    assert answer.from_cache and not answer.checked and answer.cached_at == START
    feed = [exchange("GET", "/changes", "$changes_none", params={"resume_token": "r1.start"})]
    assert step(server, feed, again.sync_changes).reset is None


def test_a_withdrawal_naming_no_record_and_a_voided_token_mean_every_unit_is_checked():
    client, server, _ = scripted()
    started(client, server)
    unnamed = event(
        "withdrawal", withdrawal={"scope_disclosure": "revalidate_releases", "action": "revalidate"}
    )
    feed = [exchange("GET", "/changes", changes(unnamed), params={"resume_token": "r1.start"})]
    result = step(server, feed, client.sync_changes)
    assert (
        result.marked == 2 and result.purged == [] and set(flags(client)[U1["anchor_id"]]) == {"needs_check"}
    )
    # A server restarted with tokens that lasted only as long as its process cannot open the old one.
    voided = {"code": "invalid_cursor", "status": 400, "detail": "This cursor or resume token is not valid."}
    feed = [
        exchange("GET", "/changes", voided, 400, params={"resume_token": "r1.next"}),
        exchange("GET", "/changes", changes(token="r1.fresh"), params={"start": "now"}),
    ]
    assert step(server, feed, client.sync_changes).reset == "invalid_cursor"
    assert client.cache.feed()["resume_token"] == "r1.fresh"


def test_a_new_epoch_on_a_page_a_first_text_and_a_corrected_legal_status():
    client, server, _ = scripted()
    started(client, server)
    first = event("text_version_published", change_reason="first_text", work={"id": "src-new"},
                  version={"id": "ver-new", "language": "en"})  # fmt: skip
    status = event("metadata_corrected", work={"id": U2["source_id"]}, changed_fields=["legal_status"])
    title = event("metadata_corrected", work={"id": U1["source_id"]}, changed_fields=["title"])
    feed = [exchange("GET", "/changes", changes(first, status, title), params={"resume_token": "r1.start"})]
    result = step(server, feed, client.sync_changes)
    assert result.events == 3 and result.marked == 1 and result.reset is None
    assert flags(client) == {U1["anchor_id"]: [], U2["anchor_id"]: ["currentness"]}
    feed = [
        exchange("GET", "/changes", changes(token="r9.next", epoch="9"), params={"resume_token": "r1.next"})
    ]
    assert step(server, feed, client.sync_changes).reset == "new_epoch"
    assert flags(client) == {
        U1["anchor_id"]: ["needs_check"],
        U2["anchor_id"]: ["currentness", "needs_check"],
    }


def test_what_the_check_operation_finds_updates_the_cache():
    client, server, _ = scripted()
    started(client, server)
    newer = {"schema_version": "oa-check-1", "dataset": "1:41", "results": [
        {"index": 0, "result": "newer_version", "anchor_id": U1["anchor_id"], "newer_anchor_id": "anc-sda-2-s40"},
        {"index": 1, "result": "unchanged", "anchor_id": U2["anchor_id"], "currentness": U2["currentness"]},
    ]}  # fmt: skip
    page = step(
        server,
        [exchange("POST", "/currentness", newer, json={"items": ITEMS})],
        lambda: client.check(PACK["evidence"]),
    )
    assert [r["result"] for r in page["results"]] == ["newer_version", "unchanged"]
    assert flags(client) == {U1["anchor_id"]: ["newer_version"], U2["anchor_id"]: []}
    # A known newer version is fetched again at once: checking it again would say the same.
    refetch = exchange("GET", "/retrieve", "$pack_breach", headers={"ETag": ETAG}, params={"q": Q})
    refetch["request"]["headers"] = {"if-none-match": ETAG}
    assert not step(server, [refetch], lambda: client.retrieve(Q, jurisdiction="ng")).from_cache
    gone = {"schema_version": "oa-check-1", "dataset": "1:41", "results": [
        {"index": 0, "result": "unavailable", "anchor_id": U1["anchor_id"]},
    ]}  # fmt: skip
    step(server, [exchange("POST", "/currentness", gone)], lambda: client.check([ITEMS[0]]))
    assert (
        client.cache.scan("pack") == [] and client.cache.scan("unit") == []
    )  # its pack, and so U2, went too


def test_the_check_operation_sends_a_hundred_items_at_a_time():
    client, server, _ = scripted()
    items = [{"anchor_id": f"anc-{n}"} for n in range(150)]

    def page(size):
        return {"dataset": "1:1", "results": [{"index": n, "result": "unavailable"} for n in range(size)]}

    posts = [exchange("POST", "/currentness", page(size), json={"items": [{}] * size}) for size in (100, 50)]
    found = step(server, posts, lambda: client.check(items))
    assert [r["index"] for r in found["results"]] == list(range(150))


def test_a_304_confirms_the_cached_pack_and_a_long_retry_after_is_not_waited_for():
    client, server, clock = scripted()
    started(client, server)
    clock.advance(3600)
    # The feed is down for an hour: one attempt, no wait. Its pack is revalidated instead of reused.
    feed = exchange("GET", "/changes", "$problem_unavailable", 503, {"Retry-After": "3600"})
    same = exchange("GET", "/retrieve", status=304, headers={"ETag": ETAG}, params={"q": Q})
    same["request"]["headers"] = {"if-none-match": ETAG}
    answer = step(server, [feed, same], lambda: client.retrieve(Q, jurisdiction="ng"))
    assert answer.from_cache and answer.checked and not answer.offline
    assert [u.label for u in answer.units] == ["checked", "checked"] and clock.now() == START + timedelta(
        hours=1
    )


def test_retry_after_may_be_an_http_date():
    client, server, clock = scripted()
    step(server, [exchange("GET", "/changes", "$changes_start")], client.sync_changes)
    later = format_datetime(START + timedelta(seconds=5), usegmt=True)
    limited = exchange("GET", "/retrieve", "$problem_rate_limited", 429, {"Retry-After": later})
    fresh = exchange("GET", "/retrieve", "$pack_breach", not_before=5)
    assert step(server, [limited, fresh], lambda: client.retrieve(Q)).checked


def test_a_pack_of_other_profiles_is_not_reused():
    client, server, _ = scripted()
    started(client, server)
    reranked = PACK | {"revisions": PACK["revisions"] | {"ranking": PACK["revisions"]["ranking"] + "-next"}}
    other = exchange("GET", "/retrieve", reranked, params={"q": "breach report"})
    step(server, [other], lambda: client.retrieve("breach report", jurisdiction="ng"))
    again = exchange("GET", "/retrieve", reranked, params={"q": Q})
    assert not step(server, [again], lambda: client.retrieve(Q, jurisdiction="ng")).from_cache


def test_saved_answers_go_with_a_withdrawal_and_after_seven_days_without_the_feed():
    client, server, clock = scripted()
    answer = started(client, server)
    client.save_answer("a-1", {"answer": "Within 72 hours."}, answer)
    kept = client.saved_answer("a-1")
    assert kept.body == {"answer": "Within 72 hours."} and kept.saved_at == START
    assert [u.label for u in kept.units] == ["currentness_not_checked"] * 2
    feed = [exchange("GET", "/changes", "$changes_withdrawal", params={"resume_token": "r1.start"})]
    assert step(server, feed, client.sync_changes).purged == ["ver-sda-1"]
    assert client.saved_answer("a-1") is None and client.cache.withdrawn("ver-sda-1")
    with pytest.raises(ValueError):
        client.save_answer("a-2", {}, answer)  # it cites the withdrawn text
    client.save_answer(
        "a-3", {}, oa.Answer(PACK | {"evidence": [U2]}, (oa.Cited(U2, "checked"),), False, True)
    )
    clock.advance(8 * 86400)
    with pytest.raises(oa.CacheExpired):
        client.saved_answer("a-3")


def test_the_client_never_logs_query_text_even_through_httpx(caplog):
    client, server, _ = scripted()
    with caplog.at_level("DEBUG"):
        started(client, server)
    assert any("/retrieve" in m for m in caplog.messages)
    assert not any("controller" in m or U1["text"] in m for m in caplog.messages)
