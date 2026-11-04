# Open Alvary

Open, rights-aware legal-data infrastructure for African jurisdictions: laws and judgments as
structured, versioned public data, for people and machines.

- Website and documentation: https://open.alvary.ai
- Catalogue: Nigerian records are public now. Other jurisdictions are added as their sources are verified.
- Text is published only after a rights review and a person's approval. Nothing in this repository is
  legal text from a source, and the corpus is not kept here.

## What is here

| Folder | What it holds |
|---|---|
| `api/`, `accounts/` | the public service: website data, keyed REST, MCP, the developer console |
| `web/` | the website and its documentation |
| `workbench/` | the tool people use to review and approve documents |
| `ingestion/` | the readers that turn official files into review documents |
| `retrieval/`, `evaluations/`, `benchmarks/` | search and how it is measured |
| `rights/`, `migrations/`, `schema/`, `operations/` | the rights gate, the database and publishing |
| `deployment/`, `scripts/` | how it is built and run |
| `tests/` | the tests; the legal text in them is invented |

## Working on it

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock -r deployment/ingestion-jobs.requirements.lock && pip install --no-deps -e .
pytest -q --fast
```

The PostgreSQL tests need a database with `pgvector`: set `TEST_DATABASE_URL` and run `pytest -q -m postgres`.

## Licences and contact

Software: MIT ([LICENSE](LICENSE)). Catalogue metadata: [DATA-LICENSE.md](DATA-LICENSE.md). The licences do not
cover linked legal texts. Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md).
Maintained by Ruhu, Inc.: hello@ruhu.ai.
