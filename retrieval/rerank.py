"""The reranker (RSV-05, RET-02): an open-weight cross-encoder served by Open Alvary scores at most 50 fused
candidates against the query; its order then replaces the fused order among them. Configured by
OPEN_ALVARY_RERANKER_URL (a text-embeddings-inference compatible /rerank endpoint) and
OPEN_ALVARY_RERANKER_PROFILE (the pinned model and revision it serves, reported in each pack). Without
them, or when it fails or is slow, the fused order stands and the pack says so."""

import os

MAX_CANDIDATES = 50
MAX_CHARS = 2_000  # of each candidate's words
TIMEOUT_SECONDS = 5


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
