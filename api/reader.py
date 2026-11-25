"""The website's reader projection, ``reader-1`` (PRD-04): one public text and only what the record page
uses to lay it out. The keyed API keeps the full node record; this is not it and never replaces it.

A node is sent in one of two shapes. A full node is something the page can head, list or link to; an
indentation node (a subsection, paragraph or subparagraph) only says how far its words are set in. Nodes
come in document order and carry no parent: the page reads nesting from their spans, which is sound only
where spans and parents agree, so that is confirmed here for every text, and a text where they do not is
sent with no nodes and shown plain.
"""

from collections.abc import Sequence
from datetime import date

SCHEMA = "reader-1"
INDENTS = frozenset({"subsection", "paragraph", "subparagraph"})
UNSENT = frozenset({"definition", "quotation"})  # the page lays their words out as the text around them
JUDGMENT = frozenset({"opinion", "order"})


def reader_text(texts: Sequence, record_language=None, version_id=None, language=None):
    """The text the record page opens among a record's public ones (PRD-03), and how it was chosen:
    ``(text, matched, missed)``. The one named by ID; else the latest in the language asked for; else the
    latest in the record's own language; else the latest in the first language by code. ``missed`` lists
    what was asked for and is not there. A choice of what to show first, never a statement of which text
    is in force or which language prevails. With no text, all three are None."""
    if not texts:
        return None, None, None

    def latest(wanted):
        group = [t for t in texts if wanted and t.language.casefold() == wanted.casefold()]
        replaced = {t.prior_version_id for t in group if t.prior_version_id}
        # Texts that name each other as replaced leave none: then all of them count.
        return max([t for t in group if t.id not in replaced] or group, key=_age, default=None)

    missed = []
    if version_id is not None:
        for text in texts:
            if text.id == version_id:
                return text, "version", []  # the language, if one was sent too, is not looked at
        missed.append("version")
    if language is not None:
        if found := latest(language):
            return found, "language", missed
        missed.append("language")
    first = min(t.language.casefold() for t in texts)
    return latest(record_language) or latest(first), "default", missed


def _age(text) -> tuple:
    return (text.effective_from or date.min, text.retrieved_at, text.id)


def listed(texts: Sequence) -> list[dict]:
    """A record's public texts without their words, by language code and within a language latest first."""
    by_language: dict[str, list] = {}
    for text in texts:
        by_language.setdefault(text.language.casefold(), []).append(text)
    return [
        text.model_dump(mode="json", include={"id", "language", "effective_from", "retrieved_at"})
        for language in sorted(by_language)
        for text in sorted(by_language[language], key=_age, reverse=True)
    ]


def reader_nodes(nodes: Sequence) -> list[dict] | None:
    """A text's nodes as the page reads them, or None where spans and parents do not agree."""
    by_id = {node.id: node for node in nodes}

    def ancestors(node) -> list:
        found, at = [], by_id.get(node.parent_id)
        while at is not None and len(found) <= len(by_id):
            found.append(at)
            at = by_id.get(at.parent_id)
        return found

    chains = {node.id: ancestors(node) for node in nodes}

    def own_paragraph(node) -> bool:
        """A judgment's numbered paragraph: its body, not a part of a provision."""
        parent = by_id.get(node.parent_id)
        return node.kind == "paragraph" and parent is not None and parent.kind in JUDGMENT

    def sent(node) -> bool:
        if node.end_byte == node.start_byte or node.kind in UNSENT or own_paragraph(node):
            return False
        # A line of the printed table of contents is known by the contents node around it.
        return node.kind != "contents_entry" or not any(a.kind == "contents" for a in chains[node.id])

    going = {node.id for node in nodes if sent(node)}
    ordered = sorted(
        (node for node in nodes if node.id in going),
        key=lambda n: (n.start_byte, -n.end_byte, len(chains[n.id]), n.order, n.id),
    )
    out, open_ = [], []
    for node in ordered:
        while open_ and open_[-1].end_byte <= node.start_byte:
            open_.pop()
        nearest = next((a.id for a in chains[node.id] if a.id in going), None)
        if (open_ and node.end_byte > open_[-1].end_byte) or (open_[-1].id if open_ else None) != nearest:
            return None  # two spans cross, or a node sits in another that is not its ancestor
        open_.append(node)
        if node.kind in INDENTS:
            depth = sum(a.kind in INDENTS and not own_paragraph(a) for a in (node, *chains[node.id]))
            out.append(
                {
                    "kind": node.kind,
                    "start_byte": node.start_byte,
                    "end_byte": node.end_byte,
                    "depth": min(3, depth),
                }
            )
            continue
        full = {
            "id": node.id,
            "kind": node.kind,
            "locator": node.locator,
            "start_byte": node.start_byte,
            "end_byte": node.end_byte,
        }
        for name in ("label", "heading", "cell_states"):
            if value := getattr(node, name, None):
                full[name] = value
        out.append(full)
    return out


def answer(version, nodes: Sequence, texts: Sequence, matched, missed, notice=None) -> dict:
    """The ``reader-1`` answer: ``version`` with its ``nodes``, or no text. ``texts`` are the record's
    public texts' details."""
    shown = {"schema": SCHEMA, "modes": ["reading"], "text": None, "texts": listed(texts)}
    if version is None:
        return {**shown, "matched": None, "missed": None, "nodes": [], "notice": notice}
    details = version.model_dump(
        mode="json", include={"id", "language", "content_hash", "effective_from", "retrieved_at"}
    )
    return {
        **shown,
        "text": {**details, "bytes": len(version.canonical_text.encode())},
        "matched": matched,
        "missed": missed,
        "canonical_text": version.canonical_text,
        "nodes": reader_nodes(nodes) or [],
        "notice": None,
    }
