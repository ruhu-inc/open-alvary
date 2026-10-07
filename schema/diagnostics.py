"""Static structural diagnostics; never source words, IDs, credentials or SQL text."""

from typing import Literal

CANONICAL_REASONS = (
    "source_order_mismatch",
    "canonical_byte_limit",
    "candidate_reuse_or_type",
    "body_content_missing",
    "duplicate_local_id",
    "table_grid_limit",
    "duplicate_cell_candidate",
    "cell_shape_missing",
    "cell_out_of_bounds",
    "cell_shape_mismatch",
    "candidate_in_multiple_cells",
    "cell_overlap",
    "unavailable_cell_conflict",
    "table_coverage_or_headers",
    "nested_or_duplicate_notice",
    "footnote_definitions",
    "footnote_order",
    "marker_scope_or_target",
    "marker_overlap",
    "marker_bounds",
    "marker_utf8_boundary",
    "marker_normalization_boundary",
    "footnote_content_missing",
    "exclusion_reason_missing",
    "repeated_header_mismatch",
    "furniture_target_forbidden",
    "candidate_coverage_or_order",
    "canonical_metadata_limit",
    "serializer_mismatch",
    "database_structure_invalid",
)
CanonicalReason = Literal[*CANONICAL_REASONS]
