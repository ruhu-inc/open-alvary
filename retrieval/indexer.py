"""Semantic index generations (RSV-10): prepared by the operator, filled by background workers, validated,
then activated atomically; rolled back only to a generation that still covers what is public.

  python -m retrieval.indexer prepare PROFILE.json [--backend pgvector|exact]   (administrator URL)
  python -m retrieval.indexer work [--batches N]                                 (the worker, repeatedly)
  python -m retrieval.indexer validate GENERATION
  python -m retrieval.indexer activate GENERATION
  python -m retrieval.indexer rollback PROFILE_ID GENERATION
  python -m retrieval.indexer purge
  python -m retrieval.indexer status

Publication queues a job for each new text in every building or active generation, in its own
transaction (api.text_index.sync); a worker leases jobs, reads the text's units, embeds their windows
outside any transaction, and writes all of the text's vectors at once. A text waiting for its vectors is
still found by the lexical arms, and the evidence pack reports the dense coverage; publication never
waits for embeddings. Withdrawal is enforced at query time (vectors join the section index), and purge
removes the vectors afterwards.
"""

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.engine import Connection, Engine

from retrieval.vectors import Profile, Store, embedder_for, fitted, profile_hash, windows

MAX_ATTEMPTS = 3
LEASE_SECONDS = 600
BATCH = 32  # windows per embedding request
RECOMPUTE_FLOOR = 0.995  # cosine between a stored vector and its recomputation (halfvec rounds)
ANN_FLOOR = 0.98  # EMB-03's proposed approximate-search recall@20 against exact search


def _t(conn: Connection, name: str) -> str:
    from changes.record import namespace

    return namespace(conn).qualified(conn, "changes", name)


def available(conn: Connection) -> bool:
    return conn.dialect.name == "postgresql" and (
        conn.scalar(text("SELECT to_regclass(:t)"), {"t": _t(conn, "index_generation")}) is not None
    )


class EngineStore:
    """A generation whose vectors live in the search engine (backend ``vespa``): a field of each unit,
    one vector per window, set by partial update."""

    def __init__(self, engine, vectors: str, dims: int):
        self.engine, self.vectors, self.dims = engine, vectors, dims

    def replace(self, version_id: str, rows: list[dict]) -> None:
        by_unit: dict[int, dict[str, list[float]]] = {}
        for row in rows:
            windows = by_unit.setdefault(row["unit_start"], {})
            windows[str(len(windows))] = row["embedding"]
        for unit_start, windows in sorted(by_unit.items()):
            if not self.engine.update_embedding(version_id, unit_start, self.vectors, windows):
                raise LookupError(
                    f"{version_id}:{unit_start} is not in the engine yet; the projector feeds it first"
                )

    def purge(self, live: str) -> int:
        return 0  # the projector deletes a withdrawn text's units, vectors and all


def search_engine():
    from retrieval.engine import Engine, config_from_env

    config = config_from_env()
    if config is None:
        raise LookupError("No search engine: set OPEN_ALVARY_ENGINE_URL")
    return Engine(*config)


def store_for(conn: Connection, generation, engine=None):
    if generation["backend"] == "vespa":
        return EngineStore(
            engine or search_engine(), generation["vectors_table"], generation["profile"]["dims"]
        )
    from changes.record import namespace

    table = namespace(conn).qualified(conn, "changes", generation["vectors_table"])
    return Store(conn, table, generation["backend"], generation["profile"]["dims"])


def generation(conn: Connection, generation_id: str):
    row = (
        conn.execute(
            text(f"SELECT * FROM {_t(conn, 'index_generation')} WHERE generation_id = :g"),
            {"g": generation_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise LookupError(f"No generation {generation_id}")
    return dict(row)


def active(conn: Connection, profile_id: str):
    """The profile's active generation, or None."""
    if not available(conn):
        return None
    row = (
        conn.execute(
            text(f"SELECT * FROM {_t(conn, 'index_generation')} WHERE profile_id = :p AND state = 'active'"),
            {"p": profile_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def queue(conn: Connection, version_ids) -> int:
    """Queue embedding jobs for new texts in every building or active generation (publication calls this
    inside its transaction)."""
    if not version_ids or not available(conn):
        return 0
    return conn.execute(
        text(
            f"INSERT INTO {_t(conn, 'index_job')} (generation_id, version_id, state)"
            f" SELECT g.generation_id, v, 'queued' FROM {_t(conn, 'index_generation')} g,"
            " unnest(CAST(:versions AS text[])) AS v WHERE g.state IN ('building', 'validating', 'active')"
            " ON CONFLICT DO NOTHING"
        ),
        {"versions": sorted(version_ids)},
    ).rowcount


def prepare(admin: Engine, profile: Profile, *, backend: str | None = None, engine=None) -> str:
    """A new building generation for ``profile``: its vectors table (or, with backend ``vespa``, a vector
    field of the search engine's units) and a job for every indexed text. Without a backend named: the
    engine when one is configured or given; else pgvector unless the extension cannot be installed (then
    exact search, or a refusal when ``backend`` asked for pgvector)."""
    from api.text_index import _tables
    from changes.record import namespace
    from retrieval.engine import config_from_env

    if backend == "vespa" or (backend is None and (engine is not None or config_from_env() is not None)):
        return _prepare_engine(admin, profile, engine or search_engine())
    with admin.begin() as conn:
        ns = namespace(conn)
        has_vector = conn.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"))
        if not has_vector and backend != "exact":
            try:
                with conn.begin_nested():
                    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                has_vector = True
            except Exception:
                has_vector = False
        chosen = backend or ("pgvector" if has_vector else "exact")
        if chosen == "pgvector" and not has_vector:
            raise RuntimeError("pgvector is not available to this database or role")
        generation_id = f"gen_{uuid.uuid4().hex}"
        table_name = f"vectors_{generation_id[4:]}"
        units, _ = _tables(conn)
        jurisdictions = sorted(conn.scalars(text(f"SELECT DISTINCT jurisdiction_id FROM {units}")).all())
        store = Store(conn, ns.qualified(conn, "changes", table_name), chosen, profile.dims)
        store.create(jurisdictions, {"release": ns.role("release"), "serving": ns.role("serving")})
        conn.execute(
            text(
                f"INSERT INTO {_t(conn, 'index_generation')} (generation_id, profile_id, profile, backend,"
                " vectors_table, state, manifest) VALUES (:g, :p, CAST(:profile AS jsonb), :b, :t, 'building',"
                " CAST(:manifest AS jsonb))"
            ),
            {
                "g": generation_id, "p": profile.id, "profile": json.dumps(profile.as_dict()), "b": chosen,
                "t": table_name, "manifest": json.dumps({"profile_hash": profile_hash(profile),
                                                         "prepared_at": datetime.now(UTC).isoformat()}),
            },
        )  # fmt: skip
        conn.execute(
            text(
                f"INSERT INTO {_t(conn, 'index_job')} (generation_id, version_id, state)"
                f" SELECT :g, version_id, 'queued' FROM {units} GROUP BY version_id"
            ),
            {"g": generation_id},
        )
    return generation_id


def _prepare_engine(admin: Engine, profile: Profile, engine) -> str:
    """A generation whose vectors are a new field of the engine's units, deployed with the others."""
    from api.text_index import _tables
    from retrieval import projector
    from retrieval.engine import slug

    generation_id = f"gen_{uuid.uuid4().hex}"
    vectors = f"emb_{slug(profile.id)[:40]}_{generation_id[4:12]}"
    with admin.begin() as conn:
        units, _ = _tables(conn)
        conn.execute(
            text(
                f"INSERT INTO {_t(conn, 'index_generation')} (generation_id, profile_id, profile, backend,"
                " vectors_table, state, manifest) VALUES (:g, :p, CAST(:profile AS jsonb), 'vespa', :t, 'building',"
                " CAST(:manifest AS jsonb))"
            ),
            {
                "g": generation_id, "p": profile.id, "profile": json.dumps(profile.as_dict()), "t": vectors,
                "manifest": json.dumps({"profile_hash": profile_hash(profile), "prepared_at": datetime.now(UTC).isoformat()}),
            },
        )  # fmt: skip
        conn.execute(
            text(
                f"INSERT INTO {_t(conn, 'index_job')} (generation_id, version_id, state)"
                f" SELECT :g, version_id, 'queued' FROM {units} GROUP BY version_id"
            ),
            {"g": generation_id},
        )
    projector.deploy(admin, engine)
    return generation_id


def _lease(engine: Engine, batch: int) -> list[tuple[str, str]]:
    with engine.begin() as conn:
        jobs = _t(conn, "index_job")
        rows = conn.execute(
            text(
                f"UPDATE {jobs} j SET state = 'running', attempts = j.attempts + 1,"
                f" lease_until = statement_timestamp() + interval '{LEASE_SECONDS} seconds',"
                " updated_at = statement_timestamp() WHERE (j.generation_id, j.version_id) IN ("
                f" SELECT generation_id, version_id FROM {jobs} WHERE state = 'queued'"
                " OR (state = 'running' AND lease_until < statement_timestamp())"
                f" OR (state = 'failed' AND attempts < {MAX_ATTEMPTS}"
                " AND updated_at < statement_timestamp() - attempts * interval '1 minute')"
                " ORDER BY updated_at, generation_id, version_id LIMIT :n FOR UPDATE SKIP LOCKED)"
                " RETURNING j.generation_id, j.version_id"
            ),
            {"n": batch},
        ).all()
    return [(r.generation_id, r.version_id) for r in rows]


def _inputs(conn: Connection, version_id: str, profile: Profile) -> list[dict]:
    """The windows of a text's units, with the words each embeds: the work's title and the unit's
    headings as context, then the window's words."""
    from api.store import tables
    from api.text_index import _tables

    units, _ = _tables(conn)
    rows = conn.execute(
        text(f"SELECT start_byte, end_byte, node_id, jurisdiction_id, heading, pinpoint, work_id FROM {units}"
             " WHERE version_id = :v ORDER BY start_byte"),
        {"v": version_id},
    ).all()  # fmt: skip
    if not rows:
        return []
    versions, sources = tables["versions"], tables["sources"]
    payload = conn.scalar(select(versions.c.payload).where(versions.c.id == version_id))
    source = conn.scalar(select(sources.c.payload).where(sources.c.id == rows[0].work_id)) or {}
    title = source.get("title")
    raw = payload["canonical_text"].encode()
    found = []
    for row in rows:
        words = raw[row.start_byte : row.end_byte].decode("utf-8")
        context = " — ".join(filter(None, (title, row.pinpoint, row.heading)))
        for start, end in windows(words, profile.window_chars, profile.window_overlap):
            begin = row.start_byte + len(words[:start].encode())
            finish = row.start_byte + len(words[:end].encode())
            found.append(
                {
                    "version_id": version_id, "unit_start": row.start_byte, "node_id": row.node_id,
                    "jurisdiction_id": row.jurisdiction_id, "start_byte": begin, "end_byte": finish,
                    "input": fitted(context, words[start:end], profile.document_prompt),
                }
            )  # fmt: skip
    return found


def work(
    engine: Engine, *, batches: int = 1, batch: int = 10, embedders: dict | None = None, searcher=None
) -> dict:
    """Lease and run up to ``batches`` batches of jobs; returns counts. ``embedders`` maps a profile ID to
    an embedder (tests); otherwise each profile's own."""
    embedders = embedders or {}
    counts = {"done": 0, "failed": 0, "vectors": 0}
    for _ in range(batches):
        leased = _lease(engine, batch)
        if not leased:
            break
        for generation_id, version_id in leased:
            try:
                with engine.begin() as conn:
                    found = generation(conn, generation_id)
                    profile = Profile.from_dict(found["profile"])
                    inputs = _inputs(conn, version_id, profile)
                embedder = embedders.get(profile.id) or embedders.setdefault(
                    profile.id, embedder_for(profile)
                )
                vectors = []
                for start in range(0, len(inputs), BATCH):
                    chunk = inputs[start : start + BATCH]
                    vectors += embedder.embed([i["input"] for i in chunk], query=False)
                with engine.begin() as conn:
                    store_for(conn, found, searcher).replace(
                        version_id,
                        [
                            {k: v for k, v in i.items() if k != "input"} | {"embedding": e}
                            for i, e in zip(inputs, vectors, strict=True)
                        ],  # fmt: skip
                    )
                    conn.execute(
                        text(
                            f"UPDATE {_t(conn, 'index_job')} SET state = 'done', vectors = :n, error = NULL,"
                            " lease_until = NULL, updated_at = statement_timestamp()"
                            " WHERE generation_id = :g AND version_id = :v"
                        ),
                        {"n": len(vectors), "g": generation_id, "v": version_id},
                    )
                counts["done"] += 1
                counts["vectors"] += len(vectors)
            except Exception as error:  # recorded for the operator; retried with backoff
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f"UPDATE {_t(conn, 'index_job')} SET state = 'failed', lease_until = NULL,"
                            " error = :e, updated_at = statement_timestamp() WHERE generation_id = :g AND version_id = :v"
                        ),
                        {
                            "e": f"{type(error).__name__}: {str(error)[:500]}",
                            "g": generation_id,
                            "v": version_id,
                        },
                    )
                counts["failed"] += 1
    return counts


def validate(engine: Engine, generation_id: str, *, embedder=None, sample: int = 20, searcher=None) -> dict:
    """Coverage of every indexed unit, recomputation of a sample, and approximate-search recall against
    exact search (pgvector and the engine). Records the results; the generation becomes validating when
    they pass."""
    from api.text_index import _tables

    with engine.begin() as conn:
        found = generation(conn, generation_id)
    if found["backend"] == "vespa":
        return _record(engine, generation_id, _validate_engine(engine, found, embedder, searcher, sample))
    with engine.begin() as conn:
        profile = Profile.from_dict(found["profile"])
        store = store_for(conn, found)
        units, _ = _tables(conn)
        missing = conn.scalar(
            text(
                f"SELECT count(*) FROM {units} u WHERE NOT EXISTS (SELECT 1 FROM {store.table} v"
                " WHERE v.version_id = u.version_id AND v.unit_start = u.start_byte)"
            )
        )
        rows = conn.execute(
            text(
                f"SELECT v.version_id, v.unit_start, v.start_byte, v.end_byte, v.embedding::text AS embedding"
                f" FROM {store.table} v ORDER BY md5(v.version_id || v.start_byte::text) LIMIT :n"
            ),
            {"n": sample},
        ).all()
    embedder = embedder or embedder_for(profile)
    worst = 1.0
    if rows:
        with engine.begin() as conn:
            inputs = {
                (i["version_id"], i["start_byte"]): i["input"]
                for version in {r.version_id for r in rows}
                for i in _inputs(conn, version, profile)
            }
        stored = [json.loads(r.embedding.replace("{", "[").replace("}", "]")) for r in rows]
        again = embedder.embed([inputs.get((r.version_id, r.start_byte), "") for r in rows], query=False)
        worst = min(_cosine(x, y) for x, y in zip(stored, again, strict=True))
    recall = None
    if found["backend"] == "pgvector" and rows:
        recall = _ann_recall(engine, found, [json.loads(r.embedding) for r in rows[:10]])
    passed = missing == 0 and worst >= RECOMPUTE_FLOOR and (recall is None or recall >= ANN_FLOOR)
    results = {
        "validated_at": datetime.now(UTC).isoformat(), "missing_units": missing, "recompute_min_cosine": round(worst, 6),
        "ann_recall_at_20": recall, "passed": passed,
    }  # fmt: skip
    return _record(engine, generation_id, results)


def _validate_engine(engine: Engine, found, embedder, searcher, sample: int) -> dict:
    """The engine's generation: every indexed text's job done, a sample of stored vectors recomputed
    (bfloat16 rounds them), and the index's recall against exact search."""
    from api.text_index import _tables

    profile = Profile.from_dict(found["profile"])
    searcher = searcher or search_engine()
    vectors = found["vectors_table"]
    with engine.begin() as conn:
        units, _ = _tables(conn)
        missing = conn.scalar(
            text(
                f"SELECT count(*) FROM {units} u WHERE NOT EXISTS (SELECT 1 FROM {_t(conn, 'index_job')} j"
                " WHERE j.generation_id = :g AND j.version_id = u.version_id AND j.state = 'done')"
            ),
            {"g": found["generation_id"]},
        )
        picked = conn.execute(
            text(
                f"SELECT version_id, start_byte FROM {units} ORDER BY md5(version_id || start_byte::text) LIMIT :n"
            ),
            {"n": sample},
        ).all()
        inputs = {
            (i["version_id"], i["start_byte"]): i["input"]
            for version in {r.version_id for r in picked}
            for i in _inputs(conn, version, profile)
        }
    embedder = embedder or embedder_for(profile)
    stored, texts = [], []
    for version_id, start in picked:
        fields = searcher.get(f"{version_id}:{start}", vectors) or {}
        cells = fields.get(vectors) or {}
        cells = cells.get("blocks", cells) if isinstance(cells, dict) else {}  # short or long tensor form
        first = cells.get("0") if isinstance(cells, dict) else None
        if first is None or (version_id, start) not in inputs:
            missing += 1
            continue
        stored.append([float(x) for x in first])
        texts.append(inputs[(version_id, start)])
    worst = 1.0
    if stored:
        again = embedder.embed(texts, query=False)
        worst = min(_cosine(x, y) for x, y in zip(stored, again, strict=True))
    hits = total = 0
    for query in stored[:10]:
        approximate = set(searcher.nearest(query, vectors, 20, approximate=True))
        exact = set(searcher.nearest(query, vectors, 20, approximate=False))
        hits, total = hits + len(approximate & exact), total + len(exact)
    recall = round(hits / total, 4) if total else None
    passed = missing == 0 and worst >= RECOMPUTE_FLOOR and (recall is None or recall >= ANN_FLOOR)
    return {
        "validated_at": datetime.now(UTC).isoformat(), "missing_units": missing,
        "recompute_min_cosine": round(worst, 6), "ann_recall_at_20": recall, "passed": passed,
    }  # fmt: skip


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine of two vectors: stored ones are rounded (halfvec, bfloat16), so they are not quite unit length."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = (sum(x * x for x in a) * sum(y * y for y in b)) ** 0.5
    return dot / norm if norm else 0.0


def _record(engine: Engine, generation_id: str, results: dict) -> dict:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {_t(conn, 'index_generation')} SET manifest = manifest || CAST(:m AS jsonb),"
                " state = CASE WHEN :ok AND state = 'building' THEN 'validating' ELSE state END WHERE generation_id = :g"
            ),
            {"m": json.dumps({"validation": results}), "ok": results["passed"], "g": generation_id},
        )
    return results


def _ann_recall(engine: Engine, found, queries: list[list[float]]) -> float:
    from retrieval.vectors import _literal

    hits = total = 0
    for query in queries:
        with engine.begin() as conn:
            table = store_for(conn, found).table
            sql = text(
                f"SELECT version_id, start_byte FROM {table} ORDER BY embedding <=> CAST(:q AS halfvec({found['profile']['dims']}))"
                " LIMIT 20"
            )
            conn.execute(text("SET LOCAL hnsw.ef_search = 200"))
            approximate = {tuple(r) for r in conn.execute(sql, {"q": _literal(query)}).all()}
            conn.execute(text("SET LOCAL enable_indexscan = off"))
            exact = {tuple(r) for r in conn.execute(sql, {"q": _literal(query)}).all()}
        hits += len(approximate & exact)
        total += len(exact)
    return round(hits / total, 4) if total else 1.0


def activate(engine: Engine, generation_id: str, *, allow_test: bool = False) -> None:
    """Make a validated generation its profile's active one, retiring the previous, in one transaction."""
    with engine.begin() as conn:
        found = generation(conn, generation_id)
        if found["profile"].get("test") and not allow_test:
            raise PermissionError("A test profile is never activated outside tests")
        if not (found["manifest"].get("validation") or {}).get("passed"):
            raise PermissionError("Validate the generation first: it has not passed")
        if found["state"] not in ("validating", "retired"):
            raise PermissionError(f"A {found['state']} generation cannot be activated")
        table = _t(conn, "index_generation")
        conn.execute(
            text(f"UPDATE {table} SET state = 'retired', retired_at = statement_timestamp()"
                 " WHERE profile_id = :p AND state = 'active'"),
            {"p": found["profile_id"]},
        )  # fmt: skip
        conn.execute(
            text(
                f"UPDATE {table} SET state = 'active', activated_at = statement_timestamp() WHERE generation_id = :g"
            ),
            {"g": generation_id},
        )


def rollback(
    engine: Engine,
    profile_id: str,
    generation_id: str,
    *,
    embedder=None,
    allow_test: bool = False,
    searcher=None,
) -> dict:
    """Make a retired generation active again only if it still covers every unit now indexed (EMB-06);
    otherwise refuse, and the lexical arms carry the service."""
    with engine.begin() as conn:
        found = generation(conn, generation_id)
    if found["profile_id"] != profile_id or found["state"] != "retired":
        raise PermissionError("Only a retired generation of the same profile can be restored")
    results = validate(engine, generation_id, embedder=embedder, searcher=searcher)
    if not results["passed"]:
        raise PermissionError(f"The generation no longer covers what is public: {results}")
    activate(engine, generation_id, allow_test=allow_test)
    return results


def purge(engine: Engine) -> int:
    """Remove vectors of texts no longer indexed from every generation still kept."""
    from api.text_index import _tables

    removed = 0
    with engine.begin() as conn:
        units, _ = _tables(conn)
        for row in conn.execute(
            text(
                f"SELECT * FROM {_t(conn, 'index_generation')} WHERE state <> 'retired' AND backend <> 'vespa'"
            )
        ).mappings():
            removed += store_for(conn, dict(row)).purge(units)
    return removed


def status(engine: Engine) -> list[dict]:
    with engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    f"SELECT g.generation_id, g.profile_id, g.backend, g.state, g.manifest,"
                    f" count(j.*) FILTER (WHERE j.state = 'done') AS done,"
                    f" count(j.*) FILTER (WHERE j.state IN ('queued', 'running')) AS waiting,"
                    f" count(j.*) FILTER (WHERE j.state = 'failed') AS failed"
                    f" FROM {_t(conn, 'index_generation')} g LEFT JOIN {_t(conn, 'index_job')} j USING (generation_id)"
                    " GROUP BY g.generation_id ORDER BY g.created_at"
                )
            ).mappings()
        ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("profile")
    prep.add_argument("--backend", choices=("vespa", "pgvector", "exact"))
    run = commands.add_parser("work")
    run.add_argument("--batches", type=int, default=10)
    for name in ("validate", "activate"):
        commands.add_parser(name).add_argument("generation")
    back = commands.add_parser("rollback")
    back.add_argument("profile_id")
    back.add_argument("generation")
    commands.add_parser("purge")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    from api.store import engine_for

    engine = engine_for()
    if args.command == "prepare":
        with open(args.profile) as handle:
            profile = Profile.from_dict(json.load(handle))
        print(prepare(engine, profile, backend=args.backend))
    elif args.command == "work":
        print(json.dumps(work(engine, batches=args.batches)))
    elif args.command == "validate":
        print(json.dumps(validate(engine, args.generation), indent=2))
    elif args.command == "activate":
        activate(engine, args.generation)
        print(f"{args.generation} is active")
    elif args.command == "rollback":
        print(json.dumps(rollback(engine, args.profile_id, args.generation), indent=2))
    elif args.command == "purge":
        print(f"{purge(engine)} vectors removed")
    else:
        print(json.dumps(status(engine), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
