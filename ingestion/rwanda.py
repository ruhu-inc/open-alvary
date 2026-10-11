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
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from ingestion import filelist, gazette_layout
from ingestion.pipeline import MAX_BYTES
from ingestion.sync import Ledger, now, stamp

STAGING = Path(os.environ.get("ALVARY_RW_STAGING", "private-staging/rw"))

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
    "law": "law",
    "po": "presidential_order",
    "pmo": "prime_ministers_order",
    "mo": "ministerial_order",
    "reg": "regulation",
    "regul": "regulation",
    "regulation": "regulation",
    "re": "practice_direction",
    "dl": "decree_law",
    "decree": "decree",
    "ordonnance": "ordinance",
    "instructions": "instructions",
    "instruction": "instructions",
}
INSTRUMENT = re.compile(
    r"\b(OL|Law|PO|PMO|MO|REG|Regul|Regulation|RE|D\s?L|Decree|Ordonnance|Instructions?)\b\.?\s*"
    r"(?:n0|no|nº|n°|n)\.?\s*([0-9][0-9A-Za-z./-]*)",
    re.I,
)
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
    if instrument:
        out["kind"] = KINDS.get(instrument.group(1).lower().replace(" ", ""), instrument.group(1).lower())
        out["number"] = instrument.group(2).strip(".-")
        if re.search(r"[./-]OL$", out["number"], re.I):  # "01-2012-OL", "001/2026.OL": an organic law
            out["kind"] = "organic_law"
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
    """Read the stored catalogue's names again with the current rules, without visiting the site."""
    path = STAGING / "catalogue" / f"{source_id}.json"
    data = json.loads(path.read_text())
    for item in data["files"]:
        item["parsed"] = parse_item(source_id, item)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    return {
        "source": source_id,
        "files": len(data["files"]),
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


def fetch_selection(selection: dict, delay: float = 4.0, log=print) -> list[dict]:
    """Fetch each selected file once: exact catalogue URLs on the allowed hosts, no redirects, the
    ingestion size limit, PDF only; stored by fingerprint with a provenance sidecar, ledger updated."""
    import httpx

    ledger = Ledger(STAGING / "sync-ledger.json")
    results = []
    with httpx.Client(timeout=120, headers={"User-Agent": filelist.user_agent()}) as client:
        for index, item_id in enumerate(selection["items"]):
            record = ledger.data["items"].get(item_id)
            if record is None:
                raise ValueError(f"{item_id} is not in the catalogue")
            url = record["url"]
            parts = httpx.URL(url)
            if parts.scheme != "https" or parts.host not in ALLOWED_HOSTS:
                raise ValueError(f"{item_id}: {url} is not on an allowed host")
            if (
                record.get("sha256")
                and (STAGING / "sources" / record["source"] / f"{record['sha256']}.pdf").exists()
            ):
                results.append({"item": item_id, "sha256": record["sha256"], "fetched": False})
                continue
            if index:
                time.sleep(delay)
            with client.stream("GET", url, follow_redirects=False) as response:
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
            folder = STAGING / "sources" / record["source"]
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{sha}.pdf").write_bytes(raw)
            (folder / f"{sha}.json").write_text(
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
            ledger.fetched(item_id, sha, len(raw))
            ledger.save()
            log(f"fetched {record['listed']['name']} ({len(raw) // 1024} KB)")
            results.append({"item": item_id, "sha256": sha, "fetched": True})
    return results


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
    probe = sub.add_parser(
        "probe", help="measure the layout of a fetched selection (text layer, columns, languages)"
    )
    probe.add_argument("--selection", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "fetch":
        fetch_selection(load_selection(args.selection))
        return 0
    if args.command == "probe":
        probe_selection(load_selection(args.selection))
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
