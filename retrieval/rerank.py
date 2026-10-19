"""The reranker (RSV-05, RET-02): an open-weight cross-encoder served by Open Alvary scores at most 50 fused
candidates against the query; its order then replaces the fused order among them. Configured by
OPEN_ALVARY_RERANKER_URL (a text-embeddings-inference compatible /rerank endpoint) and
OPEN_ALVARY_RERANKER_PROFILE (the pinned model and revision it serves, reported in each pack). Without
them, or when it fails or is slow, the fused order stands and the pack says so.

A long unit is scored by its best passage: it is cut into the embedder's windows, and its opening and
the windows holding most of the query's words are scored, the unit taking the highest (``passages``).
A weak match, found only by meaning or by some of the query's words, must reach ``SUPPORT_FLOOR`` to be
evidence (retrieval.pack)."""

import os
import re

MAX_CANDIDATES = 50
MAX_CHARS = 2_000  # of each candidate's words
PASSAGES = 3  # the windows of one unit that are scored
# A unit found only by meaning or by some of the query's words (a weak match) is evidence when the
# reranker scores it at least this. Provisional, from the development sets: unanswerable questions'
# weak matches scored at most 0.001, and the lowest relevant weak match 0.259; units found by every
# word, a citation or a definition are never held to it (a relevant citation scored 0.0).
SUPPORT_FLOOR = 0.1
WEAK_ARMS = frozenset({"dense", "lexical_any"})
TIMEOUT_SECONDS = 5


def passages(words: str, terms: set[str]) -> list[str]:
    """The passages of a unit to score: the whole unit when it fits, else its opening and the windows
    with most of ``terms`` (casefolded query words), at most PASSAGES, in the unit's order."""
    from retrieval.vectors import WINDOW_OVERLAP, windows

    found = windows(words, MAX_CHARS, WINDOW_OVERLAP)
    if len(found) <= 1:
        return [words[:MAX_CHARS]]

    def overlap(span: tuple[int, int]) -> int:
        tokens = set(re.findall(r"\w+", words[span[0] : span[1]].casefold()))
        return sum(any(t.startswith(term[:5]) for t in tokens) for term in terms)

    best = sorted(found[1:], key=lambda span: (-overlap(span), span[0]))[: PASSAGES - 1]
    return [words[a:b] for a, b in sorted([found[0], *best])]


class HttpReranker:
    def __init__(self, url: str, profile: str, transport=None):
        import httpx

        self.profile = profile
        self.client = httpx.Client(base_url=url.rstrip("/"), timeout=TIMEOUT_SECONDS, transport=transport)

    def scores(self, query: str, texts: list[str]) -> list[float]:
        texts = [t[:MAX_CHARS] for t in texts]
        answer = self.client.post("/rerank", json={"query": query, "texts": texts, "raw_scores": False})
        answer.raise_for_status()
        found = {item["index"]: float(item["score"]) for item in answer.json()}
        if set(found) != set(range(len(texts))):
            raise ValueError("The reranker did not score every candidate")
        return [found[i] for i in range(len(texts))]


def configured():
    url, profile = os.environ.get("OPEN_ALVARY_RERANKER_URL"), os.environ.get("OPEN_ALVARY_RERANKER_PROFILE")
    return HttpReranker(url, profile) if url and profile else None
