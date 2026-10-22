"""Nigeria: the official sources we catalogue, and how each one's listing is read.

    python -m ingestion.nigeria catalogue nass-acts             # one source's listing, into the ledger
    python -m ingestion.nigeria catalogue all                   # every source due
    python -m ingestion.nigeria catalogue nicn-judgments        # resumes from the ledger's cursor
    python -m ingestion.nigeria reparse nass-acts               # the kept answers read again, no network
    python -m ingestion.nigeria status                          # last check, outcome, counts, per source
    python -m ingestion.nigeria fetch-worklist --approved-by NAME --basis "where and when approved"
    python -m ingestion.nigeria pull-fetched                    # what the cloud fetch stored or failed
    python -m ingestion.nigeria push-held                       # mirror the ledger, catalogues and runs

The sources are the official ones openly online that the owner approved on 2026-10-22 (the survey in
private-staging/ng-survey/): wave 1 and wave 2 below. The Central Bank, the Revenue Service and the
upstream petroleum regulator are left out (they need permission, or robots.txt forbids their files),
and so is the FCT High Court (a compromised site); non-official sites are never sources.

A catalogue reads listing pages, data feeds and APIs only, politely (ingestion.polite), and records
each listed document once in the sync ledger (private-staging/ng/sync-ledger.json; mirrored to
state/ng/ in the corpus bucket): its file's address, every detail its listing gives normalised
("listed") and the source's own record verbatim ("listed_raw"), the listing request it came from,
its own record page, and other renditions of it ("alternate_urls", not fetched), and never more.
Every answer a run reads is kept (catalogue/<source>/<run>/), so `reparse` reads it again with
better readers without visiting the site. A later run records only what is new, changed or no
longer listed, in each item's history; nothing is deleted. Items that may be the same document
(from one source or two) are flagged ("likely_duplicates") before anything is read twice.

It downloads no document: the cloud job (ingestion.fetch_job) fetches the approved work list, and
stores each file once by its fingerprint. A listing that stops early (a refusal, a bot check,
repeated failures) is recorded as stopped, never as "nothing listed"; what it rejects (an address off
the expected path, a title that fails the sanity check, injected spam) is logged beside what it kept.
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

from ingestion import filelist, polite
from ingestion.sync import Ledger, now, stamp

STAGING = Path(os.environ.get("ALVARY_NG_STAGING", "private-staging/ng"))
COUNTRY = "ng"

TYPES = (
    "act",
    "constitution",
    "constitutional_alteration",
    "regulation",
    "order",
    "guideline",
    "circular",
    "rules",
    "practice_direction",
    "judgment",
    "ruling",
    "executive_order",
    "gazette",
    "bill",
    "press_release",  # State House's assent announcements: evidence of assent, not law
)

DRIVE_REFUSED = (
    "robots.txt of drive.google.com allows only its viewer pages (/file/); the file itself is served "
    "from /uc and drive.usercontent.google.com, which robots.txt disallows"
)

SOURCES: dict[str, dict] = {
    "nass-acts": {
        "name": "National Assembly: Acts & Publications",
        "publisher": "National Assembly of the Federal Republic of Nigeria",
        "url": "https://nass.gov.ng/documents/magazine",
        "kinds": ["act", "constitution", "constitutional_alteration"],
        "listing": "The table's DataTables data feed, /documents/magazine_track/ (100 rows a request); each row "
        "links to /documents/download/{id}",
        "robots": "No robots.txt: the address answers with the home page. Nothing disallowed.",
        "terms": "© 2026 National Assembly of Nigeria | Privacy Policy | Terms (footer only; the links go nowhere)",
        "wave": 1,
        "hosts": ["nass.gov.ng"],
        "file_hosts": ["nass.gov.ng"],
        "cadence": "weekly",
        "fetch": True,
    },
    "nass-bills": {
        "name": "National Assembly: bills tracker (metadata, to link Acts to their bills)",
        "publisher": "National Assembly of the Federal Republic of Nigeria",
        "url": "https://nass.gov.ng/documents/bills",
        "kinds": ["bill"],
        "listing": "The tracker's DataTables data feed, /documents/bill_track/ (100 rows a request); each row "
        "links to its record page /documents/bill/{id}",
        "robots": "No robots.txt. Nothing disallowed.",
        "terms": "As for the Acts list: a footer with dead Terms and Privacy Policy links.",
        "wave": 1,
        "hosts": ["nass.gov.ng"],
        "file_hosts": ["nass.gov.ng"],
        "cadence": "weekly",
        "fetch": False,  # metadata only; the record pages can be fetched by naming the source
    },
    "nln-repository": {
        "name": "National Library of Nigeria repository: Federal Government gazettes; Constitutions, Laws and Acts",
        "publisher": "National Library of Nigeria",
        "url": "https://nigeriareposit.nln.gov.ng/handle/20.500.14186/350",
        "kinds": ["gazette", "act", "constitution", "constitutional_alteration", "circular"],
        "listing": "The repository's OAI-PMH data interface (/server/oai/request, metadata format xoai: Dublin "
        "Core with each file's name, size and MD5), one set per collection; a set the interface fails to "
        "list is read record by record (ListIdentifiers, then GetRecord). Search and browse pages are "
        "never used.",
        "robots": "Disallows /search, /browse/*, /admin/*, /processes, /submit, /workspaceitems, /profile, "
        "/workflowitems, /statistics. The data interface (/server/) is not disallowed.",
        "terms": 'Content ... provided on the Site "AS IS" for your information and personal use only '
        "(end-user agreement; it also forbids copying for any commercial purpose without permission).",
        "wave": 1,
        "hosts": ["nigeriareposit.nln.gov.ng"],
        "file_hosts": ["nigeriareposit.nln.gov.ng"],
        "cadence": "monthly",
        "fetch": True,
        "sets": {
            "col_20.500.14186_350": "Federal Government of Nigeria (gazettes)",
            "col_20.500.14186_403": "Constitutions, Laws and Acts",
        },
    },
    "moj-constitution": {
        "name": "Federal Ministry of Justice: the Constitution",
        "publisher": "Federal Ministry of Justice",
        "url": "https://justice.gov.ng/nigerian-constitution/",
        "kinds": ["constitution"],
        "listing": "The Constitution post page and its download link; the file's size from the WordPress media "
        "API",
        "robots": "Disallow: /wp-admin/ (admin-ajax.php allowed). Everything a catalogue needs is allowed.",
        "terms": "Copyright © 2023 - Federal Ministry of Justice. All Rights Reserved",
        "wave": 1,
        "hosts": ["justice.gov.ng"],
        "file_hosts": ["justice.gov.ng"],
        "cadence": "monthly",
        "fetch": True,
    },
    "supreme-court": {
        "name": "Supreme Court of Nigeria: judgments, rules and practice directions",
        "publisher": "Supreme Court of Nigeria",
        "url": "https://supremecourt.gov.ng/judgment",
        "kinds": ["judgment", "ruling", "rules", "practice_direction", "act"],
        "listing": "The court's CMS API (supreme-cms.supremecourt.gov.ng/api/v1.0): the judgment search by date "
        "window (at most 50 records a search, so windows are halved until under the cap), each record's "
        "detail (its concurring opinions), and the rules, mediation rules, practice directions and Acts "
        "lists. Files are on the court's Amazon S3 bucket.",
        "robots": "supremecourt.gov.ng and supreme-cms: 'User-agent: *' / 'Disallow:' (everything allowed). The "
        "S3 bucket's robots.txt answers 403 (no restrictions).",
        "terms": "Supreme Court of Nigeria - Copyright 2024. All rights reserved.",
        "wave": 1,
        "hosts": ["supreme-cms.supremecourt.gov.ng"],
        "file_hosts": ["scnwebsites3.s3.af-south-1.amazonaws.com", "supreme-cms.supremecourt.gov.ng"],
        "windows": {"supreme-cms.supremecourt.gov.ng": (60, 60.0)},  # x-ratelimit-limit: 60
        "cadence": "weekly",
        "fetch": True,
    },
    "nicn-judgments": {
        "name": "National Industrial Court of Nigeria: judgment portal",
        "publisher": "National Industrial Court of Nigeria",
        "url": "https://nicnadr.gov.ng/judgement/",
        "kinds": ["judgment", "ruling"],
        "listing": "The portal lists only its newest records by GET (its filters are POST forms, not used); "
        "records are read by id with GET (judgement.php?id=N), the first 64 KB of each for its header, "
        "from id 1 to the newest, a share each run, resuming from the ledger's cursor.",
        "robots": "No robots.txt (404). Nothing disallowed.",
        "terms": "© National Industrial Court of Nigeria- ALL RIGHTS RESERVED.",
        "wave": 1,
        "hosts": ["nicnadr.gov.ng"],
        "file_hosts": ["nicnadr.gov.ng"],
        "cadence": "weekly",
        "fetch": True,
        "budget": 1000,  # record pages a run
    },
    "ncaa": {
        "name": "Nigerian Civil Aviation Authority: Nig.CARs, Civil Aviation Acts and Orders",
        "publisher": "Nigerian Civil Aviation Authority",
        "url": "https://ncaa.gov.ng/documents/regulations/nigcars-2023-official-gazette/",
        "kinds": ["regulation", "act", "order"],
        "listing": "Five listing pages (the 2023 Official Gazette set, the 2023 set, the superseded 2015 set, the "
        "Acts, the Orders) and each record's page (Category, Number, Issue Date, Effective Date, Is Current, "
        "Download).",
        "robots": "Allow: / ; Disallow system folders (/umbraco/, /bin/, /config/ ...). /documents/ and /media/ "
        "allowed.",
        "terms": "Copyright @ Nigeria Civil Aviation Authority - 2026 . All Rights Reserved",
        "wave": 1,
        "hosts": ["ncaa.gov.ng"],
        "file_hosts": ["ncaa.gov.ng"],
        "cadence": "monthly",
        "fetch": True,
    },
    "ncc": {
        "name": "Nigerian Communications Commission: regulations, guidelines, determinations",
        "publisher": "Nigerian Communications Commission",
        "url": "https://ncc.gov.ng/operators/regulations-guidelines/regulations",
        "kinds": ["regulation", "rules", "guideline", "order"],
        "listing": "Three listing pages: regulations and guidelines (each in a Published and a Draft section) "
        "and determinations (with description and tags); files at /media/{id}/view.",
        "robots": "Standard Drupal: disallows /admin/, /search/, /node/add/, /user/..., /media/oembed. Listings "
        "and /media/{id}/view allowed. Site search is not used.",
        "terms": "© 2025 Nigerian Communications Commission. All Rights Reserved.",
        "wave": 1,
        "hosts": ["ncc.gov.ng"],
        "file_hosts": ["ncc.gov.ng"],
        "cadence": "monthly",
        "fetch": True,
    },
    "sec": {
        "name": "Securities and Exchange Commission: rules and regulations, Acts, guidelines, circulars",
        "publisher": "Securities and Exchange Commission, Nigeria",
        "url": "https://sec.gov.ng/our-mandate/regulation/rules-and-regulations/",
        "kinds": ["rules", "act", "guideline", "circular"],
        "listing": "The rules, Acts and guidelines pages (a heading, a description and a download block per "
        "document, with its size), and the circulars page (one web page per circular).",
        "robots": "No robots.txt (404). Nothing disallowed.",
        "terms": "A 'Website Terms of Use' page exists (Published: December 1, 2025) but has no text.",
        "wave": 1,
        "hosts": ["sec.gov.ng"],
        "file_hosts": ["sec.gov.ng"],
        "cadence": "monthly",
        "fetch": True,
    },
    "ndpc": {
        "name": "Nigeria Data Protection Commission: resources",
        "publisher": "Nigeria Data Protection Commission",
        "url": "https://ndpc.gov.ng/resources/",
        "kinds": ["act", "regulation", "guideline", "circular", "bill"],
        "listing": "The resources page's flipbooks (title, and the PDF each one shows); reports, journals and "
        "policies are left out.",
        "robots": "Disallow /wp-admin/ (admin-ajax.php allowed). Everything a catalogue needs is allowed.",
        "terms": "© Copyright 2025 by Nigeria Data Protection Commission",
        "wave": 1,
        "hosts": ["ndpc.gov.ng"],
        "file_hosts": ["ndpc.gov.ng"],
        "cadence": "monthly",
        "fetch": True,
    },
    "statehouse": {
        "name": "State House: Executive Orders and assent announcements",
        "publisher": "The Presidency (State House, Abuja)",
        "url": "https://statehouse.gov.ng/",
        "kinds": ["executive_order", "press_release"],
        "listing": "The WordPress posts API searched for 'executive order', 'assent' and 'signs'; only posts "
        "whose title is an Executive Order or an assent announcement are kept (the posts are web pages).",
        "robots": "Standard WordPress: Disallow /wp-admin/ (admin-ajax.php allowed). Posts allowed.",
        "terms": "© 2026 - The State House, Abuja",
        "wave": 1,
        "hosts": ["statehouse.gov.ng"],
        "file_hosts": ["statehouse.gov.ng"],
        "cadence": "weekly",
        "fetch": True,
    },
    "nerc": {
        "name": "Nigerian Electricity Regulatory Commission: regulations and orders",
        "publisher": "Nigerian Electricity Regulatory Commission",
        "url": "https://nerc.gov.ng/resource-category/regulations/",
        "kinds": ["regulation", "order"],
        "listing": "The regulations and orders categories, page by page (/page/N/): title, posting date, "
        "excerpt and file.",
        "robots": "Disallow /wp-admin/ (admin-ajax.php allowed); sitemaps. Everything a catalogue needs is "
        "allowed.",
        "terms": "© 2026 Nigerian Electricity Regulatory Commission. All rights reserved.",
        "wave": 2,
        "hosts": ["nerc.gov.ng"],
        "file_hosts": ["nerc.gov.ng"],
        "cadence": "monthly",
        "fetch": True,
    },
    "nafdac": {
        "name": "NAFDAC: gazetted regulations",
        "publisher": "National Agency for Food and Drug Administration and Control",
        "url": "https://nafdac.gov.ng/regulatory-resources/nafdac-regulations/",
        "kinds": ["regulation"],
        "listing": "The 'Gazetted NAFDAC Regulations' table, read from its Ninja Tables data (admin-ajax.php, "
        "with the nonce the page carries): S/N, title and file, product type, category, status.",
        "robots": "Disallow /wp-admin/, Allow /wp-admin/admin-ajax.php. Listings, uploads and admin-ajax.php "
        "allowed.",
        "terms": "No copyright notice or terms found.",
        "wave": 2,
        "hosts": ["nafdac.gov.ng"],
        "file_hosts": ["nafdac.gov.ng", "www.nafdac.gov.ng"],
        "cadence": "monthly",
        "fetch": True,
    },
    "nesrea": {
        "name": "NESREA: National Environmental Regulations and Acts",
        "publisher": "National Environmental Standards and Regulations Enforcement Agency",
        "url": "https://nesrea.gov.ng/laws-regulations/",
        "kinds": ["regulation", "act"],
        "listing": "The Laws & Regulations page: each entry's title (with its S.I. number), description and file; "
        "files on Google Drive are listed but refused (robots.txt).",
        "robots": "User-Agent: * / Disallow: /login, plus six Sitemap lines on an unrelated host (a sign of "
        "injected spam). Content paths allowed.",
        "terms": "Copyright © 2026. Powered By i4globalservices",
        "wave": 2,
        "hosts": ["nesrea.gov.ng"],
        "file_hosts": ["nesrea.gov.ng", "drive.google.com"],
        "cadence": "monthly",
        "fetch": True,
    },
    "cac": {
        "name": "Corporate Affairs Commission: regulations in its media library",
        "publisher": "Corporate Affairs Commission",
        "url": "https://www.cac.gov.ng/resources",
        "kinds": ["regulation", "gazette", "circular", "act"],
        "listing": "The media library behind the resources page (news.cac.gov.ng WordPress media API, PDFs): "
        "titles come from file names; only regulations, Acts, Gazette copies and notices on them are kept.",
        "robots": "www.cac.gov.ng/robots.txt 404; news.cac.gov.ng/robots.txt empty. No restrictions.",
        "terms": "© 2026 Powered by CAC Corporate Affairs Commission Nigeria",
        "wave": 2,
        "hosts": ["news.cac.gov.ng"],
        "file_hosts": ["news.cac.gov.ng"],
        "cadence": "monthly",
        "fetch": True,
    },
    "naicom": {
        "name": "National Insurance Commission: laws, guidelines, circulars, directives",
        "publisher": "National Insurance Commission",
        "url": "https://naicom.gov.ng/regulatory-instruments/",
        "kinds": ["act", "guideline", "regulation", "circular"],
        "listing": "The WordPress posts API for the Laws, Guidelines, Circulars and Directives categories; each "
        "post's address is its file. Files on storage.naicom.website (off the official host, broken "
        "certificate) and images are left out.",
        "robots": "User-agent: * / Allow: / . Everything allowed.",
        "terms": "Copyright © 2026 NAICOM • All Rights Reserved",
        "wave": 2,
        "hosts": ["naicom.gov.ng"],
        "file_hosts": ["naicom.gov.ng"],
        "cadence": "monthly",
        "fetch": True,
        "categories": {"158": "Laws", "109": "Guidelines", "157": "Circulars", "161": "Directives"},
    },
    "court-of-appeal": {
        "name": "Court of Appeal: judgments",
        "publisher": "Court of Appeal of Nigeria",
        "url": "https://courtofappeal.gov.ng/judgment",
        "kinds": ["judgment"],
        "listing": "The court's judgment API (coa-admin.courtofappeal.gov.ng/api/judgement/search): appeal "
        "number, parties, panel, date, subject matter, division and a Google Drive link. The files are "
        "refused: Google Drive's robots.txt disallows downloads.",
        "robots": "The API host's robots.txt has '# Disallow: /' commented out (all allowed). Google Drive: only "
        "/file/ viewer pages allowed; /uc and drive.usercontent.google.com disallowed.",
        "terms": "No licence terms (/terms is a template page about personal information).",
        "wave": 2,
        "hosts": ["coa-admin.courtofappeal.gov.ng"],
        "file_hosts": ["drive.google.com"],
        "cadence": "monthly",
        "fetch": True,
    },
    "fhc-rules": {
        "name": "Federal High Court: rules and practice directions",
        "publisher": "Federal High Court of Nigeria",
        "url": "https://fhc.gov.ng/federal-high-court-rules-and-practice-directions/",
        "kinds": ["rules", "practice_direction"],
        "listing": "The rules page lists its rules and practice directions by title only ('contained in the "
        "Federal High Courts' Handbook'); the one file the survey found on the court's host is kept beside "
        "them. Spam links on the site are rejected.",
        "robots": "Disallow /wp-admin/ (admin-ajax.php allowed). All else allowed.",
        "terms": "Copyright © 2026 Federal High Court of Nigeria",
        "wave": 2,
        "hosts": ["fhc.gov.ng"],
        "file_hosts": ["fhc.gov.ng", "www.fhc.gov.ng"],
        "cadence": "monthly",
        "fetch": True,
        "seeds": [
            {
                "url": "https://www.fhc.gov.ng/fhc/Pre-ElectionMPD.pdf",
                "type": "practice_direction",
                "listed": {
                    "file_name": "Pre-ElectionMPD.pdf",
                    "found_by": "survey of 2026-10-22: HEAD 200, application/pdf, 10,985,009 bytes",
                    "survey_note": "Federal High Court (Pre-Election) Practice Directions 2026 as a PDF",
                    "size_bytes": 10985009,
                },
            }
        ],
    },
}


# --- what a listing says, normalised ---------------------------------------------------------------


def clean(text) -> str:
    """A listing's text without markup, entities or runs of white space."""
    if text is None:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(text))
    return " ".join(html.unescape(text).replace("\xa0", " ").split())


def year_in(title: str) -> int | None:
    """The last year a title names (18xx to 20xx)."""
    years = re.findall(r"(?<!\d)((?:18|19|20)\d\d)(?!\d)", title or "")
    return int(years[-1]) if years else None


MONTHS = "january february march april may june july august september october november december".split()


def to_date(text) -> str | None:
    """'11/08/2023' (day first), 'Wednesday, August 9, 2023', 'Oct. 10, 2026', '12 TH OCTOBER 2026',
    '2023-05' or '2015-05-27' -> an ISO date (or year-month); None when the text is not a date."""
    if not text:
        return None
    t = " ".join(str(text).replace(",", " ").split())
    m = re.fullmatch(r"((?:18|19|20)\d\d)-(\d\d)(?:-(\d\d))?(?:[T ].*)?", t)
    if m:
        if m.group(3):
            return _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return f"{m.group(1)}-{m.group(2)}" if 1 <= int(m.group(2)) <= 12 else None
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/((?:18|19|20)\d\d)", t)
    if m:
        return _valid(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    def month(word: str) -> int | None:
        word = word.lower().rstrip(".")
        for n, name in enumerate(MONTHS, 1):
            if len(word) >= 3 and name.startswith(word):
                return n
        return None

    words = [
        w
        for w in t.split()
        if w.lower().rstrip(".")
        not in (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "mon",
            "tue",
            "wed",
            "thu",
            "fri",
            "sat",
            "sun",
        )
    ]
    t = " ".join(words)
    m = re.fullmatch(r"([A-Za-z.]+) (\d{1,2})(?:st|nd|rd|th)? ((?:18|19|20)\d\d)", t, re.I)
    if m and month(m.group(1)):
        return _valid(int(m.group(3)), month(m.group(1)), int(m.group(2)))
    m = re.fullmatch(r"(\d{1,2}) ?(?:st|nd|rd|th)? (?:of )?([A-Za-z.]+) ((?:18|19|20)\d\d)", t, re.I)
    if m and month(m.group(2)):
        return _valid(int(m.group(3)), month(m.group(2)), int(m.group(1)))
    return None


def _valid(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


SPAM = re.compile(
    r"gacor|togel|sbobet|maxwin|wangi4d|\bslot\s*(?:online|gacor|88|thailand)|\bslot\d+|judi\s+online|"
    r"\bsitus\s|\bbandar\s|deposit\s+pulsa|rtp\s+live|\w+poker\b|bakatpoker|linklist\.bio|heylink|"
    r"\bviagra\b|\bcialis\b",
    re.I,
)


def sane_title(title: str) -> str | None:
    """Why a title is not to be trusted (empty, too short or long, no words, spam), or None."""
    title = (title or "").strip()
    if len(re.findall(r"[A-Za-z]", title)) < 3:
        return "no title"
    if len(title) > 5000:
        return "a title over 5,000 characters"
    if re.search(r"https?://", title):
        return "an address in the title"
    if SPAM.search(title):
        return "spam words in the title"
    return None


def law_type(title: str) -> str | None:
    """The kind of instrument a title names, or None when it names none."""
    t = title.lower()
    if re.search(r"\bexecutive orders?\b", t):
        return "executive_order"
    if re.search(r"\bbill\b", t) and not re.search(r"\bacts?\b", t):
        return "bill"
    if re.search(r"\bconstitution\b", t) and re.search(r"\balteration\b|\bamendment\b", t):
        return "constitutional_alteration"
    if re.search(r"\bconstitution\b", t):
        return "constitution"
    if re.search(r"\bgazette\b", t):
        return "gazette"
    if re.search(r"\bpractice directions?\b", t):
        return "practice_direction"
    if re.search(r"\bregulations?\b", t):
        return "regulation"
    if re.search(r"\bcode of (?:conduct|practice)\b", t):
        return "guideline"
    if re.search(
        r"\bacts?\b|\bdecrees?\b|\bedicts?\b|\blaws?\b|\bcodes?\b|\bordinances?\b|\bproclamations?\b", t
    ):
        return "act"
    if re.search(r"\blegislation\b", t):
        return "act"
    if re.search(r"\brules\b", t):
        return "rules"
    if re.search(r"\borders?\b", t):
        return "order"
    if re.search(r"\bguidelines?\b|\bguidance\b|\bframework\b", t):
        return "guideline"
    if re.search(r"\bcirculars?\b|\bdirectives?\b|\bnotice\b", t):
        return "circular"
    return None


def file_name(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def on_path(url: str, hosts, pattern: str) -> str | None:
    """Why an address is not where this source keeps its files (another host, another path), or None."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "not a web address"
    if (parts.hostname or "").lower() not in hosts:
        return f"off the source's hosts ({parts.hostname})"
    if not re.search(pattern, parts.path + (f"?{parts.query}" if parts.query else ""), re.I):
        return f"off the expected path ({parts.path})"
    return None


def https(url: str) -> str:
    return "https://" + url[len("http://") :] if url.startswith("http://") else url


def fragment(element, limit: int = 20000) -> str | None:
    """An HTML element as the page has it (the source's record verbatim), up to `limit` characters."""
    if element is None:
        return None
    text = str(element)
    return text if len(text) <= limit else text[:limit] + "<!-- cut -->"


class SourceStopped(Exception):
    """A reader stopped its source: repeated failures, or an answer it cannot read."""


class Failures:
    """Failures in a row: the third stops the source."""

    def __init__(self, limit: int = 3):
        self.limit, self.count = limit, 0

    def ok(self) -> None:
        self.count = 0

    def failed(self, reason: str) -> None:
        self.count += 1
        if self.count >= self.limit:
            raise SourceStopped(f"{self.count} failures in a row; the last: {reason}")


class Found:
    """What a reader found in one source's listing: the items to fetch (one per document, with its
    provenance), the entries listed without a file, and what it rejected and why."""

    def __init__(self, source_id: str):
        self.source = source_id
        self.items: dict[str, dict] = {}
        self.without_file: list[dict] = []
        self.rejected: list[dict] = []
        self.notes: list[str] = []
        self.cursor: dict | None = None
        self.complete = True  # False: the reader saw only part of the listing (by design or not)
        self.resumable = False  # the listing is read a share at a time, from the cursor

    def add(
        self,
        url: str,
        type_: str,
        listed: dict,
        *,
        raw=None,
        listing_url: str | None = None,
        record_url: str | None = None,
        alternates: list[str] | None = None,
        refused: str | None = None,
    ) -> dict:
        """One document: `url` its file (or page), `listed` the listing's details normalised, `raw`
        the source's own record verbatim, `listing_url` the request it came from, `record_url` its
        own record page, `alternates` other renditions of it (kept, not fetched)."""
        if type_ not in TYPES:
            raise ValueError(f"unknown type {type_}")
        listed = {k: v for k, v in listed.items() if v not in (None, "", [], {})}
        held = self.items.get(url)
        if held is not None:  # the same file listed again: its other listing kept beside the first
            other = {"listed": listed, "listing_url": listing_url, "record_url": record_url}
            other = {k: v for k, v in other.items() if v}
            if listed != held["listed"] and other not in held.setdefault("also_listed_as", []):
                held["also_listed_as"].append(other)
            for alternate in alternates or []:
                if alternate != url and alternate not in held.setdefault("alternate_urls", []):
                    held["alternate_urls"].append(alternate)
            if not held.get("also_listed_as"):
                held.pop("also_listed_as", None)
            return held
        item = {"url": url, "type": type_, "listed": listed, "source": self.source}
        if listing_url:
            item["listing_url"] = listing_url
        if record_url:
            item["record_url"] = record_url
        if raw is not None:
            item["listed_raw"] = raw
        alternates = [a for a in dict.fromkeys(alternates or []) if a and a != url]
        if alternates:
            item["alternate_urls"] = alternates
        if refused:
            item["refused"] = refused
        self.items[url] = item
        return item

    def no_file(self, type_: str, listed: dict, reason: str, **provenance) -> None:
        entry = {"type": type_, "reason": reason, **{k: v for k, v in listed.items() if v not in (None, "")}}
        self.without_file.append(entry | {k: v for k, v in provenance.items() if v})

    def reject(self, reason: str, **what) -> None:
        self.rejected.append({"reason": reason, **{k: v for k, v in what.items() if v not in (None, "")}})


def _ok(answer: polite.Answer) -> polite.Answer:
    if answer.status != 200:
        raise SourceStopped(f"HTTP {answer.status} at {answer.final_url}")
    return answer


def _json(answer: polite.Answer):
    try:
        return _ok(answer).json()
    except ValueError as error:
        raise SourceStopped(f"not JSON at {answer.final_url}: {error}") from error


def _soup(answer: polite.Answer):
    from bs4 import BeautifulSoup

    return BeautifulSoup(_ok(answer).text, "html.parser")


# --- National Assembly -----------------------------------------------------------------------------


def nass_feed(client, found: Found, path: str, log=print) -> tuple[str, list[list]]:
    """Every row of a National Assembly DataTables feed, and the request that gave them. The whole
    list is asked for at once: the feed has no order of its own, so pages of 100 repeat some rows and
    miss others."""
    url = f"https://nass.gov.ng/documents/{path}/?draw=1&start=0&length=10000"
    page = _json(client.get(url))
    total, rows = int(page.get("recordsTotal") or 0), page.get("data") or []
    ids = [str(row[-1]) for row in rows if row]
    if len(set(ids)) != len(ids):
        found.notes.append(f"the feed repeats {len(ids) - len(set(ids))} rows")
    if len(rows) != total:
        found.complete = False
        found.notes.append(f"the feed says {total} rows; {len(rows)} came")
    log(f"{path}: {len(rows)} of {total} rows")
    found.cursor = {"records_total": total, "rows_read": len(rows)}
    return url, rows


def read_nass_acts(client, found: Found, state: dict, log=print, budget=None) -> None:
    url, rows = nass_feed(client, found, "magazine_track", log)
    for row in rows:
        nass_act(found, row, url)


def nass_act(found: Found, row: list, listing_url: str) -> None:
    """One row of the Acts feed: [title, uploaded, chamber, parliament, session, category, id]."""
    padded = list(row) + [None] * (7 - len(row))
    raw_title, uploaded, chamber, parliament, session, category, doc_id = padded[:7]
    title = clean(raw_title)
    doc_id = str(doc_id or "").strip()
    if not re.fullmatch(r"\d+", doc_id):
        found.reject("no document id", title=title, raw=row)
        return
    url = f"https://nass.gov.ng/documents/download/{doc_id}"
    why = sane_title(title)
    if why:
        found.reject(why, url=url, title=title, raw=row)
        return
    kind = law_type(title)
    if kind not in ("act", "constitution", "constitutional_alteration", "bill"):
        found.reject("not a law by its title (a magazine, handbook or motion)", url=url, title=title, raw=row)
        return
    found.add(
        url,
        kind,
        {
            "title": title,
            "year": year_in(title),
            "chamber": clean(chamber),
            "parliament": clean(parliament),
            "session": clean(session),
            "uploaded": to_date(uploaded),
            "uploaded_as_listed": uploaded,
            "category": clean(category),
            "document_id": doc_id,
        },
        raw=row,
        listing_url=listing_url,
    )


def read_nass_bills(client, found: Found, state: dict, log=print, budget=None) -> None:
    url, rows = nass_feed(client, found, "bill_track", log)
    for row in rows:
        nass_bill(found, row, url)


BILL_NUMBER = re.compile(
    r"^\s*((?:HB|SB)\s*\.?\s*\d+[A-Z]?)\s*[.:-]?\s*(.*?)\s*(?:\(\s*(?:HB|SB)\s*\.?\s*\d+[A-Z]?\s*\))?\s*$",
    re.I,
)


def nass_bill(found: Found, row: list, listing_url: str) -> None:
    """One row of the bills feed: [title, chamber, first, second, committee, third reading, id]."""
    padded = list(row) + [None] * (7 - len(row))
    raw_title, chamber, first, second, committee, third, record_id = padded[:7]
    text = clean(raw_title)
    record_id = str(record_id or "").strip()
    if not re.fullmatch(r"\d+", record_id):
        found.reject("no record id", title=text, raw=row)
        return
    url = f"https://nass.gov.ng/documents/bill/{record_id}"
    why = sane_title(text)
    if why:
        found.reject(why, url=url, title=text, raw=row)
        return
    numbered = BILL_NUMBER.match(text)
    number, title = (re.sub(r"\s+", " ", numbered.group(1)), numbered.group(2)) if numbered else (None, text)
    found.add(
        url,
        "bill",
        {
            "title": title or text,
            "bill_number": number,
            "year": year_in(title or text),
            "chamber": clean(chamber),
            "readings": {
                k: clean(v)
                for k, v in (("first", first), ("second", second), ("committee", committee), ("third", third))
                if clean(v)
            },
            "record_id": record_id,
        },
        raw=row,
        listing_url=listing_url,
        record_url=url,
    )


# --- National Library repository (OAI-PMH) -------------------------------------------------------

OAI = "https://nigeriareposit.nln.gov.ng/server/oai/request"
NLN_API = "https://nigeriareposit.nln.gov.ng/server/api"
NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "x": "http://www.lyncode.com/xoai"}


def read_nln(client, found: Found, state: dict, log=print, budget=None) -> None:
    failures = Failures()
    for set_spec, collection in SOURCES["nln-repository"]["sets"].items():
        entries = []
        try:
            entries = [
                (url, *nln_oai(r))
                for url, r in _oai_list(client, f"{OAI}?verb=ListRecords&metadataPrefix=xoai&set={set_spec}")
            ]
            failures.ok()
        except polite.Unreachable as error:
            found.notes.append(f"{set_spec}: ListRecords failed ({error}); read record by record")
            for identifier in _oai_identifiers(client, set_spec):
                url = f"{OAI}?verb=GetRecord&metadataPrefix=xoai&identifier={quote(identifier, safe=':/')}"
                try:
                    entries += [(page, *nln_oai(r)) for page, r in _oai_list(client, url)]
                    failures.ok()
                except polite.Unreachable as again:
                    found.notes.append(f"{identifier}: GetRecord failed ({again}); read from the REST API")
                    try:
                        entries.append(nln_rest(client, identifier))
                        failures.ok()
                    except (polite.Unreachable, SourceStopped) as third:
                        found.reject("the data interface failed for this record", identifier=identifier)
                        failures.failed(str(third))
        for entry in entries:
            nln_entry(found, *entry, set_spec=set_spec, collection=collection)
        log(f"nln-repository: {set_spec} ({collection}): {len(entries)} records")


def _oai_list(client, url: str) -> list[tuple[str, object]]:
    """Every <record> of an OAI-PMH answer with the request it came in, following resumption tokens."""
    from xml.etree import ElementTree

    records = []
    while url:
        answer = _ok(client.get(url))
        try:
            root = ElementTree.fromstring(answer.content)
        except ElementTree.ParseError as error:
            raise SourceStopped(f"not OAI-PMH XML at {url}: {error}") from error
        error = root.find("oai:error", NS)
        if error is not None:
            if error.get("code") == "noRecordsMatch":
                return records
            raise SourceStopped(f"OAI-PMH error {error.get('code')}: {error.text}")
        records += [(url, record) for record in root.findall(".//oai:record", NS)]
        token = root.find(".//oai:resumptionToken", NS)
        verb = parse_qs(urlsplit(url).query).get("verb", ["ListRecords"])[0]
        url = (
            f"{OAI}?verb={verb}&resumptionToken={quote(token.text)}"
            if token is not None and token.text
            else ""
        )
    return records


def _oai_identifiers(client, set_spec: str) -> list[str]:
    from xml.etree import ElementTree

    out, url = [], f"{OAI}?verb=ListIdentifiers&metadataPrefix=oai_dc&set={set_spec}"
    while url:
        root = ElementTree.fromstring(_ok(client.get(url)).content)
        out += [
            h.findtext("oai:identifier", namespaces=NS)
            for h in root.findall(".//oai:header", NS)
            if h.get("status") != "deleted"
        ]
        token = root.find(".//oai:resumptionToken", NS)
        url = (
            f"{OAI}?verb=ListIdentifiers&resumptionToken={quote(token.text)}"
            if token is not None and token.text
            else ""
        )
    return out


def xoai_fields(metadata) -> tuple[dict, list[dict], dict]:
    """The Dublin Core fields of an xoai record ({"dc.title": [...]} ...), its ORIGINAL files, and the
    repository's own fields (handle, last modified)."""
    x = "{http://www.lyncode.com/xoai}"
    fields: dict[str, list[str]] = {}

    def walk(element, path):
        for child in element:
            if child.tag == f"{x}element":
                name = child.get("name")
                if path == [] and name in ("bundles", "others", "repository", "license"):
                    continue
                walk(child, path + [name])
            elif child.tag == f"{x}field" and child.get("name") == "value" and child.text:
                key = ".".join(path[:-1]) if len(path) > 1 else ".".join(path)
                fields.setdefault(key, []).append(child.text.strip())

    root = metadata.find(f"{x}metadata")
    if root is None:
        return {}, [], {}
    walk(root, [])
    files = []
    for bundle in root.iter(f"{x}element"):
        if bundle.get("name") != "bundle" or (bundle.findtext(f"{x}field[@name='name']") or "") != "ORIGINAL":
            continue
        for bitstream in bundle.iter(f"{x}element"):
            if bitstream.get("name") == "bitstream":
                files.append({f.get("name"): (f.text or "").strip() for f in bitstream.findall(f"{x}field")})
    others = {}
    for element in root.findall(f"{x}element"):
        if element.get("name") == "others":
            others = {f.get("name"): (f.text or "").strip() for f in element.findall(f"{x}field")}
    return fields, files, others


def gazette_number(text: str) -> dict:
    """'No. 120 Vol. 106', 'Vol.106 No.120' -> {"gazette_number": 120, "gazette_volume": 106}."""
    out = {}
    number = re.search(r"\bNo\.?\s*(\d+)", text or "", re.I)
    volume = re.search(r"\bVol\.?\s*(\d+)", text or "", re.I)
    if number:
        out["gazette_number"] = int(number.group(1))
    if volume:
        out["gazette_volume"] = int(volume.group(1))
    return out


def nln_oai(record) -> tuple:
    """An OAI-PMH record as (identifier, deleted, fields, files, others, raw XML)."""
    from xml.etree import ElementTree

    header = record.find("oai:header", NS)
    identifier = header.findtext("oai:identifier", namespaces=NS) if header is not None else None
    deleted = header is not None and header.get("status") == "deleted"
    metadata = record.find("oai:metadata", NS)
    fields, files, others = xoai_fields(metadata) if metadata is not None else ({}, [], {})
    raw = ElementTree.tostring(record, encoding="unicode")
    raw = re.sub(r'(<ns\d+:field name="bin">)[^<]{200,}(</ns\d+:field>)', r"\1[licence text, cut]\2", raw)
    return identifier, deleted, fields, files, others, raw


def nln_rest(client, identifier: str) -> tuple:
    """One record from the repository's REST API (its handle, the item, its ORIGINAL bundle's files):
    the same shape as an OAI-PMH record, for a record the OAI interface fails on."""
    handle = identifier.rsplit(":", 1)[-1]
    url = f"{NLN_API}/pid/find?id={quote(handle)}"
    item = _json(client.get(url))
    fields = {
        key: [v.get("value") for v in values if v.get("value")]
        for key, values in (item.get("metadata") or {}).items()
    }
    files, raw = [], {"item": item, "bitstreams": []}
    bundles = _json(client.get(f"{NLN_API}/core/items/{item['uuid']}/bundles"))
    for bundle in (bundles.get("_embedded") or {}).get("bundles", []):
        if bundle.get("name") != "ORIGINAL":
            continue
        listing = _json(client.get(bundle["_links"]["bitstreams"]["href"]))
        for b in (listing.get("_embedded") or {}).get("bitstreams", []):
            raw["bitstreams"].append(b)
            files.append(
                {
                    "name": b.get("name"),
                    "size": str(b.get("sizeBytes") or ""),
                    "url": f"https://nigeriareposit.nln.gov.ng/bitstreams/{b.get('uuid')}/download",
                    "checksum": (b.get("checkSum") or {}).get("value"),
                    "checksumAlgorithm": (b.get("checkSum") or {}).get("checkSumAlgorithm"),
                    "sid": str(b.get("sequenceId") or ""),
                }
            )
    others = {"handle": item.get("handle"), "lastModifyDate": item.get("lastModified")}
    return url, identifier, False, fields, files, others, raw


SECONDARY = re.compile(
    r"\bessays?\b|\bstory\b|\bthemes\b|\bhandbook\b|\bintroduction\b|\breports? of cases\b|\blaw reports?\b"
    r"|\bdebates\b|\bmanual\b|\bgazetteers?\b|\byear ?book\b|\bbudget\b|\b\d+ years\b|\bwhite paper\b"
    r"|\bsessional paper\b|\bconstitutionalism\b|^table of\b|\bcompendium\b|\bproposed amendment\b",
    re.I,
)


def nln_entry(
    found: Found, listing_url, identifier, deleted, fields, files, others, raw, set_spec: str, collection: str
) -> None:
    """One repository record: an item per ORIGINAL file, with its record verbatim and the details its
    Dublin Core fields give, normalised. A secondary work (a book about the law, law reports, debates,
    a manual or an index) is left out; a title naming no kind of instrument takes its collection's
    (the gazettes collection also holds the Head of Service's circulars)."""
    if deleted:
        found.reject("deleted in the repository", identifier=identifier)
        return
    title = clean((fields.get("dc.title") or [""])[0])
    first = lambda key: (fields.get(key) or [None])[0]  # noqa: E731
    why = sane_title(title)
    if why:
        found.reject(why, identifier=identifier, title=title)
        return
    if SECONDARY.search(title):
        found.reject(
            "a secondary work (a book, law reports, debates, a manual or an index), not a legal instrument",
            identifier=identifier,
            title=title,
            item_type=first("dc.type"),
        )
        return
    kind, type_from = law_type(title), "title"
    if kind is None:
        type_from = "collection"
        kind = "act" if "laws" in collection.lower() else "circular"
    series = first("dc.relation.ispartofseries")
    details = {
        "title": title,
        "year": year_in(title) or year_in(first("dc.date.issued") or ""),
        "date_issued": first("dc.date.issued"),
        "date_accessioned": first("dc.date.accessioned"),
        "publisher": first("dc.publisher"),
        "series": series,
        **gazette_number(series or (title if kind == "gazette" else "")),
        "abstract": first("dc.description.abstract"),
        "description": first("dc.description"),
        "language": first("dc.language.iso"),
        "item_type": first("dc.type"),
        "authors": fields.get("dc.contributor.author"),
        "subjects": fields.get("dc.subject"),
        "handle": others.get("handle"),
        "collection": collection,
        "oai_set": set_spec,
        "oai_identifier": identifier,
        "last_modified": others.get("lastModifyDate"),
        "type_from": type_from,
        "metadata": fields,
    }
    record_url = first("dc.identifier.uri")
    if not files:
        found.no_file(kind, details, "the record has no file", record_url=record_url, listing_url=listing_url)
        return
    for bitstream in files:
        uuid = re.search(r"/bitstreams/([0-9a-f-]{36})/", bitstream.get("url", ""))
        if not uuid:
            found.reject("a file without a repository address", identifier=identifier, file=bitstream)
            continue
        url = f"{NLN_API}/core/bitstreams/{uuid.group(1)}/content"
        size = bitstream.get("size")
        size = int(size) if size and size.isdigit() else None
        found.add(
            url,
            kind,
            {
                **details,
                "file": {
                    "name": bitstream.get("name"),
                    "original_name": bitstream.get("originalName"),
                    "format": bitstream.get("format"),
                    "size_bytes": size,
                    "checksum": bitstream.get("checksum"),
                    "checksum_algorithm": bitstream.get("checksumAlgorithm"),
                    "sequence": bitstream.get("sid"),
                    "repository_url": bitstream.get("url"),
                },
                "size_bytes": size,
            },
            raw=raw,
            listing_url=listing_url,
            record_url=record_url,
        )


# --- Ministry of Justice ---------------------------------------------------------------------------


def read_moj(client, found: Found, state: dict, log=print, budget=None) -> None:
    page_url = SOURCES["moj-constitution"]["url"]
    soup = _soup(client.get(page_url))
    heading = soup.select_one("h1.entry-title")
    title = clean(heading.get_text(" ") if heading else "")
    posted = soup.select_one("span.post-date")
    modified = soup.select_one("p.modified")
    category = soup.select_one("span.categs a")
    posted_text = clean(posted.get_text(" ")).strip(" |") if posted else ""
    modified_text = clean(modified.get_text(" ")) if modified else ""
    entry = soup.select_one("div.entry") or soup
    links = [a["href"] for a in entry.find_all("a", href=True) if a["href"].lower().endswith(".pdf")]
    if not links:
        raise SourceStopped("the Constitution page has no PDF link")
    media, media_url = {}, None
    try:
        stem = file_name(links[0]).rsplit(".", 1)[0]
        media_url = f"https://justice.gov.ng/wp-json/wp/v2/media?search={quote(stem)}&per_page=20"
        for m in _json(client.get(media_url)):
            media[https(m.get("source_url", ""))] = m
    except SourceStopped as error:
        found.notes.append(f"the media API did not give the file's size: {error}")
    meta = soup.select_one("p.meta")
    for link in links:
        url = https(urljoin(page_url, link))
        why = on_path(url, {"justice.gov.ng"}, r"^/wp-content/uploads/")
        if why:
            found.reject(why, url=url)
            continue
        m = media.get(url, {})
        found.add(
            url,
            law_type(title) or "constitution",
            {
                "title": title,
                "posted": to_date(posted_text),
                "posted_as_listed": posted_text,
                "last_modified": to_date(re.sub(r"^Last modified:\s*", "", modified_text)),
                "last_modified_as_listed": modified_text,
                "category": clean(category.get_text(" ")) if category else None,
                "file_name": file_name(url),
                "link_as_listed": link,
                "media_id": m.get("id"),
                "media_date": m.get("date"),
                "media_title": clean((m.get("title") or {}).get("rendered")),
                "mime_type": m.get("mime_type"),
                "size_bytes": m.get("filesize") or (m.get("media_details") or {}).get("filesize"),
                "media_api": media_url if m else None,
            },
            raw={
                "post": "".join(filter(None, (fragment(heading), fragment(meta), fragment(modified))))
                or None,
                "media": m or None,
            },
            listing_url=page_url,
            record_url=page_url,
        )


# --- Supreme Court ---------------------------------------------------------------------------------

SC_API = "https://supreme-cms.supremecourt.gov.ng/api/v1.0"
SC_FILES = "scnwebsites3.s3.af-south-1.amazonaws.com"
SC_CAP = 50  # the most records one search returns


def read_supreme_court(client, found: Found, state: dict, log=print, budget=None) -> None:
    records: dict[int, tuple[dict, str]] = {}
    end = date.fromisoformat(state.get("run_date") or date.today().isoformat())
    windows = [(date(1960, 1, 1), end)]
    searches = 0
    while windows:
        low, high = windows.pop(0)
        url = (
            f"{SC_API}/judgment-new/search?suitNo=&judgmentDate={low.isoformat()}"
            f"&judgmentDate2={high.isoformat()}&caseTitle="
        )
        data = _json(client.get(url)).get("data") or []
        searches += 1
        if len(data) >= SC_CAP and low < high:
            middle = low + (high - low) / 2
            windows[:0] = [(low, middle), (middle + timedelta(days=1), high)]
            continue
        if len(data) >= SC_CAP:
            found.complete = False
            found.notes.append(f"{low}: one day lists {len(data)} records, the most a search returns")
        for record in data:
            records[record["case_id"]] = (record, url)
    log(f"supreme-court: {len(records)} records in {searches} searches")
    failures = Failures()
    for case_id in sorted(records):
        record, listing_url = records[case_id]
        detail = None
        if _sc_url(record.get("pdfFile")) or _sc_url(record.get("watermark_settings")):
            detail_url = f"{SC_API}/judgment/passed/{case_id}"
            answer = client.get(detail_url)
            if answer.status == 200:
                failures.ok()
                detail = answer.json()
            else:
                found.notes.append(
                    f"case {case_id}: detail HTTP {answer.status}; its opinions may be missing"
                )
                failures.failed(f"HTTP {answer.status}")
        sc_record(found, record, detail, listing_url)
    for path, kind in (
        ("supreme-court-rules?active=1", "rules"),
        ("supreme-court-rules?active=0", "rules"),
        ("mediation-rule", "rules"),
        ("practice-direction", "practice_direction"),
        ("supreme-court-acts?active=1", "act"),
        ("supreme-court-acts?active=0", "act"),
    ):
        url = f"{SC_API}/{path}"
        while url:
            body = _json(client.get(url)).get("data") or {}
            listing = body.get("courtrules") or body.get("courtacts") or {}
            for entry in listing.get("data") or []:
                sc_rule(found, entry, kind, url)
            nxt = listing.get("next_page_url")
            url = nxt if nxt and urlsplit(nxt).hostname == urlsplit(SC_API).hostname else ""
    found.cursor = {"records": len(records), "searches": searches}


def _sc_url(value) -> str | None:
    """A file address of the court's bucket, or None (the watermark field sometimes holds settings)."""
    value = (value or "").strip() if isinstance(value, str) else ""
    return value if value.startswith("https://") else None


def _justices(text: str | None) -> list[str]:
    parts = [clean(j) for j in re.split(r"[;,]", text or "")]
    return [p for p in parts if p and not re.fullmatch(r"J\.?S\.?C\.?|JCA", p)]


def sc_record(found: Found, record: dict, detail: dict | None, listing_url: str) -> None:
    """One judgment record: one item for the judgment (the original PDF, the watermarked one kept as
    its other rendition), one for each concurring or other opinion (linked to the judgment), or, for
    the older records, the court's own data record that holds the text."""
    if detail:
        record = {**record, **(detail.get("data") or {})}
    contributors = (detail or {}).get("contributors") or []
    text = record.get("judgement") or ""
    case_id = record.get("case_id")
    kind = "ruling" if (record.get("type") or "").strip().lower().startswith("ruling") else "judgment"
    raw = {
        "record": {k: v for k, v in record.items() if k != "judgement"}
        | ({"judgement_characters": len(text)} if text else {})
    }
    if contributors:
        raw["contributors"] = contributors
    details = {
        "case_id": case_id,
        "record_type": record.get("type"),
        "suit_no": clean(record.get("suitNo")),
        "parties": {
            "petitioner": clean(record.get("petitioner")),
            "respondent": clean(record.get("respondent")),
        },
        "case_title": clean(record.get("caseTitle")),
        "case_type": clean(record.get("caseType")),
        "judgment_date": to_date(record.get("dateJudgement")),
        "year": record.get("year"),
        "lead_judge": clean(record.get("leadJudgeName")),
        "main_judge": clean(record.get("mainjudge")),
        "justices": _justices(record.get("judgeName")),
        "justices_as_listed": record.get("judgeName"),
        "dissenting": clean(record.get("dissentingJudgeName")),
        "counsel": {
            "petitioner": clean(record.get("petCounsel")),
            "respondent": clean(record.get("respCounsel")),
        },
        "citation": clean(record.get("citation")),
        "decision": clean(record.get("decision")),
        "court_room_id": record.get("court_room_id"),
    }
    details = {
        k: ({a: b for a, b in v.items() if b} if isinstance(v, dict) else v) for k, v in details.items()
    }
    page = f"https://supremecourt.gov.ng/judgment-delivered/{case_id}"
    original, watermarked = _sc_url(record.get("pdfFile")), _sc_url(record.get("watermark_settings"))
    if record.get("watermark_settings") and not watermarked:
        details["watermark_settings_as_listed"] = record.get("watermark_settings")
    lead = original or watermarked
    if lead:
        why = on_path(lead, {SC_FILES}, r"\.pdf$")
        if why:
            found.reject(why, url=lead, case_id=case_id)
            return
        copies = {
            "copy": "original" if original else "watermarked",
            "other_copy": "watermarked" if original and watermarked else None,
        }
        found.add(
            lead,
            kind,
            {**details, "opinion": "lead", **copies},
            raw=raw,
            listing_url=listing_url,
            record_url=page,
            alternates=[watermarked] if original and watermarked else None,
        )
        for contributor in contributors:
            first = _sc_url(contributor.get("original_pdf") or contributor.get("pdf"))
            other = _sc_url(contributor.get("watermarked_pdf"))
            url = first or other
            if not url or on_path(url, {SC_FILES}, r"\.pdf$"):
                found.reject(
                    "an opinion without a file of the court's", case_id=case_id, contributor=contributor
                )
                continue
            found.add(
                url,
                kind,
                {
                    **{
                        k: details.get(k)
                        for k in ("case_id", "suit_no", "parties", "judgment_date", "year", "lead_judge")
                    },
                    "opinion": "contribution",
                    "justice": clean(contributor.get("justice")),
                    "copy": "original" if first else "watermarked",
                    "part_of": {"case_id": case_id, "url": lead},
                },
                raw={"contributor": contributor},
                listing_url=f"{SC_API}/judgment/passed/{case_id}",
                record_url=page,
                alternates=[other] if first and other else None,
            )
    elif text:  # older records: no file; the text is in the court's own data record
        found.add(
            f"{SC_API}/judgment/passed/{case_id}",
            kind,
            {
                **details,
                "copy": "the court's data record (its 'judgement' field holds the text)",
                "text_characters": len(text),
            },
            raw=raw,
            listing_url=listing_url,
            record_url=page,
        )
    else:
        found.no_file(kind, details, "no file and no text", record_url=page, listing_url=listing_url)


def sc_rule(found: Found, entry: dict, kind: str, listing_url: str) -> None:
    url = _sc_url(entry.get("document") or entry.get("file_name")) or ""
    title = clean(entry.get("title") or entry.get("file_description"))
    why = on_path(url, {SC_FILES}, r"\.pdf$") or sane_title(title)
    if why:
        found.reject(why, url=url, title=title, listing=listing_url)
        return
    found.add(
        url,
        kind,
        {
            "title": title,
            "contents": clean(entry.get("content")),
            "created": to_date(entry.get("created_at")),
            "updated": to_date(entry.get("updated_at")),
            "active": entry.get("active"),
            "entry_id": entry.get("id") or entry.get("ID"),
        },
        raw=entry,
        listing_url=listing_url,
    )


# --- National Industrial Court -------------------------------------------------------------------

NICN = "https://nicnadr.gov.ng/judgement/"
NICN_HEAD = 65536  # the first part of a record page holds its header


def nicn_front(soup) -> dict[int, dict]:
    """The portal's newest records as its front page lists them: id -> suit, judge, the dashes,
    parties, and the card's HTML."""
    out = {}
    for card in soup.select("div.blog-card"):
        link = card.find("a", href=re.compile(r"judgement\.php\?id=\d+"))
        if not link:
            continue
        record_id = int(re.search(r"id=(\d+)", link["href"]).group(1))
        spans = [clean(s.get_text(" ")) for s in card.select("div.blog-meta span")]
        paragraphs = [clean(p.get_text(" ")) for p in card.find_all("p") if not p.find("span")]
        out[record_id] = {
            "suit_no": clean(card.find("h4").get_text(" ")) if card.find("h4") else None,
            "judge": spans[0] if spans else None,
            "second_field": spans[1] if len(spans) > 1 else None,
            "parties": paragraphs[-1] if paragraphs else None,
            "html": fragment(card),
        }
    return out


def nicn_page(page_html: str) -> dict:
    """What the head of a record page says: the parties (its title), the header text as printed, and
    the suit number, division, judge, date and representation read from that header. Older records
    open with a structured header (Suit No / Petitioner / Respondent / Date Delivered / Judge(s))."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page_html, "html.parser")
    title = clean(soup.title.get_text(" ")) if soup.title else ""
    parties = re.sub(r"\s*-\s*National Industrial Court of Nigeria\s*$", "", title, flags=re.I).strip(" -")
    text = clean(soup.get_text(" "))
    start = text.find("GET CTC OF THIS JUDGMENT")
    head = text[start + len("GET CTC OF THIS JUDGMENT") :] if start >= 0 else ""
    head = re.sub(r"-{5,}", " ", head).strip()[:1500]
    out: dict = {"page_title": title or None, "parties": parties or None, "header_text": head or None}
    sides = re.split(r"\s+-?VS\.?-?\s+", parties, flags=re.I) if parties else []
    if len(sides) == 2:
        out["claimant"], out["defendant"] = sides[0].strip(" -"), sides[1].strip(" -")
    structured = re.search(
        r"Suit No:\s*(?P<suit>.+?)\s+Petitioner:\s*(?P<pet>.+?)\s+And\s+Respondent:\s*(?P<resp>.+?)\s+"
        r"Date Delivered:\s*(?P<date>\d{4}-\d{2}-\d{2})\s+Judge\(s\):\s*(?P<judge>.+?)\s+"
        r"(?P<cat>Judge?ments?|Rulings?)\s+Delivered",
        head,
        re.I,
    )
    if structured:
        out["structured_header"] = {
            "suit_no": structured.group("suit"),
            "petitioner": structured.group("pet"),
            "respondent": structured.group("resp"),
            "date_delivered": structured.group("date"),
            "judges": structured.group("judge"),
            "category": structured.group("cat"),
        }
    suits = re.findall(r"\b((?:NICN|NIC)\s*/\s*[A-Z]{1,6}\s*/\s*\d{1,6}[A-Z]?\s*/\s*\d{4})", head)
    if suits:
        out["suit_numbers"] = list(dict.fromkeys(re.sub(r"\s+", "", s) for s in suits))
    division = re.search(r"IN THE\s+([A-Z][A-Za-z .'-]{1,40}?)\s+JUDICIAL\s+DIVISION", head, re.I)
    if division:
        out["division"] = division.group(1).strip()
    holden = re.search(r"HOLDEN AT\s+([A-Z][A-Za-z .'-]{1,30}?)(?=\s+(?:BEFORE|DATE|SUIT|ON\b))", head, re.I)
    if holden:
        out["holden_at"] = holden.group(1).strip()
    judge = re.search(
        r"BEFORE\s+(?:HIS|HER|THEIR)\s+LORDSHIPS?\s*:?\s*(.{3,120}?)\s+(?=DATED?\b|Dated|SUIT|ON\s+\w+DAY)",
        head,
        re.I,
    )
    if judge:
        out["judge"] = judge.group(1).strip(" :,")
    when = re.search(r"\bDATED?\s*:?\s*(.{4,60}?)\s+(?=SUIT|BETWEEN)", head, re.I)
    if when:
        printed = when.group(1).strip()
        out["date_as_printed"] = printed
        out["date"] = to_date(re.sub(r"(\d)\s+(st|nd|rd|th)\b", r"\1\2", printed, flags=re.I))
    if structured and not out.get("date"):
        out["date"] = structured.group("date")
    representation = re.search(
        r"Representations?\s*:?\s*(.{3,600}?)\s+(?=RULING/JUDGMENT|JUDGE?MENT\b|RULING\b|Introduction\b)",
        head,
        re.I,
    )
    if representation:
        out["representation"] = representation.group(1).strip(" :")
    heading = re.search(
        r"\b(RULING/JUDGMENT|JUDGE?MENT|RULING)\b", head[representation.end() if representation else 0 :]
    )
    if heading:
        out["heading"] = heading.group(1)
    return {k: v for k, v in out.items() if v}


def read_nicn(client, found: Found, state: dict, log=print, budget=None) -> None:
    found.resumable = True
    found.complete = False  # a run reads a share of the ids: it never says a record is gone
    front = nicn_front(_soup(client.get(NICN)))
    if not front:
        raise SourceStopped("the portal's front page lists no records")
    newest = max(front)
    next_id = int(state.get("next_id") or 1)
    budget = budget or SOURCES["nicn-judgments"]["budget"]
    failures = Failures()
    read = empty = 0
    record_id = next_id
    while record_id <= newest and read < budget:
        url = f"{NICN}judgement.php?id={record_id}"
        read += 1
        try:
            answer = client.get(url, head=NICN_HEAD)
        except polite.Unreachable as error:
            found.reject("did not answer", url=url, record_id=record_id)
            failures.failed(str(error))
            record_id += 1
            continue
        if answer.status != 200:
            found.reject(f"HTTP {answer.status}", url=url, record_id=record_id)
            failures.failed(f"HTTP {answer.status}")
            record_id += 1
            continue
        failures.ok()
        page = nicn_page(answer.text)
        if not page.get("parties") and not page.get("suit_numbers"):
            found.reject("an empty record", url=url, record_id=record_id)
            empty += 1
        else:
            category = (page.get("structured_header") or {}).get("category", "")
            ruling = page.get("heading") == "RULING" or re.match(r"rulings?$", category, re.I)
            card = front.get(record_id) or {}
            found.add(
                url,
                "ruling" if ruling else "judgment",
                {
                    "record_id": record_id,
                    "title": page.get("parties"),
                    "suit_no": (page.get("suit_numbers") or [None])[0],
                    "judgment_date": page.get("date"),
                    "division": page.get("division"),
                    "judge": page.get("judge"),
                    "from_page": {k: v for k, v in page.items() if k != "header_text"},
                    "front_listing": {k: v for k, v in card.items() if k != "html"} or None,
                    "read_bytes": len(answer.content),
                    "page_truncated": answer.truncated,
                },
                raw={
                    "page_title": page.get("page_title"),
                    "header_text": page.get("header_text"),
                    "front_listing": card.get("html"),
                },
                listing_url=url,
                record_url=url,
            )
        if read % 100 == 0:
            log(f"nicn-judgments: id {record_id} of {newest} ({read} read this run, {empty} empty)")
        record_id += 1
    swept = record_id > newest
    found.cursor = {
        "next_id": record_id,
        "newest_id": newest,
        "sweep_finished": swept,
        "sweep_started": state.get("sweep_started") or stamp(now()),
        "read_last_run": read,
    }
    found.notes.append(
        f"ids {next_id} to {record_id - 1} read of {newest}" + ("; sweep finished" if swept else "")
    )


# --- regulators ------------------------------------------------------------------------------------

NCAA_LISTINGS = (
    (
        "Nig.CARs 2023 (Official Gazette)",
        "https://ncaa.gov.ng/documents/regulations/nigcars-2023-official-gazette/",
    ),
    ("Nig. CARs 2023", "https://ncaa.gov.ng/documents/regulations/nig-cars-2023/"),
    ("Nig. CARs 2015 (Superceded)", "https://ncaa.gov.ng/documents/regulations/nig-cars-2015-superceded/"),
    ("Civil Aviation Acts", "https://ncaa.gov.ng/documents/civil-aviation-acts/"),
    ("Civil Aviation Orders", "https://ncaa.gov.ng/documents/civil-aviation-orders/"),
)


def ncaa_cards(soup, listing_url: str) -> list[dict]:
    """The records a listing page shows: link, number, title, excerpt, effective date, card HTML."""
    path = urlsplit(listing_url).path
    out, seen = [], set()
    for card in soup.select("div.item-service"):
        link = card.select_one("h5 a[href]")
        if not link:
            continue
        href = urljoin(listing_url, link["href"])
        if not urlsplit(href).path.startswith(path) or urlsplit(href).path == path or href in seen:
            continue
        seen.add(href)
        number = card.select_one("small.no")
        effective = [s for s in card.find_all("small") if "Effective Date" in s.get_text()]
        excerpt = card.select_one("div.service-content p")
        index = card.select_one("h5 b")
        out.append(
            {
                "record_page": href,
                "title": clean(link.get_text(" ")),
                "index": clean(index.get_text(" ")) if index else None,
                "document_no": clean(number.get_text(" ")).replace("Document No.", "").strip()
                if number
                else None,
                "effective_date_as_listed": clean(effective[0].get_text(" "))
                .replace("Effective Date:", "")
                .strip()
                if effective
                else None,
                "excerpt": clean(excerpt.get_text(" ")) if excerpt else None,
                "html": fragment(card),
            }
        )
    return out


def ncaa_record(soup, page_url: str) -> tuple[dict, list[tuple[str, str]], str | None]:
    """A record page's details (Category, Number, Issue Date, Effective Date, Is Current), its
    download links (address, link text), and the HTML of its details."""
    details: dict = {}
    metas = soup.select_one("div.metas")
    for meta in soup.select("div.metas div.meta h4"):
        label = meta.find("span")
        if not label:
            continue
        name = clean(label.get_text(" ")).rstrip(":").strip()
        value = clean(meta.get_text(" ")).replace(clean(label.get_text(" ")), "", 1).strip()
        if name and name.lower() != "download" and not meta.find("a", href=re.compile(r"/media/")):
            details[name] = value
    description = soup.select_one("div.entry-content")
    if description:
        details["Description"] = clean(description.get_text(" "))
    links = []
    for a in soup.select("div.metas a[href]"):
        href = urljoin(page_url, a["href"])
        if "/media/" in urlsplit(href).path:
            links.append((href, clean(a.get_text(" "))))
    raw = "".join(filter(None, (fragment(description), fragment(metas))))
    return details, links, raw or None


def read_ncaa(client, found: Found, state: dict, log=print, budget=None) -> None:
    failures = Failures()
    for set_name, listing_url in NCAA_LISTINGS:
        cards = ncaa_cards(_soup(client.get(listing_url)), listing_url)
        log(f"ncaa: {set_name}: {len(cards)} records")
        for card in cards:
            answer = client.get(card["record_page"])
            if answer.status != 200:
                found.reject(
                    f"record page HTTP {answer.status}", **{k: v for k, v in card.items() if k != "html"}
                )
                failures.failed(f"HTTP {answer.status}")
                continue
            failures.ok()
            details, links, record_html = ncaa_record(_soup(answer), card["record_page"])
            category = (details.get("Category") or "").lower()
            kind = (
                {
                    "regulation": "regulation",
                    "act": "act",
                    "acts": "act",
                    "order": "order",
                    "orders": "order",
                }.get(category)
                or law_type(card["title"])
                or ("act" if "Acts" in set_name else "order" if "Orders" in set_name else "regulation")
            )
            listed = {
                **{k: v for k, v in card.items() if k not in ("html", "record_page")},
                "set": set_name,
                "category": details.get("Category"),
                "number": details.get("Number"),
                "issue_date": to_date(details.get("Issue Date")),
                "issue_date_as_listed": details.get("Issue Date"),
                "effective_date": to_date(
                    details.get("Effective Date") or card.get("effective_date_as_listed")
                ),
                "effective_date_as_listed": details.get("Effective Date"),
                "is_current": details.get("Is Current"),
                "description": details.get("Description"),
                "record_details": details,
            }
            raw = {"listing_card": card["html"], "record": record_html}
            if not links:
                found.no_file(
                    kind, listed, "the record page has no download link", record_url=card["record_page"]
                )
            for url, text in links:
                why = on_path(url, {"ncaa.gov.ng"}, r"^/media/[a-z0-9]+/.+\.(pdf|docx?)$") or sane_title(
                    card["title"]
                )
                if why:
                    found.reject(why, url=url, title=card["title"])
                    continue
                found.add(
                    url,
                    kind,
                    {**listed, "link_text": text, "file_name": file_name(url)},
                    raw=raw,
                    listing_url=listing_url,
                    record_url=card["record_page"],
                )


NCC_PAGES = (
    ("regulations", "https://ncc.gov.ng/operators/regulations-guidelines/regulations", "regulation"),
    ("guidelines", "https://ncc.gov.ng/guidelines", "guideline"),
    ("determinations", "https://ncc.gov.ng/industry/regulations-guidelines/determinations", "order"),
)
NOT_AN_INSTRUMENT = re.compile(r"consultation|\bpaper\b|\breply\b|\breport\b|landmark judgements", re.I)


def read_ncc(client, found: Found, state: dict, log=print, budget=None) -> None:
    for page_name, url, default in NCC_PAGES:
        soup = _soup(client.get(url))
        intro = soup.find(string=re.compile(r"Section \d+ of the Nigerian Communications Act", re.I))
        power = clean(intro) if intro else None
        if page_name == "determinations":
            for media in soup.select("div.media--type-document"):
                link = media.select_one("div.field--name-media-download a[href]") or media.select_one(
                    "a[href*='/media/']"
                )
                if not link:
                    continue
                description = media.select_one(
                    "div.field--name-field-description .field__item"
                ) or media.select_one("div.field--name-field-description")
                tags = [
                    clean(t.get_text(" "))
                    for t in media.select("div.field--name-field-tag-s div.field__item")
                ]
                ncc_item(
                    found,
                    urljoin(url, link["href"]),
                    clean(link.get_text(" ")),
                    default,
                    {
                        "page": page_name,
                        "description": clean(description.get_text(" ")) if description else None,
                        "tags": tags,
                        "kind_named": "Determination",
                    },
                    raw=fragment(media),
                    listing_url=url,
                )
            continue
        for section in soup.select("div.accordion-collapse") or [soup]:
            heading = None
            labelled = section.get("aria-labelledby")
            if labelled and soup.find(id=labelled):
                heading = clean(soup.find(id=labelled).get_text(" "))
            if not heading:
                previous = section.find_previous(["h2", "button"])
                heading = clean(previous.get_text(" ")) if previous else None
            for link in section.select("a[href*='/media/'], a[href*='/sites/default/files/']"):
                row = link.find_parent("div", class_="views-field") or link.parent
                ncc_item(
                    found,
                    urljoin(url, link["href"]),
                    clean(link.get_text(" ")),
                    default,
                    {
                        "page": page_name,
                        "section": heading,
                        "status": (heading or "").lower() or None,
                        "power": power,
                    },
                    raw=fragment(row),
                    listing_url=url,
                )


def ncc_item(
    found: Found, url: str, title: str, default: str, extra: dict, raw=None, listing_url=None
) -> None:
    why = on_path(
        url, {"ncc.gov.ng"}, r"^/media/\d+/view$|^/sites/default/files/.+\.(pdf|docx?)$"
    ) or sane_title(title)
    if why:
        found.reject(why, url=url, title=title)
        return
    if NOT_AN_INSTRUMENT.search(title):
        found.reject("not an instrument (a consultation paper, reply or report)", url=url, title=title)
        return
    kind = "rules" if re.search(r"business rules|\bcode\b", title, re.I) else default
    found.add(url, kind, {"title": title, "year": year_in(title), **extra}, raw=raw, listing_url=listing_url)


SEC_PAGES = (
    ("rules and regulations", "https://sec.gov.ng/our-mandate/regulation/rules-and-regulations/", "rules"),
    ("acts", "https://sec.gov.ng/our-mandate/regulation/acts/", "act"),
    ("guidelines", "https://sec.gov.ng/our-mandate/regulation/guidelines/", "guideline"),
)
SEC_CIRCULARS = "https://sec.gov.ng/for-investors/keep-track-of-circulars/"
GENERIC_LINK = re.compile(r"^download(?: the)?(?: full)?(?: document)?$", re.I)


def sec_title(heading: str | None, label: str) -> str | None:
    """A document's title: its link's own words where they name an instrument ("Download the Full
    Investment and Securities Act, 2025"), else the heading above it, else the link's words."""
    named = re.sub(r"^download\s+(?:the\s+)?(?:full\s+)?", "", label or "", flags=re.I).strip()
    if named and law_type(named) and not GENERIC_LINK.match(label or ""):
        return named
    return heading or (None if GENERIC_LINK.match(label or "") else label)


def sec_blocks(soup, page_url: str) -> list[dict]:
    """The documents of a Wagtail page, each with the heading and paragraphs before it and their HTML:
    a download block's file, or a file linked inside a paragraph (an item of a list, titled by its
    own link, or a "Download full document" link under a heading)."""
    out, heading, paragraphs, html_parts = [], None, [], []

    def document(link, title, description, size, html_text) -> None:
        out.append(
            {
                "url": urljoin(page_url, link["href"]),
                "title": title,
                "link_text": clean(link.get_text(" ")),
                "description": description[:2000] or None,
                "description_truncated": len(description) > 2000 or None,
                "size_as_listed": size,
                "size_bytes": filelist.size_bytes(size.split("•")[-1].strip()) if size else None,
                "html": html_text,
            }
        )

    for block in soup.select("div.block-heading_block, div.block-paragraph_block, div.block-document"):
        classes = block.get("class") or []
        if "block-heading_block" in classes:
            heading, paragraphs, html_parts = clean(block.get_text(" ")), [], [fragment(block)]
        elif "block-paragraph_block" in classes:
            links = block.find_all("a", href=re.compile(r"/documents/\d+/"))
            if not links:
                paragraphs.append(clean(block.get_text(" ")))
                html_parts.append(fragment(block, 4000))
                continue
            for link in links:
                label = clean(link.get_text(" "))
                entry = link.find_parent("li")
                if entry is not None and not GENERIC_LINK.match(label):  # a list of titled files
                    rest = clean(entry.get_text(" ")).replace(label, "", 1).strip(" :-")
                    document(link, label, rest, None, fragment(entry))
                else:
                    text = clean(block.get_text(" ")).replace(label, " ").strip()
                    title = sec_title(heading, label)
                    description = " ".join(p for p in [*paragraphs, text] if p)
                    document(
                        link,
                        title,
                        description,
                        None,
                        "".join(filter(None, [*html_parts, fragment(block, 4000)])),
                    )
            heading, paragraphs, html_parts = None, [], []
        else:
            link = block.find("a", href=True)
            if not link:
                continue
            spans = [clean(s.get_text(" ")) for s in link.find_all("span")]
            label = spans[0] if spans else clean(link.get_text(" "))
            size = next((s for s in spans if re.search(r"\b[KMG]?B\b", s)), None)
            title = sec_title(heading, label)
            description = " ".join(p for p in paragraphs if p)
            document(link, title, description, size, "".join(filter(None, [*html_parts, fragment(block)])))
            out[-1]["link_text"] = label
            heading, paragraphs, html_parts = None, [], []
    return out


def read_sec(client, found: Found, state: dict, log=print, budget=None) -> None:
    for page_name, url, default in SEC_PAGES:
        soup = _soup(client.get(url))
        published = soup.find(string=re.compile(r"Published:"))
        for block in sec_blocks(soup, url):
            title = block["title"] or block["link_text"]
            why = on_path(block["url"], {"sec.gov.ng"}, r"^/documents/\d+/.+") or sane_title(title)
            if why:
                found.reject(why, url=block["url"], title=title)
                continue
            kind = default
            if (
                default == "rules"
                and re.search(r"regulation", title, re.I)
                and not re.search(r"\brules?\b", title, re.I)
            ):
                kind = "regulation"
            found.add(
                block["url"],
                kind,
                {
                    **{k: v for k, v in block.items() if k not in ("url", "html")},
                    "title": title,
                    "year": year_in(title),
                    "page": page_name,
                    "page_published_as_listed": clean(published) if published else None,
                    "file_name": file_name(block["url"]),
                },
                raw=block["html"],
                listing_url=url,
            )
    soup = _soup(client.get(SEC_CIRCULARS))
    for link in soup.select("a[href*='/keep-track-of-circulars/']"):
        href = urljoin(SEC_CIRCULARS, link["href"])
        if href.rstrip("/") == SEC_CIRCULARS.rstrip("/") or not urlsplit(href).path.startswith(
            "/for-investors/"
        ):
            continue
        spans = [clean(s.get_text(" ")) for s in link.find_all("span")]
        title = spans[0] if spans else clean(link.get_text(" "))
        when = next((s for s in spans[1:] if to_date(s)), None)
        why = on_path(
            href, {"sec.gov.ng"}, r"^/for-investors/keep-track-of-circulars/[a-z0-9_-]+/$"
        ) or sane_title(title)
        if why:
            found.reject(why, url=href, title=title)
            continue
        found.add(
            href,
            "circular",
            {
                "title": title,
                "date": to_date(when),
                "date_as_listed": when,
                "summary": next((s for s in spans[1:] if s and s != when), None),
                "page": "circulars",
            },
            raw=fragment(link),
            listing_url=SEC_CIRCULARS,
            record_url=href,
        )


NDPC_NOT_LAW = re.compile(r"report|journal|bulletin|white paper|roadmap|policy|authorities", re.I)


def read_ndpc(client, found: Found, state: dict, log=print, budget=None) -> None:
    from bs4 import BeautifulSoup

    url = SOURCES["ndpc"]["url"]
    page = _ok(client.get(url)).text
    soup = BeautifulSoup(page, "html.parser")
    for div in soup.select("div._df_thumb"):
        flipbook = div.get("id") or ""
        options = re.search(r"window\.option_" + re.escape(flipbook) + r"\s*=\s*(\{.*?\});", page)
        try:
            settings = json.loads(options.group(1)) if options else {}
        except ValueError:
            settings = {}
        source = settings.get("source")
        label = clean(div.get_text(" "))
        title = None if label.lower() == "open book" else label
        if not source:
            found.reject("a flipbook without a file", flipbook=flipbook, title=label)
            continue
        file_url = https(source)
        name = file_name(file_url)
        why = on_path(file_url, {"ndpc.gov.ng"}, r"^/wp-content/uploads/.+\.pdf$")
        if why:
            found.reject(why, url=file_url, title=label)
            continue
        if title and NDPC_NOT_LAW.search(title):
            found.reject(
                "not a legal instrument (a report, journal, bulletin or policy)", url=file_url, title=title
            )
            continue
        if title and sane_title(title):
            found.reject(sane_title(title), url=file_url, title=title)
            continue
        if title:
            kind = law_type(title) or "guideline"
        elif "GAID" in name.upper():
            kind = "guideline"  # the General Application and Implementation Directive, untitled on the page
        else:
            found.reject("an untitled flipbook", url=file_url, flipbook=flipbook)
            continue
        language = re.search(r"\b(Yoruba|Hausa|Igbo)\b", title or "", re.I)
        found.add(
            file_url,
            kind,
            {
                "title": title,
                "label": label if not title else None,
                "year": year_in(title or ""),
                "language": language.group(1).capitalize() if language else None,
                "flipbook": flipbook,
                "slug": div.get("_slug"),
                "data_title": div.get("data-title"),
                "thumbnail": div.get("thumb"),
                "file_name": name,
            },
            raw={"flipbook": fragment(div), "settings": settings},
            listing_url=url,
        )


STATEHOUSE_QUERIES = ("executive order", "assent", "signs")
EXECUTIVE_ORDER = re.compile(r"\bexecutive orders?\b", re.I)
EO_ITSELF = re.compile(
    r"\bsigns?\b|\bsigned\b|\bno\.?\s*\d|\bissues?\b|\bfull text\b|^executive order no", re.I
)
ASSENT = re.compile(r"\bassent|\bsigns?\b.*\b(?:bills?|acts?|into law|law)\b|\bsigned into law\b", re.I)


def read_statehouse(client, found: Found, state: dict, log=print, budget=None) -> None:
    fields = "id,date,modified,slug,link,title,categories"
    for query in STATEHOUSE_QUERIES:
        page, pages = 1, 1
        while page <= pages:
            url = (
                f"https://statehouse.gov.ng/wp-json/wp/v2/posts?search={quote(query)}&per_page=100"
                f"&page={page}&_fields={fields}"
            )
            answer = _ok(client.get(url))
            pages = int(answer.headers.get("x-wp-totalpages") or 1)
            for post in answer.json():
                statehouse_post(found, post, query, url)
            page += 1


def statehouse_post(found: Found, post: dict, query: str, listing_url: str) -> None:
    title = clean((post.get("title") or {}).get("rendered"))
    url = post.get("link") or ""
    why = on_path(url, {"statehouse.gov.ng"}, r"^/[a-z0-9-]+/$") or sane_title(title)
    if why:
        found.reject(why, url=url, title=title)
        return
    if EXECUTIVE_ORDER.search(title) and EO_ITSELF.search(title):
        kind = "executive_order"
    elif ASSENT.search(title):
        kind = "press_release"
    else:
        return  # a post the search matched in its body: not an Executive Order or assent announcement
    found.add(
        url,
        kind,
        {
            "title": title,
            "date": (post.get("date") or "")[:10] or None,
            "published": post.get("date"),
            "modified": post.get("modified"),
            "post_id": post.get("id"),
            "slug": post.get("slug"),
            "categories": post.get("categories"),
            "about": "assent" if kind == "press_release" else None,
            "matched_search": query,
        },
        raw=post,
        listing_url=listing_url,
        record_url=url,
    )


NERC_CATEGORIES = (("regulations", "regulation"), ("orders", "order"))


def read_nerc(client, found: Found, state: dict, log=print, budget=None) -> None:
    for category, kind in NERC_CATEGORIES:
        page, last = 1, 1
        while page <= last:
            url = f"https://nerc.gov.ng/resource-category/{category}/" + (f"page/{page}/" if page > 1 else "")
            soup = _soup(client.get(url))
            numbers = [
                int(m.group(1))
                for a in soup.find_all("a", href=True)
                if (m := re.search(rf"/resource-category/{category}/page/(\d+)/", a["href"]))
            ]
            last = max([last, *numbers])
            for publication in soup.select("div.publication"):
                nerc_publication(found, publication, kind, category, page, url)
            if page % 10 == 0:
                log(f"nerc: {category} page {page} of {last}")
            page += 1


def nerc_publication(
    found: Found, publication, kind: str, category: str, page: int, listing_url: str
) -> None:
    title_el = publication.select_one(".title")
    title = clean(title_el.get_text(" ")) if title_el else ""
    when = publication.select_one("ul.pub-meta a")
    excerpt = publication.select_one("div.pub-excerpt")
    full = publication.find("a", string=re.compile("Read full text", re.I))
    download = publication.select_one("a.btn-download[download]") or publication.select_one("a.btn-download")
    listed = {
        "title": title,
        "year": year_in(title),
        "posted": to_date(when.get("title") or when.get_text(" ")) if when else None,
        "posted_as_listed": clean(when.get_text(" ")) if when else None,
        "excerpt": clean(excerpt.get_text(" ")) if excerpt else None,
        "category": category,
        "listing_page": page,
    }
    record_url = full["href"] if full else None
    if not download:
        found.no_file(kind, listed, "no file link", record_url=record_url, listing_url=listing_url)
        return
    url = urljoin("https://nerc.gov.ng/", download["href"].strip()).replace(" ", "%20")
    why = on_path(url, {"nerc.gov.ng"}, r"^/wp-content/uploads/") or sane_title(title)
    if why:
        found.reject(why, url=url, title=title)
        return
    if url.lower().endswith(".zip"):
        found.reject("a ZIP archive (schedules), not a document format the fetch takes", url=url, title=title)
        return
    found.add(
        url,
        kind,
        {**listed, "file_name": file_name(url)},
        raw=fragment(publication),
        listing_url=listing_url,
        record_url=record_url,
    )


def read_nafdac(client, found: Found, state: dict, log=print, budget=None) -> None:
    from bs4 import BeautifulSoup

    page = _ok(client.get(SOURCES["nafdac"]["url"])).text.replace("\\u0026", "&").replace("\\/", "/")
    data = re.search(
        r"admin-ajax\.php\?action=wp_ajax_ninja_tables_public_action&table_id=(\d+)&target_action=get-all-data"
        r"[^\"']*?ninja_table_public_nonce=([0-9a-f]+)",
        page,
    )
    if not data:
        raise SourceStopped("the regulations page carries no table data address")
    table, nonce = data.groups()
    url = (
        "https://nafdac.gov.ng/wp-admin/admin-ajax.php?action=wp_ajax_ninja_tables_public_action"
        f"&table_id={table}&target_action=get-all-data&default_sorting=new_first&skip_rows=0&limit_rows=0"
        f"&ninja_table_public_nonce={nonce}"
    )
    for row in _json(client.get(url)):
        value = row.get("value") or {}
        cell = BeautifulSoup(value.get("title") or "", "html.parser")
        title = clean(cell.get_text(" "))
        link = cell.find("a", href=True)
        listed = {
            "title": title,
            "serial": value.get("sn"),
            "year": year_in(title),
            "product_type": clean(value.get("producttype")),
            "category": clean(value.get("category")),
            "status": clean(value.get("status")),
            "table_id": table,
            "row_id": value.get("___id___"),
        }
        if not link:
            found.no_file("regulation", listed, "the row has no file", listing_url=url)
            continue
        file_url = https(link["href"])
        why = on_path(
            file_url, {"nafdac.gov.ng", "www.nafdac.gov.ng"}, r"^/wp-content/uploads/.+\.(pdf|docx?)$"
        )
        why = why or sane_title(title)
        if why:
            found.reject(why, url=file_url, title=title)
            continue
        found.add(
            file_url, "regulation", {**listed, "file_name": file_name(file_url)}, raw=row, listing_url=url
        )


SI_NUMBER = re.compile(r"S\.\s*I\.?\s*No\.?\s*(\d+)", re.I)


def read_nesrea(client, found: Found, state: dict, log=print, budget=None) -> None:
    url = SOURCES["nesrea"]["url"]
    soup = _soup(client.get(url))
    for item in soup.select("details.e-n-accordion-item"):
        summary = item.find("summary")
        title = clean(summary.get_text(" ")) if summary else ""
        region = item.find(attrs={"role": "region"}) or item
        description = " ".join(clean(p.get_text(" ")) for p in region.find_all("p"))
        links = [urljoin(url, a["href"].strip()) for a in region.find_all("a", href=True)]
        nesrea_entry(found, client, title, description, links, url, raw=fragment(item))
    for a in soup.find_all("a", href=True):  # the Acts, linked from the page's menu
        href = urljoin(url, a["href"].strip())
        text = clean(a.get_text(" "))
        if re.search(r"\bAct\b", text) and href.lower().endswith(".pdf") and "/wp-content/uploads/" in href:
            nesrea_entry(
                found,
                client,
                re.sub(r"^Download\s+", "", text),
                None,
                [href],
                url,
                kind="act",
                raw=fragment(a),
            )


def nesrea_entry(
    found: Found, client, title: str, description, links, listing_url, kind=None, raw=None
) -> None:
    why = sane_title(title)
    if why:
        found.reject(why, title=title, links=links)
        return
    kind = kind or law_type(title) or "regulation"
    si = SI_NUMBER.search(title)
    listed = {
        "title": title,
        "si_number": int(si.group(1)) if si else None,
        "year": year_in(title),
        "description": description or None,
    }
    if not links:
        found.no_file(kind, listed, "no file link", listing_url=listing_url)
    for link in links:
        drive = re.search(r"drive\.google\.com/file/d/([\w-]+)", link)
        if drive:
            download = f"https://drive.google.com/uc?export=download&id={drive.group(1)}"
            refused = None if client.permits(download) else DRIVE_REFUSED
            found.add(
                download,
                kind,
                {**listed, "link": link, "drive_id": drive.group(1)},
                raw=raw,
                listing_url=listing_url,
                refused=refused,
            )
            continue
        why = on_path(link, {"nesrea.gov.ng"}, r"^/wp-content/uploads/.+\.(pdf|docx?)$")
        if why:
            found.reject(why, url=link, title=title)
            continue
        found.add(
            https(link), kind, {**listed, "file_name": file_name(link)}, raw=raw, listing_url=listing_url
        )


CAC_KEEP = re.compile(r"regulations?|gazette|\bact\b", re.I)
CAC_NOT_LAW = re.compile(r"note ?book|form\b|result|list\b", re.I)


def read_cac(client, found: Found, state: dict, log=print, budget=None) -> None:
    page, pages, media = 1, 1, []
    fields = "id,date,modified,slug,title,source_url,mime_type,media_details,link,post"
    while page <= pages:
        url = (
            "https://news.cac.gov.ng/wp-json/wp/v2/media?per_page=100&mime_type=application/pdf"
            f"&page={page}&_fields={fields}"
        )
        answer = _ok(client.get(url))
        pages = int(answer.headers.get("x-wp-totalpages") or 1)
        media += [(m, url) for m in answer.json()]
        page += 1
    kept = [entry for entry in media if cac_keep(found, entry[0])]
    # The library holds the same file uploaded again (the same title and size): one document, its
    # later uploads kept as other renditions; different sizes stay apart (flagged as likely duplicates).
    groups: dict[tuple, list] = {}
    for m, url in kept:
        size = (m.get("media_details") or {}).get("filesize")
        key = (_title_key(clean((m.get("title") or {}).get("rendered"))), size) if size else (m.get("id"),)
        groups.setdefault(key, []).append((m, url))
    for group in groups.values():
        group.sort(key=lambda entry: entry[0].get("date") or "")
        (first, url), others = group[0], group[1:]
        cac_media(
            found, first, url, [https(o.get("source_url") or "") for o, _ in others], [o for o, _ in others]
        )


def cac_keep(found: Found, media: dict) -> bool:
    title = clean((media.get("title") or {}).get("rendered"))
    url = https(media.get("source_url") or "")
    if not CAC_KEEP.search(title) or CAC_NOT_LAW.search(title):
        found.reject(
            "not a legal instrument by its title (forms, lists, results, guides)", url=url, title=title
        )
        return False
    why = on_path(url, {"news.cac.gov.ng"}, r"^/wp-content/uploads/.+\.pdf$") or sane_title(title)
    if why:
        found.reject(why, url=url, title=title)
        return False
    return True


def cac_media(
    found: Found, media: dict, listing_url: str, alternates: list[str], other_media: list[dict]
) -> None:
    title = clean((media.get("title") or {}).get("rendered"))
    url = https(media.get("source_url") or "")
    if re.search(r"\bnotice\b", title, re.I):
        kind = "circular"
    elif re.search(r"regulation", title, re.I):
        kind = "regulation"
    elif re.search(r"gazette", title, re.I):
        kind = "gazette"
    else:
        kind = "act"
    details = media.get("media_details") or {}
    found.add(
        url,
        kind,
        {
            "title": title,
            "year": year_in(title),
            "uploaded": (media.get("date") or "")[:10] or None,
            "modified": media.get("modified"),
            "media_id": media.get("id"),
            "slug": media.get("slug"),
            "size_bytes": details.get("filesize"),
            "status": "exposure draft" if re.search(r"exposure draft", title, re.I) else None,
            "copy": "Gazette" if re.search(r"gazette", title, re.I) and kind == "regulation" else None,
            "file_name": file_name(url),
            "title_from": "the file name (as the media library names it)",
            "uploaded_again": [
                {"media_id": o.get("id"), "uploaded": (o.get("date") or "")[:10]} for o in other_media
            ],
        },
        raw={"media": media, "uploaded_again": other_media or None},
        listing_url=listing_url,
        record_url=media.get("link"),
        alternates=alternates,
    )


def read_naicom(client, found: Found, state: dict, log=print, budget=None) -> None:
    categories = SOURCES["naicom"]["categories"]
    page, pages = 1, 1
    fields = "id,date,modified,slug,link,title,categories"
    while page <= pages:
        url = (
            f"https://naicom.gov.ng/wp-json/wp/v2/posts?categories={','.join(categories)}&per_page=100"
            f"&page={page}&_fields={fields}"
        )
        answer = _ok(client.get(url))
        pages = int(answer.headers.get("x-wp-totalpages") or 1)
        for post in answer.json():
            naicom_post(found, post, categories, url)
        page += 1


def naicom_post(found: Found, post: dict, categories: dict, listing_url: str) -> None:
    title = clean((post.get("title") or {}).get("rendered"))
    link = (post.get("link") or "").strip()
    url = urljoin("https://naicom.gov.ng/", link if link.startswith(("http", "/")) else "/" + link)
    named = [categories[str(c)] for c in post.get("categories") or [] if str(c) in categories]
    listed = {
        "title": title,
        "year": year_in(title),
        "posted": (post.get("date") or "")[:10] or None,
        "modified": post.get("modified"),
        "post_id": post.get("id"),
        "slug": post.get("slug"),
        "categories": named,
        "link_as_listed": link,
    }
    if (urlsplit(url).hostname or "") == "storage.naicom.website":
        found.reject(
            "off the official host (storage.naicom.website; its certificate fails)", url=url, title=title
        )
        return
    if re.search(r"\.(jpe?g|png|gif)$", url, re.I):
        found.reject("an image, not a document", url=url, title=title)
        return
    why = on_path(url, {"naicom.gov.ng"}, r"^/wp-content/uploads/.+\.(pdf|docx?)$") or sane_title(title)
    if why:
        found.reject(why, url=url, title=title)
        return
    if "Laws" in named:
        kind = "act"
    elif re.search(r"\bregulations?\b", title, re.I):
        kind = "regulation"
    elif "Guidelines" in named:
        kind = "guideline"
    else:
        kind = "circular"
    found.add(url, kind, {**listed, "file_name": file_name(url)}, raw=post, listing_url=listing_url)


def read_court_of_appeal(client, found: Found, state: dict, log=print, budget=None) -> None:
    page, seen, total = 1, 0, None
    while total is None or seen < total:
        url = f"https://coa-admin.courtofappeal.gov.ng/api/judgement/search?division=&caseNo=&party=&date=&p={page}&ps=30"
        body = _json(client.get(url))
        records = body.get("data") or []
        total = int(body.get("total") or (body.get("meta") or {}).get("total") or len(records))
        if not records:
            break
        for record in records:
            coa_record(found, client, record, url)
        seen += len(records)
        page += 1
    found.cursor = {"records_total": total, "records_read": seen}


def coa_record(found: Found, client, record: dict, listing_url: str) -> None:
    listed = {
        "record_id": record.get("id"),
        "appeal_no": clean(record.get("file_no")),
        "parties": clean(record.get("parties")),
        "panel": [clean(j) for j in re.split(r"[;,]", record.get("justice") or "") if clean(j)],
        "judgment_date": to_date(record.get("date")),
        "subject_matter": clean(record.get("subject_matter")),
        "substantial_issue": clean(record.get("substantial_issue")),
        "division": clean(record.get("division")),
        "link": record.get("download_link"),
    }
    drive = re.search(r"drive\.google\.com/file/d/([\w-]+)", record.get("download_link") or "")
    if not drive:
        found.no_file("judgment", listed, "no Google Drive file link", listing_url=listing_url)
        return
    download = f"https://drive.google.com/uc?export=download&id={drive.group(1)}"
    refused = None if client.permits(download) else DRIVE_REFUSED
    found.add(
        download,
        "judgment",
        {**listed, "drive_id": drive.group(1)},
        raw=record,
        listing_url=listing_url,
        refused=refused,
    )


def read_fhc(client, found: Found, state: dict, log=print, budget=None) -> None:
    url = SOURCES["fhc-rules"]["url"]
    soup = _soup(client.get(url))
    content = soup.select_one("div.elementor-widget-theme-post-content") or soup.find("main") or soup
    section = None
    for element in content.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        text = clean(element.get_text(" "))
        if re.match(r"^(The )?Rules of (the )?Court", text, re.I) or re.match(r"^Rules\b", text):
            section = "rules"
            continue
        if re.match(r"^Practice Directions of the Court", text, re.I):
            section = "practice_direction"
            continue
        if element.name != "li" or not section:
            continue
        if sane_title(text) or not re.search(r"Rules|Practice Direction|Directions|Guidelines", text, re.I):
            continue
        found.no_file(
            "practice_direction" if re.search(r"Practice Direction|Directions", text, re.I) else "rules",
            {
                "title": text.rstrip("."),
                "year": year_in(text),
                "note": "listed by title only: contained in the Federal High Courts' Handbook",
                "html": fragment(element),
            },
            "listed by title only",
            listing_url=url,
        )
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        title = clean(a.get_text(" "))
        if SPAM.search(href) or SPAM.search(title):
            found.reject("spam link", url=href, title=title)
        elif href.lower().endswith(".pdf"):
            why = on_path(href, {"fhc.gov.ng", "www.fhc.gov.ng"}, r"^/(wp-content/uploads|fhc)/.+\.pdf$")
            why = why or sane_title(title)
            if why:
                found.reject(why, url=href, title=title)
            else:
                found.add(
                    href,
                    law_type(title) or "rules",
                    {"title": title, "year": year_in(title), "file_name": file_name(href)},
                    raw=fragment(a),
                    listing_url=url,
                )
    for seed in SOURCES["fhc-rules"].get("seeds", []):
        found.add(seed["url"], seed["type"], dict(seed["listed"]), listing_url="the survey of 2026-10-22")


READERS = {
    "nass-acts": read_nass_acts,
    "nass-bills": read_nass_bills,
    "nln-repository": read_nln,
    "moj-constitution": read_moj,
    "supreme-court": read_supreme_court,
    "nicn-judgments": read_nicn,
    "ncaa": read_ncaa,
    "ncc": read_ncc,
    "sec": read_sec,
    "ndpc": read_ndpc,
    "statehouse": read_statehouse,
    "nerc": read_nerc,
    "nafdac": read_nafdac,
    "nesrea": read_nesrea,
    "cac": read_cac,
    "naicom": read_naicom,
    "court-of-appeal": read_court_of_appeal,
    "fhc-rules": read_fhc,
}


# --- the catalogue ---------------------------------------------------------------------------------


def runs_of(source_id: str) -> list[Path]:
    """A source's kept listing runs, oldest first (each folder holds every answer the run read)."""
    folder = STAGING / "catalogue" / source_id
    return sorted(p for p in folder.glob("*") if (p / "index.json").exists()) if folder.exists() else []


def catalogue(
    source_id: str,
    delay: float = polite.LEAST_DELAY,
    budget: int | None = None,
    log=print,
    client=None,
    restart: bool = False,
    replay: polite.ReplayClient | None = None,
) -> dict:
    """Read one source's listing politely and record what it lists in the sync ledger. Every answer
    read is kept in catalogue/<source>/<run>/ (responses.jsonl.gz, index.json); with `replay` (a
    run's kept answers) the listing is read again from them, without the site."""
    source = SOURCES[source_id]
    started = now()
    label = "reparse" if replay else "catalogue"
    run_id = f"{started.strftime('%Y-%m-%dT%H%M%S')}-{label}-{source_id}"
    if (STAGING / "catalogue" / source_id / run_id).exists():  # two runs in one second
        run_id += f"-{len(list((STAGING / 'catalogue' / source_id).glob(run_id + '*'))) + 1}"
    ledger = Ledger(STAGING / "sync-ledger.json")
    if replay is not None:
        state = dict(replay.index.get("state_before") or {})
        budget = replay.index.get("budget")
        client = replay
    else:
        state = {} if restart else dict(ledger.source(source_id).get("cursor") or {})
        if state.get("sweep_finished"):  # a finished sweep: read only what is newer
            state = {"next_id": state.get("newest_id", 0) + 1, "sweep_started": state.get("sweep_started")}
        state["run_date"] = started.date().isoformat()
    recorder = None
    own = client is None
    if replay is None and (own or getattr(client, "record", False) is None):  # every answer kept
        recorder = polite.Recorder(STAGING / "catalogue" / source_id / run_id)
    if own:
        client = polite.PoliteClient(
            set(source["hosts"]) | set(source["file_hosts"]),
            delay=delay,
            windows=source.get("windows"),
            log=log,
            record=recorder,
        )
    elif recorder is not None:
        client.record = recorder
    found = Found(source_id)
    outcome = "ok"
    try:
        READERS[source_id](client, found, state, log=log, budget=budget)
    except polite.Refused as error:
        outcome = f"stopped: refused or bot check: {error}"
    except polite.Disallowed as error:
        outcome = f"stopped: robots.txt: {error}"
    except polite.Unreachable as error:
        outcome = f"stopped: no answer: {error}"
    except polite.OffHost as error:
        outcome = f"stopped: off the source's hosts: {error}"
    except SourceStopped as error:
        outcome = f"stopped: {error}"
    except Exception as error:  # noqa: BLE001 - recorded, and the check is marked incomplete
        outcome = f"error: {type(error).__name__}: {error}"
    finally:
        if own:
            client.close()
    if outcome != "ok":
        log(f"{source_id}: {outcome}")
    requests = sum(client.requests.values())
    if recorder is not None:
        recorder.close(
            {
                "run": run_id,
                "source": source_id,
                "started_at": stamp(started),
                "state_before": state,
                "budget": budget,
                "outcome": outcome,
                "robots": client.robots_state(),
            }
        )
    crawl = {
        "complete": found.complete and outcome == "ok",
        "outcome": outcome if not replay else f"{outcome} (read again from run {replay.index.get('run')})",
        "pages_read": requests,
        "files": list(found.items.values()),
    }
    cursor = found.cursor or {"items": len(found.items)}
    if outcome != "ok" and found.resumable:
        cursor = {k: v for k, v in {**state, **(found.cursor or {})}.items() if k != "run_date"}
    checked = dict(ledger.source(source_id))
    changes = ledger.observe_listing(
        source_id,
        source["url"],
        source["cadence"],
        crawl,
        run_id,
        cursor=cursor,
        at=started,
        shown=lambda i: i["listed"],
    )
    if replay is not None:  # reading kept answers again is not a check of the site
        state_now = ledger.data["sources"][source_id]
        for key in ("last_checked_at", "last_check_outcome", "last_check_complete", "next_due", "cursor"):
            if key in checked:
                state_now[key] = checked[key]
        state_now["last_reparsed_at"] = stamp(started)
    for item in found.items.values():
        record = ledger.data["items"][f"{source_id}:{item['url']}"]
        for key in ("refused", "also_listed_as"):
            if item.get(key):
                record[key] = item[key]
            else:
                record.pop(key, None)
    state_out = ledger.data["sources"][source_id]
    state_out["counts"]["items_held_in_ledger"] = sum(
        1 for r in ledger.data["items"].values() if r.get("source") == source_id and not r.get("gone")
    )
    if replay is None:
        state_out["last_run"] = run_id
    ledger.save_source(source_id)
    path = STAGING / "catalogue" / f"{source_id}.json"
    previous = (
        json.loads(path.read_text()) if found.resumable and path.exists() and state.get("next_id") else {}
    )
    items = {i["url"]: i for i in previous.get("items", [])} | found.items
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "source": source_id,
                **{k: v for k, v in source.items() if k != "seeds"},
                "checked_at": stamp(started),
                "run": run_id,
                "outcome": crawl["outcome"],
                "complete": crawl["complete"],
                "requests": dict(client.requests),
                "robots": {host: robots.get("note") for host, robots in client.robots_state().items()},
                "refused_by_robots": client.refused_paths,
                "notes": found.notes,
                "items": list(items.values()),
                "without_file": previous.get("without_file", []) + found.without_file,
                "rejected": previous.get("rejected", []) + found.rejected,
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n"
    )
    summary = {
        "run": run_id,
        "source": source_id,
        "started_at": stamp(started),
        "finished_at": stamp(now()),
        "outcome": crawl["outcome"],
        "complete": crawl["complete"],
        "requests": requests,
        "items": len(found.items),
        "items_in_ledger": state_out["counts"]["items_held_in_ledger"],
        "refused": sum(1 for i in found.items.values() if i.get("refused")),
        "with_alternates": sum(1 for i in found.items.values() if i.get("alternate_urls")),
        "without_file": len(found.without_file),
        "rejected": len(found.rejected),
        "types": _count(i["type"] for i in found.items.values()),
        "stated_bytes": sum(_stated_bytes(i["listed"]) or 0 for i in found.items.values()),
        "changes": {k: len(v) for k, v in changes.items()},
        "cursor": cursor,
    }
    runs = STAGING / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(json.dumps({**summary, "changed_items": changes}, indent=1) + "\n")
    return summary


def reparse(source_id: str, log=print) -> list[dict]:
    """Read a source's kept listing answers again with the current readers, without the site, and
    record what they now find in the ledger: the latest run, or for a listing read a share at a time
    every run since the last that started from the beginning."""
    runs = runs_of(source_id)
    if not runs:
        raise ValueError(f"{source_id} has no kept listing runs")
    index = {run: json.loads((run / "index.json").read_text()) for run in runs}
    if SOURCES[source_id].get("budget"):
        starts = [i for i, run in enumerate(runs) if not index[run]["state_before"].get("next_id")]
        runs = runs[starts[-1] :] if starts else runs
    else:  # the latest run that read the whole listing, else the latest
        whole = [run for run in runs if index[run].get("outcome") == "ok"]
        runs = (whole or runs)[-1:]
    return [catalogue(source_id, log=log, replay=polite.ReplayClient(run, log=log)) for run in runs]


def _count(values) -> dict:
    out: dict = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def _stated_bytes(listed: dict) -> int | None:
    size = listed.get("size_bytes") or (listed.get("file") or {}).get("size_bytes")
    return int(size) if isinstance(size, int | float) or (isinstance(size, str) and size.isdigit()) else None


# --- likely duplicates across and within sources ----------------------------------------------------

TITLE_NOISE = {
    "the", "of", "and", "a", "an", "in", "for", "to", "on", "federal", "republic", "nigeria", "nigerian", "pdf",
    "published", "gazette", "copy", "final", "signed",
}  # fmt: skip
FAMILY = {"constitutional_alteration": "constitution", "judgment": "case", "ruling": "case"}


def _title_key(title: str) -> str:
    """A title as its words, markup, punctuation, filler, plurals and order aside: 'JOINT REVENUE BOARD
    OF NIGERIA (ESTABLISHMENT) ACT, 2025' and 'Joint Revenue Board of Nigeria (Establishment) Act 2025'
    alike, '1999 CONSTITUTION OF NIGERIA' and 'Constitution of the Federal Republic of Nigeria 1999' too."""
    words = re.findall(r"[a-z0-9]+", clean(title).lower().replace("&", " and "))
    stems = {w[:-1] if w.endswith("s") and len(w) > 4 else w for w in words if w not in TITLE_NOISE}
    return " ".join(sorted(stems))


def duplicate_keys(record: dict) -> list[tuple[str, str]]:
    """What may make two items the same document: their (type, normalised title with its year), an
    instrument's (type, number, year), a judgment's (suit or appeal number, date)."""
    listed, kind = record.get("listed") or {}, record.get("type") or ""
    family = FAMILY.get(kind, kind)
    keys = []
    if family == "case":
        suit = listed.get("suit_no") or listed.get("appeal_no")
        if suit and listed.get("judgment_date") and listed.get("opinion") != "contribution":
            number = re.sub(r"[^A-Z0-9]", "", suit.upper())
            keys.append(("case", f"{number} {listed['judgment_date']}"))
        return keys
    title = _title_key(listed.get("title") or "")
    words = title.split()
    if len(words) >= 2 and any(not w.isdigit() for w in words) and re.search(r"\b(?:18|19|20)\d\d\b", title):
        keys.append(("title", f"{family} {title}"))
    year = listed.get("year")
    for field in ("si_number", "bill_number", "number"):
        if listed.get(field) and year:
            number = re.sub(r"\W", "", str(listed[field]).upper())
            keys.append(("number", f"{family} {field} {number} {year}"))
    if listed.get("gazette_number") and year:
        keys.append(("number", f"gazette {listed['gazette_number']} {listed.get('gazette_volume')} {year}"))
    return keys


def flag_duplicates(ledger: Ledger | None = None) -> dict:
    """Mark each item that may be the same document as another (from any source) with
    `likely_duplicates` (the others and why), so the reading step can choose before reading twice;
    the files themselves are compared by fingerprint when fetched. Saved under the ledger's lock."""
    import fcntl

    path = STAGING / "sync-ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            ledger = Ledger(path)
            groups: dict[tuple, list[str]] = {}
            for item_id, record in ledger.data["items"].items():
                if record.get("gone"):
                    continue
                for key in duplicate_keys(record):
                    groups.setdefault(key, []).append(item_id)
            flags: dict[str, list[dict]] = {}
            for (by, _), members in groups.items():
                if len(members) < 2 or len(members) > 20:  # a key shared by many is no evidence
                    continue
                for item_id in members:
                    for other in members:
                        if other != item_id and all(f["item"] != other for f in flags.get(item_id, [])):
                            flags.setdefault(item_id, []).append({"item": other, "by": by})
            for item_id, record in ledger.data["items"].items():
                if item_id in flags:
                    record["likely_duplicates"] = sorted(flags[item_id], key=lambda f: f["item"])
                else:
                    record.pop("likely_duplicates", None)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(ledger.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            tmp.replace(path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    across = sum(
        1
        for item_id, f in flags.items()
        if any(
            ledger.data["items"][o["item"]]["source"] != ledger.data["items"][item_id]["source"] for o in f
        )
    )
    return {"items_flagged": len(flags), "flagged_across_sources": across}


# --- the fetch work list ---------------------------------------------------------------------------

WORKLIST = f"work/{COUNTRY}/fetch.json"
LARGEST = 128 * 1024**2  # the largest file taken (a 72 MB Gazette copy of the Companies Regulations)
HTML_SOURCES = {"nicn-judgments", "statehouse", "nass-bills"}


def expects(record: dict) -> str:
    """What the fetched address should be: a web page, the court's JSON record, or a document."""
    if record["source"] in HTML_SOURCES or (record["source"] == "sec" and record.get("type") == "circular"):
        return "html"
    if record["url"].startswith(f"{SC_API}/"):
        return "json"
    return "document"


def size_limit(stated: int | None) -> int:
    """The most a file may be: a quarter over the size its listing states (listings round), at least
    the usual ingestion limit, never more than the largest taken; the largest when none is stated."""
    from ingestion.pipeline import MAX_BYTES

    if not stated:
        return LARGEST
    return max(MAX_BYTES, min(LARGEST, int(stated * 1.25)))


def host_of(url: str) -> str:
    """The host whose politeness an address falls under ("www." aside)."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _changed_since_fetch(record: dict) -> bool:
    """Whether the listing has changed since the item's file was last fetched."""
    fetched = record.get("last_fetched") or ""
    return any(
        h.get("change") == "listing changed" and h.get("at", "") > fetched for h in record.get("history", [])
    )


def fetch_worklist(approval: dict, sources: list[str] | None = None, recheck: bool = False) -> dict:
    """The items the cloud job fetches, of the chosen sources (by default those marked for fetching):
    every item not yet stored, and every stored item whose listing has changed since (or, with
    `recheck`, every stored item), sent with its validators (ETag, Last-Modified) so that an
    unchanged file is not taken again. Refused and no-longer-listed items are left out. Each item
    carries what the job needs to fetch and describe it: no ledger is needed in the cloud."""
    for key in ("approved_by", "basis"):
        if not (approval.get(key) or "").strip():
            raise ValueError(f"a fetch work list needs {key}")
    chosen = sources or [s for s, meta in SOURCES.items() if meta.get("fetch")]
    ledger = Ledger(STAGING / "sync-ledger.json")
    items, left_out = [], {}
    for item_id, record in sorted(ledger.data["items"].items()):
        if record.get("source") not in chosen:
            continue
        stored = bool(record.get("stored"))
        reason = (
            "refused"
            if record.get("refused")
            else "no longer listed"
            if record.get("gone")
            else "stored, unchanged"
            if stored and not recheck and not _changed_since_fetch(record)
            else None
        )
        if reason:
            left_out[reason] = left_out.get(reason, 0) + 1
            continue
        stated = _stated_bytes(record.get("listed") or {})
        known = None
        if stored:
            known = {
                "sha256": record.get("sha256"),
                "stored": record.get("stored"),
                **{
                    k: v
                    for k, v in (record.get("validators") or {}).items()
                    if k in ("etag", "last_modified")
                },
            }
        items.append(
            {
                "item": item_id,
                "key": item_key(item_id),
                "source": record["source"],
                "url": record["url"],
                "host": host_of(record["url"]),
                "type": record.get("type"),
                "expect": expects(record),
                "stated_bytes": stated,
                "size_limit": size_limit(stated),
                "listed": record.get("listed"),
                "record_url": record.get("record_url"),
                "listing_url": record.get("listing_url"),
                "alternate_urls": record.get("alternate_urls"),
                "known": known,
            }
        )
    made = stamp(now())
    return {
        "id": made.replace(":", "").replace("-", ""),
        "made_at": made,
        "country": COUNTRY,
        "approval": {**approval, "approved_at": approval.get("approved_at") or made},
        "delay": max(polite.LEAST_DELAY, float(approval.get("delay") or polite.LEAST_DELAY)),
        "sources": {
            s: {"file_hosts": SOURCES[s]["file_hosts"], "windows": SOURCES[s].get("windows")} for s in chosen
        },
        "items_total": len(items),
        "rechecks": sum(1 for i in items if i["known"]),
        "by_source": _count(i["source"] for i in items),
        "by_host": _count(i["host"] for i in items),
        "stated_bytes": sum(i["stated_bytes"] or 0 for i in items),
        "items_with_stated_size": sum(1 for i in items if i["stated_bytes"]),
        "left_out": left_out,
        "items": items,
    }


def item_key(item_id: str) -> str:
    """The name of an item's fetch records: its id's fingerprint (ids hold addresses)."""
    return hashlib.sha256(item_id.encode()).hexdigest()


def pull_fetched(store, log=print) -> dict:
    """Record in the ledger what the cloud fetch of the current work list did: for each item done,
    its fetch record (stored, the same file already stored from another listing, or unchanged), and
    what failed and which hosts it stopped on."""
    from concurrent.futures import ThreadPoolExecutor

    prefix = f"work/{COUNTRY}/"
    worklist = json.loads(store.get(WORKLIST))
    ledger = Ledger(STAGING / "sync-ledger.json")
    by_key = {item_key(item_id): item_id for item_id in ledger.data["items"]}
    done = [n.rsplit("/", 1)[-1] for n in store.list(f"{prefix}fetch-done/{worklist['id']}/")]
    with ThreadPoolExecutor(16) as pool:
        records = list(pool.map(lambda k: json.loads(store.get(f"{prefix}fetched/{k}.json")), done))
    counts = {"stored": 0, "already_stored": 0, "unchanged": 0}
    for fetched in records:
        record = ledger.data["items"].get(fetched["item"])
        if record is None:
            continue
        outcome = fetched["outcome"]
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome != "unchanged" or fetched.get("sha256") != record.get("sha256"):
            ledger.data["sources"].setdefault(record["source"], {"source": record["source"]})
            ledger.fetched(fetched["item"], fetched["sha256"], fetched["bytes"])
        record.update(
            {
                "stored": fetched["stored"],
                "format": fetched["suffix"].lstrip("."),
                "last_fetched": fetched["retrieved_at"],
                "validators": fetched.get("validators") or {},
            }
        )
        if fetched.get("final_url") and fetched["final_url"] != record["url"]:
            record["final_url"] = fetched["final_url"]
        if outcome == "already_stored":  # the same bytes as another listing's file: stored once
            record["same_file_as"] = fetched.get("same_file_as")
            primary = ledger.data["items"].get(fetched.get("same_file_as") or "")
            if primary is not None and fetched["item"] not in primary.setdefault("other_sources", []):
                primary["other_sources"].append(fetched["item"])
        elif outcome == "unchanged":
            record["history"].append(
                {"at": fetched["retrieved_at"], "change": f"checked: unchanged ({fetched.get('how')})"}
            )
        record.pop("fetch_failed", None)
    failed = 0
    for name in store.list(f"{prefix}fetch-failed/"):
        item_id = by_key.get(name.rsplit("/", 1)[-1][: -len(".json")])
        record = ledger.data["items"].get(item_id) if item_id else None
        if record is None or name.rsplit("/", 1)[-1][: -len(".json")] in done:
            continue
        failure = json.loads(store.get(name))
        if failure.get("worklist") != worklist["id"]:
            continue
        if record.get("fetch_failed") != failure["reason"]:
            record["fetch_failed"] = failure["reason"]
            record["history"].append(
                {"at": failure["at"], "change": "fetch failed", "reason": failure["reason"]}
            )
        failed += 1
    stopped = []
    for name in store.list(f"{prefix}fetch-stopped/{worklist['id']}/"):
        stop = json.loads(store.get(name))
        stopped.append(stop["host"])
        for source_id in stop.get("sources", []):
            state = ledger.data["sources"].setdefault(source_id, {"source": source_id})
            state["last_fetch_outcome"] = f"stopped at {stop['at']} on {stop['host']}: {stop['reason']}"
    ledger.save()
    summary = {"worklist": worklist["id"], **counts, "failed": failed, "hosts_stopped": sorted(stopped)}
    log(json.dumps(summary))
    return summary


# --- the bucket ------------------------------------------------------------------------------------


def push_held(store, log=print) -> int:
    """Files held here uploaded (none, ordinarily: the cloud job fetches), the ledger and catalogues
    mirrored, and every kept listing run not yet in the bucket copied to state/ng/catalogue/."""
    from ingestion import rwanda

    pushed = rwanda.push_held(store, log=log, staging=STAGING, country=COUNTRY)
    rwanda.mirror_state(store, staging=STAGING, country=COUNTRY)
    there = set(store.list(f"state/{COUNTRY}/catalogue/"))
    for source_id in SOURCES:
        for run in runs_of(source_id):
            for path in sorted(run.iterdir()):
                name = f"state/{COUNTRY}/{path.relative_to(STAGING)}"
                if name not in there:
                    kind = "application/gzip" if path.suffix == ".gz" else "application/json"
                    store.put(name, path.read_bytes(), kind)
    for path in sorted((STAGING / "work").glob("*.json")) if (STAGING / "work").exists() else []:
        store.put(
            f"state/{COUNTRY}/{path.relative_to(STAGING)}",
            path.read_bytes(),
            "application/json",
            replace=True,
        )
    return pushed


def status() -> list[dict]:
    ledger = Ledger(STAGING / "sync-ledger.json")
    rows = []
    for source_id, source in SOURCES.items():
        state = ledger.source(source_id)
        records = [r for r in ledger.data["items"].values() if r.get("source") == source_id]
        rows.append(
            {
                "source": source_id,
                "wave": source["wave"],
                "cadence": source["cadence"],
                "last_checked_at": state.get("last_checked_at"),
                "outcome": state.get("last_check_outcome"),
                "due": ledger.due(source_id, source["cadence"]),
                "cursor": state.get("cursor"),
                "items": sum(1 for r in records if not r.get("gone")),
                "no_longer_listed": sum(1 for r in records if r.get("gone")),
                "refused": sum(1 for r in records if r.get("refused")),
                "likely_duplicates": sum(1 for r in records if r.get("likely_duplicates")),
                "stored": sum(1 for r in records if r.get("stored")),
                "fetch_failed": sum(1 for r in records if r.get("fetch_failed") and not r.get("stored")),
                "last_fetch_outcome": state.get("last_fetch_outcome"),
                "kept_runs": len(runs_of(source_id)),
            }
        )
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingestion.nigeria")
    sub = parser.add_subparsers(dest="command", required=True)
    cat = sub.add_parser(
        "catalogue", help="read a source's listing and update the sync ledger (no downloads)"
    )
    cat.add_argument("source", choices=[*SOURCES, "all"])
    cat.add_argument(
        "--delay", type=float, default=polite.LEAST_DELAY, help="seconds between requests to a host"
    )
    cat.add_argument("--max-requests", type=int, help="records read this run (the NICN walk; default 1000)")
    cat.add_argument("--force", action="store_true", help="check even if not due")
    cat.add_argument("--restart", action="store_true", help="start a resumable listing from its beginning")
    again = sub.add_parser("reparse", help="read a source's kept listing answers again (no network)")
    again.add_argument("source", choices=[*SOURCES, "all"])
    sub.add_parser("status", help="last check, outcome, cursor and counts, per source")
    work = sub.add_parser(
        "fetch-worklist", help="write the cloud fetch job's work list (operator's approval)"
    )
    work.add_argument("--approved-by", required=True)
    work.add_argument("--basis", required=True, help="where and when the fetch was approved")
    work.add_argument("--source", action="append", choices=list(SOURCES), help="these sources only")
    work.add_argument("--recheck", action="store_true", help="also every stored item (conditional requests)")
    work.add_argument("--local-only", action="store_true", help="write the list here only, not to the bucket")
    sub.add_parser(
        "pull-fetched", help="record in the ledger what the cloud fetch stored, failed or stopped on"
    )
    sub.add_parser(
        "push-held", help="mirror the ledger, catalogues and kept listing runs to the corpus bucket"
    )
    args = parser.parse_args(argv)
    if args.command == "status":
        for row in status():
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.command == "reparse":
        for source_id in list(SOURCES) if args.source == "all" else [args.source]:
            if runs_of(source_id):
                for summary in reparse(source_id, log=lambda m: print(m, flush=True)):
                    print(json.dumps(summary, ensure_ascii=False), flush=True)
        print(json.dumps(flag_duplicates()))
        return 0
    if args.command == "fetch-worklist":
        print(json.dumps(flag_duplicates()))
        worklist = fetch_worklist(
            {"approved_by": args.approved_by, "basis": args.basis}, args.source, recheck=args.recheck
        )
        local = STAGING / "work" / "fetch.json"
        local.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(worklist, ensure_ascii=False)
        local.write_text(body + "\n")
        if not args.local_only:
            from ingestion import corpus_store

            store = corpus_store.open_store()
            push_held(store)
            store.put(WORKLIST, body.encode(), "application/json", replace=True)
        summary = {k: v for k, v in worklist.items() if k not in ("items", "sources")}
        print(json.dumps(summary | {"object": WORKLIST, "local": str(local)}, ensure_ascii=False))
        return 0
    if args.command == "pull-fetched":
        from ingestion import corpus_store

        store = corpus_store.open_store()
        pull_fetched(store)
        print(json.dumps(flag_duplicates()))
        push_held(store)
        return 0
    if args.command == "push-held":
        from ingestion import corpus_store

        print(json.dumps({"stored": push_held(corpus_store.open_store())}))
        return 0
    if args.delay < polite.LEAST_DELAY:
        parser.error(f"--delay must be at least {polite.LEAST_DELAY:.0f} seconds")
    ledger = Ledger(STAGING / "sync-ledger.json")
    chosen = list(SOURCES) if args.source == "all" else [args.source]
    code = 0
    for source_id in chosen:
        cursor = ledger.source(source_id).get("cursor") or {}
        unfinished = cursor.get("next_id") and not cursor.get("sweep_finished")
        if (
            not args.force
            and not args.restart
            and not unfinished
            and not ledger.due(source_id, SOURCES[source_id]["cadence"])
        ):
            print(f"{source_id}: not due until {ledger.source(source_id).get('next_due')}")
            continue
        summary = catalogue(
            source_id, args.delay, args.max_requests, log=lambda m: print(m, flush=True), restart=args.restart
        )
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if summary["outcome"] != "ok":
            code = 2
    print(json.dumps(flag_duplicates()))
    return code


if __name__ == "__main__":
    sys.exit(main())
