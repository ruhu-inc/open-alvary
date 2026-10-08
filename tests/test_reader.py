"""reader-1 projection: exact canonical text, ordered structure, tables and footnotes."""

import copy
import json
from pathlib import Path

import pytest

from rights.reader import ReaderError, project
from schema.approval import PrivateVersionView

FIXTURE = json.loads(Path("tests/fixtures/reader-three-notice.json").read_text())


def view(data=FIXTURE):
    return PrivateVersionView.model_validate(data)


def test_projection_is_exact_ordered_and_identity_only():
    descriptor = project(view())
    data = FIXTURE["canonical_text"].encode()
    assert descriptor.schema_version == "reader-1" and descriptor.version_id == FIXTURE["version_id"]
    for node in descriptor.nodes:
        assert node.node_id.startswith("nod_")
        if node.text is not None:
            assert node.text.encode() == data[node.start : node.end]
    kinds = [n.kind for n in descriptor.nodes]
    assert kinds[0] == "notice" and kinds.count("notice") == 3
    starts = [n.start for n in descriptor.nodes if n.kind == "notice"]
    assert starts == sorted(starts)  # notices in document order


def test_children_follow_parents_and_table_grid_is_explicit():
    descriptor = project(view())
    seen = set()
    for node in descriptor.nodes:
        assert node.parent_id is None or node.parent_id in seen
        seen.add(node.node_id)
    [table] = [n for n in descriptor.nodes if n.kind == "table"]
    assert (table.rows, table.columns) == (2, 3)
    assert [(c.row, c.column, c.row_span, c.column_span) for c in table.cells][0] == (0, 0, 1, 2)
    assert [c.header for c in table.cells] == [True, False, False, True, False]
    assert [c.state for c in table.cells] == ["present", "empty", "present", "present", "empty"]
    empty = [n for n in descriptor.nodes if n.kind == "table_cell" and n.state == "empty"]
    assert all(n.text is None for n in empty)


def test_footnote_markers_point_at_their_own_notice_group():
    descriptor = project(view())
    by_id = {n.node_id: n for n in descriptor.nodes}
    referencing = [n for n in descriptor.nodes if n.references]
    assert len(referencing) == 2
    targets = {r.footnote_node_id for n in referencing for r in n.references}
    assert len(targets) == 2 and all(by_id[t].kind == "footnote_group" for t in targets)
    for node in referencing:
        for ref in node.references:
            assert node.text.encode()[ref.start : ref.end] == b"[n]"


def test_limitations_and_modes_are_safe_observations():
    descriptor = project(view())
    assert descriptor.modes.page_layout == "unavailable" and descriptor.modes.original == "unavailable"
    assert descriptor.limitations == ("geometry_unavailable", "original_not_eligible", "synthetic_fixture")


@pytest.mark.parametrize("tamper", ["text", "span", "identity", "marker"])
def test_any_inconsistency_fails_closed(tamper):
    data = copy.deepcopy(FIXTURE)
    if tamper == "text":
        data["canonical_text"] = data["canonical_text"].replace("Café", "Cafe", 1)
    elif tamper == "span":
        data["record_set"]["nodes"][0]["span_hash"] = "0" * 64
    elif tamper == "identity":
        data["record_set"]["node_ids"].popitem()
    else:
        data["record_set"]["markers"][0]["scope"] = "missing"
    with pytest.raises(ReaderError):
        project(view(data))
