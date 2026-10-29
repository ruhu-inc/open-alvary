"""Where every printed line of a source went (owner, 2026-10-29: account for every line).

A preparer turns a source file into review documents. This module says, for one file, where each
of its printed lines and words ended up: in a document's text, set apart as page furniture (running
headers, page numbers) or left out deliberately with a record (a court portal's summary card, a law
report's headnotes), or nowhere at all, with the reason. Nothing here changes what a preparer writes:
the audit run (ingestion.prepare_job, PREPARE_MODE=audit) prepares each file into an AuditCollection,
which writes no document and keeps these reports instead, so that what is lost can be measured
before anything is fixed.

The reasons a line can reach no document:

  no_language                 Rwanda: a column whose language was not recognised (a cover page whose
                              title lines span the page, an issue's front page): it is in no language's
                              text
  before_first_instrument     printed before the first instrument the reader recognised (an issue's
                              masthead and contents, a cover page)
  between_instruments         between two recognised instruments, in no one's range
  after_last_instrument       after the last instrument's range
  language_without_version    a language printed in the file for which no version was made
  no_instrument_found         the file gave no instrument at all
  annex_without_instrument    Rwanda: an annex whose instrument was not found
  not_in_any_document         Nigeria's PDFs: a page no instrument's pages include
  front_page                  the same, for page 1 (a Gazette's masthead and contents)
  dropped_within_page         words of a page that is in a document but did not reach it
  inside_judgment_column      a court page: words in the judgment's column that reach neither the
                              reading nor any record of what was left out
"""

import json
import re
from collections import Counter

WORD = re.compile(r"[^\W_]+")
SAMPLES_PER_REASON = 3
SAMPLE_CHARS = 160


def words(text: str) -> list[str]:
    return WORD.findall((text or "").casefold())


def texts(value) -> list[str]:
    """Every printed string inside a line, node, table or record: a dict's "text", a list's items."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out = list(texts(value.get("text"))) if isinstance(value.get("text"), str) else []
        for key in ("table", "header_rows", "body_rows", "rows", "cells", "children"):
            if key in value:
                out += texts(value[key])
        return out
    if isinstance(value, (list, tuple)):
        return [t for item in value for t in texts(item)]
    return []


def count(value) -> int:
    return sum(len(words(t)) for t in texts(value))


class Ledger:
    """One file's account: lines and words by where they went, the reasons for those that went
    nowhere, the pages concerned and a few of the lines themselves as evidence."""

    def __init__(self, pages: int):
        self.pages = pages
        self.lines = Counter()
        self.words = Counter()
        self.unplaced: dict[str, dict] = {}
        self.placed_pages: set[int] = set()
        self.content_pages: set[int] = set()

    def add(self, where: str, line, page: int | None, reason: str | None = None) -> None:
        n = count(line)
        self.lines[where] += 1
        self.words[where] += n
        if page is not None and n and where != "furniture":
            self.content_pages.add(page)
            if where in ("document", "annex"):
                self.placed_pages.add(page)
        if where == "unplaced":
            entry = self.unplaced.setdefault(reason, {"lines": 0, "words": 0, "pages": set(), "samples": []})
            entry["lines"] += 1
            entry["words"] += n
            if page is not None:
                entry["pages"].add(page)
            text = " ".join(texts(line)).strip()
            if text and len(entry["samples"]) < SAMPLES_PER_REASON:
                entry["samples"].append({"page": page, "text": text[:SAMPLE_CHARS]})

    def add_words(self, where: str, n: int, page: int | None = None, reason: str | None = None, sample=None):
        """Words counted without lines (a court page's words, a page's words lost inside it)."""
        if not n:
            return
        self.words[where] += n
        if where == "unplaced":
            entry = self.unplaced.setdefault(reason, {"lines": 0, "words": 0, "pages": set(), "samples": []})
            entry["words"] += n
            if page is not None:
                entry["pages"].add(page)
            if sample and len(entry["samples"]) < SAMPLES_PER_REASON:
                entry["samples"].append({"page": page, "text": sample[:SAMPLE_CHARS]})

    def report(self, **more) -> dict:
        unplaced_pages = sorted(self.content_pages - self.placed_pages)
        return {
            "pages": self.pages,
            "lines": dict(self.lines),
            "words": dict(self.words),
            "unplaced": {
                reason: {**entry, "pages": sorted(entry["pages"])}
                for reason, entry in sorted(self.unplaced.items())
            },
            # Pages with printed words of which none reached a document or an annex.
            "pages_unplaced": unplaced_pages,
            "first_page_unplaced": 1 in unplaced_pages,
            **more,
        }


# --- Rwanda: the Gazette's and the RLRC's PDFs (ingestion.rwanda, ingestion.rw_gazette) ---------------


def rwanda_issue(
    split: list[dict],
    roles: dict[int, int],
    found: list[dict],
    written_annexes: set[int],
    page_count: int,
    languages: tuple[str, ...] = ("rw", "en", "fr"),
) -> dict:
    """One Rwandan file's account. `split` is its pages as rw_gazette.split_issue gives them (columns
    with their language and lines, furniture apart), `roles` its annex pages (rw_gazette.annex_pages),
    `found` its instruments with each language version's range in that language's stream
    (rw_gazette.streams, which leaves annex pages out), `written_annexes` the annexes that became
    documents (those whose instrument was found)."""
    ledger = Ledger(page_count)
    # Each language's stream, position by position, as rw_gazette.streams builds it.
    position: dict[str, list[tuple[int, int, int]]] = {language: [] for language in languages}
    for page in split:
        if page["page"] in roles:
            continue
        for c, column in enumerate(page["columns"]):
            if column["language"] in position:
                position[column["language"]] += [(page["page"], c, i) for i in range(len(column["lines"]))]
    placed: set[tuple[int, int, int]] = set()
    ranges: dict[str, list[tuple[int, int]]] = {language: [] for language in languages}
    for entry in found:
        for language, version in entry["versions"].items():
            if language not in position:
                continue
            ranges[language].append((version["start"], version["end"]))
            placed.update(position[language][version["start"] : version["end"]])
    index = {key: (language, k) for language, keys in position.items() for k, key in enumerate(keys)}
    any_version = any(ranges.values())

    def why(key: tuple[int, int, int], column: dict) -> str:
        if column["language"] not in position:
            return "no_language"
        if not any_version:
            return "no_instrument_found"
        language, k = index[key]
        spans = ranges[language]
        if not spans:
            return "language_without_version"
        if k < min(start for start, _ in spans):
            return "before_first_instrument"
        if k >= max(end for _, end in spans):
            return "after_last_instrument"
        return "between_instruments"

    for page in split:
        number = page["page"]
        for line in page.get("furniture", []):
            ledger.add("furniture", line, number)
        for c, column in enumerate(page["columns"]):
            for i, line in enumerate(column["lines"]):
                key = (number, c, i)
                if number in roles:
                    if roles[number] in written_annexes:
                        ledger.add("annex", line, number)
                    else:
                        ledger.add("unplaced", line, number, "annex_without_instrument")
                elif key in placed:
                    ledger.add("document", line, number)
                else:
                    ledger.add("unplaced", line, number, why(key, column))
    without_words = [
        p["page"] for p in split if not p.get("furniture") and not any(c["lines"] for c in p["columns"])
    ]
    starts = [
        version["pages"][0]
        for entry in found
        for version in entry["versions"].values()
        if version.get("pages")
    ]
    return ledger.report(
        pages_without_words=without_words,
        instruments=len(found),
        first_document_page=min(starts) if starts else None,
        title_from_listing=any(entry.get("title_from_listing") for entry in found),
    )


# --- Nigeria: PDFs read by the Layout Parser (ingestion.ng_documents) ----------------------------------


def _node_where(node: dict) -> str:
    return "furniture" if node.get("kind") == "furniture" else "document"


def ng_pdf(reading: dict, parts: list[dict], ocr_check_error: str | None = None) -> dict:
    """One Nigerian PDF's account: `reading` the file's whole reading (ng_documents.cloud_reading),
    `parts` the readings of the documents made from it (ng_documents.documents)."""
    pages = reading.get("pages", [])
    ledger = Ledger(reading.get("summary", {}).get("pages") or len(pages))
    in_parts: dict[int, dict] = {}
    for part in parts:
        for page in part.get("pages", []):
            kept = in_parts.get(page["page"])
            # A page shared by two documents (an issue's page holding the end of one and the start of
            # the next) counts once, by the fuller of its two copies.
            if kept is None or count(page.get("nodes", [])) > count(kept.get("nodes", [])):
                in_parts[page["page"]] = page
    for page in pages:
        number = page["page"]
        if number in in_parts:
            part_page = in_parts[number]
            for node in part_page.get("nodes", []):
                ledger.add(_node_where(node), node, number)
            lost = sum(count(n) for n in page.get("nodes", []) if _node_where(n) == "document") - sum(
                count(n) for n in part_page.get("nodes", []) if _node_where(n) == "document"
            )
            if lost > 0:
                ledger.add_words("unplaced", lost, number, "dropped_within_page")
            continue
        reason = (
            "front_page"
            if number == 1 and parts
            else "not_in_any_document"
            if parts
            else "no_instrument_found"
        )
        for node in page.get("nodes", []):
            if node.get("kind") == "furniture":
                ledger.add("furniture", node, number)
            else:
                ledger.add("unplaced", node, number, reason)
    return ledger.report(
        documents=len(parts),
        **({"ocr_check_error": ocr_check_error} if ocr_check_error else {}),
    )


# --- Nigeria: the National Industrial Court's record pages (ingestion.nicn) ----------------------------

INVISIBLE = ("script", "style", "noscript", "template", "head")
JUDGMENT_COLUMN = "div.light-background div.col-12"


def _unplaced_run(tokens: list[str], left: Counter) -> tuple[int, str | None]:
    """How many of `tokens` (in printed order) the placed words do not account for, and the first run
    of them as evidence."""
    missing, run, sample, before, context = 0, [], None, [], []
    for token in tokens:
        if left[token] > 0:
            left[token] -= 1
            if run and sample is None:
                sample = " ".join([*context, "[" + " ".join(run) + "]", token])
            run = []
            before = (before + [token])[-3:]
        else:
            if not run:
                context = before
            missing += 1
            run.append(token)
    if sample is None and run:
        sample = " ".join([*context, "[" + " ".join(run) + "]"])
    return missing, sample


def nicn_page(raw: bytes, judgment) -> dict:
    """One court record page's account, by its words (a web page has no lines): those in the
    judgment's column against those in the reading, the furniture, the portal's summary card and a
    law report's headnotes kept beside it; and, apart, the portal's own words outside that column."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw.decode("utf-8", "replace"), "html.parser")
    for element in soup.select(",".join(INVISIBLE)):
        element.decompose()
    column = soup.select_one(JUDGMENT_COLUMN) or soup.body or soup
    inside_text = column.get_text(" ")
    if (
        getattr(judgment, "markup_as_text", False)
        or len(re.findall(r"</?(?:p|span|div)\b", inside_text)) >= 5
    ):
        # The portal printed the judgment's markup as text: its words are those of the text parsed again.
        inside_text = BeautifulSoup(inside_text, "html.parser").get_text(" ")
    inside = words(inside_text)
    everything = words(soup.get_text(" "))
    outside = sum((Counter(everything) - Counter(inside)).values())
    ledger = Ledger(1)
    document = [
        t
        for unit in judgment.units
        for t in words(
            " ".join(filter(None, [unit.marker or ""] + texts(unit.text) + texts(unit.rows or [])))
        )
    ]
    furniture = [t for item in judgment.furniture for t in words(" ".join(texts(item)))]
    set_aside = [
        t
        for item in [judgment.summary or "", *(judgment.editorial or [])]
        for t in words(" ".join(texts(item)))
    ]
    left = Counter(document) + Counter(furniture) + Counter(set_aside)
    missing, sample = _unplaced_run(inside, left)
    ledger.add_words("document", len(document), 1)
    ledger.add_words("furniture", len(furniture), 1)
    ledger.add_words("set_aside", len(set_aside), 1)
    ledger.add_words("unplaced", missing, 1, "inside_judgment_column", sample)
    held = (
        "partial"
        if judgment.partial
        else "empty"
        if judgment.empty
        else "text_held"
        if judgment.text_held
        else None
    )
    return ledger.report(
        words_in_column=len(inside),
        portal_words_outside_column=outside,
        **({"held": held} if held else {}),
    )


# --- across files --------------------------------------------------------------------------------------


def summarise(reports: list[dict]) -> dict:
    """Many files' accounts as one: totals, each reason's files, words, lines and pages, and the files
    that lose the most."""
    totals_words, totals_lines = Counter(), Counter()
    reasons: dict[str, dict] = {}
    first_page, any_unplaced = 0, 0
    by_source: dict[str, Counter] = {}
    for report in reports:
        totals_words.update(report.get("words", {}))
        totals_lines.update(report.get("lines", {}))
        if report.get("first_page_unplaced"):
            first_page += 1
        if report.get("unplaced"):
            any_unplaced += 1
        source = by_source.setdefault(report.get("source") or "?", Counter())
        source["files"] += 1
        source["words"] += sum(report.get("words", {}).values())
        source["unplaced_words"] += report.get("words", {}).get("unplaced", 0)
        source["files_with_unplaced"] += bool(report.get("unplaced"))
        source["first_page_unplaced"] += bool(report.get("first_page_unplaced"))
        for reason, entry in report.get("unplaced", {}).items():
            total = reasons.setdefault(
                reason, {"files": 0, "lines": 0, "words": 0, "pages": 0, "examples": []}
            )
            total["files"] += 1
            total["lines"] += entry.get("lines", 0)
            total["words"] += entry["words"]
            total["pages"] += len(entry.get("pages", []))
            if len(total["examples"]) < 5 and entry.get("samples"):
                total["examples"].append(
                    {"file": report.get("name") or report.get("sha"), **entry["samples"][0]}
                )
    worst = sorted(reports, key=lambda r: r.get("words", {}).get("unplaced", 0), reverse=True)[:20]
    all_words = sum(totals_words.values())
    return {
        "files": len(reports),
        "files_with_unplaced": any_unplaced,
        "files_whose_first_page_is_unplaced": first_page,
        "words": dict(totals_words),
        "lines": dict(totals_lines),
        "unplaced_share": round(totals_words.get("unplaced", 0) / all_words, 4) if all_words else 0.0,
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1]["words"])),
        "by_source": {k: dict(v) for k, v in sorted(by_source.items())},
        "worst": [
            {
                "file": r.get("name") or r.get("sha"),
                "url": r.get("url"),
                "unplaced_words": r.get("words", {}).get("unplaced", 0),
                "reasons": sorted(r.get("unplaced", {})),
            }
            for r in worst
            if r.get("words", {}).get("unplaced")
        ],
    }


def main(argv: list[str] | None = None) -> int:
    """python -m ingestion.accounting summary <folder>: the summary of an audit run's task reports,
    downloaded from work/<country>/audit/lines/<run>/."""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="python -m ingestion.accounting")
    sub = parser.add_subparsers(dest="action", required=True)
    summary = sub.add_parser("summary")
    summary.add_argument("folder", type=Path)
    args = parser.parse_args(argv)
    reports, failed, unreported = [], [], []
    for path in sorted(args.folder.glob("task-*.json")):
        task = json.loads(path.read_text())
        reports += task.get("reports", [])
        failed += task.get("failed", [])
        unreported += task.get("not_reported", [])
    out = summarise(reports)
    out["failed"] = len(failed)
    out["not_reported"] = len(unreported)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
