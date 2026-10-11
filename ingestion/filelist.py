"""Polite catalogue of a TYPO3 "filelist" folder browser, the listings RLRC and MINIJUST publish.

Reads listing pages only: the folders, the files (name, address, size and date as shown) and the
further pages of a long folder. It never downloads a listed file. Links are followed exactly as the
site renders them (TYPO3 signs each listing address with a cHash, so addresses are never built), the
crawl stays under its starting path, the crawler identifies itself honestly, waits between requests
and stops at any refusal (403, 429) or repeated server error.
"""

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PATH_KEY = "tx_filelist_filelist[path]"
PAGE_KEY = "tx_filelist_filelist[currentPage]"


def user_agent() -> str:
    """Who we are, honestly; a contact is added only when the operator sets one."""
    contact = os.environ.get("ALVARY_CRAWLER_CONTACT", "").strip()
    return "OpenAlvaryBot/0.1 (catalogue of official legal texts" + (f"; {contact})" if contact else ")")


class Refused(Exception):
    """The site refused or rate-limited us: stop, record it, do not retry around it."""


@dataclass
class Listing:
    path: str
    page: int
    folders: list[dict] = field(default_factory=list)  # {name, path, url}
    files: list[dict] = field(default_factory=list)  # {name, url, size, modified}
    pages: list[dict] = field(default_factory=list)  # {page, url}


def _query(href: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(href).query).items()}


class _Rows(HTMLParser):
    """Rows of the listing table: each a list of cells, each cell its links and its text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self.links: list[str] = []  # every link on the page, for pagination
        self._row: list[dict] | None = None
        self._cell: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = {"links": [], "text": ""}
        elif tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
            if self._cell is not None:
                self._cell["links"].append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None and self._row is not None:
            self._cell["text"] = " ".join(self._cell["text"].split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data


def parse_listing(html: str, url: str) -> Listing:
    here = _query(url)
    listing = Listing(path=here.get(PATH_KEY, ""), page=int(here.get(PAGE_KEY, "1") or 1))
    rows = _Rows()
    rows.feed(html)
    for cells in rows.rows:
        links = [link for cell in cells for link in cell["links"]]
        if not links:
            continue
        href = urljoin(url, links[0])
        if "/fileadmin/" in urlsplit(href).path:
            texts = [cell["text"] for cell in cells]
            listing.files.append(
                {
                    "name": texts[1] if len(texts) > 1 else href.rsplit("/", 1)[-1],
                    "url": href,
                    "size": texts[2] if len(texts) > 2 else "",
                    "modified": texts[3] if len(texts) > 3 else "",
                }
            )
        elif PATH_KEY in _query(href):
            path = _query(href)[PATH_KEY]
            if len(path) > len(listing.path) and path.startswith(listing.path):  # deeper, not ".."
                name = (cells[1]["text"] if len(cells) > 1 else "") or path.rstrip("/").rsplit("/", 1)[-1]
                listing.folders.append({"name": name, "path": path, "url": href})
    seen_pages = {listing.page}
    for link in rows.links:
        href = urljoin(url, link)
        query = _query(href)
        if PAGE_KEY in query and query.get(PATH_KEY, listing.path) == listing.path:
            number = int(query[PAGE_KEY] or 1)
            if number not in seen_pages:
                seen_pages.add(number)
                listing.pages.append({"page": number, "url": href})
    unique = {}
    for folder in listing.folders:
        unique.setdefault(folder["path"], folder)
    listing.folders = list(unique.values())
    return listing


def fetcher(host: str, timeout: int = 60) -> Callable[[str], str]:
    """GET a listing page from one host; refusals raise Refused, redirects off the host are refused."""

    class SameHost(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if urlsplit(newurl).netloc != urlsplit(host).netloc:
                raise Refused(f"redirected off {urlsplit(host).netloc}: {newurl}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = build_opener(SameHost)

    def fetch(url: str) -> str:
        for attempt in range(2):
            try:
                with opener.open(
                    Request(url, headers={"User-Agent": user_agent()}), timeout=timeout
                ) as response:
                    return response.read().decode("utf-8", "replace")
            except HTTPError as error:
                if error.code in (401, 403, 429):
                    raise Refused(f"{error.code} at {url}") from error
                if attempt == 0 and error.code >= 500:
                    time.sleep(30)
                    continue
                raise
            except URLError:
                if attempt == 0:
                    time.sleep(30)
                    continue
                raise
        raise RuntimeError("unreachable")

    return fetch


def crawl(
    start_url: str,
    root: str,
    fetch: Callable[[str], str],
    delay: float = 4.0,
    max_pages: int = 3000,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda message: None,
) -> dict:
    """Every file under `root`, with the folder it is listed in. `complete` is False when the crawl
    stopped early (refusal, error or page limit), so that missing files are not read as removed."""
    queue = [start_url]
    done: set[tuple[str, int]] = set()
    files: dict[str, dict] = {}
    folders: dict[str, str] = {}
    outcome, pages_read = "ok", 0
    while queue:
        url = queue.pop(0)
        key = (_query(url).get(PATH_KEY, ""), int(_query(url).get(PAGE_KEY, "1") or 1))
        if key in done:
            continue
        if pages_read >= max_pages:
            outcome = "page_limit"
            break
        if pages_read:
            sleep(delay)
        try:
            listing = parse_listing(fetch(url), url)
        except Refused as error:
            outcome = f"refused: {error}"
            break
        except Exception as error:  # noqa: BLE001 - recorded, and the crawl is marked incomplete
            outcome = f"error: {type(error).__name__}: {error}"
            break
        pages_read += 1
        done.add(key)
        for folder in listing.folders:
            if folder["path"].startswith(root):
                folders[folder["path"]] = folder["name"]
                queue.append(folder["url"])
        queue.extend(page["url"] for page in listing.pages)
        for item in listing.files:
            files[item["url"]] = {**item, "folder": listing.path}
        log(f"{pages_read:4d} pages · {len(files):5d} files · {listing.path or '/'} p{listing.page}")
    return {
        "complete": outcome == "ok",
        "outcome": outcome,
        "pages_read": pages_read,
        "folders": folders,
        "files": sorted(files.values(), key=lambda f: (f["folder"], f["name"])),
    }


# --- what the listings show, normalised ---------------------------------------------------------

UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}


def size_bytes(text: str) -> int | None:
    """'2 MB' -> 2097152, '1,004 KB' -> 1028096 (as shown: an approximation, the listing rounds).
    A comma before three digits groups thousands; otherwise it is a decimal comma."""
    match = re.fullmatch(r"\s*([\d.,]+)\s*([KMG]?B)\s*", text or "", re.I)
    if not match:
        return None
    number = re.sub(r",(?=\d{3}\b)", "", match.group(1)).replace(",", ".")
    return int(float(number) * UNITS[match.group(2).upper()])


def listed_date(text: str) -> str | None:
    """'Aug 23, 2023' -> '2023-08-23'."""
    try:
        return datetime.strptime(" ".join((text or "").split()), "%b %d, %Y").date().isoformat()
    except ValueError:
        return None
