"""Checks a recorded HTTP answer against REST v1's OpenAPI document (G13): its status is one the route
declares, its body has the declared media type and matches the declared schema, every header the
document requires is there, and every header the document names (ETag, Retry-After, X-Request-ID,
RateLimit, RateLimit-Policy) is one it declares for that status.

The schemas are JSON Schema 2020-12 as FastAPI and api.openapi_docs write them; this checks the keywords
they use, and fails on any other, so a schema it cannot read is never passed by default."""

import re
from datetime import date, datetime

# The response headers the document declares where they are sent; any of them on an answer whose status
# does not declare it is a difference between the document and the service.
NAMED_HEADERS = ("etag", "retry-after", "x-request-id", "ratelimit", "ratelimit-policy")
# Keywords that only describe; everything else must be checked below.
ANNOTATIONS = {"title", "description", "default", "examples", "deprecated", "readOnly", "writeOnly"}
CHECKED = {
    "$ref", "allOf", "anyOf", "oneOf", "const", "enum", "type", "properties", "required",
    "additionalProperties", "items", "prefixItems", "minItems", "maxItems", "minLength", "maxLength",
    "pattern", "format", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
}  # fmt: skip
TYPES = {
    "null": lambda v: v is None,
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def resolve(spec: dict, ref: str) -> dict:
    node = spec
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def schema_errors(spec: dict, schema, value, where: str = "$") -> list[str]:
    """Every way ``value`` differs from ``schema``; empty when it matches."""
    if schema is True or schema == {}:
        return []
    if schema is False:
        return [f"{where}: nothing is allowed here"]
    unknown = set(schema) - CHECKED - ANNOTATIONS
    if unknown:
        return [f"{where}: the check does not read {sorted(unknown)}"]
    out: list[str] = []
    if "$ref" in schema:
        out += schema_errors(spec, resolve(spec, schema["$ref"]), value, where)
    for part in schema.get("allOf", ()):
        out += schema_errors(spec, part, value, where)
    if "anyOf" in schema and not any(not schema_errors(spec, s, value, where) for s in schema["anyOf"]):
        out.append(f"{where}: matches none of anyOf ({_short(value)})")
    if "oneOf" in schema and sum(not schema_errors(spec, s, value, where) for s in schema["oneOf"]) != 1:
        out.append(f"{where}: does not match exactly one of oneOf ({_short(value)})")
    if "const" in schema and value != schema["const"]:
        out.append(f"{where}: {_short(value)} is not {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where}: {_short(value)} is not one of {schema['enum']}")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(TYPES[t](value) for t in types):
            return out + [f"{where}: {_short(value)} is not {' or '.join(types)}"]
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        out += [f"{where}: {name} is missing" for name in schema.get("required", ()) if name not in value]
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                out += schema_errors(spec, properties[name], item, f"{where}.{name}")
            elif extra is False:
                out.append(f"{where}: {name} is not in the document")
            elif isinstance(extra, dict):
                out += schema_errors(spec, extra, item, f"{where}.{name}")
    if isinstance(value, list):
        prefix = schema.get("prefixItems", [])
        for index, item in enumerate(value):
            part = prefix[index] if index < len(prefix) else schema.get("items")
            if part is not None:
                out += schema_errors(spec, part, item, f"{where}[{index}]")
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            out.append(f"{where}: {len(value)} items is out of bounds")
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
            out.append(f"{where}: {len(value)} characters is out of bounds")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{where}: {_short(value)} does not match {schema['pattern']}")
        out += _format(schema.get("format"), value, where)
    if TYPES["number"](value):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            out.append(f"{where}: {value} is out of bounds")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            out.append(f"{where}: {value} is out of bounds")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            out.append(f"{where}: {value} is out of bounds")
    return out


def _format(name, value: str, where: str) -> list[str]:
    try:
        if name == "date-time":
            if datetime.fromisoformat(value).tzinfo is None:
                return [f"{where}: {value} has no time zone"]
        elif name == "date":
            date.fromisoformat(value)
    except ValueError:
        return [f"{where}: {value} is not a {name}"]
    return []


def _short(value) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def operation(spec: dict, path: str, method: str = "get") -> dict:
    return spec["paths"][path][method]


def response_errors(spec: dict, path: str, response, method: str = "get") -> list[str]:
    """Every way a recorded answer of ``path`` (a path as the document writes it, such as
    ``/sources/{source_id}``) differs from what the document declares for it."""
    where = f"{method.upper()} {path} {response.status_code}"
    declared = operation(spec, path, method)["responses"].get(str(response.status_code))
    if declared is None:
        return [f"{where}: the status is not declared"]
    out = []
    content = declared.get("content")
    if not content:
        if response.content:
            out.append(f"{where}: declares no body, but one was sent")
    else:
        media = response.headers.get("content-type", "").split(";")[0].strip()
        if media in content:
            schema = content[media]["schema"]
            if media.endswith("json"):
                out += [f"{where}: {e}" for e in schema_errors(spec, schema, response.json())]
            elif schema.get("type") != "string":
                out.append(f"{where}: {media} is declared as {schema}, not as text or bytes")
        elif not any(c["schema"].get("format") == "binary" for c in content.values()):
            # A binary download is declared as bytes of any type; anything else must be the type named.
            out.append(f"{where}: {media} is not one of {sorted(content)}")
    headers = {name.lower(): spec_header(spec, value) for name, value in declared.get("headers", {}).items()}
    for name, header in headers.items():
        if header.get("required") and name not in response.headers:
            out.append(f"{where}: the required header {name} is missing")
        if name in response.headers and header["schema"].get("type") == "integer":
            if not response.headers[name].isdigit():
                out.append(f"{where}: {name} is not a whole number of seconds")
    for name in NAMED_HEADERS:
        if name in response.headers and name not in headers:
            out.append(f"{where}: sends {name}, which the document does not declare for it")
    return out


def spec_header(spec: dict, value: dict) -> dict:
    return resolve(spec, value["$ref"]) if "$ref" in value else value
