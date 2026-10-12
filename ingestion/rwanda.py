"""Rwanda: the sources we catalogue and how their listings are read (docs/research/rwanda-corpus-strategy.md).

    python -m ingestion.rwanda catalogue                 # every source due, listing pages only
    python -m ingestion.rwanda catalogue --source rlrc-laws --force
    python -m ingestion.rwanda status                    # last check, last change, lag, counts

The catalogue reads listing pages only and records every listed file in the sync ledger; nothing is
downloaded. The Official Gazette (MINIJUST) is the source of record; the RLRC "Laws of Rwanda" tree
says what is in force or repealed and files each law under its subject.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from ingestion import corpus_store, filelist, gazette_layout, ruled_tables, rw_gazette
from ingestion.pipeline import MAX_BYTES
from ingestion.sync import Ledger, now, stamp

STAGING = Path(os.environ.get("ALVARY_RW_STAGING", "private-staging/rw"))
COUNTRY = "rw"

SOURCES = {
    "minijust-gazette": {
        "title": "Official Gazette of the Republic of Rwanda (MINIJUST archive)",
        "start": "https://www.minijust.gov.rw/official-gazette",
        "host": "https://www.minijust.gov.rw",
        "root": "/user_upload/Minijust/Official_gazettes_2/",
        "cadence": "daily",
    },
    "rlrc-laws": {
        "title": "Rwanda Law Reform Commission, Laws of Rwanda (in force, repealed, international)",
        "start": "https://www.rlrc.gov.rw/mandate/laws-of-rwanda",
        "host": "https://www.rlrc.gov.rw",
        "root": "/user_upload/RLRC/Laws_of_Rwanda_v2/",
        "cadence": "weekly",
    },
}

# --- what a file name says ---------------------------------------------------------------------------

KINDS = {
    "ol": "organic_law",
    "orl": "organic_law",
    "law": "law",
    "loi": "law",
    "loiorganique": "organic_law",
    "organiclaw": "organic_law",
    "po": "presidential_order",
    "ap": "presidential_order",  # arrêté présidentiel
    "arretepresidentiel": "presidential_order",
    "presidentialorder": "presidential_order",
    "pmo": "prime_ministers_order",
    "primeministersorder": "prime_ministers_order",
    "mo": "ministerial_order",
    "am": "ministerial_order",  # arrêté ministériel
    "arreteministeriel": "ministerial_order",
    "ministerialorder": "ministerial_order",
    "reg": "regulations",
    "regul": "regulations",
    "regulation": "regulations",
    "regulations": "regulations",
    "re": "practice_direction",
    "dl": "decree_law",
    "decretloi": "decree_law",
    "decree": "decree",
    "decret": "decree",
    "ordonnance": "ordinance",
    "ordonnanceloi": "ordinance",
    "ordonnancelegislative": "ordinance",
    "instructions": "instructions",
    "instruction": "instructions",
}
_KIND_WORDS = (
    r"Organic\s+Law|Loi\s+organique|Presidential\s+Order|Prime\s+Minister'?\s?s?\s+Order|Ministerial\s+Order"
    r"|Arr[êe]t[ée]\s+pr[ée]sidentiel|Arr[êe]t[ée]\s+minist[ée]riel|D[ée]cret[- ]loi"
    r"|Ordonnance(?:[- ]loi|\s+l[ée]gislative)?|ORL|OL|O\.\s?L|Law|Loi|P\.?\s?O|PMO|M\.?\s?O|A\.?\s?P|A\.?\s?M|REG|Regul"
    r"|Regulations?|RE|D\.?\s?L|Decree|D[ée]cret|Instructions?"
)
INSTRUMENT = re.compile(
    rf"\b({_KIND_WORDS})\b\.?\s*(?:n0|no|nº|n°|n)\.?\s*([0-9][0-9A-Za-z./-]*)",
    re.I,
)
INSTRUMENT_WITHOUT_NUMBER = re.compile(rf"\b({_KIND_WORDS})\b", re.I)
TREATY = re.compile(
    r"\b(convention|protocol|protocole|treaty|trait[ée]|charter|charte|agreement|accord|memorandum|amendment)",
    re.I,
)
TREATY_KINDS = {"protocole": "protocol", "traité": "treaty", "traite": "treaty", "charte": "charter"}


def _kind_key(word: str) -> str:
    plain = unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", plain)


OF_DATE = re.compile(r"\bof\s*(?:(\d{1,2})\s+(\d{1,2})\s+)?((?:19|20)\d\d)\b", re.I)


def _plain(name: str) -> str:
    return " ".join(re.sub(r"\.pdf$", "", name, flags=re.I).replace("_", " ").split())


def parse_rlrc(name: str, folder: str) -> dict:
    """Subject code, status, kind, number and year from an RLRC file name and its folder."""
    text = _plain(name)
    out: dict = {}
    code = re.match(r"^(\d+(?:\.\d+)*)(?:\.|\s)", text + " ")
    if code:
        out["subject_code"] = code.group(1)
    if "/Laws_in_force/" in folder:
        out["status"] = "in_force"
    elif "/Repealed_laws/" in folder:
        out["status"] = "repealed"
    elif "International_Legal_Instruments" in folder:
        out["status"] = "international"
    domain = re.search(r"/(?:Laws_in_force|Repealed_laws)/(\d+)\._([^/]+)/", folder)
    if domain:
        out["domain"] = f"{domain.group(1)} {domain.group(2).replace('_', ' ')}"
    if re.search(r"\bconstitution\b", text, re.I) and not INSTRUMENT.search(text):
        out["kind"] = "constitution"
    instrument = INSTRUMENT.search(text)
    if out.get("status") == "international":
        treaty = TREATY.search(text)
        word = treaty.group(1).lower() if treaty else "agreement"
        out["kind"] = TREATY_KINDS.get(
            word, {"accord": "agreement", "memorandum": "agreement", "amendment": "protocol"}.get(word, word)
        )
        instrument = None
    elif not instrument and "kind" not in out:
        bare = INSTRUMENT_WITHOUT_NUMBER.search(text)
        if bare and _kind_key(bare.group(1)) in KINDS:
            out["kind"] = KINDS[_kind_key(bare.group(1))]
    if instrument:
        out["kind"] = KINDS.get(_kind_key(instrument.group(1)), instrument.group(1).lower())
        out["number"] = instrument.group(2).strip(".-")
        if re.search(r"[./-]OL$", out["number"], re.I):  # "01-2012-OL", "001/2026.OL": an organic law
            out["kind"] = "organic_law"
        if out["kind"] == "organic_law" and re.search(r"B\.?O\.?R\.?U|\b19[1-5]\d\b", text):
            out["kind"] = (
                "ordinance"  # "O.L." in the Bulletin Officiel du Ruanda-Urundi: ordonnance législative
            )
        rest = text[instrument.end() :]
        dated = OF_DATE.match(rest.strip()) or OF_DATE.search(rest[:40])
        if dated:
            out["year"] = int(dated.group(3))
            if dated.group(1):
                out["date"] = f"{dated.group(3)}-{int(dated.group(2)):02d}-{int(dated.group(1)):02d}"
    if "year" not in out:
        years = re.findall(r"\b((?:19|20)\d\d)\b", text)
        if years:
            out["year"] = int(years[-1] if "kind" not in out else years[0])
    out["parsed"] = bool(out.get("kind") and out.get("year"))
    return out


def parse_gazette(name: str) -> dict:
    """Issue number, its edition (special, bis, ter) and date from a gazette file name. Names vary
    by era: "Official_Gazette_no_38_bis_of_22.09.2014", "24bis_of_11.06.2018", "Official Gazette
    nº 29 of 21/09/2020", "Igazeti ya Leta n° Idasanzwe yo ku wa 18/10/2024"."""
    text = _plain(name)
    out: dict = {}
    # One separator throughout ("14.11.2016", "21/09/2020", "29- 07- 2019", "16 05 2022" once the
    # underscores are spaces), so that an issue number followed by a date ("no 02 11.01.2021") is
    # not read as a date.
    date = re.search(
        r"(?<!\d)(\d{1,2})\s?([./-])\s?(\d{1,2})\s?\2\s?((?:19|20)?\d\d)(?!\d)", text
    ) or re.search(r"(?<!\d)(\d{1,2})( )(\d{1,2}) ((?:19|20)\d\d)(?!\d)", text)
    if date and 1 <= int(date.group(3)) <= 12 and 1 <= int(date.group(1)) <= 31:
        year = date.group(4) if len(date.group(4)) == 4 else f"20{date.group(4)}"
        out["date"] = f"{year}-{int(date.group(3)):02d}-{int(date.group(1)):02d}"
    if re.search(r"\b(sp[ée]?cial|idasanzwe|sp)\b", text, re.I):
        out["special"] = True
    edition = re.search(r"(?<=\d)\s*(bis|ter|quater)\b|\b(bis|ter|quater)\b", text, re.I)
    if edition:
        out[(edition.group(1) or edition.group(2)).lower()] = True
    number = re.search(
        r"(?:^|\bn(?:o|0|°|º|r)?\.?\s*|\bOG\s+)(\d{1,3})(?=\s*(?:bis|ter|quater)\b|\b)(?![./]\d)",
        text,
        re.I,
    )
    if number and int(number.group(1)) and not (date and number.start(1) == date.start(1)):
        out["number"] = int(number.group(1))
    out["parsed"] = bool(out.get("date") and (out.get("number") or out.get("special")))
    return out


def parse_item(source_id: str, item: dict) -> dict:
    """What a listed file's name says; a gazette whose shown name says too little is read again
    from the file name in its address, which the archive often writes more fully."""
    if source_id != "minijust-gazette":
        return parse_rlrc(item["name"], item["folder"])
    parsed = parse_gazette(item["name"])
    if not parsed["parsed"]:
        from_url = parse_gazette(unquote(item["url"].rsplit("/", 1)[-1]))
        if from_url["parsed"]:
            return from_url | {"from": "address"}
    return parsed


# --- the catalogue -----------------------------------------------------------------------------------


def catalogue(source_id: str, delay: float, max_pages: int, log=print) -> dict:
    source = SOURCES[source_id]
    run_id = f"{datetime.now(UTC).strftime('%Y-%m-%dT%H%M%S')}-catalogue-{source_id}"
    started = now()
    result = filelist.crawl(
        source["start"],
        source["root"],
        filelist.fetcher(source["host"]),
        delay=delay,
        max_pages=max_pages,
        log=log,
    )
    for item in result["files"]:
        item["size_bytes"] = filelist.size_bytes(item["size"])
        item["listed_date"] = filelist.listed_date(item["modified"])
        item["parsed"] = parse_item(source_id, item)
    ledger = Ledger(STAGING / "sync-ledger.json")
    newest = max((f["listed_date"] or "" for f in result["files"]), default="")
    changes = ledger.observe_listing(
        source_id,
        source["start"],
        source["cadence"],
        result,
        run_id,
        cursor={"newest_listed_date": newest, "files": len(result["files"])},
        at=started,
    )
    ledger.save()
    out_dir = STAGING / "catalogue"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{source_id}.json").write_text(
        json.dumps(
            {"source": source_id, **source, "checked_at": stamp(started), **result},
            ensure_ascii=False,
            indent=1,
        )
        + "\n"
    )
    total = sum(f["size_bytes"] or 0 for f in result["files"])
    summary = {
        "run": run_id,
        "source": source_id,
        "started_at": stamp(started),
        "finished_at": stamp(now()),
        "outcome": result["outcome"],
        "complete": result["complete"],
        "pages_read": result["pages_read"],
        "files": len(result["files"]),
        "bytes_listed": total,
        "unparsed": sum(1 for f in result["files"] if not f["parsed"]["parsed"]),
        "changes": {k: len(v) for k, v in changes.items()},
    }
    runs = STAGING / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(json.dumps({**summary, "changed_items": changes}, indent=1) + "\n")
    return summary


def reparse(source_id: str) -> dict:
    """Read the stored catalogue's names and sizes again with the current rules, without visiting
    the site."""
    path = STAGING / "catalogue" / f"{source_id}.json"
    data = json.loads(path.read_text())
    for item in data["files"]:
        item["size_bytes"] = filelist.size_bytes(item["size"])
        item["parsed"] = parse_item(source_id, item)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    return {
        "source": source_id,
        "files": len(data["files"]),
        "bytes_listed": sum(f["size_bytes"] or 0 for f in data["files"]),
        "unparsed": sum(1 for f in data["files"] if not f["parsed"]["parsed"]),
    }


# --- fetching an approved selection ---------------------------------------------------------------

ALLOWED_HOSTS = {"www.rlrc.gov.rw", "www.minijust.gov.rw"}
LARGEST_GAZETTE = 128 * 1024**2  # the archive lists issues up to about 71 MB (scanned 2010s issues)


def size_limit(listed: dict) -> int:
    """The most a file may be: the usual ingestion limit, or a little over the size its listing
    shows (listings round), never more than the largest gazette allowed."""
    shown = filelist.size_bytes(listed.get("size", "")) or 0
    return max(MAX_BYTES, min(LARGEST_GAZETTE, int(shown * 1.25)))


def load_selection(path: Path) -> dict:
    """An operator-approved list of catalogue items to fetch: who approved it, when, and why."""
    selection = json.loads(Path(path).read_text())
    for key in ("approved_by", "approved_at", "purpose", "items"):
        if not selection.get(key):
            raise ValueError(f"selection {path} lacks {key}")
    return selection


RESERVE_BYTES = 30 * 1024**3  # free space left on the disk before a long fetch stops


class FetchStopped(Exception):
    """A long fetch stopped on purpose: the site refused, or the disk is nearly full."""


def fetch_selection(
    selection: dict,
    delay: float = 4.0,
    log=print,
    keep_going: bool = False,
    store=None,
    keep_local: bool = True,
) -> list[dict]:
    """Fetch each selected file once: exact catalogue URLs on the allowed hosts, no redirects, the
    ingestion size limit, PDF only; stored by fingerprint with a provenance sidecar, ledger updated.

    With keep_going (a bulk run), a file that cannot be had (gone, not a PDF, larger than listed) is
    recorded and the run goes on; a refusal (401, 403, 429) still stops it at once, and so does free
    disk space falling below RESERVE_BYTES. A run can be repeated: files already held are skipped."""
    import httpx

    ledger = Ledger(STAGING / "sync-ledger.json")
    results = []
    with httpx.Client(timeout=120, headers={"User-Agent": filelist.user_agent()}) as client:
        for index, item_id in enumerate(selection["items"]):
            try:
                results.append(
                    _fetch_one(client, ledger, selection, item_id, index, delay, log, store, keep_local)
                )
            except FetchStopped:
                raise
            except Exception as error:  # noqa: BLE001 - recorded per file when keeping going
                if not keep_going:
                    raise
                ledger.data["items"].setdefault(item_id, {}).setdefault("history", []).append(
                    {"at": stamp(now()), "change": "fetch failed", "reason": str(error)[:300]}
                )
                ledger.save()
                log(f"skipped {item_id}: {error}")
                results.append({"item": item_id, "fetched": False, "error": str(error)[:300]})
    return results


def _fetch_one(client, ledger, selection, item_id, index, delay, log, store=None, keep_local=True) -> dict:
    import httpx

    record = ledger.data["items"].get(item_id)
    if record is None:
        raise ValueError(f"{item_id} is not in the catalogue")
    url = record["url"]
    parts = httpx.URL(url)
    if parts.scheme != "https" or parts.host not in ALLOWED_HOSTS:
        raise ValueError(f"{item_id}: {url} is not on an allowed host")
    held_here = (STAGING / "sources" / record["source"] / f"{record.get('sha256')}.pdf").exists()
    if record.get("sha256") and (held_here or record.get("stored")):
        return {"item": item_id, "sha256": record["sha256"], "fetched": False}
    if keep_local and shutil.disk_usage(STAGING).free < RESERVE_BYTES:
        raise FetchStopped(f"stopped: less than {RESERVE_BYTES // 1024**3} GiB free on the disk")
    if index:
        time.sleep(delay)
    with client.stream("GET", url, follow_redirects=False) as response:
        if response.status_code in (401, 403, 429):
            raise FetchStopped(f"stopped: the site answered {response.status_code} at {url}")
        if response.status_code != 200:
            raise ValueError(f"{item_id}: HTTP {response.status_code}")
        raw, limit = bytearray(), size_limit(record["listed"])
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > limit:
                raise ValueError(f"{item_id}: larger than its listing shows ({limit} bytes)")
        headers = {k: response.headers.get(k) for k in ("content-type", "last-modified", "etag")}
    if not raw.startswith(b"%PDF-"):
        raise ValueError(f"{item_id}: not a PDF")
    sha = hashlib.sha256(raw).hexdigest()
    sidecar = (
        json.dumps(
            {
                "item": item_id,
                "url": url,
                "listed": record["listed"],
                "retrieved_at": stamp(now()),
                "sha256": sha,
                "bytes": len(raw),
                "headers": headers,
                "selection": {k: selection[k] for k in ("approved_by", "approved_at", "purpose")},
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n"
    )
    if keep_local:
        folder = STAGING / "sources" / record["source"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{sha}.pdf").write_bytes(raw)
        (folder / f"{sha}.json").write_text(sidecar)
    ledger.fetched(item_id, sha, len(raw))
    if store is not None:
        name = corpus_store.source_name(COUNTRY, record["source"], sha)
        store.put(name, bytes(raw), "application/pdf", {"sha256": sha, "url": url})
        store.put(name[: -len(".pdf")] + ".json", sidecar.encode(), "application/json")
        ledger.data["items"][item_id]["stored"] = store.uri(name)
    ledger.save()
    log(f"fetched {record['listed']['name']} ({len(raw) // 1024} KB)")
    return {"item": item_id, "sha256": sha, "fetched": True}


def push_held(store, log=print) -> int:
    """Upload to the corpus bucket every file already held here and not yet stored there."""
    ledger = Ledger(STAGING / "sync-ledger.json")
    pushed = 0
    for record in ledger.data["items"].values():
        sha = record.get("sha256")
        local = STAGING / "sources" / str(record.get("source")) / f"{sha}.pdf"
        if not sha or record.get("stored") or not local.exists():
            continue
        name = corpus_store.source_name(COUNTRY, record["source"], sha)
        store.put(name, local.read_bytes(), "application/pdf", {"sha256": sha, "url": record["url"]})
        sidecar = local.with_suffix(".json")
        if sidecar.exists():
            store.put(name[: -len(".pdf")] + ".json", sidecar.read_bytes(), "application/json")
        record["stored"] = store.uri(name)
        pushed += 1
        log(f"stored {record['listed']['name'][:70]}")
    ledger.save()
    return pushed


def mirror_state(store) -> None:
    """The sync ledger, catalogues, selections and access decisions, copied to the bucket."""
    for path in [
        STAGING / "sync-ledger.json",
        *sorted((STAGING / "catalogue").glob("*.json")),
        *sorted((STAGING / "selections").glob("*.json")),
        *sorted((STAGING / "access").glob("*.json")),
    ]:
        if path.exists():
            name = f"state/{COUNTRY}/{path.relative_to(STAGING)}"
            store.put(name, path.read_bytes(), "application/json", replace=True)


def bulk_selection(approved_by: str, purpose: str, sources: list[str]) -> dict:
    """Every catalogued file of the given sources not yet held, as a selection recording who approved
    it; nothing is assumed: the approver is named on the command line."""
    if not approved_by.strip():
        raise ValueError("a bulk fetch needs the approver's name")
    ledger = Ledger(STAGING / "sync-ledger.json")
    items = sorted(
        item_id
        for item_id, record in ledger.data["items"].items()
        if record.get("source") in sources and not record.get("sha256") and record.get("url")
    )
    return {
        "purpose": purpose,
        "approved_by": approved_by,
        "approved_at": stamp(now()),
        "terms": "private-staging/rw/access/pol-01-2026-10-11.json",
        "items": items,
    }


def probe_selection(selection: dict, log=print) -> list[dict]:
    """The layout of every fetched file in a selection, written beside it."""
    ledger = Ledger(STAGING / "sync-ledger.json")
    out = []
    for item_id in selection["items"]:
        record = ledger.data["items"][item_id]
        pdf = STAGING / "sources" / record["source"] / f"{record['sha256']}.pdf"
        pages = gazette_layout.probe(pdf)
        summary = gazette_layout.summary(pages)
        layout_dir = STAGING / "layout"
        layout_dir.mkdir(parents=True, exist_ok=True)
        (layout_dir / f"{record['sha256']}.json").write_text(
            json.dumps({"item": item_id, "summary": summary, "pages": pages}, indent=1) + "\n"
        )
        log(f"{record['listed']['name']}: {json.dumps(summary)}")
        out.append({"item": item_id, **summary})
    return out


COLLECTION = STAGING / "collection"
RIGHTS_BASIS = (
    "Pilot sample, private review only: acquired under the operator's POL-01 acceptance of "
    "2026-10-11 (MINIJUST and RLRC publish without terms; robots allow). Publication of text waits "
    "for the copyright check of official texts under Law n° 055/2024."
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def prepare_selection(selection: dict, log=print) -> dict:
    """Each fetched gazette issue of a selection split into its instruments, and each instrument's
    language versions written as readings the workbench reviews, with the issue's PDF beside them,
    the details read from the text, and the check of its articles across the three languages."""
    import os
    import shutil

    ledger = Ledger(STAGING / "sync-ledger.json")
    COLLECTION.mkdir(parents=True, exist_ok=True)
    path = COLLECTION / "manifest.json"
    manifest = (
        json.loads(path.read_text())
        if path.exists()
        else {
            "collection": "rw-pilot",
            "jurisdiction": "rw",
            "description": "Rwanda pilot: laws from a sample of Official Gazette issues, 2004 to 2026",
            "rights_basis": RIGHTS_BASIS,
            "documents": [],
        }
    )
    documents = {d["id"]: d for d in manifest["documents"]}
    summary = []
    for item_id in selection["items"]:
        record = ledger.data["items"][item_id]
        if record["source"] not in ("minijust-gazette", "rlrc-laws") or not record.get("sha256"):
            continue
        sha = record["sha256"]
        # This issue is read afresh: what an earlier reading of it produced is replaced.
        for stale in [k for k, d in documents.items() if d.get("url") == record["url"]]:
            for suffix in (".reading.json", ".pdf", ".provenance.json"):
                (COLLECTION / f"{stale}{suffix}").unlink(missing_ok=True)
            del documents[stale]
        pdf = STAGING / "sources" / record["source"] / f"{sha}.pdf"
        sidecar = json.loads(pdf.with_suffix(".json").read_text())
        words = gazette_layout.text_layer(pdf)
        layouts = [len(gazette_layout.columns(page)) for page in words]
        split = rw_gazette.split_issue(words, ruled_tables.document_tables(pdf, words, layouts))
        scanned = ruled_tables.image_pages(pdf)
        roles = rw_gazette.annex_pages(split)
        streams = rw_gazette.streams(split, skip=set(roles))
        furniture = {page["page"]: [line["text"] for line in page["furniture"]] for page in split}

        def write_document(doc_id, reading, pages, record=record, sidecar=sidecar, pdf=pdf, sha=sha):
            """A review document: its reading, the issue's PDF beside it and how it was obtained."""
            (COLLECTION / f"{doc_id}.reading.json").write_text(
                json.dumps(reading, ensure_ascii=False, indent=1) + "\n"
            )
            target = COLLECTION / f"{doc_id}.pdf"
            if not target.exists():
                try:
                    os.link(pdf, target)
                except OSError:
                    shutil.copyfile(pdf, target)
            (COLLECTION / f"{doc_id}.provenance.json").write_text(
                json.dumps(
                    {
                        "url": record["url"],
                        "retrieved_at": sidecar["retrieved_at"],
                        "sha256": sha,
                        "bytes": sidecar["bytes"],
                        "issue": record["listed"]["name"],
                        "pages": pages,
                    },
                    ensure_ascii=False,
                    indent=1,
                )
                + "\n"
            )

        if record["source"] == "minijust-gazette":
            issue = parse_item("minijust-gazette", {"name": record["listed"]["name"], "url": record["url"]})
        else:
            # An RLRC file is an extract of the Gazette: its issue is in the running header.
            headers = Counter(
                text
                for texts in furniture.values()
                for text in texts
                if re.match(r"(?i)official gazette", text)
            )
            issue = parse_gazette(headers.most_common(1)[0][0]) if headers else {"parsed": False}
        sizes = {page["page"]: (page["width"], page["height"]) for page in split}
        found = rw_gazette.instruments(streams)
        for entry in found:
            lines = {lang: streams[lang][v["start"] : v["end"]] for lang, v in entry["versions"].items()}
            versions = {lang: rw_gazette.structure(lines[lang], lang) for lang in lines}
            check = rw_gazette.alignment(versions)
            rw_gazette.flag_differences(versions)
            if entry.get("also_printed_as"):
                # The same law printed with two numbers (over its contents and over its body).
                for nodes in versions.values():
                    if nodes:
                        nodes[0].setdefault("flags", []).append(
                            {
                                "severity": "high",
                                "kind": "number_differs",
                                "primary": entry["number"],
                                "others": {"Printed over the contents": entry["also_printed_as"]},
                            }
                        )
            values = rw_gazette.suggest(lines, entry, issue)
            number = issue.get("date", "") if entry["number"] == "constitution" else entry["number"]
            work = f"rw-{entry['kind'].replace('_', '-')}-{_slug(number)}"
            for language, nodes in versions.items():
                doc_id = f"{work}-{language}"
                reading = rw_gazette.reading(nodes, furniture, sha, {"alignment": check}, sizes, scanned)
                write_document(doc_id, reading, entry["versions"][language]["pages"])
                documents[doc_id] = {
                    "id": doc_id,
                    "type": entry["kind"],
                    "title": rw_gazette.sentence_case(entry["versions"][language]["title"]),
                    "year": int(values["identity.year"]) if values.get("identity.year") else None,
                    "jurisdiction": "rw",
                    "language": language,
                    "work": work,
                    "versions": {lang: f"{work}-{lang}" for lang in versions},
                    "url": record["url"],
                    "issue": {"name": record["listed"]["name"], **issue},
                    "pages": entry["versions"][language]["pages"],
                    "scanned_pages": sorted(
                        p
                        for p in scanned
                        if entry["versions"][language]["pages"][0]
                        <= p
                        <= entry["versions"][language]["pages"][1]
                    ),
                    "also_printed_as": entry.get("also_printed_as"),
                    "suggested": {
                        **values,
                        "publication.pages": "{}–{}".format(*entry["versions"][language]["pages"]),
                        "publication.url": record["url"],
                    },
                    "alignment": check,
                }
            summary.append(
                {
                    "work": work,
                    "languages": list(versions),
                    "articles": check["articles"],
                    "aligned": check["aligned"],
                }
            )
        for annex in rw_gazette.annexes(split, roles):
            parent = next(
                (e for e in found if annex["refers_to"] and e["number"] == annex["refers_to"]), None
            )
            parent = parent or next(
                (
                    e
                    for e in reversed(found)
                    if min(v["pages"][0] for v in e["versions"].values()) < annex["pages"][0]
                ),
                None,
            )
            if parent is None:
                continue
            number = issue.get("date", "") if parent["number"] == "constitution" else parent["number"]
            work = f"rw-{parent['kind'].replace('_', '-')}-{_slug(number)}"
            part = f"annex-{annex['annex']}"
            for language, lines in annex["versions"].items():
                doc_id = f"{work}-{part}-{language}"
                nodes = rw_gazette.structure(lines, language, part="body")
                reading = rw_gazette.reading(nodes, furniture, sha, {"annex_of": work}, sizes, scanned)
                write_document(doc_id, reading, [annex["pages"][0], annex["pages"][-1]])
                documents[doc_id] = {
                    "id": doc_id,
                    "type": parent["kind"],
                    "title": f"Annex: {annex['title'][:200]}",
                    "jurisdiction": "rw",
                    "language": language,
                    "work": work,
                    "part": part,
                    "versions": {lang: f"{work}-{part}-{lang}" for lang in annex["versions"]},
                    "url": record["url"],
                    "issue": {"name": record["listed"]["name"], **issue},
                    "pages": [annex["pages"][0], annex["pages"][-1]],
                    "scanned_pages": sorted(set(annex["pages"]) & scanned),
                }
            summary.append(
                {
                    "work": work,
                    "annex": part,
                    "languages": list(annex["versions"]),
                    "pages": [annex["pages"][0], annex["pages"][-1]],
                }
            )
        log(f"{record['listed']['name']}: {len(found)} instruments, {len(set(roles.values()))} annexes")
    manifest["documents"] = sorted(documents.values(), key=lambda d: d["id"])
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    return {"documents": len(manifest["documents"]), "instruments": summary}


def status() -> list[dict]:
    ledger = Ledger(STAGING / "sync-ledger.json")
    rows = []
    for source_id, source in SOURCES.items():
        state = ledger.source(source_id)
        rows.append(
            {
                "source": source_id,
                "cadence": source["cadence"],
                "last_checked_at": state.get("last_checked_at"),
                "outcome": state.get("last_check_outcome"),
                "last_changed_at": state.get("last_changed_at"),
                "last_fetched_at": state.get("last_fetched_at"),
                "next_due": state.get("next_due"),
                "due": ledger.due(source_id, source["cadence"]),
                "counts": state.get("counts"),
            }
        )
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingestion.rwanda")
    sub = parser.add_subparsers(dest="command", required=True)
    cat = sub.add_parser("catalogue", help="read listing pages and update the sync ledger (no downloads)")
    cat.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    cat.add_argument("--delay", type=float, default=4.0, help="seconds between requests (at least 4)")
    cat.add_argument("--max-pages", type=int, default=3000)
    cat.add_argument("--force", action="store_true", help="check even if not due")
    sub.add_parser("status", help="last check, last change and what is due, per source")
    again = sub.add_parser("reparse", help="read the stored catalogue's file names again (no network)")
    again.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    fetch = sub.add_parser("fetch", help="fetch the files of an operator-approved selection")
    fetch.add_argument("--selection", required=True, type=Path)
    fetch.add_argument("--keep-going", action="store_true", help="record a file that fails and go on")
    fetch.add_argument("--to-bucket", action="store_true", help="store each file in the corpus bucket")
    fetch.add_argument("--no-local", action="store_true", help="keep no copy here (with --to-bucket)")
    sub.add_parser("push-held", help="upload files held here to the corpus bucket, and mirror the state")
    bulk = sub.add_parser("bulk-selection", help="write a selection of every catalogued file not yet held")
    bulk.add_argument("--approved-by", required=True)
    bulk.add_argument("--purpose", required=True)
    bulk.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    bulk.add_argument("--out", required=True, type=Path)
    probe = sub.add_parser(
        "probe", help="measure the layout of a fetched selection (text layer, columns, languages)"
    )
    probe.add_argument("--selection", required=True, type=Path)
    prepare = sub.add_parser(
        "prepare", help="split fetched gazette issues into instruments and write readings for review"
    )
    prepare.add_argument("--selection", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "push-held":
        store = corpus_store.CorpusStore()
        print(json.dumps({"stored": push_held(store)}))
        mirror_state(store)
        return 0
    if args.command == "fetch":
        if args.no_local and not args.to_bucket:
            parser.error("--no-local needs --to-bucket")
        store = corpus_store.CorpusStore() if args.to_bucket else None
        try:
            results = fetch_selection(
                load_selection(args.selection),
                keep_going=args.keep_going,
                store=store,
                keep_local=not args.no_local,
                log=lambda message: print(message, flush=True),
            )
        except FetchStopped as stop:
            print(stop)
            return 3
        finally:
            if store is not None:
                mirror_state(store)
        failed = [r for r in results if r.get("error")]
        print(
            json.dumps(
                {"files": len(results), "fetched": sum(r["fetched"] for r in results), "failed": len(failed)}
            )
        )
        return 0
    if args.command == "bulk-selection":
        chosen = list(SOURCES) if args.source == "all" else [args.source]
        selection = bulk_selection(args.approved_by, args.purpose, chosen)
        args.out.write_text(json.dumps(selection, ensure_ascii=False, indent=1) + "\n")
        print(json.dumps({"items": len(selection["items"]), "out": str(args.out)}))
        return 0
    if args.command == "probe":
        probe_selection(load_selection(args.selection))
        return 0
    if args.command == "prepare":
        print(json.dumps(prepare_selection(load_selection(args.selection)), indent=1))
        return 0
    if args.command == "reparse":
        for source_id in list(SOURCES) if args.source == "all" else [args.source]:
            if (STAGING / "catalogue" / f"{source_id}.json").exists():
                print(json.dumps(reparse(source_id)))
        return 0
    if args.command == "status":
        for row in status():
            print(json.dumps(row))
        return 0
    if args.delay < 4:
        parser.error("--delay must be at least 4 seconds")
    ledger = Ledger(STAGING / "sync-ledger.json")
    chosen = list(SOURCES) if args.source == "all" else [args.source]
    for source_id in chosen:
        if not args.force and not ledger.due(source_id, SOURCES[source_id]["cadence"]):
            print(f"{source_id}: not due until {ledger.source(source_id).get('next_due')}")
            continue
        summary = catalogue(source_id, args.delay, args.max_pages, log=lambda m: print(m, flush=True))
        print(json.dumps(summary))
        if not summary["complete"]:
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
