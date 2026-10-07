"""Bounded private OA-text-1 serializer; source candidates remain unverified."""

import json
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from schema.assembly import CandidateInput
from schema.canonical import CanonicalPlan, ProjectionDetails

PROFILE_DESCRIPTOR = '{"id":"OA-text-1","scope":"original-synthetic","encoding":"UTF-8","normalization":"NFC","line_endings":"LF","whitespace":"literal","block_separator":"LF LF","table_column_separator":"TAB","table_row_separator":"LF","terminator":"one added LF","dehyphenation":"unsupported","alignment":"whole private candidate"}'
PROFILE_HASH = sha256(PROFILE_DESCRIPTOR.encode()).hexdigest()
MAX_CANONICAL_BYTES = 128 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024


class CanonicalValidationError(ValueError):
    def __init__(self):
        super().__init__("Unsupported or inconsistent private canonical structure")


def normalize_literal(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


@dataclass(frozen=True)
class Projection:
    canonical_bytes: bytes
    details: ProjectionDetails


def compile_projection(
    plan: CanonicalPlan, candidates: tuple[CandidateInput, ...], order: tuple[UUID, ...]
) -> Projection:
    """Every selected candidate is consumed exactly once or explicitly excluded."""

    def fail():
        raise CanonicalValidationError()

    source = {c.id: c for c in candidates}
    if len(source) != len(candidates) or set(source) != set(order) or len(order) != len(source):
        fail()
    rank = {id: i for i, id in enumerate(order)}
    result = bytearray()
    used, body_order, headers, local_ids = [], [], set(), set()
    spans, normalizations, markers_out, notice_ranges, incomplete = [], [], [], [], []
    offsets = {key: [] for key in ("block", "cell", "column", "row", "terminator")}
    grid_positions = 0

    def append(data: bytes):
        result.extend(data)
        if len(result) > MAX_CANONICAL_BYTES:
            fail()

    def separator(kind):
        offsets[kind].append(len(result))
        append(b"\n\n" if kind in {"block", "cell"} else b"\t" if kind == "column" else b"\n")

    def candidate(id, types):
        if id not in source or id in used or source[id].block_type not in types:
            fail()
        used.append(id)
        return source[id]

    def leaf(id, types, *, body=False):
        c = candidate(id, types)
        if body:
            body_order.append(id)
        literal = c.text.encode()
        lf = c.text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalize_literal(c.text).encode()
        ops = (["lf"] if lf != c.text else []) + (["nfc"] if normalize_literal(c.text) != lf else [])
        normalizations.append(
            dict(
                candidate_id=id,
                source_hash=sha256(literal).hexdigest(),
                source_bytes=len(literal),
                normalized_hash=sha256(normalized).hexdigest(),
                normalized_bytes=len(normalized),
                operations=ops,
            )
        )
        start = len(result)
        append(normalized)
        if normalized:
            spans.append(
                dict(candidate_id=id, start=start, end=len(result), span_hash=sha256(normalized).hexdigest())
            )
        return c

    def block(item):
        nonlocal grid_positions
        if item.kind == "text":
            c = source.get(item.candidate_id)
            if c is None or c.content_state != "present":
                fail()
            leaf(item.candidate_id, {"heading", "paragraph", "list_item"}, body=True)
            return
        if item.table_local_id in local_ids:
            fail()
        local_ids.add(item.table_local_id)
        grid_positions += item.rows * item.columns
        if grid_positions > 10000:
            fail()
        positions, origins = {}, {}
        table_candidates = set()
        for cell in item.cells:
            if len(set(cell.candidates)) != len(cell.candidates):
                fail()
            first = source.get(cell.candidates[0])
            if first is None or first.cell is None or first.cell.table_local_id != item.table_local_id:
                fail()
            shape = first.cell
            if shape.row + shape.row_span > item.rows or shape.column + shape.column_span > item.columns:
                fail()
            for id in cell.candidates:
                c = source.get(id)
                if (
                    c is None
                    or c.cell != shape
                    or (len(cell.candidates) > 1 and c.content_state != "present")
                ):
                    fail()
                if id in table_candidates:
                    fail()
                table_candidates.add(id)
            origins[shape.row, shape.column] = cell
            for row in range(shape.row, shape.row + shape.row_span):
                for col in range(shape.column, shape.column + shape.column_span):
                    if (row, col) in positions:
                        fail()
                    positions[row, col] = "covered"
        for absent in item.unavailable:
            if (
                absent.row >= item.rows
                or absent.column >= item.columns
                or (absent.row, absent.column) in positions
                or not absent.reason.strip()
            ):
                fail()
            positions[absent.row, absent.column] = "unavailable"
        if (
            len(positions) != item.rows * item.columns
            or len(set(item.headers)) != len(item.headers)
            or not set(item.headers) <= table_candidates
        ):
            fail()
        headers.update(item.headers)
        partial = bool(item.unavailable)
        for row in range(item.rows):
            if row:
                separator("row")
            for col in range(item.columns):
                if col:
                    separator("column")
                cell = origins.get((row, col))
                if cell is None:
                    continue
                for index, id in enumerate(cell.candidates):
                    if index:
                        separator("cell")
                    c = leaf(id, {"table_cell"}, body=True)
                    partial |= c.content_state in {"illegible", "unsupported"}
        if partial:
            incomplete.append(item.table_local_id)

    def scope(scope_id, blocks, footnotes, refs):
        before = set(used)
        for index, item in enumerate(blocks):
            if index:
                separator("block")
            if item.kind == "notice":
                if scope_id is not None or item.local_id in local_ids:
                    fail()
                local_ids.add(item.local_id)
                start = len(result)
                scope(item.local_id, item.blocks, item.footnotes, item.markers)
                notice_ranges.append(
                    dict(
                        local_id=item.local_id,
                        start=start,
                        end=len(result),
                        content_hash=sha256(result[start:]).hexdigest(),
                    )
                )
            else:
                block(item)
        direct = set(used) - before
        if scope_id is None:
            # Document-level markers cannot target leaves belonging to a notice.
            nested = {m["candidate_id"] for m in markers_out}
            notice_candidates = set()
            for value in notice_ranges:
                notice_candidates.update(
                    s["candidate_id"]
                    for s in spans
                    if value["start"] <= s["start"] and s["end"] <= value["end"]
                )
            direct -= notice_candidates | nested
        definitions = {f.local_id: f for f in footnotes}
        if len(definitions) != len(footnotes) or any(not f.candidates for f in footnotes):
            fail()
        if any(
            list(f.candidates) != sorted(f.candidates, key=lambda id: rank.get(id, -1)) for f in footnotes
        ):
            fail()
        reference_order, seen_markers, previous_ends = [], set(), {}
        for ref in sorted(refs, key=lambda r: (rank.get(r.candidate_id, -1), r.source_start)):
            key = (ref.candidate_id, ref.source_start, ref.source_end)
            if ref.candidate_id not in direct or ref.footnote_id not in definitions or key in seen_markers:
                fail()
            if ref.source_start < previous_ends.get(ref.candidate_id, 0):
                fail()
            previous_ends[ref.candidate_id] = ref.source_end
            seen_markers.add(key)
            c = source[ref.candidate_id]
            raw = c.text.encode()
            try:
                if not 0 <= ref.source_start < ref.source_end <= len(raw):
                    fail()
                prefix = normalize_literal(raw[: ref.source_start].decode()).encode()
                ending = normalize_literal(raw[: ref.source_end].decode()).encode()
                marker = normalize_literal(raw[ref.source_start : ref.source_end].decode()).encode()
            except UnicodeError:
                fail()
            full = normalize_literal(c.text).encode()
            if (
                not full.startswith(prefix)
                or not full.startswith(ending)
                or full[len(prefix) : len(ending)] != marker
                or not marker
            ):
                fail()
            span = next(s for s in spans if s["candidate_id"] == ref.candidate_id)
            markers_out.append(
                dict(
                    candidate_id=ref.candidate_id,
                    source_start=ref.source_start,
                    source_end=ref.source_end,
                    start=span["start"] + len(prefix),
                    end=span["start"] + len(ending),
                    marker_hash=sha256(marker).hexdigest(),
                    footnote_id=ref.footnote_id,
                    scope=scope_id,
                )
            )
            if ref.footnote_id not in reference_order:
                reference_order.append(ref.footnote_id)
        orphans = sorted(
            (f.local_id for f in footnotes if f.local_id not in reference_order),
            key=lambda id: rank.get(definitions[id].candidates[0], -1),
        )
        for index, id in enumerate(reference_order + orphans):
            if blocks or index:
                separator("block")
            for child, cid in enumerate(definitions[id].candidates):
                if source.get(cid) is None or source[cid].content_state != "present":
                    fail()
                if child:
                    separator("block")
                leaf(cid, {"footnote"})

    scope(None, plan.blocks, plan.footnotes, plan.markers)
    emitted = list(used)
    for exclude in plan.exclusions:
        c = candidate(exclude.candidate_id, {"heading", "paragraph", "list_item", "table_cell", "footnote"})
        if not exclude.reason.strip():
            fail()
        if exclude.kind == "repeated_header":
            target = source.get(exclude.target_candidate_id)
            if (
                target is None
                or target.id not in headers
                or target.id not in emitted
                or c.cell is None
                or target.cell is None
                or c.cell.table_local_id != target.cell.table_local_id
                or normalize_literal(c.text) != normalize_literal(target.text)
                or c.content_state != target.content_state
            ):
                fail()
        elif exclude.target_candidate_id is not None:
            fail()
    if (
        set(used) != set(order)
        or len(used) != len(order)
        or body_order != [id for id in order if id in body_order]
    ):
        fail()
    if plan.blocks or plan.footnotes:
        separator("terminator")
    details = ProjectionDetails.model_validate(
        dict(
            spans=spans,
            normalizations=normalizations,
            separators=offsets,
            markers=markers_out,
            notice_ranges=notice_ranges,
            incomplete_tables=incomplete,
            publication_eligible=False,
        )
    )
    if (
        len(
            json.dumps(
                {"plan": plan.model_dump(mode="json"), "projection": details.model_dump(mode="json")},
                ensure_ascii=False,
            ).encode()
        )
        > MAX_PAYLOAD_BYTES
    ):
        fail()
    return Projection(bytes(result), details)
