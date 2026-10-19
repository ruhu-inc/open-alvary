"""The dense arm's parts (RSV-04, RSV-10, EMB): index profiles, how units are cut into windows, the
embedders that turn words into vectors, and the stores that keep and search a generation's vectors.

A profile pins everything that shapes the vector space: the model and its revision, dimensions, metric,
stored precision, the instructions put before queries and documents, and the window size. Changing any
of it is a new profile and a new generation (EMB-05). Queries and documents always use the same profile.

Embedders:
- ``http``: an OpenAI-compatible ``/embeddings`` endpoint that Open Alvary runs itself for an
  open-weight model (EMB: the production path never depends on a proprietary API; where it runs and
  its budget are D29). The endpoint comes from the environment, never from a profile file.
- ``hashing``: deterministic feature hashing of words, for tests and wiring checks only. It finds
  shared words, not meaning, and a generation built with it is labelled ``test`` and refused for
  activation unless tests allow it.

Stores:
- ``pgvector``: ``halfvec`` with an HNSW index per jurisdiction partition, searched with iterative
  index scans so filters keep their recall.
- ``exact``: plain ``real[]`` columns searched exactly, for databases without the extension and
  for small scopes; bounded by EXACT_LIMIT.
"""

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

EXACT_LIMIT = 50_000  # windows an exact search may read
WINDOW_CHARS, WINDOW_OVERLAP = 2_000, 200  # about 400-500 tokens of English, overlapping by a sentence
TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class Profile:
    id: str
    model: str
    revision: str
    dims: int
    embedder: str  # http or hashing
    metric: str = "cosine"
    precision: str = "halfvec"
    query_prompt: str = ""
    document_prompt: str = ""
    window_chars: int = WINDOW_CHARS
    window_overlap: int = WINDOW_OVERLAP
    test: bool = False

    @classmethod
    def from_dict(cls, values: dict) -> "Profile":
        profile = cls(**values)
        if profile.embedder not in ("http", "hashing"):
            raise ValueError("embedder is http or hashing")
        if profile.embedder == "hashing" and not profile.test:
            raise ValueError("a hashing profile is for tests only: set test to true")
        if not 8 <= profile.dims <= 4000:
            raise ValueError("dims must be 8 to 4,000 (pgvector halfvec's limit)")
        if profile.embedder == "http" and profile.revision in ("", "pin-before-use"):
            raise ValueError("pin the exact model revision before building with this profile (EMB-05)")
        if profile.metric != "cosine":
            raise ValueError("only cosine distance is supported")
        if (
            not 200 <= profile.window_chars <= 20_000
            or not 0 <= profile.window_overlap < profile.window_chars // 2
        ):
            raise ValueError("windows must be 200 to 20,000 characters, overlapping less than half")
        return profile

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def windows(words: str, size: int, overlap: int) -> list[tuple[int, int]]:
    """Character ranges of ``words`` no longer than ``size``, cut after a sentence or a line where one is
    near the end, each starting ``overlap`` characters before the previous one ended."""
    if len(words) <= size:
        return [(0, len(words))] if words.strip() else []
    found, start = [], 0
    while start < len(words):
        end = min(len(words), start + size)
        if end < len(words):
            window = words[start:end]
            cut = max(window.rfind(". "), window.rfind(".\n"), window.rfind("\n"))
            if cut > size // 2:
                end = start + cut + 1
        found.append((start, end))
        if end >= len(words):
            break
        start = max(end - overlap, start + 1)
    return found


# --- embedders -------------------------------------------------------------------------------------


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


class HashingEmbedder:
    """Words hashed into signed buckets (test only): shared words make vectors close."""

    def __init__(self, profile: Profile):
        self.dims = profile.dims

    def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        vectors = []
        for value in texts:
            vector = [0.0] * self.dims
            for word in re.findall(r"\w+", value.casefold()):
                digest = hashlib.sha256(word.encode()).digest()
                index = int.from_bytes(digest[:4], "big") % self.dims
                vector[index] += 1.0 if digest[4] & 1 else -1.0
            vectors.append(_normalise(vector))
        return vectors


class HttpEmbedder:
    """An OpenAI-compatible embeddings endpoint serving the profile's model (OPEN_ALVARY_EMBEDDER_URL,
    with an optional OPEN_ALVARY_EMBEDDER_KEY)."""

    def __init__(self, profile: Profile, url: str | None = None, key: str | None = None, transport=None):
        import httpx

        self.profile = profile
        url = url or os.environ.get("OPEN_ALVARY_EMBEDDER_URL")
        if not url:
            raise LookupError("No embedder: set OPEN_ALVARY_EMBEDDER_URL")
        headers = {"Authorization": f"Bearer {key or os.environ.get('OPEN_ALVARY_EMBEDDER_KEY', '')}"}
        self.client = httpx.Client(
            base_url=url.rstrip("/"), headers=headers, timeout=TIMEOUT_SECONDS, transport=transport
        )

    def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        prompt = self.profile.query_prompt if query else self.profile.document_prompt
        answer = self.client.post(
            "/embeddings", json={"model": self.profile.model, "input": [prompt + t for t in texts]}
        )
        answer.raise_for_status()
        data = sorted(answer.json()["data"], key=lambda d: d["index"])
        vectors = [_normalise([float(x) for x in d["embedding"]]) for d in data]
        if len(vectors) != len(texts) or any(len(v) != self.profile.dims for v in vectors):
            raise ValueError("The embedder answered the wrong number or size of vectors")
        if any(not all(math.isfinite(x) for x in v) for v in vectors):
            raise ValueError("The embedder answered a vector that is not finite")
        return vectors


def embedder_for(profile: Profile):
    return HashingEmbedder(profile) if profile.embedder == "hashing" else HttpEmbedder(profile)


# --- stores ---------------------------------------------------------------------------------------


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


@dataclass
class Store:
    """One generation's vectors table: (version_id, unit_start) names the section index's unit; each row
    is one window of it, with its own byte span."""

    conn: Connection
    table: str  # qualified
    backend: str
    dims: int
    partitions: list[str] = field(default_factory=list)

    def create(self, jurisdictions: list[str], roles: dict[str, str]) -> None:
        """Create the table (and, with pgvector, a partition and an HNSW index per jurisdiction plus a
        default partition), granted to the capability roles. Needs an administrator."""
        column = f"halfvec({self.dims})" if self.backend == "pgvector" else "real[]"
        partitioned = " PARTITION BY LIST (jurisdiction_id)" if self.backend == "pgvector" else ""
        self.conn.execute(
            text(
                f"CREATE TABLE {self.table} (version_id text NOT NULL, unit_start integer NOT NULL,"
                " node_id text NOT NULL, jurisdiction_id text NOT NULL, start_byte integer NOT NULL,"
                f" end_byte integer NOT NULL, embedding {column} NOT NULL,"
                " PRIMARY KEY (jurisdiction_id, version_id, unit_start, start_byte))" + partitioned
            )
        )
        if self.backend == "pgvector":
            base = self.table.rsplit(".", 1)
            for n, jurisdiction in enumerate([*jurisdictions, None]):
                name = f'{base[0]}."{base[1].strip(chr(34))}_p{n}"'
                if jurisdiction is None:
                    self.conn.execute(text(f"CREATE TABLE {name} PARTITION OF {self.table} DEFAULT"))
                else:
                    # DDL takes no parameters: the ID is checked against the jurisdiction ID pattern first.
                    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,159}", jurisdiction):
                        raise ValueError(f"Not a jurisdiction ID: {jurisdiction!r}")
                    self.conn.execute(
                        text(
                            f"CREATE TABLE {name} PARTITION OF {self.table} FOR VALUES IN ('{jurisdiction}')"
                        )
                    )
                self.conn.execute(text(f"CREATE INDEX ON {name} USING hnsw (embedding halfvec_cosine_ops)"))
        self.conn.execute(text(f"CREATE INDEX ON {self.table} (version_id)"))
        for capability, privileges in (("release", "SELECT, INSERT, DELETE"), ("serving", "SELECT")):
            role = roles.get(capability)
            if role and self.conn.scalar(text("SELECT to_regrole(:r)"), {"r": role}) is not None:
                quoted = self.conn.dialect.identifier_preparer.quote_identifier(role)
                self.conn.execute(text(f"GRANT {privileges} ON {self.table} TO {quoted}"))

    def replace(self, version_id: str, rows: list[dict]) -> None:
        """A text's vectors, all at once (its old ones removed in the same transaction)."""
        self.conn.execute(text(f"DELETE FROM {self.table} WHERE version_id = :v"), {"v": version_id})
        if not rows:
            return
        cast = (
            f"CAST(:embedding AS halfvec({self.dims}))"
            if self.backend == "pgvector"
            else "CAST(:embedding AS real[])"
        )
        self.conn.execute(
            text(
                f"INSERT INTO {self.table} (version_id, unit_start, node_id, jurisdiction_id, start_byte, end_byte,"
                f" embedding) VALUES (:version_id, :unit_start, :node_id, :jurisdiction_id, :start_byte, :end_byte,"
                f" {cast})"
            ),
            [
                r | {"embedding": _literal(r["embedding"]) if self.backend == "pgvector" else r["embedding"]}
                for r in rows
            ],
        )

    def purge(self, live: str) -> int:
        """Remove vectors of texts no longer in the section index (``live`` is its table)."""
        return self.conn.execute(
            text(
                f"DELETE FROM {self.table} v WHERE NOT EXISTS (SELECT 1 FROM {live} u WHERE u.version_id = v.version_id)"
            )
        ).rowcount

    def search(self, units: str, where: str, params: dict, query: list[float], limit: int) -> list:
        """The best windows' units for ``query`` within the filters (``where`` over the section index
        ``units`` as u), best first, one row per unit with its distance; only units still indexed, so a
        withdrawn text is never returned even before its vectors are purged."""
        from api.text_index import _COLUMNS

        if self.backend == "pgvector":
            self.conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
            self.conn.execute(text("SET LOCAL hnsw.ef_search = 200"))
            q = f"CAST(:q AS halfvec({self.dims}))"
            pruned = " AND v.jurisdiction_id = ANY(:f_jurisdictions)" if "f_jurisdictions" in params else ""
            rows = self.conn.execute(
                text(
                    f"SELECT {_COLUMNS}, best.distance FROM (SELECT DISTINCT ON (w.version_id, w.unit_start)"
                    " w.version_id, w.unit_start, w.distance FROM (SELECT v.version_id, v.unit_start,"
                    f" v.embedding <=> {q} AS distance FROM {self.table} v JOIN {units} u"
                    f" ON u.version_id = v.version_id AND u.start_byte = v.unit_start WHERE {where}{pruned}"
                    f" ORDER BY v.embedding <=> {q} LIMIT :window_limit) w"
                    " ORDER BY w.version_id, w.unit_start, w.distance) best"
                    f" JOIN {units} u ON u.version_id = best.version_id AND u.start_byte = best.unit_start"
                    " ORDER BY best.distance, u.version_id, u.start_byte LIMIT :limit"
                ),
                params | {"q": _literal(query), "window_limit": limit * 10, "limit": limit},
            ).all()
            return [(row, float(row.distance)) for row in rows]
        rows = self.conn.execute(
            text(
                f"SELECT {_COLUMNS}, v.embedding FROM {self.table} v JOIN {units} u"
                f" ON u.version_id = v.version_id AND u.start_byte = v.unit_start WHERE {where}"
                f" LIMIT {EXACT_LIMIT + 1}"
            ),
            params,
        ).all()
        if len(rows) > EXACT_LIMIT:
            raise OverflowError("Too many windows in scope for exact search: narrow the filters")
        best: dict[tuple, tuple] = {}
        for row in rows:
            distance = 1.0 - sum(a * b for a, b in zip(row.embedding, query, strict=True))
            key = (row.version_id, row.start_byte)
            if key not in best or distance < best[key][1]:
                best[key] = (row, distance)
        return sorted(best.values(), key=lambda item: (item[1], item[0].version_id, item[0].start_byte))[
            :limit
        ]


def profile_hash(profile: Profile) -> str:
    return hashlib.sha256(json.dumps(profile.as_dict(), sort_keys=True).encode()).hexdigest()
