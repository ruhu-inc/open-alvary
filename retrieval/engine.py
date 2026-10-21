"""The dedicated search engine (RSV-04): Vespa holds every searchable unit and does lexical, dense and
filtered search in one place; PostgreSQL stays the system of record and the engine is rebuilt from it
(retrieval.projector follows the change log).

One document per section-index unit (``unit``): its identity and filters as attributes, its words,
headings, work title and identifiers (citation, pinpoint and names) as BM25 fields, analysed as the
section index analyses them (``locale``: stemmed in the same languages, word for word in the others),
and one multi-vector field per embedding profile, ``emb_<profile>``,
holding a vector for each window of the unit (tensor<bfloat16>(w{}, x[dims]) with an HNSW index:
compressed, and searched by its best window). A new embedding generation is a new field, filled by
partial updates and validated before queries use it: blue/green inside the engine.

Rank profiles are written here and documented in each pack's ``ranking.features``:
  lexical     1.0·bm25(headings) + 0.4·bm25(words) + 0.2·bm25(title) + 0.1·bm25(identifiers),
              with b = 0.3 for words and headings
  dense_<p>   closeness(field, emb_<p>)
Every query carries the hard filters (jurisdiction, language, type, status, year, the date's
effective interval) in its YQL, so filtering happens before ranking. The engine only ever holds units
of texts anyone may read now; every hit is still rechecked against PostgreSQL before its words are
served.

Sharding: units are distributed over the content cluster's nodes by bucket, and every query filters
on ``jurisdiction_id`` (a fast-search attribute). When a jurisdiction's size calls for it, it moves to
its own content cluster (``CLUSTERS``), so a query for one country touches only its nodes.
"""

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

LEXICAL = "1.0 * bm25(headings) + 0.4 * bm25(words) + 0.2 * bm25(title) + 0.1 * bm25(identifiers)"
FIELDS = ("headings", "words", "title", "identifiers")
# BM25's length normalisation (b, default 0.75) is weakened for words and headings: a part's own unit
# is its heading alone, and at 0.75 it outranks the sections under it. Measured on the NDPA development
# set through the whole pack (nDCG@10 0.746 to 0.783; experiments/embeddings/screen-2026-10-19.md).
LENGTH_NORMALISATION = {"words": 0.3, "headings": 0.3}
# The language the engine's linguistics (OpenNLP) split and stem each analysis in: French, Portuguese,
# Spanish and Arabic with Snowball's stemmers, as PostgreSQL's configurations of these names; English
# with KStem, PostgreSQL's with Snowball's (checked on Vespa 8.763.13).
LOCALES = {"english": "en", "french": "fr", "portuguese": "pt", "spanish": "es", "arabic": "ar"}
# simple: a language the engine knows and has no stemmer for (Kiswahili's), so that a unit in any
# language and the query are split alike and never stemmed. An unknown tag would be guessed at.
WORD_FOR_WORD = "sw"
TIMEOUT_SECONDS = 10
EPOCH = date(1970, 1, 1)
UNKNOWN_DAY = -1


def locale(config: str) -> str:
    """The language the engine analyses a config's units and query words in."""
    return LOCALES.get(config, WORD_FOR_WORD)


def schema_field(name: str, kind: str, indexing: str, *settings: str | None) -> str:
    body = "".join(f"\n            {line}" for line in (f"indexing: {indexing}", *settings) if line)
    return f"        field {name} type {kind} {{{body}\n        }}"


def slug(profile_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", profile_id.casefold()).strip("_")[:60]


def days(value) -> int:
    return (value - EPOCH).days if value else UNKNOWN_DAY


@dataclass
class Application:
    """The Vespa application: the unit schema with one embedding field per profile (id → dims)."""

    profiles: dict[str, int] = field(default_factory=dict)  # vector field (emb_…) → dimensions
    clusters: dict[str, list[str]] = field(default_factory=dict)  # reserved: jurisdiction groups

    def schema(self) -> str:
        attributes = [
            ("unit_id", "string"), ("version_id", "string"), ("work_id", "string"), ("node_id", "string"),
            ("unit_kind", "string"), ("kind", "string"), ("locator", "string"), ("pinpoint", "string"),
            ("heading", "string"), ("language", "string"), ("jurisdiction_id", "string"),
            ("document_type", "string"), ("legal_status", "string"), ("content_hash", "string"),
            ("config", "string"), ("analyzer", "string"), ("start_byte", "int"), ("end_byte", "int"),
            ("year", "int"), ("effective_from", "long"), ("effective_until", "long"), ("replaced", "bool"),
        ]  # fmt: skip
        fast = {
            "version_id",
            "work_id",
            "language",
            "jurisdiction_id",
            "document_type",
            "legal_status",
            "unit_kind",
            "kind",
        }
        lines = ["schema unit {", "    document unit {"]
        # First, so the text fields after it are analysed in their analysis's language (not the text's
        # own: a language the section index matches word for word is never stemmed here either).
        lines.append(schema_field("locale", "string", "set_language | summary | attribute"))
        for name, kind in attributes:
            lines.append(
                schema_field(
                    name, kind, "summary | attribute", "attribute: fast-search" if name in fast else None
                )
            )
        for name in FIELDS:
            # One analysis for every field of the default fieldset: mixing word matching with stemming
            # would make the fieldset match without stemming.
            lines.append(
                schema_field(name, "string", "index | summary", "index: enable-bm25", "stemming: best")
            )
        for vectors, dims in sorted(self.profiles.items()):
            lines.append(
                f"        field {vectors} type tensor<bfloat16>(w{{}}, x[{dims}]) {{\n"
                "            indexing: attribute | index\n"
                "            attribute { distance-metric: angular }\n"
                "            index { hnsw { max-links-per-node: 16  neighbors-to-explore-at-insert: 200 } }\n"
                "        }"
            )
        lines.append("    }")
        lines.append(f"    fieldset default {{ fields: {', '.join(FIELDS)} }}")
        features = " ".join(f"bm25({name})" for name in FIELDS)
        properties = "".join(f"            bm25({name}).b: {b}\n" for name, b in LENGTH_NORMALISATION.items())
        lines.append(
            "    rank-profile lexical {\n"
            f"        first-phase {{ expression: {LEXICAL} }}\n"
            f"        match-features {{ {features} }}\n"
            f"        rank-properties {{\n{properties}        }}\n"
            "    }"
        )
        for vectors, dims in sorted(self.profiles.items()):
            lines.append(
                f"    rank-profile dense_{vectors} {{\n"
                f"        inputs {{ query(q) tensor<float>(x[{dims}]) }}\n"
                f"        first-phase {{ expression: closeness(field, {vectors}) }}\n"
                f"        match-features {{ closeness(field, {vectors}) }}\n"
                "    }"
            )
        lines.append("}")
        return "\n".join(lines) + "\n"

    def services(self) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<services version="1.0">\n'
            '  <container id="default" version="1.0"><search/><document-api/></container>\n'
            '  <content id="units" version="1.0">\n'
            "    <min-redundancy>1</min-redundancy>\n"
            '    <documents><document type="unit" mode="index"/></documents>\n'
            '    <nodes><node hostalias="node1" distribution-key="0"/></nodes>\n'
            "  </content>\n"
            "</services>\n"
        )

    def overrides(self, today: date | None = None) -> str:
        """Retired generations' vector fields are removed from the schema: Vespa asks for that to be
        allowed explicitly, for at most 30 days ahead."""
        until = ((today or date.today()) + timedelta(days=28)).isoformat()
        allowed = ("schema-removal", "indexing-change", "field-type-change", "content-type-removal")
        lines = "".join(f'  <allow until="{until}">{name}</allow>\n' for name in allowed)
        return f"<validation-overrides>\n{lines}</validation-overrides>\n"

    def package(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("services.xml", self.services())
            archive.writestr("schemas/unit.sd", self.schema())
            archive.writestr("validation-overrides.xml", self.overrides())
        return buffer.getvalue()


class Engine:
    """Vespa over HTTP: deploy the application, feed and delete units, search."""

    def __init__(self, query_url: str, config_url: str | None = None, transport=None):
        self.query = httpx.Client(
            base_url=query_url.rstrip("/"), timeout=TIMEOUT_SECONDS, transport=transport
        )
        self.config = (
            httpx.Client(base_url=config_url.rstrip("/"), timeout=120, transport=transport)
            if config_url
            else None
        )

    # --- the application ------------------------------------------------------------------------

    def deploy(self, application: Application) -> dict:
        if self.config is None:
            raise RuntimeError("Deploying needs the config server's URL")
        answer = self.config.post(
            "/application/v2/tenant/default/prepareandactivate",
            content=application.package(),
            headers={"Content-Type": "application/zip"},
        )
        answer.raise_for_status()
        self._converge()
        return answer.json()

    def _converge(self, seconds: float = 180) -> None:
        """Wait until every service runs the application just activated (activation is asynchronous)."""
        import time

        url = "/application/v2/tenant/default/application/default/environment/prod/region/default/instance/default/serviceconverge"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if self.config.get(url).json().get("converged"):
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(1)
        raise TimeoutError("The engine did not converge on the new application")

    def ready(self) -> bool:
        try:
            return self.query.get("/state/v1/health").json().get("status", {}).get("code") == "up"
        except (httpx.HTTPError, ValueError):
            return False

    # --- documents ------------------------------------------------------------------------------

    @staticmethod
    def doc_id(version_id: str, start_byte: int) -> str:
        return f"{version_id}:{start_byte}"

    def put(self, unit: dict) -> None:
        identifier = unit["unit_id"]  # version:start for a unit, version:document for a document unit
        answer = self.query.post(f"/document/v1/oa/unit/docid/{identifier}", json={"fields": unit})
        answer.raise_for_status()

    def reanalyse(self, unit: dict) -> None:
        """Assign every field of a unit again (a new analyzer), so its words are analysed anew while its
        vectors are kept; a unit the engine lacks is created."""
        identifier = unit["unit_id"]
        answer = self.query.put(
            f"/document/v1/oa/unit/docid/{identifier}",
            json={"fields": {name: {"assign": value} for name, value in unit.items()}},
            params={"create": "true"},
        )
        answer.raise_for_status()

    def update_embedding(
        self, version_id: str, start_byte: int, vectors: str, windows: dict[str, list[float]]
    ) -> bool:
        """Set a unit's vectors in a vector field (window label → vector); False when the unit is not in the
        engine (yet, or any more)."""
        identifier = self.doc_id(version_id, start_byte)
        cells = {"blocks": {label: vector for label, vector in windows.items()}}
        answer = self.query.put(
            f"/document/v1/oa/unit/docid/{identifier}",
            json={"fields": {vectors: {"assign": cells}}},
            params={"create": "false"},
        )
        if answer.status_code == 412 or answer.status_code == 404:
            return False
        answer.raise_for_status()
        return True

    def delete_version(self, version_id: str) -> int:
        """Remove every unit of a text (selection by version)."""
        removed, token = 0, None
        while True:
            params = {"selection": f'unit.version_id=="{version_id}"', "cluster": "units"}
            if token:
                params["continuation"] = token
            answer = self.query.delete("/document/v1/oa/unit/docid", params=params)
            answer.raise_for_status()
            body = answer.json()
            removed += body.get("documentCount", 0)
            token = body.get("continuation")
            if not token:
                return removed

    def get(self, identifier: str, fields: str) -> dict | None:
        answer = self.query.get(
            f"/document/v1/oa/unit/docid/{identifier}",
            params={"fieldSet": f"unit:{fields}", "format.tensors": "short-value"},
        )
        if answer.status_code == 404:
            return None
        answer.raise_for_status()
        return answer.json().get("fields", {})

    def nearest(self, vector: list[float], vectors: str, limit: int, *, approximate: bool) -> list[str]:
        """The nearest units' IDs, by the index or exactly: to measure the index's recall."""
        annotation = f"targetHits: {limit}" + ("" if approximate else ", approximate: false")
        yql = f"select unit_id from unit where ({{{annotation}}}nearestNeighbor({vectors}, q))"
        hits = self._search(yql, {"ranking": f"dense_{vectors}", "input.query(q)": vector}, limit)
        return [h["unit_id"] for h in hits]

    def visit(self, selection: str | None = None, fields: str = "version_id,start_byte") -> list[dict]:
        found, token = [], None
        while True:
            params = {"cluster": "units", "fieldSet": f"unit:{fields}", "wantedDocumentCount": 1000}
            if selection:
                params["selection"] = selection
            if token:
                params["continuation"] = token
            answer = self.query.get("/document/v1/oa/unit/docid", params=params)
            answer.raise_for_status()
            body = answer.json()
            found += [d["fields"] for d in body.get("documents", [])]
            token = body.get("continuation")
            if not token:
                return found

    # --- search ---------------------------------------------------------------------------------

    @staticmethod
    def where(filters) -> str:
        """The hard filters as YQL, over the same fields as the section index's Filters."""

        def quoted(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

        clauses = []
        if filters.jurisdictions is not None:
            # An applicable-law scope's admitted works join the jurisdictions (api.applicable).
            either = [f"jurisdiction_id contains {quoted(j)}" for j in filters.jurisdictions]
            either += [f"work_id contains {quoted(w)}" for w in filters.admitted]
            clauses.append("(" + " or ".join(either) + ")" if either else "false")
        if filters.language:
            clauses.append(f"language contains {quoted(filters.language.split('-')[0].casefold())}")
        if filters.types:
            clauses.append(
                "(" + " or ".join(f"document_type contains {quoted(t)}" for t in filters.types) + ")"
            )
        if filters.statuses:
            clauses.append(
                "(" + " or ".join(f"legal_status contains {quoted(s)}" for s in filters.statuses) + ")"
            )
        if filters.kinds:
            clauses.append("(" + " or ".join(f"kind contains {quoted(k)}" for k in filters.kinds) + ")")
        if filters.year_from is not None:
            clauses.append(f"year >= {int(filters.year_from)}")
        if filters.year_to is not None:
            clauses.append(f"year <= {int(filters.year_to)}")
        if filters.source_id:
            clauses.append(f"work_id contains {quoted(filters.source_id)}")
        if filters.version_id:
            clauses.append(f"version_id contains {quoted(filters.version_id)}")
        if filters.as_of:
            day = days(filters.as_of)
            clauses.append(
                f"(effective_from > {UNKNOWN_DAY} and effective_from <= {day}"
                f" and (effective_until = {UNKNOWN_DAY} or effective_until > {day}))"
            )
        return " and ".join(clauses)

    def _search(self, yql: str, body: dict, limit: int) -> "Hits":
        answer = self.query.post(
            "/search/", json={"yql": yql, "hits": limit, "timeout": f"{TIMEOUT_SECONDS}s", **body}
        )
        answer.raise_for_status()
        root = answer.json().get("root", {})
        found = Hits(
            {
                **hit["fields"],
                "score": hit.get("relevance"),
                "features": hit["fields"].get("matchfeatures", {}),
            }
            for hit in root.get("children", [])
            if "fields" in hit
        )
        found.total = int(root.get("fields", {}).get("totalCount", len(found)))
        return found

    def lexical(
        self, terms: dict[str, str], filters, limit: int, unit_kind: str = "unit", *, every: bool = True
    ) -> "Hits":
        """Units (or document units) matching the query, by the lexical profile. ``terms`` holds, for each
        analysis (the section index's configs), the query's words that analysis indexes (its stopwords
        removed: api.text_index.query_terms); a unit matches through its own analysis, as in PostgreSQL,
        its words analysed in that analysis's language (never guessed from the query). With ``every``, a
        unit must hold all of them; otherwise any (weakAnd, rarer words weighing more by BM25), for a
        question written as a sentence."""
        branches, params = [], {}
        for n, (config, words) in enumerate(sorted(terms.items())):
            if not words.strip():
                continue
            annotations = ['grammar: "all"'] if every else ['grammar: "weakAnd"', "targetHits: 1000"]
            annotations.append(f'language: "{locale(config)}"')
            branches.append(
                f'(config contains "{config}" and ({{{", ".join(annotations)}}}userInput(@q{n})))'
            )
            params[f"q{n}"] = words
        if not branches:
            return Hits()
        where = " and ".join(filter(None, (self.where(filters), f'unit_kind contains "{unit_kind}"')))
        yql = f"select * from unit where ({' or '.join(branches)})" + (f" and {where}" if where else "")
        return self._search(yql, params | {"ranking": "lexical"}, limit)

    def dense(self, vector: list[float], vectors: str, filters, limit: int) -> list[dict]:
        """Units whose best window is nearest the query vector, within the filters (HNSW, filtered)."""
        where = " and ".join(filter(None, (self.where(filters), 'unit_kind contains "unit"')))
        yql = f"select * from unit where ({{targetHits: {max(limit, 100)}}}nearestNeighbor({vectors}, q))" + (
            f" and {where}" if where else ""
        )
        return self._search(yql, {"ranking": f"dense_{vectors}", "input.query(q)": vector}, limit)


class Hits(list):
    """Hits, best first, and how many matched (Vespa's totalCount)."""

    total: int = 0


def document(row: dict, words: str, headings: str, title: str, identifiers: str) -> dict:
    """A section-index unit as an engine document; ``analyzer`` is the section index's analysis it was
    fed under."""
    return {
        "unit_id": f"{row['version_id']}:{row['start_byte']}",
        "version_id": row["version_id"], "work_id": row["work_id"], "node_id": row.get("node_id") or "",
        "unit_kind": row.get("unit_kind", "unit"), "kind": row.get("kind") or "document",
        "locator": row.get("locator") or "document", "pinpoint": row.get("pinpoint") or "",
        "heading": row.get("heading") or "", "language": (row.get("language") or "und").split("-")[0].casefold(),
        "locale": locale(row["config"]), "jurisdiction_id": row["jurisdiction_id"],
        "document_type": row["document_type"], "legal_status": row["legal_status"],
        "content_hash": row["content_hash"], "config": row["config"], "analyzer": row.get("analyzer") or "",
        "start_byte": row["start_byte"], "end_byte": row["end_byte"], "year": row.get("year") or 0,
        "effective_from": days(row.get("effective_from")), "effective_until": days(row.get("effective_until")),
        "replaced": bool(row.get("replaced")), "words": words, "headings": headings, "title": title,
        "identifiers": identifiers,
    }  # fmt: skip


def config_from_env() -> tuple[str, str | None] | None:
    import os

    url = os.environ.get("OPEN_ALVARY_ENGINE_URL")
    return (url, os.environ.get("OPEN_ALVARY_ENGINE_CONFIG_URL")) if url else None


__all__ = ["Application", "Engine", "document", "slug", "json"]
