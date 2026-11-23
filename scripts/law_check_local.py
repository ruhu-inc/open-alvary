"""Law check on this computer, for trying the page and for the browser tests.

It serves the page from ``web/public/agents`` and answers ``/agents/mcp`` with the real server part,
the real keyed MCP and the real verifier, over a small in-memory catalogue that holds one invented
copy of section 1 of the Freedom of Information Act 2011. Nothing here reads a secret or calls another
machine.

Two things make a whole question runnable away from the live site:

- The page's agent files name ``https://open.alvary.ai``. Here they are served with that address
  replaced by this server's own (``scripts.law_check_site``), so the agents call this server.
- ``/test-llm/v1`` is a scripted stand-in for an AI provider. It asks for ``check_claim`` once and then
  answers, so a turn can be run with no key and no outside call. Put ``[altered]`` in the question to
  have it quote the section with one word changed, and ``[plain]`` to have it answer with no check.

    .venv/bin/python -m scripts.law_check_local --port 8771

Then open http://127.0.0.1:8771/agents/ .
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from contextlib import contextmanager
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from accounts import keys
from api.catalogue import LegacyCatalogue
from api.credentials import CredentialGate, FailedKeys, GateConfig
from api.law_check import LawCheckMCP
from api.mcp import create_router as create_mcp_router
from api.receipts import ReceiptSigner
from api.repositories import LegacyRepository, PublicUnavailable
from api.service import AGENTS_CSP, BASE_SECURITY_HEADERS
from api.store import Store, metadata
from schema.models import Corpus, RightsRecord, SourceVersion, StructureNode
from scripts.law_check_site import AGENTS_DIR, file_for_origin

ROOT = Path(__file__).resolve().parent.parent
SOURCE_ID = "ng-foi-act-2011"
CITATION = "Freedom of Information Act 2011, s. 1(1)"
TRUE_QUOTE = (
    "Notwithstanding anything contained in any other Act, law or regulation, the right of any person to "
    "access or request information, whether or not contained in any written form, which is in the custody "
    "or possession of any public official, agency or institution howsoever described, is established."
)
ALTERED_QUOTE = TRUE_QUOTE.replace("any person", "any citizen")
TEXT = (
    "Freedom of Information Act 2011\n"
    f"Section 1. (1) {TRUE_QUOTE}\n"
    "(2) An applicant under this Act needs not demonstrate any specific interest in the information "
    "being applied for.\n"
)


def _plan() -> dict:
    """The law-check plan as committed, so the local server runs under the real plan's settings."""
    tiers = json.loads((ROOT / "accounts/tiers.json").read_text(encoding="utf-8"))
    tier = next(tier for tier in tiers if tier["id"] == "law-check")
    return {"id": tier["id"], "includes": tier["includes"], "allowance": tier["allowance"]}


def _store() -> Store:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    store = Store(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    corpus = Corpus.model_validate_json((ROOT / "ingestion/catalogue/ng.json").read_text(encoding="utf-8"))
    if not any(source.id == SOURCE_ID for source in corpus.sources):
        raise SystemExit(f"{SOURCE_ID} is not in ingestion/catalogue/ng.json")

    raw = TEXT.encode()
    digest = sha256(raw).hexdigest()
    section = TEXT.index("Section 1.")
    first = TEXT.index("(1) Notwithstanding")
    second = TEXT.index("(2) An applicant")
    version = SourceVersion(
        id="ver-ng-foi-2011-local-1",
        source_id=SOURCE_ID,
        canonical_url="https://example.org/local-fixture/freedom-of-information-act-2011",
        language="en",
        effective_from=date(2011, 5, 28),
        retrieved_at=now,
        raw_hash=digest,
        content_hash=digest,
        parser="local-fixture",
        canonical_text=TEXT,
        verification_status="verified",
        content_verifier="local-fixture",
        content_verified_at=now,
    )
    corpus.versions.append(version)
    corpus.structure.extend(
        [
            StructureNode(
                id="anc-ng-foi-s1",
                source_id=SOURCE_ID,
                version_id=version.id,
                kind="section",
                locator="sec_1",
                label="1",
                heading="Right of access to records",
                order=0,
                start_byte=len(TEXT[:section].encode()),
                end_byte=len(raw),
                source_hash=digest,
            ),
            StructureNode(
                id="anc-ng-foi-s1-1",
                source_id=SOURCE_ID,
                version_id=version.id,
                parent_id="anc-ng-foi-s1",
                kind="subsection",
                locator="sec_1__subsec_1",
                label="(1)",
                order=1,
                start_byte=len(TEXT[:first].encode()),
                end_byte=len(TEXT[:second].encode()),
                source_hash=digest,
            ),
        ]
    )
    for index, rights in enumerate(corpus.rights):
        if rights.source_id == SOURCE_ID:
            corpus.rights[index] = RightsRecord(
                id=rights.id,
                source_id=SOURCE_ID,
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
                evidence=[str(version.canonical_url)],
                legal_note="Local test fixture",
                database_rights_notes="Local test fixture",
                approved_content_hashes=[digest],
                metadata_public=True,
                metadata_basis="Local test fixture",
            )
            break
    store.save(Corpus.model_validate(corpus.model_dump()))
    return store


def _signer() -> ReceiptSigner:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return ReceiptSigner(pem, kid="oa-receipt-local", valid_from="2026-01-01T00:00:00Z")


def _sse(chunks: list[dict]) -> StreamingResponse:
    def body():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


def _tool_call(name: str, arguments: dict) -> StreamingResponse:
    call = {"index": 0, "id": "call_local_1", "type": "function"}
    call["function"] = {"name": name, "arguments": json.dumps(arguments)}
    return _sse(
        [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [call]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )


def _answer(text: str) -> StreamingResponse:
    half = len(text) // 2
    return _sse(
        [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text[:half]}}]},
            {"choices": [{"index": 0, "delta": {"content": text[half:]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
    )


async def scripted_model(request: Request) -> Response:
    """One chat completion from the scripted stand-in. Which agent is asking is told from the tools it
    was given: the agent that holds ``check_claim`` calls it once; an agent that can ask the verifier
    asks it once; whoever already has a tool's answer writes the reply."""
    body = await request.json()
    tools = [tool["function"] for tool in body.get("tools") or () if tool.get("type") == "function"]
    messages = body.get("messages") or []
    said = json.dumps(messages)
    answered = any(message.get("role") == "tool" for message in messages)
    altered = "[altered]" in said
    quote = ALTERED_QUOTE if altered else TRUE_QUOTE

    if not answered and "[plain]" not in said:
        check = next((tool for tool in tools if "check_claim" in tool["name"]), None)
        if check:
            return _tool_call(check["name"], {"citation": CITATION, "quote": quote, "jurisdiction": "ng"})
        verifier = next((tool for tool in tools if tool["name"].endswith("verifier")), None)
        if verifier:
            ask = f"Check this quote of {CITATION}{' [altered]' if altered else ''}: {quote}"
            properties = (verifier.get("parameters") or {}).get("properties") or {}
            wanted = (verifier.get("parameters") or {}).get("required") or list(properties)
            arguments = {
                name: ask for name in wanted if properties.get(name, {}).get("type", "string") == "string"
            }
            return _tool_call(verifier["name"], arguments or {"inquiry": ask})

    if any(tool["name"].endswith("verifier") for tool in tools):
        return _answer(
            f'Section 1(1) of the Freedom of Information Act 2011 says: "{quote}"\n\n'
            "- It speaks of **any person**.\n"
            "- <b>This tag must show as text.</b>\n\n"
            '```cruse-ui\n{"components": [{"type": "card", "title": "Checked: found"}]}\n```'
        )
    return _answer("The quote was checked against the published text.")


def build_app(origin: str) -> FastAPI:
    store = _store()
    signer = _signer()
    plan = _plan()

    # The in-memory database has one connection. The page sends several calls at once (the runtime
    # runs up to four tools together), so they take turns here; without this the server froze.
    one_at_a_time = threading.Lock()

    @contextmanager
    def catalogue_session():
        with one_at_a_time, store.engine.begin() as connection:
            yield LegacyCatalogue(LegacyRepository(connection))

    keyed = FastAPI()
    keyed.include_router(
        create_mcp_router(
            catalogue_session,
            (SQLAlchemyError, ValidationError, PublicUnavailable),
            lambda request: request.state.access["reach"],
            signer=lambda: signer,
        )
    )

    def check_credential(_asked):
        return {
            "ok": True,
            "organisation": "org_lawcheck_local",
            "credential": "key_" + "a" * 32,
            "tier": plan,
            "scope": {"all": True},
        }

    gate = CredentialGate(
        keyed,
        config=GateConfig(f"{origin}/mcp", "mcp", "mcp", f"{origin}/.well-known/oauth", jsonrpc=True),
        check=check_credential,
        record_usage=lambda _entry: None,
        admission=None,
        verifier=None,
        key_pepper=b"p" * 32,
        failed_keys=FailedKeys(),
    )
    issued = keys.generate()
    law_check = LawCheckMCP(
        gate,
        admission_config=None,
        admission_store=None,
        key_provider=lambda: issued.secret,
        require_admission=False,
    )

    async def agents_file(request: Request) -> Response:
        relative = request.path_params.get("path") or "index.html"
        if relative.endswith("/"):
            relative += "index.html"
        target = (AGENTS_DIR / relative).resolve()
        if AGENTS_DIR.resolve() not in target.parents or not target.is_file():
            return Response("Not found", status_code=404, media_type="text/plain")
        headers = {
            **BASE_SECURITY_HEADERS,
            "Content-Security-Policy": AGENTS_CSP,
            "Cache-Control": "no-store",
        }
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix in {".js", ".mjs"}:
            media_type = "text/javascript"
        return Response(file_for_origin(target, origin), media_type=media_type, headers=headers)

    def key_list(_request: Request) -> Response:
        return JSONResponse(signer.jwks(), headers={"Cache-Control": "no-store"})

    app = FastAPI()
    app.router.routes.extend(
        [
            Route("/agents/mcp", law_check, methods=["GET", "POST", "OPTIONS", "DELETE"]),
            Route("/.well-known/open-alvary-receipt-keys.json", key_list),
            Route("/test-llm/v1/chat/completions", scripted_model, methods=["POST"]),
            Route("/agents", agents_file),
            Route("/agents/{path:path}", agents_file),
        ]
    )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve Law check on this computer.")
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args()
    origin = f"http://127.0.0.1:{args.port}"
    uvicorn.run(build_app(origin), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
