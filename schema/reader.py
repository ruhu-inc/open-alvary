"""Reader projection contract (RDR-03, ``reader-1``).

A projection of one approved version's current representation for the Reading mode. Every
text value is an exact canonical substring addressed by UTF-8 byte offsets; the projection is
never a second text authority. Mode availability is an observation, not a permission: each
delivery still passes current policy and byte budgets.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from schema.approval import RepresentationID, VersionID
from schema.models import Hash

LimitationCode = Literal[
    "geometry_unavailable",
    "original_not_eligible",
    "synthetic_fixture",
    "cell_unavailable",
]


class ReaderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModeAvailability(ReaderModel):
    reading: Literal["available"] = "available"
    page_layout: Literal["available", "unavailable"]
    original: Literal["available", "unavailable"]


class FootnoteReference(ReaderModel):
    """A marker inside a node's text, as UTF-8 byte offsets relative to that node."""

    start: int = Field(ge=0)
    end: int = Field(gt=0)
    footnote_node_id: str


class ReaderCell(ReaderModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(ge=1)
    column_span: int = Field(ge=1)
    node_id: str
    header: bool
    state: Literal["present", "empty", "unavailable"]


class ReaderNode(ReaderModel):
    node_id: str
    anchor_id: str | None
    parent_id: str | None
    kind: Literal["notice", "paragraph", "table", "table_cell", "footnote_group", "footnote"]
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)
    text: str | None = None
    state: Literal["present", "empty", "unavailable"]
    references: tuple[FootnoteReference, ...] = ()
    rows: int | None = None
    columns: int | None = None
    cells: tuple[ReaderCell, ...] = ()


class ReaderDescriptor(ReaderModel):
    schema_version: Literal["reader-1"] = "reader-1"
    version_id: VersionID
    content_hash: Hash
    representation_id: RepresentationID
    representation_revision: int = Field(ge=1)
    serialization_profile_id: Literal["OA-text-1"]
    modes: ModeAvailability
    limitations: tuple[LimitationCode, ...]
    nodes: tuple[ReaderNode, ...] = Field(max_length=200)
