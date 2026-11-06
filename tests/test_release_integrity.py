"""A release's rows checked a row at a time (exports.integrity) answer as Corpus.model_validate does for them
all at once: on a valid release of invented records that uses every rule, on each rule broken in turn, and on
releases broken at random."""

import copy
import inspect
import json
import random
from hashlib import sha256

import pytest

from exports import integrity, verify
from exports.release import write_release
from exports.verify import release_available
from schema.models import Corpus

NOW = "2026-01-01T00:00:00Z"
# Invented texts, with characters of two, three and four bytes so that offsets can fall inside one.
FIRST = "Synthetic Act — not law.\nSection 1. Café ✓.\nSection 2. 𝔘 test.\n"
SECOND = "Synthetic Act — not law, corrected.\nSection 1. Café ✓✓.\n"
FRENCH = "Loi synthétique — sans valeur.\nArticle 1. Essai.\n"


def digest(value: str | bytes) -> str:
    return sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def span(text: str, start: str, end: str | None = None) -> tuple[int, int]:
    raw = text.encode()
    first = raw.index(start.encode())
    return first, (raw.index(end.encode(), first) + len(end.encode())) if end else len(raw)


def version(id_, source, text, language="en", **more):
    return {
        "id": id_,
        "source_id": source,
        "canonical_url": f"https://example.org/{id_}",
        "retrieved_at": NOW,
        "raw_hash": digest(text),
        "content_hash": digest(text),
        "canonical_text": text,
        "parser": "fixture",
        "language": language,
        **more,
    }


def node(id_, version_id, text, start, end, source="zz-act-1", **more):
    return {
        "id": id_,
        "source_id": source,
        "version_id": version_id,
        "kind": more.pop("kind", "section"),
        "locator": id_,
        "order": 0,
        "start_byte": start,
        "end_byte": end,
        "source_hash": digest(text),
        **more,
    }


def release() -> dict:
    """Every table, every reference a rule looks at, and a node at every place a rule can check."""
    raw = FIRST.encode()
    whole, section_1, section_2 = (0, len(raw)), span(FIRST, "Section 1", ".\n"), span(FIRST, "Section 2")
    rows = {
        "languages": [
            {"id": "en", "name": "English", "tag": "en"},
            {"id": "fr", "name": "French", "tag": "fr"},
        ],
        "jurisdictions": [
            {"id": "zz", "name": "Synthetica", "country_code": "ZZ", "languages": ["en", "fr"]},
            {"id": "zz-north", "name": "North Synthetica", "country_code": "ZZ", "parent_id": "zz"},
        ],
        "authorities": [
            {
                "id": "zz-assembly",
                "jurisdiction_id": "zz",
                "name": "Synthetic Assembly",
                "kind": "legislature",
                "official_url": "https://example.org/assembly",
            }
        ],
        "sources": [
            {
                "id": source,
                "jurisdiction_id": jurisdiction,
                "authority_id": "zz-assembly",
                "title": f"Synthetic Act {n}",
                "document_type": "act",
                "canonical_url": f"https://example.org/{source}",
                "citation": f"Synthetic Act {n}",
                "language": "en",
            }
            for n, (source, jurisdiction) in enumerate(
                (("zz-act-1", "zz"), ("zz-act-2", "zz"), ("zz-act-3", "zz-north")), 1
            )
        ],
        "rights": [{"id": f"rights-{s}", "source_id": s} for s in ("zz-act-1", "zz-act-2", "zz-act-3")],
        "versions": [
            version("zz-act-1-v1", "zz-act-1", FIRST),
            version("zz-act-1-v2", "zz-act-1", SECOND, prior_version_id="zz-act-1-v1"),
            version("zz-act-1-fr", "zz-act-1", FRENCH, language="fr"),
            version("zz-act-2-v1", "zz-act-2", ""),
        ],
        "structure": [
            node("n-doc", "zz-act-1-v1", FIRST, *whole, kind="document", span_hash=digest(FIRST)),
            node("n-s1", "zz-act-1-v1", FIRST, *section_1, parent_id="n-doc"),
            node(
                "n-s1-a",
                "zz-act-1-v1",
                FIRST,
                *span(FIRST, "Café", "✓"),
                parent_id="n-s1",
                kind="subsection",
                span_hash=digest("Café ✓"),
            ),
            node(
                "n-s2",
                "zz-act-1-v1",
                FIRST,
                *section_2,
                parent_id="n-doc",
                span_hash=digest(raw[slice(*section_2)]),
            ),
            node(
                "n-toc",
                "zz-act-1-v1",
                FIRST,
                0,
                0,
                parent_id="n-doc",
                kind="contents_entry",
                target_node_id="n-s2",
            ),
            node("n2-doc", "zz-act-1-v2", SECOND, 0, len(SECOND.encode()), kind="document"),
            node("n3-empty", "zz-act-2-v1", "", 0, 0, source="zz-act-2", kind="document"),
        ],
        "citations": [
            {"id": "c-1", "source_id": "zz-act-1", "value": "Synthetic Act 1"},
            {
                "id": "c-2",
                "source_id": "zz-act-1",
                "value": "s. 1",
                "version_id": "zz-act-1-v1",
                "node_id": "n-s1",
            },
            {"id": "c-3", "source_id": "zz-act-2", "value": "Synthetic Act 2", "version_id": "zz-act-2-v1"},
        ],
        "amendments": [
            {
                "id": "a-1",
                "source_id": "zz-act-3",
                "target_source_id": "zz-act-1",
                "kind": "amends",
                "evidence_url": "https://example.org/a-1",
            }
        ],
        "translations": [
            {
                "id": "t-1",
                "source_version_id": "zz-act-1-v1",
                "translated_version_id": "zz-act-1-fr",
                "language": "fr",
                "translator": "Synthetic translator",
                "method": "human",
                "provenance_url": "https://example.org/t-1",
            }
        ],
    }
    return json.loads(json.dumps(rows))  # as a release file's lines give them


def at_once(rows: dict) -> bool:
    try:
        Corpus.model_validate(copy.deepcopy(rows))
    except (ValueError, KeyError, TypeError):
        return False
    return True


def a_row_at_a_time(rows: dict) -> bool:
    given = copy.deepcopy(rows)
    return integrity.valid(lambda name: iter(given[name]))


def row(rows, table, id_):
    return next(r for r in rows[table] if r["id"] == id_)


def setting(table, id_, **values):
    def change(rows):
        row(rows, table, id_).update(values)

    return change


def adding(table, value):
    return lambda rows: rows[table].append(value)


def copying(table, id_, **values):
    return lambda rows: rows[table].append({**row(rows, table, id_), **values})


def test_a_valid_release_is_valid_both_ways():
    rows = release()
    assert at_once(rows) and a_row_at_a_time(rows)


s1_start, s1_end = span(FIRST, "Section 1", ".\n")
inside_dash = FIRST.encode().index("—".encode()) + 1
inside_four = FIRST.encode().index("𝔘".encode()) + 2

BROKEN = {
    "a source twice": copying("sources", "zz-act-2"),
    "a language twice": copying("languages", "fr", name="Français"),
    "a node twice": copying("structure", "n-s2"),
    "a parent jurisdiction missing": setting("jurisdictions", "zz-north", parent_id="zz-south"),
    "a jurisdiction's language missing": lambda rows: rows["jurisdictions"][0]["languages"].append("de"),
    "a jurisdiction's language no ID could be": lambda rows: rows["jurisdictions"][0]["languages"].append(
        "é"
    ),
    "a jurisdiction's language that is no text": lambda rows: rows["jurisdictions"][0]["languages"].append(
        "\ud800"
    ),
    "an authority's jurisdiction missing": setting("authorities", "zz-assembly", jurisdiction_id="yy"),
    "a source's jurisdiction missing": setting("sources", "zz-act-3", jurisdiction_id="yy"),
    "a source's authority missing": setting("sources", "zz-act-3", authority_id="yy-court"),
    "a source's language missing": setting("sources", "zz-act-3", language="de"),
    "a rights record's source missing": setting("rights", "rights-zz-act-3", source_id="zz-act-9"),
    "two rights records for a source": copying("rights", "rights-zz-act-1", id="rights-zz-act-1-again"),
    "a version's source missing": setting("versions", "zz-act-2-v1", source_id="zz-act-9"),
    "a version's language missing": setting("versions", "zz-act-1-v2", language="de"),
    "a prior version missing": setting("versions", "zz-act-1-v2", prior_version_id="zz-act-1-v0"),
    "a version its own prior": setting("versions", "zz-act-1-v2", prior_version_id="zz-act-1-v2"),
    "a prior version of another source": setting("versions", "zz-act-2-v1", prior_version_id="zz-act-1-v1"),
    "a node's version missing": setting("structure", "n2-doc", version_id="zz-act-1-v9"),
    "a node naming another source": setting("structure", "n2-doc", source_id="zz-act-2"),
    "a node naming another text": setting("structure", "n2-doc", source_hash=digest(FIRST)),
    "a node past its text": setting("structure", "n2-doc", end_byte=len(SECOND.encode()) + 1),
    "a node past any text there could be": setting("structure", "n2-doc", end_byte=2**63),
    "a node far past any text": setting("structure", "n2-doc", end_byte=2**70),
    "a node starting inside a character": setting(
        "structure", "n-s2", start_byte=inside_four, span_hash=None
    ),
    "a node ending inside a character": setting("structure", "n-s2", end_byte=inside_four, span_hash=None),
    "a node ending inside a character of two bytes": setting("structure", "n-toc", end_byte=inside_dash),
    "a span hash that is not the span's": setting("structure", "n-s1", span_hash=digest("Section 1")),
    "a contents entry's target missing": setting("structure", "n-toc", target_node_id="n-s9"),
    "a contents entry into another version": setting("structure", "n-toc", target_node_id="n2-doc"),
    "a parent missing": setting("structure", "n-s1", parent_id="n-s9"),
    "a parent in another version": setting("structure", "n2-doc", parent_id="n-doc"),
    "a parent not around its child": setting("structure", "n-s1-a", start_byte=s1_start - 1, span_hash=None),
    "a node its own parent": setting("structure", "n-doc", parent_id="n-doc"),
    "two nodes each the other's parent": lambda rows: (
        row(rows, "structure", "n-doc").update(parent_id="n-doc-2"),
        rows["structure"].append({**row(rows, "structure", "n-doc"), "id": "n-doc-2", "parent_id": "n-doc"}),
    ),
    "a citation's source missing": setting("citations", "c-1", source_id="zz-act-9"),
    "a citation's version of another source": setting("citations", "c-3", version_id="zz-act-1-v1"),
    "a citation's version missing": setting("citations", "c-3", version_id="zz-act-2-v9"),
    "a citation's node missing": setting("citations", "c-2", node_id="n-s9"),
    "a citation's node in another version": setting("citations", "c-2", node_id="n2-doc"),
    "a citation's node with no version": lambda rows: row(rows, "citations", "c-2").pop("version_id"),
    "an amendment's source missing": setting("amendments", "a-1", source_id="zz-act-9"),
    "an amendment's target missing": setting("amendments", "a-1", target_source_id="zz-act-9"),
    "a translation's original missing": setting("translations", "t-1", source_version_id="zz-act-1-v9"),
    "a translation missing": setting("translations", "t-1", translated_version_id="zz-act-1-de"),
    "a translation's language missing": setting("translations", "t-1", language="de"),
    "a translation of another source": setting("translations", "t-1", source_version_id="zz-act-2-v1"),
    "a translation in another language": setting("translations", "t-1", translated_version_id="zz-act-1-v2"),
    # And what each row's own model refuses.
    "a field no model has": setting("sources", "zz-act-1", shelf="B4"),
    "a text that is not its hash": setting("versions", "zz-act-1-v2", canonical_text=SECOND + " "),
    "a node ending before it starts": setting("structure", "n-s1", end_byte=s1_start - 1),
    "an ID no ID could be": setting("sources", "zz-act-2", id="zz act 2"),
    "a row that is no object": adding("citations", ["c-9", "zz-act-1"]),
}


@pytest.mark.parametrize("broken", BROKEN)
def test_each_rule_broken_is_refused_both_ways(broken):
    rows = release()
    BROKEN[broken](rows)
    assert (at_once(rows), a_row_at_a_time(rows)) == (False, False)


STILL_VALID = {
    "a node with no span hash": setting("structure", "n-s1", span_hash=None),
    "a node ending where its parent ends": setting("structure", "n-s1-a", end_byte=s1_end, span_hash=None),
    "a node ending at its text's end": setting(
        "structure", "n-s2", span_hash=None, end_byte=len(FIRST.encode())
    ),
    "a citation of a source alone": lambda rows: row(rows, "citations", "c-2").update(
        version_id=None, node_id=None
    ),
    "a jurisdiction's language twice": lambda rows: rows["jurisdictions"][0]["languages"].append("en"),
    "a node with padding stripped from its ID": setting("structure", "n-toc", id=" n-toc "),
}


@pytest.mark.parametrize("change", STILL_VALID)
def test_what_breaks_no_rule_is_valid_both_ways(change):
    rows = release()
    STILL_VALID[change](rows)
    assert (at_once(rows), a_row_at_a_time(rows)) == (True, True)


def test_releases_broken_at_random_are_answered_the_same_both_ways():
    """Values moved between rows of every table, rows copied and dropped, offsets moved: whatever the rows, the two
    answer alike, and both answers come up."""
    chance = random.Random(20261106)
    base = release()
    ids = sorted({r["id"] for table in base.values() for r in table} | {"zz-act-9", "en", "fr", "zz"})
    answers = []
    for _ in range(600):
        rows = copy.deepcopy(base)
        for _ in range(chance.choice((1, 1, 2))):
            table = chance.choice([name for name in rows if rows[name]])
            target = chance.choice(rows[table])
            move = chance.random()
            if move < 0.1:
                rows[table].remove(target)
            elif move < 0.2:
                rows[table].append(copy.deepcopy(target))
            else:
                field = chance.choice(sorted(target))
                value = target[field]
                if isinstance(value, int) and not isinstance(value, bool):
                    target[field] = max(0, value + chance.choice((-3, -1, 1, 2, 4)))
                elif isinstance(value, str) and field not in ("canonical_text",):
                    target[field] = chance.choice(ids) if chance.random() < 0.8 else value
                elif value is None:
                    target[field] = chance.choice(ids)
        answers.append(at_once(rows))
        assert a_row_at_a_time(rows) == answers[-1], rows
    assert True in answers and False in answers


def test_it_reads_every_file_of_a_corpus_and_keeps_each_rule_of_corpus_integrity():
    """The files are Corpus's fields. Corpus.integrity is the code these rules were written from: when it changes,
    exports.integrity must say the same, and then this records the new code."""
    assert set(integrity.KEPT) == set(Corpus.model_fields) == set(integrity.MODELS)
    code = inspect.getsource(Corpus.integrity)
    assert digest(code) == "68b9d23576a84c55bd7593eea9fbf49f943c4d99590abfe03881d9f911a1f307", (
        "Corpus.integrity changed: make exports.integrity say the same"
    )


def test_a_scratch_space_that_fails_is_no_answer_and_never_a_withdrawal(
    cleared, store, tmp_path, monkeypatch
):
    """No temporary folder, or a scratch database that fails: an error for this request, nothing remembered, and the
    release served again once the scratch space is back."""
    store.save(cleared)
    path = write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    monkeypatch.setattr(verify, "_validated", {})
    made = integrity.tempfile.TemporaryDirectory

    def no_room(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(integrity.tempfile, "TemporaryDirectory", no_room)
    with pytest.raises(integrity.ScratchUnavailable):
        release_available(path, store)
    monkeypatch.setattr(integrity.tempfile, "TemporaryDirectory", made)
    monkeypatch.setattr(integrity, "TABLES", "CREATE TABLE broken (")
    with pytest.raises(integrity.ScratchUnavailable):
        release_available(path, store)
    assert verify._validated == {}
    monkeypatch.undo()
    monkeypatch.setattr(verify, "_validated", {})
    assert release_available(path, store)
