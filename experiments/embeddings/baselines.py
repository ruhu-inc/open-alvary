"""Alvary's current retrieval models as a baseline for the screen (EMB-01): OpenAI text-embedding-3-large
and Cohere rerank-v3.5. Approved for the synthetic laws and the NDPA development set only. Nothing
here is a dependency of the service, and no key is ever written to a file the repository tracks.

The keys come from the environment (the gitignored .env): BASELINE_OPENAI_API_KEY and
BASELINE_COHERE_API_KEY.

  OpenAI: the service's own HttpEmbedder speaks its /embeddings API (``openai_profile``).
  Cohere: ``CohereReranker`` answers the reranker interface (``scores``) from Cohere's /v2/rerank.
"""

import os

TIMEOUT_SECONDS = 30
MAX_CHARS = 2_000  # as the service's reranker sends

OPENAI_URL = "https://api.openai.com/v1"
OPENAI_MODEL = "text-embedding-3-large"
COHERE_URL = "https://api.cohere.com"
COHERE_MODEL = "rerank-v3.5"


def openai_profile(observed: str) -> dict:
    """A profile for text-embedding-3-large at its full 3,072 dimensions. The API serves no revision, so
    the date it was called stands in for one (EMB-05 cannot pin a hosted model)."""
    return {
        "id": "openai-text-embedding-3-large",
        "model": OPENAI_MODEL,
        "revision": f"api-{observed}",
        "dims": 3072,
        "embedder": "http",
    }


def openai_embedder(profile):
    from retrieval.vectors import HttpEmbedder

    return HttpEmbedder(profile, url=OPENAI_URL, key=_key("BASELINE_OPENAI_API_KEY"))


class CohereReranker:
    def __init__(self, observed: str, transport=None):
        import httpx

        self.profile = f"cohere-{COHERE_MODEL}@api-{observed}"
        self.client = httpx.Client(
            base_url=COHERE_URL,
            headers={"Authorization": f"Bearer {_key('BASELINE_COHERE_API_KEY')}"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    def scores(self, query: str, texts: list[str]) -> list[float]:
        answer = self.client.post(
            "/v2/rerank",
            json={"model": COHERE_MODEL, "query": query, "documents": [t[:MAX_CHARS] for t in texts]},
        )
        answer.raise_for_status()
        found = {item["index"]: float(item["relevance_score"]) for item in answer.json()["results"]}
        if set(found) != set(range(len(texts))):
            raise ValueError("The reranker did not score every candidate")
        return [found[i] for i in range(len(texts))]


def _key(name: str) -> str:
    key = os.environ.get(name)
    if not key:
        raise LookupError(f"Set {name} in the gitignored .env to run the baseline")
    return key
