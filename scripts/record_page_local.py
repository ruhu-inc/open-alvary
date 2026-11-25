"""The public site on this computer with two published texts, for trying the record page and for its
browser tests.

It serves the built website (``web/dist``; build it first with ``npm --prefix web run build``) and the
website's own data path (``/site/v1``, the real one) over a small database in memory that holds:

- ``rw-law-000-2099``: an invented Rwandan law, not legislation, in Kinyarwanda, English and French. Its
  structure is made by the structure rules that Rwandan laws are published with (``rw-law-1``), from
  units as a reviewer leaves them: a printed table of contents, two chapters, a division and four
  articles in each language.
- ``ng-foi-act-2011``: the catalogue's record with one invented text and no structure, as texts were
  published before they had any.

Nothing here reads a secret or calls another machine.

    .venv/bin/python -m scripts.record_page_local --port 8774

Then open http://127.0.0.1:8774/sources/rw-law-000-2099 .
"""

from __future__ import annotations

import argparse
import tempfile
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from sqlalchemy import create_engine
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

from api.site import create_site_app
from api.store import Store, metadata
from schema.models import (
    Authority,
    Corpus,
    Jurisdiction,
    Language,
    RightsRecord,
    Source,
    SourceVersion,
    StructureNode,
)
from workbench.serialize import serialize
from workbench.structure import RW_LAW, derive

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "web" / "dist"
LAW_ID = "rw-law-000-2099"
PLAIN_ID = "ng-foi-act-2011"
OFFICIAL = "https://example.org/local-fixture"

# One language column each, as a reviewer leaves its units: ("h", …) a heading, ("p", …) a paragraph.
# The lines of the printed table of contents are paragraphs, the body's headings are headings.
LAW = {
    "rw": [
        ("h", "ITEGEKO N° 000/2099 RYO KU WA 01/01/2099 RIGENGA IGITABO CY’IGERAGEZA"),
        ("p", "Iyi nyandiko ni iy’igerageza ry’urubuga. Si itegeko."),
        ("h", "ISHAKIRO"),
        ("p", "UMUTWE WA MBERE: INGINGO RUSANGE"),
        ("p", "Ingingo ya mbere: Icyo iri tegeko rigamije"),
        ("p", "Ingingo ya 2: Ibisobanuro by’amagambo"),
        ("p", "UMUTWE WA II: IGITABO"),
        ("p", "Icyiciro cya mbere: Gufata neza igitabo"),
        ("p", "Ingingo ya 3: Ibyandikwa mu gitabo"),
        ("p", "Ingingo ya 4: Igihe iri tegeko ritangira gukurikizwa"),
        ("h", "UMUTWE WA MBERE: INGINGO RUSANGE"),
        ("h", "Ingingo ya mbere: Icyo iri tegeko rigamije"),
        ("p", "Iri tegeko rigenga igitabo cy’igerageza."),
        ("h", "Ingingo ya 2: Ibisobanuro by’amagambo"),
        ("p", "Muri iri tegeko, amagambo akurikira asobanurwa ku buryo bukurikira:"),
        ("p", "1° «igitabo»: igitabo cy’igerageza;"),
        ("p", "2° «icyanditswe»: umurongo w’igitabo."),
        ("h", "UMUTWE WA II: IGITABO"),
        ("h", "Icyiciro cya mbere: Gufata neza igitabo"),
        ("h", "Ingingo ya 3: Ibyandikwa mu gitabo"),
        ("p", "Icyanditswe cyose gishyirwa mu gitabo ku munsi cyakiriweho."),
        ("p", "Igitabo kibikwa ku buryo buri wese ashobora kugisoma."),
        ("h", "Ingingo ya 4: Igihe iri tegeko ritangira gukurikizwa"),
        ("p", "Iri tegeko ritangira gukurikizwa ku munsi ritangarijweho."),
        ("p", "Bikorewe i Kigali, ku wa 01/01/2099"),
    ],
    "en": [
        ("h", "LAW N° 000/2099 OF 01/01/2099 GOVERNING AN INVENTED REGISTER"),
        ("p", "This text is invented to test the website. It is not a law."),
        ("h", "TABLE OF CONTENTS"),
        ("p", "CHAPTER ONE: GENERAL PROVISIONS"),
        ("p", "Article One: Purpose of this Law"),
        ("p", "Article 2: Definition of terms"),
        ("p", "CHAPTER II: THE REGISTER"),
        ("p", "Section One: Keeping the register"),
        ("p", "Article 3: Entries in the register"),
        ("p", "Article 4: Commencement"),
        ("h", "CHAPTER ONE: GENERAL PROVISIONS"),
        ("h", "Article One: Purpose of this Law"),
        ("p", "This Law governs an invented register."),
        ("h", "Article 2: Definition of terms"),
        ("p", "In this Law, the following terms have the following meanings:"),
        ("p", "1° “register”: the invented register kept for this test;"),
        ("p", "2° “entry”: a line of the register."),
        ("h", "CHAPTER II: THE REGISTER"),
        ("h", "Section One: Keeping the register"),
        ("h", "Article 3: Entries in the register"),
        ("p", "Every entry is made in the register on the day it is received."),
        ("p", "The register is kept so that anyone may read it."),
        ("h", "Article 4: Commencement"),
        ("p", "This Law comes into force on the date of its publication."),
        ("p", "Done at Kigali, on 01/01/2099"),
    ],
    "fr": [
        ("h", "LOI N° 000/2099 DU 01/01/2099 RÉGISSANT UN REGISTRE INVENTÉ"),
        ("p", "Ce texte est inventé pour essayer le site. Ce n’est pas une loi."),
        ("h", "TABLE DES MATIÈRES"),
        ("p", "CHAPITRE PREMIER: DISPOSITIONS GÉNÉRALES"),
        ("p", "Article premier: Objet de la présente loi"),
        ("p", "Article 2: Définitions des termes"),
        ("p", "CHAPITRE II: DU REGISTRE"),
        ("p", "Section première: Tenue du registre"),
        ("p", "Article 3: Inscriptions au registre"),
        ("p", "Article 4: Entrée en vigueur"),
        ("h", "CHAPITRE PREMIER: DISPOSITIONS GÉNÉRALES"),
        ("h", "Article premier: Objet de la présente loi"),
        ("p", "La présente loi régit un registre inventé."),
        ("h", "Article 2: Définitions des termes"),
        ("p", "Aux fins de la présente loi, les termes suivants ont les significations suivantes:"),
        ("p", "1° «registre»: le registre inventé tenu pour cet essai;"),
        ("p", "2° «inscription»: une ligne du registre."),
        ("h", "CHAPITRE II: DU REGISTRE"),
        ("h", "Section première: Tenue du registre"),
        ("h", "Article 3: Inscriptions au registre"),
        ("p", "Toute inscription est portée au registre le jour même où elle est reçue."),
        ("p", "Le registre est tenu de façon que chacun puisse le lire."),
        ("h", "Article 4: Entrée en vigueur"),
        ("p", "La présente loi entre en vigueur le jour de sa publication."),
        ("p", "Fait à Kigali, le 01/01/2099"),
    ],
}
PLAIN_TEXT = (
    "Freedom of Information Act 2011 (invented copy for tests)\n"
    "Section 1. (1) The right of any person to access or request information is established.\n"
    "(2) An applicant under this Act needs not demonstrate any specific interest in the information "
    "being applied for.\n"
)


def _version(source_id: str, language: str, text: str, now: datetime) -> SourceVersion:
    digest = sha256(text.encode()).hexdigest()
    return SourceVersion(
        id=f"ver-{source_id}-{language}-local",
        source_id=source_id,
        canonical_url=f"{OFFICIAL}/{source_id}",
        language=language,
        effective_from=date(2011, 5, 28) if source_id == PLAIN_ID else None,
        retrieved_at=now,
        raw_hash=digest,
        content_hash=digest,
        parser="local-fixture",
        canonical_text=text,
        verification_status="verified",
        content_verifier="local-fixture",
        content_verified_at=now,
    )


def law_version(language: str, now: datetime) -> tuple[SourceVersion, list[StructureNode]]:
    """One language of the invented law: its text as oa-text-1 writes it, and its rw-law-1 structure."""
    units = [
        {"kind": "heading" if kind == "h" else "paragraph", "text": text} for kind, text in LAW[language]
    ]
    pages = [{"page": 1, "units": units}]
    serialized = serialize(pages)
    structure = derive(serialized, pages, RW_LAW, language)
    if serialized.errors or structure.errors:
        raise SystemExit(f"The invented law does not read as a Rwandan law: {structure.errors}")
    version = _version(LAW_ID, language, serialized.text, now)
    ids = {id(node): f"nod-{LAW_ID}-{language}-{index}" for index, node in enumerate(structure.nodes)}
    raw = serialized.text.encode()
    nodes = [
        StructureNode(
            id=ids[id(node)],
            source_id=LAW_ID,
            version_id=version.id,
            parent_id=ids[id(node.parent)] if node.parent else None,
            kind=node.kind,
            locator=node.locator,
            order=node.order,
            start_byte=node.start,
            end_byte=node.end,
            page=node.page_start,
            page_end=node.page_end,
            source_hash=version.content_hash,
            label=node.label or None,
            heading=node.heading or None,
            span_hash=sha256(raw[node.start : node.end]).hexdigest(),
        )
        for node in structure.nodes
    ]
    return version, nodes


def _rights(rights_id: str, source_id: str, digests: list[str], now: datetime) -> RightsRecord:
    return RightsRecord(
        id=rights_id,
        source_id=source_id,
        status="OPEN",
        rights_holder="Local fixture",
        licence="LOCAL-TEST-FIXTURE",
        redistribution_allowed="yes",
        commercial_reuse_allowed="yes",
        derivatives_allowed="yes",
        attribution_required=True,
        attribution_text="Local test fixture",
        privacy_status="cleared",
        reviewer="local-fixture",
        reviewed_at=now,
        evidence=[f"{OFFICIAL}/{source_id}"],
        legal_note="Local test fixture",
        database_rights_notes="Local test fixture",
        approved_content_hashes=digests,
        metadata_public=True,
        metadata_basis="Local test fixture",
    )


def corpus() -> Corpus:
    now = datetime.now(UTC).replace(microsecond=0)
    held = Corpus.model_validate_json((ROOT / "ingestion/catalogue/ng.json").read_text(encoding="utf-8"))
    if not any(source.id == PLAIN_ID for source in held.sources):
        raise SystemExit(f"{PLAIN_ID} is not in ingestion/catalogue/ng.json")
    held.languages += [
        Language(id="rw", name="Kinyarwanda", tag="rw"),
        Language(id="fr", name="French", tag="fr"),
    ]
    held.jurisdictions.append(
        Jurisdiction(
            id="rw",
            name="Rwanda",
            country_code="RW",
            legal_systems=["civil law"],
            languages=["rw", "en", "fr"],
        )
    )
    held.authorities.append(
        Authority(
            id="rw-parliament",
            jurisdiction_id="rw",
            name="Parliament of Rwanda (local fixture)",
            kind="legislature",
            official_url=OFFICIAL,
        )
    )
    held.sources.append(
        Source(
            id=LAW_ID,
            jurisdiction_id="rw",
            authority_id="rw-parliament",
            title="Law n° 000/2099 of 01/01/2099 governing an invented register",
            document_type="act",
            canonical_url=f"{OFFICIAL}/{LAW_ID}",
            citation="Law n° 000/2099 of 01/01/2099",
            # The record's own language is not the first text served (English): the page opens in it all
            # the same.
            language="rw",
            summary="An invented text for testing the record page. It is not legislation.",
            year=2099,
        )
    )
    digests = []
    for language in ("rw", "en", "fr"):
        version, nodes = law_version(language, now)
        held.versions.append(version)
        held.structure.extend(nodes)
        digests.append(version.content_hash)
    held.rights.append(_rights("rights-" + LAW_ID, LAW_ID, digests, now))
    plain = _version(PLAIN_ID, "en", PLAIN_TEXT, now)
    held.versions.append(plain)
    for index, rights in enumerate(held.rights):
        if rights.source_id == PLAIN_ID:
            held.rights[index] = _rights(rights.id, PLAIN_ID, [plain.content_hash], now)
    return Corpus.model_validate(held.model_dump())


def build_store(folder: Path) -> Store:
    # A file, not memory: the record page asks for five things at once, each on its own connection.
    folder.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{folder / 'site.db'}", connect_args={"check_same_thread": False})
    metadata.create_all(engine)
    store = Store(engine)
    store.save(corpus())
    return store


def build_app(folder: Path, dist: Path = DIST) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/site/v1", create_site_app(build_store(folder), folder))

    @app.get("/{path:path}")
    def page(path: str, request: Request) -> Response:
        """The built site: a file where there is one, else the page that routes every address."""
        index = dist / "index.html"
        if not index.is_file():
            return PlainTextResponse(
                "web/dist is not built: run `npm --prefix web run build` first", status_code=503
            )
        target = (dist / path).resolve()
        if path and dist.resolve() in target.parents and target.is_file():
            return FileResponse(target, headers={"Cache-Control": "no-store"})
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the public site with two published texts.")
    parser.add_argument("--port", type=int, default=8774)
    args = parser.parse_args()
    if not (DIST / "index.html").is_file():
        raise SystemExit("web/dist is not built: run `npm --prefix web run build` first")
    with tempfile.TemporaryDirectory() as folder:
        uvicorn.run(build_app(Path(folder)), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
