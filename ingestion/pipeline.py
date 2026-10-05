"""Operator-only ingestion. No public URL fetching or write API."""

import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from rights.policy import decide
from schema.models import Corpus, RightsRecord, Source, SourceVersion, StructureNode

MAX_BYTES = 25 * 1024 * 1024


class OCRRequired(ValueError):
    pass


class Adapter(Protocol):
    def fetch(self, source: Source) -> bytes: ...
    def parse(self, raw: bytes) -> str: ...


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return re.sub(r"[^\S\n\t]+", " ", text).strip()


class ManualText:
    def __init__(self, text: str):
        self.text = text

    def fetch(self, source: Source) -> bytes:
        return self.text.encode()

    def parse(self, raw: bytes) -> str:
        return raw.decode("utf-8")


class OfficialDocument:
    """Exact URL allowlist; no redirects, cookies, or browser credentials.

    Only trusted operators configure this adapter. Run with restricted egress;
    it is intentionally not an arbitrary-URL service.
    """

    def __init__(
        self, approved_urls: set[str], media_type: str, ocr: Callable[[bytes, int], str] | None = None
    ):
        self.approved_urls = approved_urls
        self.media_type = media_type
        self.ocr = ocr

    def fetch(self, source: Source) -> bytes:
        url = str(source.canonical_url)
        parsed = urlsplit(url)
        if source.url_kind != "document" or url not in self.approved_urls or parsed.scheme != "https":
            raise ValueError("Fetching requires an explicitly approved HTTPS document URL")
        if parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ValueError("Unexpected URL credentials or port")
        with httpx.stream(
            "GET",
            url,
            follow_redirects=False,
            timeout=30,
            headers={"User-Agent": "OpenAlvary/0.1 (operator-approved ingestion)"},
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise ValueError("Source exceeds ingestion size limit")
            return bytes(raw)

    def parse(self, raw: bytes) -> str:
        if self.media_type == "text/html":
            soup = BeautifulSoup(raw, "html.parser")
            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.decompose()
            return soup.get_text("\n", strip=True)
        if self.media_type == "application/pdf":
            pages = []
            for number, page in enumerate(PdfReader(BytesIO(raw)).pages, 1):
                text = page.extract_text() or ""
                if not text.strip():
                    if self.ocr is None:
                        raise OCRRequired(f"Page {number} needs OCR; configure an OCR provider")
                    text = self.ocr(raw, number)
                    if not text.strip():
                        raise OCRRequired(f"OCR returned empty text for page {number}")
                pages.append(text)
            return "\n\n".join(pages)
        if self.media_type == "text/plain":
            return raw.decode("utf-8")
        raise ValueError("Unsupported media type")


def ingest(
    source: Source, rights: RightsRecord, adapter: Adapter, retrieved_at: datetime | None = None
) -> tuple[SourceVersion, list[StructureNode], bool]:
    if rights.source_id != source.id:
        raise ValueError("Rights record does not belong to this source")
    if rights.status == "RESTRICTED":
        raise ValueError("Restricted material belongs outside Open Alvary")
    raw = adapter.fetch(source)
    if len(raw) > MAX_BYTES:
        raise ValueError("Source exceeds ingestion size limit")
    text = normalize(adapter.parse(raw))
    if not text:
        raise ValueError("Parser produced empty text")
    digest = sha256(text.encode()).hexdigest()
    version = SourceVersion(
        id=f"v-{digest[:32]}-{sha256(source.id.encode()).hexdigest()[:12]}",
        source_id=source.id,
        canonical_url=source.canonical_url,
        retrieved_at=retrieved_at or datetime.now(UTC),
        raw_hash=sha256(raw).hexdigest(),
        content_hash=digest,
        canonical_text=text,
        parser=f"{type(adapter).__name__}/0.1",
        language=source.language,
    )
    # Generic root node only. Jurisdiction adapters add legally meaningful nodes after review.
    node = StructureNode(
        id=f"{version.id}:root",
        source_id=source.id,
        version_id=version.id,
        kind="document",
        locator="document",
        order=0,
        start_byte=0,
        end_byte=len(text.encode()),
        source_hash=digest,
    )
    return version, [node], decide(rights, version).full_text


def persist_ingestion(store, source_id: str, adapter: Adapter):
    corpus = store.load()
    source = next(s for s in corpus.sources if s.id == source_id)
    rights = next(r for r in corpus.rights if r.source_id == source_id)
    version, nodes, eligible = ingest(source, rights, adapter)
    existing = next((v for v in corpus.versions if v.id == version.id), None)
    if existing:
        return existing, decide(rights, existing).full_text
    corpus.versions.append(version)
    corpus.structure.extend(nodes)
    store.save(Corpus.model_validate(corpus.model_dump(mode="json")))
    return version, eligible
