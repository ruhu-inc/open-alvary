"""Bounded private OA-text-1 serializer; source candidates remain unverified."""

import json
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from schema.assembly import CandidateInput
from schema.canonical import CanonicalPlan, ProjectionDetails
from schema.diagnostics import CANONICAL_REASONS, CanonicalReason

PROFILE_DESCRIPTOR = '{"id":"OA-text-1","scope":"original-synthetic","encoding":"UTF-8","normalization":"NFC","line_endings":"LF","whitespace":"literal","block_separator":"LF LF","table_column_separator":"TAB","table_row_separator":"LF","terminator":"one added LF","dehyphenation":"unsupported","alignment":"whole private candidate"}'
PROFILE_HASH = sha256(PROFILE_DESCRIPTOR.encode()).hexdigest()
MAX_CANONICAL_BYTES = 128 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024


class CanonicalValidationError(ValueError):
    def __init__(self, reason_code: CanonicalReason):
        if reason_code not in CANONICAL_REASONS:
            raise ValueError("Unknown canonical diagnostic")
        self.reason_code = reason_code
        super().__init__("Private canonical structure rejected: " + reason_code)


def normalize_literal(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


@dataclass(frozen=True)
class Projection:
    canonical_bytes: bytes
    details: ProjectionDetails


class _ProjectionCompiler:
    def __init__(self, plan, candidates, order):
        self.plan = plan
        self.candidates = candidates
        self.order = order
        self.source = {c.id: c for c in self.candidates}
        if (
            len(self.source) != len(self.candidates)
            or set(self.source) != set(self.order)
            or len(self.order) != len(self.source)
        ):
            self.fail("source_order_mismatch")
        self.rank = {id: i for i, id in enumerate(self.order)}
        self.result = bytearray()
        self.used, self.body_order, self.headers, self.local_ids = ([], [], set(), set())
        self.spans, self.normalizations, self.markers_out, self.notice_ranges, self.incomplete = (
            [],
            [],
            [],
            [],
            [],
        )
        self.offsets = {key: [] for key in ("block", "cell", "column", "row", "terminator")}
        self.grid_positions = 0

    def fail(self, reason_code):
        raise CanonicalValidationError(reason_code)

    def append(self, data: bytes):
        self.result.extend(data)
        if len(self.result) > MAX_CANONICAL_BYTES:
            self.fail("canonical_byte_limit")

    def separator(self, kind):
        self.offsets[kind].append(len(self.result))
        self.append(b"\n\n" if kind in {"block", "cell"} else b"\t" if kind == "column" else b"\n")

    def candidate(self, id, types):
        if id not in self.source or id in self.used or self.source[id].block_type not in types:
            self.fail("candidate_reuse_or_type")
        self.used.append(id)
        return self.source[id]

    def leaf(self, id, types, *, body=False):
        c = self.candidate(id, types)
        if body:
            self.body_order.append(id)
        literal = c.text.encode()
        lf = c.text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalize_literal(c.text).encode()
        ops = (["lf"] if lf != c.text else []) + (["nfc"] if normalize_literal(c.text) != lf else [])
        self.normalizations.append(
            dict(
                candidate_id=id,
                source_hash=sha256(literal).hexdigest(),
                source_bytes=len(literal),
                normalized_hash=sha256(normalized).hexdigest(),
                normalized_bytes=len(normalized),
                operations=ops,
            )
        )
        start = len(self.result)
        self.append(normalized)
        if normalized:
            self.spans.append(
                dict(
                    candidate_id=id,
                    start=start,
                    end=len(self.result),
                    span_hash=sha256(normalized).hexdigest(),
                )
            )
        return c

    def block(self, item):
        if item.kind == "text":
            c = self.source.get(item.candidate_id)
            if c is None or c.content_state != "present":
                self.fail("body_content_missing")
            self.leaf(item.candidate_id, {"heading", "paragraph", "list_item"}, body=True)
            return
        if item.table_local_id in self.local_ids:
            self.fail("duplicate_local_id")
        self.local_ids.add(item.table_local_id)
        self.grid_positions += item.rows * item.columns
        if self.grid_positions > 10000:
            self.fail("table_grid_limit")
        origins, partial = self.table_grid(item)
        for row in range(item.rows):
            if row:
                self.separator("row")
            for col in range(item.columns):
                if col:
                    self.separator("column")
                cell = origins.get((row, col))
                if cell is None:
                    continue
                for index, id in enumerate(cell.candidates):
                    if index:
                        self.separator("cell")
                    c = self.leaf(id, {"table_cell"}, body=True)
                    partial |= c.content_state in {"illegible", "unsupported"}
        if partial:
            self.incomplete.append(item.table_local_id)

    def scope(self, scope_id, blocks, footnotes, refs):
        before = set(self.used)
        for index, item in enumerate(blocks):
            if index:
                self.separator("block")
            if item.kind == "notice":
                if scope_id is not None or item.local_id in self.local_ids:
                    self.fail("nested_or_duplicate_notice")
                self.local_ids.add(item.local_id)
                start = len(self.result)
                self.scope(item.local_id, item.blocks, item.footnotes, item.markers)
                self.notice_ranges.append(
                    dict(
                        local_id=item.local_id,
                        start=start,
                        end=len(self.result),
                        content_hash=sha256(self.result[start:]).hexdigest(),
                    )
                )
            else:
                self.block(item)
        direct = set(self.used) - before
        if scope_id is None:
            nested = {m["candidate_id"] for m in self.markers_out}
            notice_candidates = set()
            for value in self.notice_ranges:
                notice_candidates.update(
                    s["candidate_id"]
                    for s in self.spans
                    if value["start"] <= s["start"] and s["end"] <= value["end"]
                )
            direct -= notice_candidates | nested
        self.emit_footnotes(scope_id, blocks, footnotes, refs, direct)

    def table_grid(self, item):
        positions, origins = ({}, {})
        table_candidates = set()
        for cell in item.cells:
            if len(set(cell.candidates)) != len(cell.candidates):
                self.fail("duplicate_cell_candidate")
            first = self.source.get(cell.candidates[0])
            if first is None or first.cell is None or first.cell.table_local_id != item.table_local_id:
                self.fail("cell_shape_missing")
            shape = first.cell
            if shape.row + shape.row_span > item.rows or shape.column + shape.column_span > item.columns:
                self.fail("cell_out_of_bounds")
            for id in cell.candidates:
                c = self.source.get(id)
                if (
                    c is None
                    or c.cell != shape
                    or (len(cell.candidates) > 1 and c.content_state != "present")
                ):
                    self.fail("cell_shape_mismatch")
                if id in table_candidates:
                    self.fail("candidate_in_multiple_cells")
                table_candidates.add(id)
            origins[shape.row, shape.column] = cell
            for row in range(shape.row, shape.row + shape.row_span):
                for col in range(shape.column, shape.column + shape.column_span):
                    if (row, col) in positions:
                        self.fail("cell_overlap")
                    positions[row, col] = "covered"
        for absent in item.unavailable:
            if (
                absent.row >= item.rows
                or absent.column >= item.columns
                or (absent.row, absent.column) in positions
                or (not absent.reason.strip())
            ):
                self.fail("unavailable_cell_conflict")
            positions[absent.row, absent.column] = "unavailable"
        if (
            len(positions) != item.rows * item.columns
            or len(set(item.headers)) != len(item.headers)
            or (not set(item.headers) <= table_candidates)
        ):
            self.fail("table_coverage_or_headers")
        self.headers.update(item.headers)
        partial = bool(item.unavailable)
        return (origins, partial)

    def emit_footnotes(self, scope_id, blocks, footnotes, refs, direct):
        definitions = {f.local_id: f for f in footnotes}
        if len(definitions) != len(footnotes) or any(not f.candidates for f in footnotes):
            self.fail("footnote_definitions")
        if any(
            list(f.candidates) != sorted(f.candidates, key=lambda id: self.rank.get(id, -1))
            for f in footnotes
        ):
            self.fail("footnote_order")
        reference_order = self.map_markers(scope_id, refs, direct, definitions)
        orphans = sorted(
            (f.local_id for f in footnotes if f.local_id not in reference_order),
            key=lambda id: self.rank.get(definitions[id].candidates[0], -1),
        )
        for index, id in enumerate(reference_order + orphans):
            if blocks or index:
                self.separator("block")
            for child, cid in enumerate(definitions[id].candidates):
                if self.source.get(cid) is None or self.source[cid].content_state != "present":
                    self.fail("footnote_content_missing")
                if child:
                    self.separator("block")
                self.leaf(cid, {"footnote"})

    def map_markers(self, scope_id, refs, direct, definitions):
        reference_order, seen_markers, previous_ends = ([], set(), {})
        for ref in sorted(refs, key=lambda r: (self.rank.get(r.candidate_id, -1), r.source_start)):
            key = (ref.candidate_id, ref.source_start, ref.source_end)
            if ref.candidate_id not in direct or ref.footnote_id not in definitions or key in seen_markers:
                self.fail("marker_scope_or_target")
            if ref.source_start < previous_ends.get(ref.candidate_id, 0):
                self.fail("marker_overlap")
            previous_ends[ref.candidate_id] = ref.source_end
            seen_markers.add(key)
            c = self.source[ref.candidate_id]
            raw = c.text.encode()
            try:
                if not 0 <= ref.source_start < ref.source_end <= len(raw):
                    self.fail("marker_bounds")
                prefix = normalize_literal(raw[: ref.source_start].decode()).encode()
                ending = normalize_literal(raw[: ref.source_end].decode()).encode()
                marker = normalize_literal(raw[ref.source_start : ref.source_end].decode()).encode()
            except UnicodeError:
                self.fail("marker_utf8_boundary")
            full = normalize_literal(c.text).encode()
            if (
                not full.startswith(prefix)
                or not full.startswith(ending)
                or full[len(prefix) : len(ending)] != marker
                or (not marker)
            ):
                self.fail("marker_normalization_boundary")
            span = next(s for s in self.spans if s["candidate_id"] == ref.candidate_id)
            self.markers_out.append(
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
        return reference_order

    def compile(self):
        self.scope(None, self.plan.blocks, self.plan.footnotes, self.plan.markers)
        emitted = list(self.used)
        for exclude in self.plan.exclusions:
            c = self.candidate(
                exclude.candidate_id, {"heading", "paragraph", "list_item", "table_cell", "footnote"}
            )
            if not exclude.reason.strip():
                self.fail("exclusion_reason_missing")
            if exclude.kind == "repeated_header":
                target = self.source.get(exclude.target_candidate_id)
                if (
                    target is None
                    or target.id not in self.headers
                    or target.id not in emitted
                    or (c.cell is None)
                    or (target.cell is None)
                    or (c.cell.table_local_id != target.cell.table_local_id)
                    or (normalize_literal(c.text) != normalize_literal(target.text))
                    or (c.content_state != target.content_state)
                ):
                    self.fail("repeated_header_mismatch")
            elif exclude.target_candidate_id is not None:
                self.fail("furniture_target_forbidden")
        if (
            set(self.used) != set(self.order)
            or len(self.used) != len(self.order)
            or self.body_order != [id for id in self.order if id in self.body_order]
        ):
            self.fail("candidate_coverage_or_order")
        if self.plan.blocks or self.plan.footnotes:
            self.separator("terminator")
        details = ProjectionDetails.model_validate(
            dict(
                spans=self.spans,
                normalizations=self.normalizations,
                separators=self.offsets,
                markers=self.markers_out,
                notice_ranges=self.notice_ranges,
                incomplete_tables=self.incomplete,
                publication_eligible=False,
            )
        )
        if (
            len(
                json.dumps(
                    {
                        "plan": self.plan.model_dump(mode="json"),
                        "projection": details.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                ).encode()
            )
            > MAX_PAYLOAD_BYTES
        ):
            self.fail("canonical_metadata_limit")
        return Projection(bytes(self.result), details)


def compile_projection(
    plan: CanonicalPlan, candidates: tuple[CandidateInput, ...], order: tuple[UUID, ...]
) -> Projection:
    return _ProjectionCompiler(plan, candidates, order).compile()
