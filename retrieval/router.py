"""Query routes (RSV-03, router profile oa-route-1): deterministic rules decide which plans run; the
router records them and never changes the caller's filters or assumes a jurisdiction or date."""

import re
from dataclasses import dataclass
from datetime import date

from api.text_index import fold

ROUTER = "oa-route-1"

# "meaning of 'personal data'", "definition of data controller in the NDPA", "what is a "data subject"?"
_DEFINITION = re.compile(
    r"""(?:\bmeaning\s+of\b|\bdefinition\s+of\b|\bdefine\b|\bdefined\s+as\b|\bwhat\s+is\s+(?:an?\s+|the\s+)?(?=["“‘']))"""
    r"""\s*["“‘']?(?P<term>[^"”’'?]{2,80}?)["”’']?(?:\s+(?:in|under)\s+(?P<work>[^?]+?))?\s*\??\s*$""",
    re.IGNORECASE,
)
# Words that carry no topic once an instrument's name is taken out ("what does the NDPA say").
_FILLER = frozenset(
    "a an the of in on under to for by and or what which does do did say says said about is are was were "
    "this that these those act law provision provisions section sections text".split()
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


def plan(query: str, *, citation, works: dict[str, str], as_of: date | None, jurisdictions) -> Plan:
    routes: list[str] = []
    provision = getattr(citation, "provision", None)
    if provision is not None and provision.status in ("resolved", "ambiguous"):
        routes.append("citation")
    left = residual(query, list(works.values()))
    if works and "citation" not in routes:
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
        tuple(routes), citation if "citation" in routes else None, works, term, left, as_of, comparative
    )
