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
import subprocess
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


# Waits, in seconds, before trying a file again when the network itself fails: about an hour in all.
NETWORK_WAITS = (30, 60, 120, 300, 600, 600, 600, 600, 600)


def _signed_out(error: Exception) -> bool:
    """The Google sign-in has expired: nothing can be stored until the operator signs in again."""
    import google.auth.exceptions

    return isinstance(error, google.auth.exceptions.RefreshError)


def _network_trouble(error: Exception) -> bool:
    """A failure of the connection (name lookup, connect, timeout, a server error from the bucket,
    the Google sign-in unreachable), not of the file: worth waiting out rather than skipping."""
    import google.auth.exceptions
    import httpx
    import requests

    if isinstance(
        error,
        httpx.TransportError
        | requests.exceptions.ConnectionError
        | requests.exceptions.Timeout
        | google.auth.exceptions.TransportError,
    ):
        return True
    if isinstance(error, requests.exceptions.HTTPError) and error.response is not None:
        return error.response.status_code >= 500
    return isinstance(error, OSError)


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def file_kind(data: bytes) -> tuple[str, str] | None:
    """What a fetched file is, by its bytes: a PDF (its header within the first kilobyte, as readers
    allow) or a Word document (a zip holding word/document.xml); anything else is not taken. A
    source's Word file is kept as it is: a better source than a PDF of it."""
    if b"%PDF-" in data[:1024]:
        return ".pdf", "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        import io
        import zipfile

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "word/document.xml" in archive.namelist():
                    return ".docx", DOCX
        except zipfile.BadZipFile:
            return None
    return None


def fetch_selection(
    selection: dict,
    delay: float = 4.0,
    log=print,
    keep_going: bool = False,
    store=None,
    keep_local: bool = True,
    sleep=time.sleep,
) -> list[dict]:
    """Fetch each selected file once: exact catalogue URLs on the allowed hosts, no redirects, the
    ingestion size limit, PDF only; stored by fingerprint with a provenance sidecar, ledger updated.

    With keep_going (a bulk run), a file that cannot be had (gone, not a PDF, larger than listed) is
    recorded and the run goes on; a refusal (401, 403, 429) still stops it at once, and so does free
    disk space falling below RESERVE_BYTES. When the network itself fails, the same file is tried
    again after a wait (NETWORK_WAITS, with a fresh connection), and after about an hour without a
    network the run stops rather than skipping everything. A run can be repeated: files already held
    are skipped."""
    import httpx

    def connect():
        return httpx.Client(timeout=120, headers={"User-Agent": filelist.user_agent()})

    ledger = Ledger(STAGING / "sync-ledger.json")
    results = []
    client = connect()
    try:
        for index, item_id in enumerate(selection["items"]):
            waits = 0
            while True:
                try:
                    results.append(
                        _fetch_one(
                            client, ledger, selection, item_id, index, delay, log, store, keep_local, sleep
                        )
                    )
                    break
                except FetchStopped:
                    raise
                except Exception as error:  # noqa: BLE001 - network waited out, file failures recorded
                    if _signed_out(error):
                        raise FetchStopped(
                            "stopped: the Google sign-in has expired; run `gcloud auth application-default login`"
                        ) from error
                    if _network_trouble(error):
                        if waits == len(NETWORK_WAITS):
                            raise FetchStopped(
                                "stopped: the network has been unavailable for about an hour"
                            ) from error
                        log(
                            f"network trouble ({type(error).__name__}); trying again in {NETWORK_WAITS[waits]} s"
                        )
                        sleep(NETWORK_WAITS[waits])
                        waits += 1
                        client.close()
                        client = connect()
                        continue
                    if not keep_going:
                        raise
                    ledger.data["items"].setdefault(item_id, {}).setdefault("history", []).append(
                        {"at": stamp(now()), "change": "fetch failed", "reason": str(error)[:300]}
                    )
                    ledger.save_items([item_id])
                    log(f"skipped {item_id}: {error}")
                    results.append({"item": item_id, "fetched": False, "error": str(error)[:300]})
                    break
    finally:
        client.close()
    return results


def _fetch_one(
    client, ledger, selection, item_id, index, delay, log, store=None, keep_local=True, sleep=time.sleep
) -> dict:
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
        sleep(delay)
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
    kind = file_kind(bytes(raw))
    if kind is None:
        raise ValueError(f"{item_id}: not a PDF or a Word document")
    suffix, media_type = kind
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
        (folder / f"{sha}{suffix}").write_bytes(raw)
        (folder / f"{sha}.json").write_text(sidecar)
    ledger.fetched(item_id, sha, len(raw))
    if suffix != ".pdf":
        ledger.data["items"][item_id]["format"] = suffix[1:]
    if store is not None:
        name = corpus_store.source_name(COUNTRY, record["source"], sha, suffix)
        store.put(name, bytes(raw), media_type, {"sha256": sha, "url": url})
        store.put(name[: -len(suffix)] + ".json", sidecar.encode(), "application/json")
        ledger.data["items"][item_id]["stored"] = store.uri(name)
    ledger.save_items([item_id])
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


def rlrc_index() -> tuple[dict, str | None]:
    """RLRC's listing by (kind, number, year): whether each law is filed among the laws in force or
    the repealed ones, and under which subject; and when the listing was read."""
    path = STAGING / "catalogue" / "rlrc-laws.json"
    if not path.exists():
        return {}, None
    catalogue = json.loads(path.read_text())
    index: dict = {}
    for item in catalogue["files"]:
        parsed = item["parsed"]
        number = re.match(r"0*(\d+)", parsed.get("number") or "")
        if (
            parsed.get("kind")
            and number
            and parsed.get("year")
            and parsed.get("status") in ("in_force", "repealed")
        ):
            index.setdefault((parsed["kind"], int(number.group(1)), parsed["year"]), []).append(item)
    return index, (catalogue.get("checked_at") or "")[:10] or None


def _subjects(folder: str) -> str:
    """'…/Laws_in_force/10._Business/10.16._Telecommunication/' -> 'Business › Telecommunication'."""
    tail = re.split(r"/(?:Laws_in_force|Repealed_laws)/", folder)[-1]
    names = [re.sub(r"^[\d.]+_*", "", part).replace("_", " ").strip() for part in tail.strip("/").split("/")]
    return " › ".join(name for name in names if name)


def rlrc_status(index: dict, checked: str | None, kind: str, number: str, year: str | None) -> dict:
    """Legal status and subjects suggested from RLRC's listing, where it files this instrument."""
    lead = re.match(r"0*(\d+)", number or "")
    if not (lead and year):
        return {}
    found = index.get((kind, int(lead.group(1)), int(year)), [])
    statuses = {item["parsed"]["status"] for item in found}
    if len(statuses) != 1:
        return {}  # not listed, or listed both ways: left for the reviewer
    status = statuses.pop()
    item = found[0]
    subject = _subjects(item["folder"])
    where = "the laws in force" if status == "in_force" else "the repealed laws"
    return {
        "status.legal": status,
        "status.as_of": checked,
        "status.note": f"RLRC's Laws of Rwanda files it among {where}"
        + (f", under {item['parsed'].get('subject_code', '')} {subject}" if subject else "")
        + f" (listing read {checked}).",
        "classification.subjects": [subject] if subject else [],
    }


# What the review list needs of a document stays in the manifest; what only its own page needs (the
# details suggested from the text, the article check, the issue) goes in <id>.document.json, so the
# manifest of tens of thousands of documents stays small enough to load at once.
DETAILS = ("suggested", "suggested_absent", "alignment", "issue", "also_printed_as")


def split_entry(document: dict) -> tuple[dict, dict]:
    """A document's manifest entry (with its page and flag counts) and its details."""
    entry = {k: v for k, v in document.items() if k not in DETAILS and k != "summary"}
    summary = document.get("summary")
    if summary:
        entry["page_count"] = summary["pages"]
        entry["flag_counts"] = summary["flags"]
    return entry, {k: document[k] for k in DETAILS if document.get(k) is not None}


def work_id(kind: str, number: str, year: str | None) -> str:
    """A work's id: its kind and number, and its year where the number does not carry one (an
    order's "N° 002/01" is numbered again every year)."""
    if year and not re.search(r"(?:19|20)\d\d", number):
        number = f"{number}-{year}"
    return f"rw-{kind.replace('_', '-')}-{_slug(number)}"


# Where the same document comes from several files, the one reviewed: the Official Gazette's
# printing before a copy, then the copy with fewer scanned pages and fewer high flags.
SOURCE_RANK = {"minijust-gazette": 0, "rlrc-laws": 1}


def one_per_id(documents: list[dict]) -> list[dict]:
    """The manifest's documents, one per id, the others it was found in recorded on it."""
    groups: dict[str, list[dict]] = {}
    for document in documents:
        groups.setdefault(document["id"], []).append(document)

    def rank(document: dict) -> tuple:
        source = (document.get("source_object") or "").split("/")[2:3]
        return (
            SOURCE_RANK.get(source[0] if source else "", 9),
            len(document.get("scanned_pages") or []),
            (document.get("flag_counts") or {}).get("high", 0),
            document.get("url") or "",
        )

    out = []
    for doc_id in sorted(groups):
        chosen, *others = sorted(groups[doc_id], key=rank)
        if others:
            chosen = {
                **chosen,
                "other_sources": [{k: d.get(k) for k in ("url", "source_object", "objects")} for d in others],
            }
        out.append(chosen)
    return out


class LocalCollection:
    """Review documents in a folder here (the samples): readings, provenance, the issue's PDF beside them."""

    def __init__(self, folder: Path | None = None):
        self.folder = folder or COLLECTION
        self.folder.mkdir(parents=True, exist_ok=True)

    def manifest(self) -> dict:
        path = self.folder / "manifest.json"
        return json.loads(path.read_text()) if path.exists() else new_manifest()

    def done(self, sha: str) -> bool:
        return False  # the samples are always read afresh

    def remove(self, doc_id: str) -> None:
        for suffix in (".reading.json", ".pdf", ".provenance.json", ".document.json"):
            (self.folder / f"{doc_id}{suffix}").unlink(missing_ok=True)

    def write(self, doc_id: str, reading: dict, provenance: dict, pdf: Path) -> None:
        import os
        import shutil

        (self.folder / f"{doc_id}.reading.json").write_text(
            json.dumps(reading, ensure_ascii=False, indent=1) + "\n"
        )
        target = self.folder / f"{doc_id}.pdf"
        if not target.exists():
            try:
                os.link(pdf, target)
            except OSError:
                shutil.copyfile(pdf, target)
        (self.folder / f"{doc_id}.provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=1) + "\n"
        )

    def finish_issue(self, sha: str, documents: list[dict], readings: dict | None = None) -> None:
        pass

    def save_manifest(self, manifest: dict) -> None:
        light = []
        for document in manifest["documents"]:
            entry, details = split_entry(document)
            if details:
                (self.folder / f"{document['id']}.document.json").write_text(
                    json.dumps(details, ensure_ascii=False, indent=1) + "\n"
                )
            light.append(entry)
        manifest = {**manifest, "documents": light}
        (self.folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")


class BucketCollection:
    """Review documents in the corpus bucket, under collections/<country>/: each document's reading
    and provenance, an index of each issue's documents (so a run resumes issue by issue), and the
    manifest assembled from those indexes. No PDF is copied: a document names its source object."""

    def __init__(self, store, prefix: str = f"collections/{COUNTRY}/"):
        self.store, self.prefix = store, prefix

    def manifest(self) -> dict:
        return new_manifest()

    def done(self, sha: str) -> bool:
        """Prepared by this version of the pipeline with the readings there are now: an index from an
        older pipeline is redone, and so is a scan prepared before its OCR or Layout Parser reading."""
        name = f"{self.prefix}issues/{sha}.json"
        if not self.store.exists(name):
            return False
        index = json.loads(self.store.get(name))
        if not isinstance(index, dict) or index.get("pipeline") != rw_gazette.PIPELINE:
            return False
        used = index.get("readings") or {}
        return all(
            used.get(folder) or not self.store.exists(f"readings/{COUNTRY}/{folder}/{sha}.json")
            for folder in ("ocr", "docai")
        )

    def remove(self, doc_id: str) -> None:
        pass  # a document read again is written over

    def write(self, doc_id: str, reading: dict, provenance: dict, pdf: Path) -> None:
        # Under the source file's own folder: the same law printed in a Gazette issue and held by
        # RLRC gives the same document id, and neither may overwrite the other.
        folder = f"{self.prefix}files/{provenance['sha256']}/"
        self.store.put(
            f"{folder}{doc_id}.reading.json",
            json.dumps(reading, ensure_ascii=False).encode(),
            "application/json",
            replace=True,
        )
        self.store.put(
            f"{folder}{doc_id}.provenance.json",
            json.dumps(provenance, ensure_ascii=False).encode(),
            "application/json",
            replace=True,
        )

    def finish_issue(self, sha: str, documents: list[dict], readings: dict | None = None) -> None:
        light = []
        for document in documents:
            entry, details = split_entry(document)
            entry["objects"] = f"files/{sha}/"
            self.store.put(
                f"{self.prefix}files/{sha}/{document['id']}.document.json",
                json.dumps(details, ensure_ascii=False).encode(),
                "application/json",
                replace=True,
            )
            light.append(entry)
        index = {"pipeline": rw_gazette.PIPELINE, "readings": readings or {}, "documents": light}
        self.store.put(
            f"{self.prefix}issues/{sha}.json",
            json.dumps(index, ensure_ascii=False).encode(),
            "application/json",
            replace=True,
        )

    def save_manifest(self, manifest: dict) -> None:
        documents = []
        for name in self.store.list(f"{self.prefix}issues/"):
            index = json.loads(self.store.get(name))
            documents += index["documents"] if isinstance(index, dict) else index  # older: a bare list
        manifest["documents"] = one_per_id(documents)
        self.store.put(
            f"{self.prefix}manifest.json",
            json.dumps(manifest, ensure_ascii=False).encode(),
            "application/json",
            replace=True,
        )


def new_manifest() -> dict:
    return {
        "collection": "rw",
        "jurisdiction": "rw",
        "description": "Rwanda: laws and orders from the Official Gazette and the RLRC's Laws of Rwanda",
        "rights_basis": RIGHTS_BASIS,
        "documents": [],
    }


def prepare_selection(
    selection: dict, log=print, collection=None, store=None, skip_done: bool = False, save: bool = True
) -> dict:
    """Each fetched gazette issue of a selection split into its instruments, and each instrument's
    language versions written as readings the workbench reviews, with the details read from the
    text and the check of its articles across the three languages: here (LocalCollection, the issue's
    PDF beside them) or in the corpus bucket (BucketCollection, read from and written to it)."""
    import tempfile

    ledger = Ledger(STAGING / "sync-ledger.json")
    collection = collection or LocalCollection()
    manifest = collection.manifest()
    scratch = tempfile.TemporaryDirectory(dir=STAGING)
    documents = {d["id"]: d for d in manifest["documents"]}
    summary = []
    for item_id in selection["items"]:
        record = ledger.data["items"][item_id]
        if record["source"] not in ("minijust-gazette", "rlrc-laws") or not record.get("sha256"):
            continue
        sha = record["sha256"]
        if record.get("format", "pdf") != "pdf":
            log(f"not prepared (a Word document; not yet read by this pipeline): {record['listed']['name']}")
            continue
        if skip_done and collection.done(sha):
            continue
        # This issue is read afresh: what an earlier reading of it produced is replaced.
        for stale in [k for k, d in documents.items() if d.get("url") == record["url"]]:
            collection.remove(stale)
            del documents[stale]
        source_object = corpus_store.source_name(COUNTRY, record["source"], sha)
        pdf = STAGING / "sources" / record["source"] / f"{sha}.pdf"
        if pdf.exists():
            sidecar = json.loads(pdf.with_suffix(".json").read_text())
        elif store is not None and record.get("stored"):
            pdf = Path(scratch.name) / "issue.pdf"
            pdf.write_bytes(store.get(source_object))
            sidecar = json.loads(store.get(source_object[: -len(".pdf")] + ".json"))
        else:
            continue
        words = gazette_layout.text_layer(pdf)
        # Scanned pages our OCR has read: its words and their places, in place of the scan's own
        # text layer (often none, or someone else's OCR), laid out like any other page.
        ocr_pages: set[int] = set()
        ocr = stored_reading("ocr", sha, store)
        for number, page in (ocr or {}).get("pages", {}).items():
            if int(number) <= len(words):
                words[int(number) - 1] = gazette_layout.from_ocr(page, words[int(number) - 1])
                ocr_pages.add(int(number))
        layouts = [len(gazette_layout.columns(page)) for page in words]
        split = rw_gazette.split_issue(words, ruled_tables.document_tables(pdf, words, layouts))
        scanned = ruled_tables.image_pages(pdf)
        # Scanned pages the Layout Parser has read: its words checked against the OCR's, in the
        # OCR's layout; where they differ, its words are taken and the difference flagged.
        layout = stored_reading("docai", sha, store)
        if layout:
            readings = layout["pages"]
            split = [
                rw_gazette.correct_page(page, readings[str(page["page"])])
                if str(page["page"]) in readings
                else page
                for page in split
            ]
        read_pages = {page["page"] for page in split if page.get("read_by") == "docai"}
        one_reading = (ocr_pages & scanned) - read_pages
        scanned = scanned - read_pages - ocr_pages
        roles = rw_gazette.annex_pages(split)
        streams = rw_gazette.streams(split, skip=set(roles))
        furniture = {page["page"]: [line["text"] for line in page["furniture"]] for page in split}

        def write_document(
            doc_id,
            reading,
            pages,
            record=record,
            sidecar=sidecar,
            pdf=pdf,
            sha=sha,
            source_object=source_object,
        ):
            """A review document: its reading, and how the issue it comes from was obtained."""
            collection.write(
                doc_id,
                reading,
                {
                    "url": record["url"],
                    "retrieved_at": sidecar["retrieved_at"],
                    "sha256": sha,
                    "bytes": sidecar["bytes"],
                    "issue": record["listed"]["name"],
                    "pages": pages,
                    "source_object": source_object,
                },
                pdf,
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
        if not found and record["source"] == "rlrc-laws":
            # An RLRC file is one text; where its heading is not one the openings know (an older
            # law, a treaty), the whole file is that text, named as RLRC lists it.
            listed = parse_item(record["source"], {**record["listed"], "url": record["url"]})
            found = rw_gazette.whole_file(
                streams,
                kind=listed.get("kind") or "other",
                # Named by its year alone, many files would share one name: the file's own fingerprint
                # tells them apart.
                number=listed.get("number")
                or (f"{listed['year']}-{sha[:6]}" if listed.get("year") else sha[:12]),
                date=listed.get("date"),
                title=listing_title(record["listed"]["name"]),
            )
        rlrc, rlrc_checked = rlrc_index()
        for entry in found:
            lines = {lang: streams[lang][v["start"] : v["end"]] for lang, v in entry["versions"].items()}
            versions = {lang: rw_gazette.structure(lines[lang], lang) for lang in lines}
            check = rw_gazette.alignment(versions)
            rw_gazette.flag_differences(versions)
            if entry.get("title_from_listing"):
                for nodes in versions.values():
                    if nodes:
                        nodes[0].setdefault("flags", []).append(
                            {
                                "severity": "medium",
                                "kind": "title_from_listing",
                                "primary": entry["versions"][next(iter(entry["versions"]))]["title"],
                                "others": {},
                            }
                        )
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
            values |= {
                k: v
                for k, v in rlrc_status(
                    rlrc, rlrc_checked, entry["kind"], entry["number"], (entry.get("date") or "")[:4] or None
                ).items()
                if v
            }
            # A law the Senate did not sit on (its preamble names only the Chamber of Deputies).
            absent = (
                {"step:adopted:senate.date": "not_applicable"}
                if entry["kind"] == "law"
                and values.get("step:adopted:deputies.date")
                and not values.get("step:adopted:senate.date")
                else {}
            )
            number = issue.get("date", "") if entry["number"] == "constitution" else entry["number"]
            work = work_id(entry["kind"], number, (entry.get("date") or issue.get("date") or "")[:4])
            for language, nodes in versions.items():
                doc_id = f"{work}-{language}"
                reading = rw_gazette.reading(
                    nodes,
                    furniture,
                    sha,
                    {
                        "alignment": check,
                        "scans_read_by_ocr": sorted(ocr_pages),
                        "scans_read_by_docai": sorted(read_pages),
                    },
                    sizes,
                    scanned,
                    one_reading,
                )
                write_document(doc_id, reading, entry["versions"][language]["pages"])
                documents[doc_id] = {
                    "id": doc_id,
                    "summary": reading["summary"],
                    "type": entry["kind"],
                    "title": rw_gazette.sentence_case(entry["versions"][language]["title"]),
                    "year": int(values["identity.year"]) if values.get("identity.year") else None,
                    "jurisdiction": "rw",
                    "language": language,
                    "work": work,
                    "versions": {lang: f"{work}-{lang}" for lang in versions},
                    "url": record["url"],
                    "source_object": source_object,
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
                    "suggested_absent": absent,
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
            work = work_id(parent["kind"], number, (parent.get("date") or issue.get("date") or "")[:4])
            part = f"annex-{annex['annex']}"
            for language, lines in annex["versions"].items():
                doc_id = f"{work}-{part}-{language}"
                nodes = rw_gazette.structure(lines, language, part="body")
                reading = rw_gazette.reading(
                    nodes, furniture, sha, {"annex_of": work}, sizes, scanned, one_reading
                )
                write_document(doc_id, reading, [annex["pages"][0], annex["pages"][-1]])
                documents[doc_id] = {
                    "id": doc_id,
                    "summary": reading["summary"],
                    "type": parent["kind"],
                    "title": f"Annex: {annex['title'][:200]}",
                    "jurisdiction": "rw",
                    "language": language,
                    "work": work,
                    "part": part,
                    "versions": {lang: f"{work}-{part}-{lang}" for lang in annex["versions"]},
                    "url": record["url"],
                    "source_object": source_object,
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
        collection.finish_issue(
            sha,
            [d for d in documents.values() if d.get("url") == record["url"]],
            {"ocr": bool(ocr), "docai": bool(layout)},
        )
        if pdf.parent == Path(scratch.name):
            pdf.unlink(missing_ok=True)
        log(f"{record['listed']['name']}: {len(found)} instruments, {len(set(roles.values()))} annexes")
    scratch.cleanup()
    manifest["documents"] = sorted(documents.values(), key=lambda d: d["id"])
    if save:  # a cloud task prepares file by file and assembles the manifest once, at its end
        collection.save_manifest(manifest)
    return {"documents": len(manifest["documents"]), "instruments": summary}


def listing_title(name: str) -> str:
    """A title from a source's file name: "10.4.3.1.__LOI_DU_19_FEVRIER_1964_RELATIVE_AU_SOINS_MEDICAUX
    -J.O._N0_5-1.3.1964.pdf" -> "LOI DU 19 FEVRIER 1964 RELATIVE AU SOINS MEDICAUX-J.O. N0 5-1.3.1964"."""
    title = re.sub(r"\.(pdf|docx?)$", "", name, flags=re.I)
    title = re.sub(r"^\d+(?:\.\d+)*\.?_*", "", title)  # RLRC's subject code
    return re.sub(r"\s+", " ", title.replace("_", " ")).strip()


def stored_reading(folder: str, sha: str, store=None) -> dict | None:
    """A file's reading by one of the services (folder "ocr" or "docai"), here or in the bucket."""
    local = STAGING / folder / f"{sha}.json"
    if local.exists():
        return json.loads(local.read_text())
    remote = f"readings/{COUNTRY}/{folder}/{sha}.json"
    if store is not None and store.exists(remote):
        return json.loads(store.get(remote))
    return None


def scanned_in_survey() -> dict[str, int]:
    """The ledger items whose file the survey found scanned pages in, with how many: one item per
    file (a file listed in several folders or by both sources is read once)."""
    found = {}
    for path in sorted((STAGING / "survey").glob("*.json")):
        row = json.loads(path.read_text())
        if row.get("scanned_pages"):
            found[row["item"]] = row["scanned_pages"]
    return found


WORKLIST = f"work/{COUNTRY}/scan-reading.json"


def scan_worklist(approval: dict) -> dict:
    """The files whose scanned pages the cloud job reads (scan_job): every file the survey found
    scans in, once each (a file in several folders or both sources is read once), largest first so
    that the job's tasks share the pages evenly, with the operator's approval of the budget."""
    ledger = Ledger(STAGING / "sync-ledger.json")
    files, seen = [], set()
    for item_id, pages in scanned_in_survey().items():
        record = ledger.data["items"].get(item_id, {})
        sha = record.get("sha256")
        if not sha or sha in seen or not record.get("stored") or record.get("format", "pdf") != "pdf":
            continue
        seen.add(sha)
        files.append(
            {
                "item": item_id,
                "source": record["source"],
                "sha256": sha,
                "name": record["listed"]["name"],
                "scanned_pages": pages,
            }
        )
    files.sort(key=lambda f: (-f["scanned_pages"], f["sha256"]))
    return {
        "made_at": stamp(now()),
        "approval": approval,
        "pages": sum(f["scanned_pages"] for f in files),
        "files": files,
    }


def scan_progress(store) -> dict:
    """How far the cloud reading has come: per reader, the work list's files and pages read so far."""
    worklist = json.loads(store.get(WORKLIST))
    out = {"files": len(worklist["files"]), "pages": worklist["pages"]}
    for kind, folder in (("ocr", "ocr"), ("layout", "docai")):
        done = {
            name.rsplit("/", 1)[-1][: -len(".json")] for name in store.list(f"readings/{COUNTRY}/{folder}/")
        }
        read = [f for f in worklist["files"] if f["sha256"] in done]
        out[kind] = {"files": len(read), "pages": sum(f["scanned_pages"] for f in read)}
    return out


def read_scans(
    selection: dict | None = None,
    log=print,
    store=None,
    max_pages: int | None = None,
    files_at_once: int = 1,
    reader=None,
    kind: str = "layout",
    again: bool = False,
    worklist: list[dict] | None = None,
) -> dict:
    """Every scanned page of the selection's files (or, without a selection, of every file the survey
    found scans in) read by Document AI (billed): the pages are put in one PDF per file, read, and
    the reading kept per page in <kind>/<sha256>.json and in the corpus bucket (readings/rw/<kind>/).
    A file already read is not read again, unless `again` (a selection's files only: a reader
    improved since). In the cloud (scan_job) the files come as a `worklist` instead: each with its
    item, source, fingerprint, name and scanned pages, so no ledger or survey is needed there.

    Two readings, `kind`: "ocr" (Enterprise Document OCR: every word with its place, from which the
    page is laid out as a text layer would be) and "layout" (the Layout Parser: the words and
    structure that check the OCR's words; its folder is docai/).

    `max_pages` is the approved budget: no file is started that would take the pages read past it.
    Several files are read at once (each in short requests side by side). A failed file is noted
    and the run goes on; an expired sign-in, a refusal (billing, permission) or five failures in a
    row stop it, and a repeated run resumes."""
    import tempfile
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    from io import BytesIO

    import google.api_core.exceptions
    from pypdf import PdfReader, PdfWriter

    from ingestion import readers

    folder = {"layout": "docai", "ocr": "ocr"}[kind]
    if reader is None:
        reader = readers.DocAIReader() if kind == "layout" else readers.DocOCRReader()
        reader.preflight()
    out_dir = STAGING / folder
    out_dir.mkdir(parents=True, exist_ok=True)
    if worklist is not None:
        records = {
            w["item"]: {
                "source": w["source"],
                "sha256": w["sha256"],
                "stored": True,
                "listed": {"name": w["name"]},
            }
            for w in worklist
        }
        expected = {w["item"]: w["scanned_pages"] for w in worklist}
    else:
        records = Ledger(STAGING / "sync-ledger.json").data["items"]
        expected = scanned_in_survey() if selection is None else dict.fromkeys(selection["items"], 0)
    already = set()
    if store is not None:
        already = {
            name.rsplit("/", 1)[-1][: -len(".json")] for name in store.list(f"readings/{COUNTRY}/{folder}/")
        }
    queue, seen = [], set()
    for item_id, pages in expected.items():
        record = records.get(item_id, {})
        sha = record.get("sha256")
        if not sha or sha in seen or record.get("format", "pdf") != "pdf":
            continue
        seen.add(sha)
        if not again and (sha in already or (out_dir / f"{sha}.json").exists()):
            continue
        queue.append((item_id, record, sha, pages))

    def read_one(record: dict, sha: str) -> int:
        with tempfile.TemporaryDirectory(dir=STAGING) as scratch:
            pdf = STAGING / "sources" / record["source"] / f"{sha}.pdf"
            if not pdf.exists():
                if store is None or not record.get("stored"):
                    return 0
                pdf = Path(scratch) / "file.pdf"
                pdf.write_bytes(store.get(corpus_store.source_name(COUNTRY, record["source"], sha)))
            scanned = sorted(ruled_tables.image_pages(pdf))
            if not scanned:
                return 0
            source = PdfReader(str(pdf))
            writer = PdfWriter()
            for number in scanned:
                writer.add_page(source.pages[number - 1])
            buffer = BytesIO()
            writer.write(buffer)
            part = Path(scratch) / "scanned.pdf"
            part.write_bytes(buffer.getvalue())
            reading = reader.read(part)
        kept = {
            "sha256": sha,
            "reader": kind,
            "version": reader.version(),
            "read_at": stamp(now()),
            "pages": {
                str(number): {k: v for k, v in page.items() if k != "page"}
                for number, page in zip(scanned, reading.pages, strict=True)
            },
            # The service's own layout, kept so a converter fix needs no new (billed) reading.
            **({"layout": reading.raw["layout"]} if getattr(reading, "raw", {}).get("layout") else {}),
        }
        target = out_dir / f"{sha}.json"
        target.write_text(json.dumps(kept, ensure_ascii=False) + "\n")
        if store is not None:
            store.put(
                f"readings/{COUNTRY}/{folder}/{sha}.json",
                target.read_bytes(),
                "application/json",
                replace=True,
            )
        return len(scanned)

    stopping = (
        google.api_core.exceptions.PermissionDenied,
        google.api_core.exceptions.Unauthenticated,
        google.api_core.exceptions.Forbidden,
    )
    pages_read = reserved = in_a_row = 0
    failed, stop = [], None
    with ThreadPoolExecutor(max_workers=max(files_at_once, 1)) as pool:
        running = {}
        while (queue and stop is None) or running:
            while queue and stop is None and len(running) < max(files_at_once, 1):
                item_id, record, sha, pages = queue[0]
                if max_pages is not None and reserved + pages > max_pages:
                    stop = f"stopped at the approved budget of {max_pages} pages"
                    break
                queue.pop(0)
                reserved += pages
                running[pool.submit(read_one, record, sha)] = (item_id, record, pages)
            if not running:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                item_id, record, pages = running.pop(future)
                name = record["listed"]["name"][:60]
                try:
                    read = future.result()
                except Exception as error:  # noqa: BLE001 - a failed file is noted; a refusal stops the run
                    reserved -= pages
                    if _signed_out(error):
                        stop = "stopped: the Google sign-in has expired; run `gcloud auth application-default login`"
                    elif isinstance(error, stopping):
                        stop = f"stopped: Document AI refused ({type(error).__name__}: {str(error)[:200]})"
                    else:
                        failed.append(
                            {"item": item_id, "error": f"{type(error).__name__}: {str(error)[:300]}"}
                        )
                        in_a_row += 1
                        log(f"failed {name}: {failed[-1]['error']}")
                        if in_a_row >= 5:
                            stop = "stopped: five files in a row failed"
                    continue
                in_a_row = 0
                reserved += read - pages
                pages_read += read
                log(f"{name}: {read} scanned pages read ({pages_read} in all)")
    if stop:
        log(stop)
    return {"pages_read": pages_read, "failed": failed, "left": len(queue), "stopped": stop}


def survey_file(pdf: Path) -> dict:
    """What one file holds, measured as the pipeline would read it (free: no OCR service)."""
    from pypdf import PdfReader

    words = gazette_layout.text_layer(pdf)
    layouts = [len(gazette_layout.columns(page)) for page in words]
    tables = ruled_tables.document_tables(pdf, words, layouts)
    split = rw_gazette.split_issue(words, tables)
    scanned = ruled_tables.image_pages(pdf)
    roles = rw_gazette.annex_pages(split)
    found = rw_gazette.instruments(rw_gazette.streams(split, skip=set(roles)))
    return {
        "pages": len(PdfReader(str(pdf)).pages),
        "scanned_pages": len(scanned),
        "text_pages": sum(1 for page in words if len(page["words"]) >= 20),
        "layouts": dict(Counter(page["layout"] for page in split)),
        "tables": sum(len(t) for t in tables.values()),
        "annex_pages": len(roles),
        "instruments": dict(Counter(entry["kind"] for entry in found)),
        "in_three_languages": sum(1 for entry in found if len(entry["versions"]) == 3),
    }


def survey(store=None, limit: int | None = None, log=print, sleep=time.sleep) -> dict:
    """Measure every stored file (read from the corpus bucket into a temporary file), keeping one
    result per file in survey/<sha256>.json, so the survey can be stopped and resumed; then sum."""
    import tempfile

    ledger = Ledger(STAGING / "sync-ledger.json")
    out_dir = STAGING / "survey"
    out_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    for item_id, record in ledger.data["items"].items():
        sha = record.get("sha256")
        target = out_dir / f"{sha}.json"
        if not sha or target.exists() or record.get("format", "pdf") != "pdf":
            continue  # a Word document is not measured here: it has no pages to scan
        if limit is not None and done >= limit:
            break
        local = STAGING / "sources" / record["source"] / f"{sha}.pdf"
        if not local.exists() and not (record.get("stored") and store is not None):
            continue
        waits = 0
        while True:
            try:
                if local.exists():
                    result = survey_file(local)
                else:
                    with tempfile.TemporaryDirectory(dir=STAGING) as scratch:
                        pdf = Path(scratch) / "file.pdf"
                        pdf.write_bytes(store.get(corpus_store.source_name(COUNTRY, record["source"], sha)))
                        result = survey_file(pdf)
                break
            except Exception as error:  # noqa: BLE001 - the network waited out; a bad file noted
                if _signed_out(error):
                    raise FetchStopped(
                        "stopped: the Google sign-in has expired; run `gcloud auth application-default login`"
                    ) from error
                if _network_trouble(error) and not isinstance(
                    error, FileNotFoundError | subprocess.CalledProcessError
                ):
                    if waits == len(NETWORK_WAITS):
                        raise FetchStopped(
                            "stopped: the network has been unavailable for about an hour"
                        ) from error
                    log(f"network trouble ({type(error).__name__}); trying again in {NETWORK_WAITS[waits]} s")
                    sleep(NETWORK_WAITS[waits])
                    waits += 1
                    continue
                result = {"error": f"{type(error).__name__}: {str(error)[:200]}"}
                break
        parsed = parse_item(record["source"], {**record["listed"], "url": record["url"]})
        result |= {
            "item": item_id,
            "source": record["source"],
            "name": record["listed"]["name"],
            "year": (parsed.get("date") or "")[:4]
            or (str(parsed.get("year")) if parsed.get("year") else None),
            "bytes": record.get("bytes"),
        }
        target.write_text(json.dumps(result, ensure_ascii=False) + "\n")
        done += 1
        log(
            f"{done:5d} {record['listed']['name'][:60]}: {result.get('pages')} pages, {result.get('scanned_pages')} scanned"
        )
    return survey_summary()


def survey_summary(price_per_1000_pages: float = 10.0) -> dict:
    """The survey summed by source and year, with the Document AI pages it implies (the scanned
    ones) at the stated price per 1,000 pages (an assumption to confirm in billing)."""
    rows = [json.loads(p.read_text()) for p in sorted((STAGING / "survey").glob("*.json"))]
    total: dict = {}
    fields = ("pages", "scanned_pages", "text_pages", "tables", "annex_pages", "in_three_languages")
    for row in rows:
        if "error" in row:
            continue
        for key in (row["source"], f"{row['source']} {row.get('year') or 'undated'}", "all"):
            bucket = total.setdefault(key, {"files": 0, **{f: 0 for f in fields}, "instruments": Counter()})
            bucket["files"] += 1
            for field in fields:
                bucket[field] += row.get(field) or 0
            bucket["instruments"].update(row.get("instruments") or {})
    for bucket in total.values():
        bucket["instruments"] = dict(bucket["instruments"])
        bucket["document_ai_cost_usd"] = round(bucket["scanned_pages"] * price_per_1000_pages / 1000, 2)
    summary = {
        "files_surveyed": len(rows),
        "errors": sum(1 for row in rows if "error" in row),
        "price_per_1000_pages": price_per_1000_pages,
        "by": dict(sorted(total.items())),
    }
    (STAGING / "survey-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    return summary


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
    measure = sub.add_parser(
        "survey", help="measure every stored file (pages, scans, tables, instruments); free"
    )
    measure.add_argument("--limit", type=int)
    scans = sub.add_parser(
        "read-scans", help="read the scanned pages of fetched issues with Document AI (billed)"
    )
    scans.add_argument(
        "--selection", type=Path, help="these files (default: every file the survey found scans in)"
    )
    scans.add_argument("--max-pages", type=int, required=True, help="the approved budget, in pages")
    scans.add_argument("--files-at-once", type=int, default=4)
    scans.add_argument("--reader", choices=["ocr", "layout"], required=True)
    scans.add_argument("--again", action="store_true", help="read the selection's files again (billed)")
    sub.add_parser("scan-progress", help="how far the cloud job has read the work list")
    work = sub.add_parser("scan-worklist", help="write the cloud job's list of files with scanned pages")
    work.add_argument("--approved-by", required=True)
    work.add_argument(
        "--max-pages", type=int, required=True, help="the approved budget, in pages, per reader"
    )
    work.add_argument("--basis", required=True, help="where and when the budget was approved")
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
    prepare.add_argument("--selection", type=Path, help="these files (samples: written here)")
    prepare.add_argument("--all", action="store_true", help="every file stored in the corpus bucket")
    prepare.add_argument("--to-bucket", action="store_true", help="write to collections/rw/ in the bucket")
    prepare.add_argument("--limit", type=int, help="at most this many issues (a trial)")
    args = parser.parse_args(argv)
    if args.command == "survey":
        summary = survey(
            corpus_store.CorpusStore(), args.limit, log=lambda message: print(message, flush=True)
        )
        print(json.dumps(summary["by"].get("all", {}), ensure_ascii=False))
        return 0
    if args.command == "scan-progress":
        print(json.dumps(scan_progress(corpus_store.CorpusStore())))
        return 0
    if args.command == "scan-worklist":
        approval = {"approved_by": args.approved_by, "max_pages": args.max_pages, "basis": args.basis}
        worklist = scan_worklist(approval)
        if worklist["pages"] > args.max_pages:
            parser.error(f"{worklist['pages']} scanned pages exceed the approved {args.max_pages}")
        corpus_store.CorpusStore().put(
            WORKLIST, json.dumps(worklist, ensure_ascii=False).encode(), "application/json", replace=True
        )
        print(json.dumps({"files": len(worklist["files"]), "pages": worklist["pages"], "object": WORKLIST}))
        return 0
    if args.command == "read-scans":
        if args.again and not args.selection:
            parser.error("--again reads a selection's files again, not the whole archive")
        result = read_scans(
            load_selection(args.selection) if args.selection else None,
            log=lambda message: print(message, flush=True),
            store=corpus_store.CorpusStore(),
            max_pages=args.max_pages,
            files_at_once=args.files_at_once,
            kind=args.reader,
            again=args.again,
        )
        print(json.dumps(result))
        return 1 if result["stopped"] and "budget" not in result["stopped"] else 0
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
        if args.all == bool(args.selection):
            parser.error("give --selection or --all")
        # Files and readings not kept here are read from the bucket, whichever collection is written.
        store = corpus_store.CorpusStore()
        if args.all:
            ledger = Ledger(STAGING / "sync-ledger.json")
            items = sorted(i for i, r in ledger.data["items"].items() if r.get("stored"))
            selection = {"items": items[: args.limit] if args.limit else items}
        else:
            selection = load_selection(args.selection)
        collection = BucketCollection(store) if args.to_bucket else LocalCollection()
        result = prepare_selection(
            selection,
            log=lambda m: print(m, flush=True),
            collection=collection,
            store=store,
            skip_done=args.to_bucket,
        )
        print(json.dumps({"documents": result["documents"], "instruments": len(result["instruments"])}))
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
