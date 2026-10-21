"""Query routes (RSV-03, router profile oa-route-3): deterministic rules decide which plans run; the
router records them and never changes the caller's filters or assumes a jurisdiction or date.

oa-route-2 adds two routes. ``link``: the query is one address, an Open Alvary page or API address of a
record or part, or a text's official address, and the evidence is what it addresses. ``page``: the
query names a page of one work ("page 18 of the NDPA", "NDPA p. 18"), or of the record a request is
limited to, and the evidence is the provisions on that page of the original file.

oa-route-3 reads citations as api.pinpoint does since 2026-10-21 (API-04: a change in how citations
are read is a new profile). An article's alinéa is its subsection ("article 10, alinéa 2", "art. 10
al. 2"), the work may follow "de", "du", "de la", "de l'" or "des" as it follows "of", and a citation
is read at its longest pinpoint only, never also as a shorter one in a work named by the rest."""

import re
from dataclasses import dataclass
from datetime import date

from api.text_index import fold

ROUTER = "oa-route-3"

# "meaning of 'personal data'", "definition of data controller in the NDPA", "what is a "data subject"?"
_DEFINITION = re.compile(
    r"""(?:\bmeaning\s+of\b|\bdefinition\s+of\b|\bdefine\b|\bdefined\s+as\b|\bwhat\s+is\s+(?:an?\s+|the\s+)?(?=["“‘']))"""
    r"""\s*["“‘']?(?P<term>[^"”’'?]{2,80}?)["”’']?(?:\s+(?:in|under)\s+(?P<work>[^?]+?))?\s*\??\s*$""",
    re.IGNORECASE,
)
_LINK = re.compile(r"^\s*(https?://[^\s<>\"]+)\s*$", re.IGNORECASE)
# "page 18", "pg. 18", "p. 18" (with its dot: "p 18" is too often something else).
_PAGE = re.compile(r"(?:^|(?<=[\s,;(]))(?:at\s+)?(?:page|pg\.?|p\.)\s*(\d{1,4})\b", re.IGNORECASE)
# Words that carry no topic once an instrument's name is taken out ("what does the NDPA say").
_FILLER = frozenset(
    "a an the of in on under to for by and or what which does do did say says said about is are was were "
    "this that these those act law provision provisions section sections text on at page".split()
)


@dataclass(frozen=True)
class Plan:
    routes: tuple[str, ...]
    citation: object | None  # the CitationView whose provision the citation route uses
    works: dict[str, str]  # works the query names, each with the name found
    term: str | None  # the definition route's term
    residual: str  # the query without the names of the works it names
    as_of: date | None
    comparative: bool
    link: str | None = None  # the address the query is, on the link route
    page: int | None = None  # the page of the original file, on the page route


def definition_term(query: str) -> str | None:
    match = _DEFINITION.search(" ".join(query.split()))
    if not match:
        return None
    term = match.group("term").strip(" .,:;\"“”‘’'")
    return term if 2 <= len(term) <= 80 else None


def residual(query: str, names: list[str]) -> str:
    """The query's folded words without the names (longest first) of the works it names."""
    words = f" {fold(query)} "
    for name in sorted(names, key=len, reverse=True):
        words = words.replace(f" {name} ", " ")
    return " ".join(words.split())


def link(query: str) -> str | None:
    found = _LINK.match(query)
    return found.group(1) if found else None


def page(query: str) -> int | None:
    found = _PAGE.search(query)
    return int(found.group(1)) if found and int(found.group(1)) >= 1 else None


def plan(
    query: str,
    *,
    citation,
    works: dict[str, str],
    as_of: date | None,
    jurisdictions,
    within: str | None = None,
) -> Plan:
    address = link(query)
    if address is not None:
        return Plan(("link",), None, {}, None, "", as_of, False, link=address)
    routes: list[str] = []
    provision = getattr(citation, "provision", None)
    if provision is not None and provision.status in ("resolved", "ambiguous"):
        routes.append("citation")
    left = residual(query, list(works.values()))
    number = page(query)
    if number is not None and "citation" not in routes and (len(works) == 1 or within):
        routes.append("page")
        left = residual(_PAGE.sub(" ", query), list(works.values()))
    if works and "citation" not in routes and "page" not in routes:
        routes.append("instrument")
    term = definition_term(query)
    if term:
        routes.append("definition")
    topical = [w for w in left.split() if w not in _FILLER]
    if topical or not routes:
        routes.append("topic")
    if as_of is not None:
        routes.append("historical")
    comparative = jurisdictions is not None and len(jurisdictions) > 1
    if comparative:
        routes.append("comparative")
    return Plan(
        tuple(routes), citation if "citation" in routes else None, works, term, left, as_of, comparative,
        page=number if "page" in routes else None,
    )  # fmt: skip
