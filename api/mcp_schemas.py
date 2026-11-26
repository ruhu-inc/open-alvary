"""JSON Schema 2020-12 outputSchema definitions for all 13 MCP tools."""

from typing import Any

from pydantic import BaseModel

from schema.public import (
    AnchorView,
    CheckClaimResponse,
    CheckPage,
    CitationView,
    CoverageView,
    EvidencePack,
    ExpressionPage,
    SearchPage,
    SourceRelationships,
    SourceView,
    TextSearchPage,
)

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


def _allow_additional(node: Any) -> Any:
    """Replace only ``"additionalProperties": False`` with ``True`` (agent-access.md:29),
    preserving typed dictionary schemas (dict-valued ``additionalProperties``)."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "additionalProperties":
                if value is False:
                    out[key] = True
                elif isinstance(value, dict):
                    out[key] = _allow_additional(value)
                else:
                    out[key] = value
            elif key in ("$defs", "properties") and isinstance(value, dict):
                out[key] = {sub_key: _allow_additional(sub_val) for sub_key, sub_val in value.items()}
            elif key == "items":
                out[key] = _allow_additional(value)
            elif key in ("prefixItems", "anyOf", "oneOf", "allOf") and isinstance(value, list):
                out[key] = [_allow_additional(item) for item in value]
            else:
                out[key] = value
        return out
    if isinstance(node, list):
        return [_allow_additional(item) for item in node]
    return node


def _pydantic_output_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = _allow_additional(model.model_json_schema(mode="serialization"))
    return {"$schema": DRAFT_2020_12, **raw}


_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}
_NULLABLE_INT: dict[str, Any] = {"type": ["integer", "null"]}
_NULLABLE_OBJECT: dict[str, Any] = {"type": ["object", "null"], "additionalProperties": True}
_STRING_ARRAY: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

_CHANGE_EVENT_PROPERTIES: dict[str, Any] = {
    "schema_version": {"type": "string"},
    "event_id": {"type": "string"},
    "event_revision": {"type": "integer"},
    "event_type": {"type": "string"},
    "feed_epoch": {"type": "string"},
    "publication_revision": {"type": "integer"},
    "committed_at": {"type": "string"},
    "observed_at": _NULLABLE_STRING,
    "publisher_published_at": _NULLABLE_STRING,
    "legal_effective": _NULLABLE_OBJECT,
    "jurisdiction": _NULLABLE_OBJECT,
    "work": _NULLABLE_OBJECT,
    "version": _NULLABLE_OBJECT,
    "rights_revision": _NULLABLE_INT,
    "changed_fields": _STRING_ARRAY,
    "change_reason": _NULLABLE_STRING,
    "supersedes": _NULLABLE_OBJECT,
    "summary": _NULLABLE_STRING,
    "sources": _STRING_ARRAY,
    "withdrawal": _NULLABLE_OBJECT,
    "legal_effect": _NULLABLE_OBJECT,
    "treatment": _NULLABLE_OBJECT,
    "membership": _NULLABLE_OBJECT,
    "suspension": _NULLABLE_OBJECT,
    "previous": {"type": "object", "additionalProperties": True},
    "version_diff": _NULLABLE_OBJECT,
    "rights": _NULLABLE_OBJECT,
}

_CHANGE_EVENT_BODY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": list(_CHANGE_EVENT_PROPERTIES),
    "properties": _CHANGE_EVENT_PROPERTIES,
}

CHANGE_EVENT_SCHEMA: dict[str, Any] = {
    "$schema": DRAFT_2020_12,
    **_CHANGE_EVENT_BODY,
}

LIST_CHANGES_SCHEMA: dict[str, Any] = {
    "$schema": DRAFT_2020_12,
    "type": "object",
    "additionalProperties": True,
    "required": ["events", "has_more", "feed_epoch", "resume_token"],
    "properties": {
        "events": {"type": "array", "items": _CHANGE_EVENT_BODY},
        "has_more": {"type": "boolean"},
        "feed_epoch": {"type": "string"},
        "resume_token": {"type": "string"},
        "next_cursor": {"type": "string"},
    },
}

OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "check_claim": _pydantic_output_schema(CheckClaimResponse),
    "check_currentness": _pydantic_output_schema(CheckPage),
    "get_anchor": _pydantic_output_schema(AnchorView),
    "get_change": CHANGE_EVENT_SCHEMA,
    "get_coverage": _pydantic_output_schema(CoverageView),
    "get_relationships": _pydantic_output_schema(SourceRelationships),
    "get_source": _pydantic_output_schema(SourceView),
    "list_changes": LIST_CHANGES_SCHEMA,
    "list_expressions": _pydantic_output_schema(ExpressionPage),
    "resolve_citation": _pydantic_output_schema(CitationView),
    "retrieve_evidence": _pydantic_output_schema(EvidencePack),
    "search_catalogue": _pydantic_output_schema(SearchPage),
    "search_text": _pydantic_output_schema(TextSearchPage),
}
