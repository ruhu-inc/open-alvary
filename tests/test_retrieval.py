"""The retrieval service's rules without a database (RSV-03, RSV-05, RSV-06, RSV-08): routes, fusion,
signals, bands, diversification, derived links and record-level currentness."""

from datetime import date
from types import SimpleNamespace

from api.text_index import fold, name_forms
from retrieval import links, router
from retrieval.legal_time import record_currentness
from retrieval.ranking import BAND_WIDTH, PER_WORK, RRF_K, Hit, apply_signals, diversify, fuse


def hit(version="ver_a", start=0, end=10, work="w1", node=None, unit="provision", status="current",
        replaced=False, jurisdiction="ng", document_type="act", locator=None, language="en"):  # fmt: skip
    return Hit(
        version_id=version, start_byte=start, end_byte=end, work_id=work, node_id=node or f"n{start}",
        unit=unit, kind="section", locator=locator or f"sec_{start}", pinpoint=None, heading=None,
        language=language, config="english", content_hash="0" * 64, legal_status=status, replaced=replaced,
        jurisdiction_id=jurisdiction, document_type=document_type,
    )  # fmt: skip


def test_routes_are_decided_by_rules_and_recorded():
    assert router.definition_term('meaning of "personal data" in the NDPA') == "personal data"
    assert router.definition_term("What is a “data subject”?") == "data subject"
    assert router.definition_term("data breach notification") is None
    cited = SimpleNamespace(provision=SimpleNamespace(status="resolved"))
    assert router.plan("NDPA s. 23", citation=cited, works={}, as_of=None, jurisdictions=None).routes == (
        "citation",
        "topic",
    )
    named = router.plan("the NDPA", citation=None, works={"w": "ndpa"}, as_of=None, jurisdictions=None)
    assert named.routes == ("instrument",) and named.residual == "the"
    both = router.plan(
        "consent under the NDPA",
        citation=None,
        works={"w": "ndpa"},
        as_of=date(2024, 1, 1),
        jurisdictions=("ng", "rw"),
    )
    assert both.routes == ("instrument", "topic", "historical", "comparative") and both.comparative
    assert router.plan("x", citation=None, works={}, as_of=None, jurisdictions=None).routes == ("topic",)


def test_names_fold_to_letters_and_digits_with_and_without_the_year():
    assert fold("Nigeria Data Protection Act, 2023") == "nigeria data protection act 2023"
    assert name_forms(["Nigeria Data Protection Act, 2023", "NDPA", "Act No. 37 of 2023", "Act"]) == [
        "nigeria data protection act 2023",
        "nigeria data protection act",
        "ndpa",
        "act no 37 of 2023",
    ]


def test_fusion_collapses_each_arm_to_units_then_adds_reciprocal_ranks():
    a, b = hit(start=0), hit(start=20)
    fused = fuse({"lexical": [a, a, b], "document": [b]})
    assert [c.hit.start_byte for c in fused] == [20, 0]  # b is in both arms
    assert fused[0].arms == {"lexical": 2, "document": 1}
    assert abs(fused[0].fused - (1 / (RRF_K + 2) + 1 / (RRF_K + 1))) < 1e-12


def test_signals_lead_with_the_route_then_the_instrument_and_order_bands_by_authority():
    units = [hit(start=n * 10, document_type="regulation") for n in range(BAND_WIDTH - 1)]
    units.append(hit(version="ver_c", start=0, document_type="constitution"))
    ranked = apply_signals(fuse({"lexical": units}), set())
    assert ranked[0].hit.document_type == "constitution"  # within the first band, the higher instrument
    assert any(s[0] == "instrument_authority" for s in ranked[0].signals)
    repealed = hit(version="ver_r", start=0, status="repealed")
    defined = hit(version="ver_d", start=5)
    ranked = apply_signals(fuse({"lexical": [repealed, hit(start=30)], "definition": [defined]}), {"w9"})
    assert ranked[0].hit.version_id == "ver_d" and ranked[0].signals[0][0] == "route_match"
    assert next(c for c in ranked if c.hit.version_id == "ver_r").signals[0] == (
        "legal_status",
        "repealed",
        "x0.8",
    )


def test_diversification_collapses_overlaps_and_duplicates_caps_texts_and_interleaves():
    section, inside = hit(start=0, end=100), hit(start=10, end=20, node="def")
    kept = diversify(fuse({"a": [section, inside]}), scoped=True, comparative=False)
    assert len(kept) == 1 and "collapsed into it" in kept[0].diversification[0]
    first, again = hit(version="ver_1", locator="sec_5"), hit(version="ver_2", locator="sec_5")
    kept = diversify(fuse({"a": [first, again]}), scoped=True, comparative=False)
    assert len(kept) == 1 and kept[0].also_in == ["ver_2"]
    many = [hit(start=n * 10, end=n * 10 + 5) for n in range(PER_WORK + 2)]
    other = hit(version="ver_b", work="w2", start=0)
    order = diversify(fuse({"a": [*many, other]}), scoped=False, comparative=False)
    assert [c.hit.version_id for c in order] == ["ver_a"] * PER_WORK + ["ver_b"] + ["ver_a"] * 2
    assert "deferred" in order[-1].diversification[0]  # moved behind the other text, never dropped
    assert len(diversify(fuse({"a": many}), scoped=True, comparative=False)) == PER_WORK + 2
    mixed = [
        hit(version="v1", jurisdiction="ng"),
        hit(version="v2", work="w2", jurisdiction="ng"),
        hit(version="v3", work="w3", jurisdiction="rw"),
    ]
    order = diversify(fuse({"a": mixed}), scoped=False, comparative=True)
    assert [c.hit.jurisdiction_id for c in order] == ["ng", "rw", "ng"]


def node(id_, kind, start, end, locator, parent=None):
    return SimpleNamespace(
        id=id_, kind=kind, start_byte=start, end_byte=end, locator=locator, parent_id=parent, order=start
    )


def test_definitions_in_scope_and_references_are_derived_from_the_text():
    raw = (
        "1. The Fund shall pay. See section 2 and s. 9.\n\n2. “Fund” means the Students Loans Fund.".encode()
    )
    defined_at = raw.index("“Fund”".encode())
    nodes = [
        node("n1", "section", 0, defined_at - 5, "sec_1"),
        node("n2", "section", defined_at - 3, len(raw), "sec_2"),
        node("d", "definition", defined_at, len(raw), "sec_2__def_fund", "n2"),
    ]
    defined = links.definitions(raw, nodes)
    assert [(t, n.id) for t, n in defined] == [("Fund", "d")]
    words = raw[: defined_at - 5].decode()
    assert [n.id for _, n in links.in_scope(words, defined, set())] == ["d"]
    assert links.in_scope("The fundamental rule", defined, set()) == []  # whole words only
    assert links.in_scope(words, defined, {"d"}) == []  # never its own definition
    by_locator = {n.locator: n for n in nodes}
    assert [(shown, n.id) for shown, n in links.references(words, by_locator, "sec_1")] == [("s. 2", "n2")]


def test_record_currentness_speaks_for_the_whole_text_only():
    version = SimpleNamespace(effective_from=date(2023, 6, 12), effective_until=None)
    today = date(2026, 10, 19)
    now = record_currentness("current", version, None, today)
    assert now.state == "in_force" and now.basis == "record" and now.assessed_at == today
    assert record_currentness("repealed", version, None, today).state == "not_in_force"
    assert record_currentness("unknown", version, None, today).state == "unknown"
    assert record_currentness("current", version, date(2020, 1, 1), today).state == "not_in_force"
    assert record_currentness("current", version, date(2024, 1, 1), today).state == "in_force"
    undated = SimpleNamespace(effective_from=None, effective_until=None)
    assert record_currentness("current", undated, date(2024, 1, 1), today).state == "unknown"
