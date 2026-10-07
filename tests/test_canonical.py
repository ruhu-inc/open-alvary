"""Hand-calculated OA-text-1 fixtures, not legal extraction or approval evidence."""

from hashlib import sha256
from uuid import UUID

import pytest

from ingestion.canonical import CanonicalValidationError, compile_projection
from schema.assembly import CandidateInput
from schema.canonical import CanonicalPlan


def leaf(index, text, **updates):
    data = dict(
        id=UUID(int=index),
        adapter_local_id=f"local{index}",
        region_key=f"region{index}",
        artifact_class="native_text",
        block_type="paragraph",
        text=text,
        unavailable_page_reason="Original synthetic fixture has no page",
        unavailable_geometry_reason="Original synthetic fixture has no geometry",
    )
    data.update(updates)
    return CandidateInput.model_validate(data)


def project(candidates, plan, order=None):
    return compile_projection(
        CanonicalPlan.model_validate(plan), tuple(candidates), tuple(order or [c.id for c in candidates])
    )


def texts(candidates):
    return dict(blocks=[dict(kind="text", candidate_id=c.id) for c in candidates])


def test_literal_nfc_lf_whitespace_bidi_symbols_and_added_terminator():
    candidates = [
        leaf(1, "  Cafe\u0301\r\n§ — \tلا\u2067RTL\u2069 😀\r"),
        leaf(2, "Do not pay 1,000.00. No repair of abbrev.\n"),
    ]
    result = project(candidates, texts(candidates))
    expected = (
        "  Café\n§ — \tلا\u2067RTL\u2069 😀\n\n\nDo not pay 1,000.00. No repair of abbrev.\n\n".encode()
    )
    assert result.canonical_bytes == expected
    assert result.details.normalizations[0].operations == ("lf", "nfc")
    assert result.details.separators.block == (len("  Café\n§ — \tلا\u2067RTL\u2069 😀\n".encode()),)
    assert result.details.publication_eligible is False
    for span in result.details.spans:
        assert sha256(expected[span.start : span.end]).hexdigest() == span.span_hash
        expected[: span.start].decode()
        expected[: span.end].decode()


def table_candidates():
    def cell(id, text, row, col, **extra):
        kwargs = dict(block_type="table_cell", cell=dict(table_local_id="t1", row=row, column=col))
        kwargs.update(extra)
        return leaf(id, text, **kwargs)

    return [
        cell(1, "Amount\tunit", 0, 0, cell=dict(table_local_id="t1", row=0, column=0, column_span=2)),
        cell(2, "", 0, 2, content_state="empty"),
        cell(3, "1,000.00\nnot waived", 1, 0),
        cell(4, "", 1, 1, content_state="illegible", content_reason="Unreadable original synthetic cell"),
        cell(5, "", 1, 2, content_state="unsupported", content_reason="Unsupported original synthetic cell"),
    ]


def table_plan(candidates):
    return dict(
        blocks=[
            dict(
                kind="table",
                table_local_id="t1",
                rows=2,
                columns=3,
                cells=[dict(candidates=[c.id]) for c in candidates],
                headers=[candidates[0].id],
            )
        ]
    )


def test_merged_empty_illegible_cells_literal_tabs_newlines_and_grid_lineage():
    candidates = table_candidates()
    result = project(candidates, table_plan(candidates))
    assert result.canonical_bytes == b"Amount\tunit\t\t\n1,000.00\nnot waived\t\t\n"
    assert result.details.incomplete_tables == ("t1",)
    assert [s.candidate_id for s in result.details.spans] == [candidates[0].id, candidates[2].id]
    assert len(result.details.separators.column) == 4 and len(result.details.separators.row) == 1


def test_cell_child_blocks_use_two_lf_without_duplicate_grid_positions():
    a = leaf(1, "First", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0))
    b = leaf(2, "Second", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0))
    result = project(
        [a, b],
        dict(
            blocks=[
                dict(
                    kind="table", table_local_id="t", rows=1, columns=1, cells=[dict(candidates=[a.id, b.id])]
                )
            ]
        ),
    )
    assert result.canonical_bytes == b"First\n\nSecond\n" and result.details.separators.cell == (5,)


def test_furniture_and_repeated_header_are_explicit_and_not_in_canonical_bytes():
    a = leaf(1, "Header", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0))
    repeat = leaf(2, "Header", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0))
    furniture = leaf(3, "Page 9", block_type="heading")
    plan = dict(
        blocks=[
            dict(
                kind="table",
                table_local_id="t",
                rows=1,
                columns=1,
                cells=[dict(candidates=[a.id])],
                headers=[a.id],
            )
        ],
        exclusions=[
            dict(
                candidate_id=repeat.id,
                kind="repeated_header",
                target_candidate_id=a.id,
                reason="Original synthetic visual repeat proposal",
            ),
            dict(
                candidate_id=furniture.id,
                kind="furniture",
                reason="Original synthetic page furniture proposal",
            ),
        ],
    )
    assert project([a, repeat, furniture], plan).canonical_bytes == b"Header\n"
    altered = repeat.model_copy(update={"text": "Substantive different header"})
    with pytest.raises(CanonicalValidationError):
        project([a, altered, furniture], plan)


def test_notice_footnotes_follow_first_marker_then_unreferenced_source_order():
    a = leaf(1, "Notice [b] then [a].")
    fa = leaf(2, "Footnote A", block_type="footnote")
    fb = leaf(3, "Footnote B", block_type="footnote")
    fc = leaf(4, "Unreferenced C", block_type="footnote")
    final = leaf(5, "Second notice.")
    plan = dict(
        blocks=[
            dict(
                kind="notice",
                local_id="notice1",
                blocks=[dict(kind="text", candidate_id=a.id)],
                footnotes=[
                    dict(local_id="a", candidates=[fa.id]),
                    dict(local_id="b", candidates=[fb.id]),
                    dict(local_id="c", candidates=[fc.id]),
                ],
                markers=[
                    dict(candidate_id=a.id, source_start=16, source_end=19, footnote_id="a"),
                    dict(candidate_id=a.id, source_start=7, source_end=10, footnote_id="b"),
                ],
            ),
            dict(kind="notice", local_id="notice2", blocks=[dict(kind="text", candidate_id=final.id)]),
        ]
    )
    result = project([a, fa, fb, fc, final], plan)
    assert (
        result.canonical_bytes
        == b"Notice [b] then [a].\n\nFootnote B\n\nFootnote A\n\nUnreferenced C\n\nSecond notice.\n"
    )
    assert [m.footnote_id for m in result.details.markers] == ["b", "a"]
    for notice in result.details.notice_ranges:
        assert sha256(result.canonical_bytes[notice.start : notice.end]).hexdigest() == notice.content_hash


def test_empty_document_has_no_bytes_or_fabricated_span():
    c = leaf(1, "Fictional furniture")
    result = project(
        [c],
        dict(
            blocks=[],
            exclusions=[
                dict(candidate_id=c.id, kind="furniture", reason="Exclude in original synthetic proposal")
            ],
        ),
    )
    assert (
        result.canonical_bytes == b""
        and result.details.spans == ()
        and result.details.separators.terminator == ()
    )


@pytest.mark.parametrize("change", ["grid_gap", "overlap", "order", "omit", "repeat", "unavailable_prose"])
def test_inconsistent_structure_is_not_silently_repaired(change):
    candidates = table_candidates()
    plan = table_plan(candidates)
    if change == "grid_gap":
        plan["blocks"][0]["cells"].pop()
    elif change == "overlap":
        candidates[2] = candidates[2].model_copy(update={"cell": candidates[0].cell})
    elif change == "order":
        plan["blocks"][0]["cells"].reverse()
        candidates.reverse()
    elif change == "omit":
        plan["blocks"] = []
    elif change == "repeat":
        plan["blocks"] *= 2
    else:
        candidates = [leaf(1, "", content_state="unsupported", content_reason="Missing literal source")]
        plan = texts(candidates)
    with pytest.raises(CanonicalValidationError):
        project(candidates, plan)


@pytest.mark.parametrize("start,end", [(1, 2), (0, 1), (2, 3)])
def test_marker_inside_utf8_or_normalization_composition_is_rejected(start, end):
    c = leaf(1, "e\u0301 [x]")
    f = leaf(2, "Note", block_type="footnote")
    plan = dict(
        blocks=[dict(kind="text", candidate_id=c.id)],
        footnotes=[dict(local_id="n", candidates=[f.id])],
        markers=[dict(candidate_id=c.id, source_start=start, source_end=end, footnote_id="n")],
    )
    with pytest.raises(CanonicalValidationError):
        project([c, f], plan)


def test_explicit_unavailable_grid_position_is_not_inferred_empty():
    c = leaf(1, "Known", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0))
    plan = dict(
        blocks=[
            dict(
                kind="table",
                table_local_id="t",
                rows=1,
                columns=2,
                cells=[dict(candidates=[c.id])],
                unavailable=[
                    dict(
                        row=0,
                        column=1,
                        content_state="unsupported",
                        reason="Missing original synthetic transcription",
                    )
                ],
            )
        ]
    )
    result = project([c], plan)
    assert result.canonical_bytes == b"Known\t\n" and result.details.incomplete_tables == ("t",)


def test_literal_hyphenation_is_not_joined_or_repaired():
    c = leaf(1, "provi-\r\nsion not waived")
    result = project([c], texts([c]))
    assert result.canonical_bytes == b"provi-\nsion not waived\n"


def test_document_marker_cannot_claim_a_notice_leaf_or_overlap_another_marker():
    c = leaf(1, "[one] [two]")
    f = leaf(2, "Note", block_type="footnote")
    plan = dict(
        blocks=[dict(kind="notice", local_id="n", blocks=[dict(kind="text", candidate_id=c.id)])],
        footnotes=[dict(local_id="fn", candidates=[f.id])],
        markers=[dict(candidate_id=c.id, source_start=0, source_end=5, footnote_id="fn")],
    )
    with pytest.raises(CanonicalValidationError):
        project([c, f], plan)
    plan["blocks"] = [dict(kind="text", candidate_id=c.id)]
    plan["markers"].append(dict(candidate_id=c.id, source_start=3, source_end=8, footnote_id="fn"))
    with pytest.raises(CanonicalValidationError):
        project([c, f], plan)
