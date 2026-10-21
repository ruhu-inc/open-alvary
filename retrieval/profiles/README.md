# Semantic index profiles

A profile pins everything that shapes a vector space: the model and its exact revision, dimensions,
metric, stored precision, the instructions put before queries and documents, and how units are cut
into windows (`retrieval/vectors.py`). Changing any of it is a new profile and a new generation.

`qwen3-embedding-0.6b-1.json` is the production profile: the embedder D29 selected, pinned to the
revision the screen served (experiments/embeddings/candidates.json) and the GPU machines serve (the
`embedder` variable of deployment/gcp-terraform and deployment/azure), with the screen's query
instruction and 2,000-character windows. The tests keep the three revisions equal. The model runs on
open weights served by Open Alvary itself, never a proprietary API.

`candidate-qwen3-embedding-0.6b.json` is the template it was made from, for further EMB candidates.
Before use, set `revision` to the exact model revision being served, and serve it from an
OpenAI-compatible `/embeddings` endpoint (set `OPEN_ALVARY_EMBEDDER_URL`). Then:

    python -m retrieval.indexer prepare retrieval/profiles/<profile>.json   (administrator connection)
    python -m retrieval.indexer work                                        (repeat until nothing waits)
    python -m retrieval.indexer validate <generation>
    python -m retrieval.indexer activate <generation>

and run the service with `OPEN_ALVARY_VECTOR_PROFILE=<profile id>`. Activation for a scope also needs its
EVAL/EMB gate to pass (RSV-12): run the scope's benchmark with and without the profile and compare.
