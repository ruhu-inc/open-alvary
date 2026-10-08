"""Build the ``reader-1`` projection from an approved version's current representation.

The builder re-derives every node's text from the canonical bytes and checks it against the
recorded span hash, so a projection can never disagree with the approved text. Any missing
identity, hash mismatch or dangling reference fails closed rather than guessing.
"""

from hashlib import sha256

from schema.approval import PrivateVersionView
from schema.reader import FootnoteReference, ModeAvailability, ReaderCell, ReaderDescriptor, ReaderNode

CELL_STATES = {
    "present": "present",
    "empty": "empty",
    "illegible": "unavailable",
    "unsupported": "unavailable",
}


class ReaderError(ValueError):
    """The representation cannot be projected safely; nothing is served."""


def project(view: PrivateVersionView) -> ReaderDescriptor:
    record = view.record_set
    data = view.canonical_text.encode()
    if sha256(data).hexdigest() != view.content_hash:
        raise ReaderError("Canonical bytes differ from the approved content hash")
    nodes = {n["key"]: n for n in record["nodes"]}
    ids, anchors = record["node_ids"], record["anchor_ids"]
    if set(ids) != set(nodes):
        raise ReaderError("Every node needs a public identity")
    grids = {table["key"]: table for table in record["tables"]}
    cell_of = {}
    for table in grids.values():
        for cell in table["cells"]:
            for candidate in cell["candidates"]:
                cell_of[candidate] = (table, cell)

    def text_for(node):
        if node.get("start") is None:
            return None
        value = data[node["start"] : node["end"]]
        if node.get("span_hash") is not None and sha256(value).hexdigest() != node["span_hash"]:
            raise ReaderError("Node text differs from its approved span")
        return value.decode()

    references = {}
    for marker in record.get("markers", []):
        group = f"foot:{marker['scope']}:{marker['footnote_id']}"
        owner = next(
            (
                n
                for n in nodes.values()
                if n.get("candidate_id") == marker["candidate_id"] and n.get("start") is not None
            ),
            None,
        )
        if (
            group not in ids
            or owner is None
            or not owner["start"] <= marker["start"] < marker["end"] <= owner["end"]
        ):
            raise ReaderError("Footnote marker has no projected target")
        references.setdefault(owner["key"], []).append(
            FootnoteReference(
                start=marker["start"] - owner["start"],
                end=marker["end"] - owner["start"],
                footnote_node_id=ids[group],
            )
        )

    children = {}
    for node in nodes.values():
        if node["parent"] is not None and node["parent"] not in nodes:
            raise ReaderError("Node parent is not projected")
        children.setdefault(node["parent"], []).append(node)

    def position(node):
        if node["key"] in cell_of:
            _, cell = cell_of[node["key"]]
            return (0, cell["row"], cell["column"])
        return (1, node.get("start") if node.get("start") is not None else len(data), node["key"])

    ordered = []

    def visit(parent):
        for node in sorted(children.get(parent, []), key=position):
            ordered.append(node)
            visit(node["key"])

    visit(None)
    unavailable_cells = False
    projected = []
    for node in ordered:
        kind = node["kind"]
        state = "empty" if node.get("unavailable_reason") == "empty_source_content" else "present"
        if node["key"] in cell_of:
            state = CELL_STATES[cell_of[node["key"]][1]["content_state"]]
            unavailable_cells |= state == "unavailable"
        cells = ()
        if kind == "table":
            table = grids[node["key"]]
            headers = set(table["headers"])
            cells = tuple(
                ReaderCell(
                    row=cell["row"],
                    column=cell["column"],
                    row_span=cell["row_span"],
                    column_span=cell["column_span"],
                    node_id=ids[cell["candidates"][0]],
                    header=bool(headers & set(cell["candidates"])),
                    state=CELL_STATES[cell["content_state"]],
                )
                for cell in sorted(table["cells"], key=lambda c: (c["row"], c["column"]))
            )
        leaf = node.get("candidate_id") is not None
        projected.append(
            ReaderNode(
                node_id=ids[node["key"]],
                anchor_id=anchors.get(node["key"]),
                parent_id=ids[node["parent"]] if node["parent"] is not None else None,
                kind=kind,
                start=node.get("start"),
                end=node.get("end"),
                text=text_for(node) if leaf else None,
                state=state,
                references=tuple(sorted(references.get(node["key"], []), key=lambda r: r.start)),
                rows=grids[node["key"]]["rows"] if kind == "table" else None,
                columns=grids[node["key"]]["columns"] if kind == "table" else None,
                cells=cells,
            )
        )
    geometry = record.get("geometry_quality") == "available"
    limitations = ["original_not_eligible"]
    if not geometry:
        limitations.insert(0, "geometry_unavailable")
    if str(record.get("geometry_reason", "")).startswith("synthetic_fixture"):
        limitations.append("synthetic_fixture")
    if unavailable_cells:
        limitations.append("cell_unavailable")
    return ReaderDescriptor(
        version_id=view.version_id,
        content_hash=view.content_hash,
        representation_id=view.current_rep_id,
        representation_revision=view.rep_revision,
        serialization_profile_id=view.serialization_profile_id,
        modes=ModeAvailability(
            page_layout="available" if geometry else "unavailable", original="unavailable"
        ),
        limitations=tuple(limitations),
        nodes=tuple(projected),
    )
