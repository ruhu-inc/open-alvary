"""The retrieval job (RSV-04, RSV-10), run every minute by the platform's scheduler:

1. the section index is built when it is not, or its analyzer changed (api.text_index.ensure);
2. when the engine is configured, its application is deployed if the generations it holds changed,
   then it catches up with the change log (retrieval.projector);
3. embedding jobs run for the building and active generations (retrieval.indexer.work).

A step that fails is reported and the next still runs; the job fails when any did, so the platform
shows it. Runs may overlap: embedding jobs are leased and the engine's writes are by unit.

    python -m retrieval.worker run
"""

import json
import sys

BATCHES = 5  # of ten texts each: a run ends well inside a minute on the GPU


def run(db=None, search=None) -> dict:
    from api import text_index
    from api.store import engine_for
    from retrieval import indexer, projector
    from retrieval.engine import Engine as SearchEngine
    from retrieval.engine import config_from_env

    db = db or engine_for()
    if search is None and (config := config_from_env()) is not None:
        search = SearchEngine(*config)
    report: dict = {}

    def step(name, action):
        try:
            report[name] = action()
        except Exception as error:  # reported, never the texts or queries
            report[name] = {"error": type(error).__name__}

    def build():
        with db.begin() as conn:
            return {"built": text_index.ensure(conn)}

    def follow():
        with db.begin() as conn:
            wanted = projector.application_hash(projector.application(conn))
            deployed = projector.state(conn).get("application_hash")
        if deployed != wanted:
            projector.deploy(db, search)
        return projector.sync(db, search)

    step("section_index", build)
    if search is not None:
        step("engine", follow)
    step("embeddings", lambda: indexer.work(db, batches=BATCHES, searcher=search))
    return report


def main(argv=None) -> int:
    command = (argv or sys.argv[1:] or ["run"])[0]
    if command != "run":
        print("python -m retrieval.worker run", file=sys.stderr)
        return 2
    report = run()
    print(json.dumps(report, default=str))
    return 1 if any(isinstance(v, dict) and "error" in v for v in report.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
