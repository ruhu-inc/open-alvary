"""Derived links within one published text (RSV-06): the definitions in scope for the terms a unit uses,
and the provisions it refers to by number. Deterministic rules over published structure, versioned as
LINKS; an unresolved reference is dropped, never guessed. Reviewed links (amendments, repeals) come
from the catalogue's checked relationships instead."""

import re

from schema.public import NodeViewRecord

LINKS = "oa-links-1"
MAX_DEFINITIONS = 5
MAX_REFERENCES = 5
# A definition opens with its quoted term: “Fund” means …; "data subject" includes …
_TERM = re.compile(r"""^\s*(?:\([a-z0-9]+\)\s*)?["“‘']([^"”’']{1,80})["”’']""", re.IGNORECASE)
# section 23, sections 23 and 24, s. 23, article 12, art. 12
_REFERENCE = re.compile(
    r"\b(?P<word>sections?|s\.|ss\.|articles?|art\.)\s*(?P<numbers>\d+[A-Za-z]?(?:\s*(?:,|and|or|to)\s*\d+[A-Za-z]?)*)",
    re.IGNORECASE,
)


def definitions(raw: bytes, nodes: list[NodeViewRecord]) -> list[tuple[str, NodeViewRecord]]:
    """Each definition node of a text with the term it defines, in document order."""
    found = []
    for node in sorted(nodes, key=lambda n: (n.start_byte, n.order)):
        if node.kind != "definition":
            continue
        match = _TERM.match(raw[node.start_byte : node.end_byte].decode("utf-8", "replace"))
        if match:
            found.append((" ".join(match.group(1).split()), node))
    return found


def in_scope(
    words: str, defined: list[tuple[str, NodeViewRecord]], own: set[str]
) -> list[tuple[str, NodeViewRecord]]:
    """The definitions whose term the unit's words use (whole words, ignoring case), first use first;
    never the unit's own definition."""
    folded = words.casefold()
    hits = []
    for term, node in defined:
        if node.id in own:
            continue
        match = re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", folded)
        if match:
            hits.append((match.start(), term, node))
    hits.sort(key=lambda h: (h[0], h[2].start_byte))
    seen, found = set(), []
    for _, term, node in hits:
        if term.casefold() in seen:
            continue
        seen.add(term.casefold())
        found.append((term, node))
    return found[:MAX_DEFINITIONS]


def references(
    words: str, by_locator: dict[str, NodeViewRecord], own: str
) -> list[tuple[str, NodeViewRecord]]:
    """The sections and articles of the same text the words refer to by number, resolved to their
    locators (sec_23, art_12); the unit's own provision and unresolved numbers are left out."""
    found, seen = [], set()
    for match in _REFERENCE.finditer(words):
        article = match.group("word").casefold().startswith("art")
        for number in re.findall(r"\d+[A-Za-z]?", match.group("numbers")):
            locator = f"{'art' if article else 'sec'}_{number}"
            node = by_locator.get(locator)
            if node is None or locator in seen or own == locator or own.startswith(locator + "__"):
                continue
            seen.add(locator)
            found.append((f"{'art.' if article else 's.'} {number}", node))
    return found[:MAX_REFERENCES]
