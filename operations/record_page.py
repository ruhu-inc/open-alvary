"""The public texts the website's record page cannot show (public-reader PRD-05), asked of a catalogue
before it is saved.

The page reads one text from ``GET /site/v1/sources/{id}/text`` (api.site). That route refuses an answer
over ``api.site.MAX_TEXT_BYTES``, and the catalogue under it refuses a text with more than
``MAX_STRUCTURE_ROWS`` nodes; the page then says the text is too large and links the official copy.
Loading a text in parts is not designed yet (PRD-05). Such a text is published all the same (owner,
2026-11-25): the keyed API serves its words within its own bound. Publishing says which texts these are,
on the run and on each one's document (operations.publish).

Nothing here is estimated. A text is chosen by api.reader.reader_text, its answer is built by
api.reader.answer and encoded by api.site.text_payload, as api.catalogue.LegacyCatalogue.reading and the
route do, and both bounds are read where the site reads them: a size here is the size the site would send.
"""

from dataclasses import dataclass
from itertools import product

from api import catalogue, reader, site
from rights.policy import decide
from schema.models import Corpus

# Stands for a text ID or a language the record does not have, which is what a link kept from before a
# correction asks for: the answer then also says what it missed, and is that much longer.
ELSEWHERE = ""
# How a document's standing begins when its text is public and the page cannot show it (the workbench
# reads this to say so beside the document).
TOO_LARGE = "its text is too large for the record page"


@dataclass(frozen=True)
class Unshown:
    """A public text the record page refuses, measured as the site measures it."""

    work: str
    language: str
    version: str
    content_hash: str
    nodes: int
    # Its largest answer in bytes, which is the one to a link that asks for a text or a language the record
    # no longer has; None when its nodes alone refuse it, before any answer is built.
    size: int | None

    @property
    def why(self) -> str:
        if self.size is None:
            return (
                f"its structure has {self.nodes:,} nodes; one answer holds at most "
                f"{catalogue.MAX_STRUCTURE_ROWS:,}"
            )
        return f"its answer is up to {self.size:,} bytes; the page reads at most {site.MAX_TEXT_BYTES:,}"


def largest_answer(text, texts, nodes, record_language) -> int:
    """The bytes of the largest answer the site gives that holds ``text``, over every way of asking that
    opens it: by its ID, by its language, or as the record's first text, with or without also asking for
    something the record does not have. ``texts`` are the record's public texts, ``text`` among them."""
    asked = product((text.id, None, ELSEWHERE), (None, text.language, ELSEWHERE))
    outcomes = set()
    for version_id, language in asked:
        chosen, matched, missed = reader.reader_text(texts, record_language, version_id, language)
        if chosen.id == text.id:
            outcomes.add((matched, tuple(missed)))
    return max(
        len(site.text_payload(reader.answer(text, nodes, texts, matched, list(missed))))
        for matched, missed in outcomes
    )


def unshown(corpus: Corpus, jurisdiction: str) -> list[Unshown]:
    """Each public text of a jurisdiction's records that the record page would refuse, by record, language
    and text ID. ``corpus`` is the catalogue as it is about to be saved."""
    places = {s.id: s for s in corpus.sources if s.jurisdiction_id == jurisdiction}
    rights = {r.source_id: r for r in corpus.rights}
    public: dict[str, list] = {}
    for version in corpus.versions:
        if version.source_id in places and decide(rights.get(version.source_id), version).full_text:
            public.setdefault(version.source_id, []).append(version)
    nodes: dict[str, list] = {v.id: [] for texts in public.values() for v in texts}
    for node in corpus.structure:
        if node.version_id in nodes:
            nodes[node.version_id].append(node)
    found = []
    for work, texts in public.items():
        texts.sort(key=lambda v: v.id)
        for text in texts:
            count = len(nodes[text.id])
            if count > catalogue.MAX_STRUCTURE_ROWS:
                found.append(Unshown(work, text.language, text.id, text.content_hash, count, None))
                continue
            size = largest_answer(text, texts, nodes[text.id], places[work].language)
            if size > site.MAX_TEXT_BYTES:
                found.append(Unshown(work, text.language, text.id, text.content_hash, count, size))
    return sorted(found, key=lambda u: (u.work, u.language, u.version))
