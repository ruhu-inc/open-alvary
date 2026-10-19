# Semantic index profiles

A profile pins everything that shapes a vector space: the model and its exact revision, dimensions,
metric, stored precision, the instructions put before queries and documents, and how units are cut
into windows (`retrieval/vectors.py`). Changing any of it is a new profile and a new generation.

No production profile is selected. The model is chosen by the embedding experiments (EMB-01–07): open
weights served by Open Alvary itself, never a proprietary API. Where inference runs, on what hardware
and with what monthly cost cap is the owner's decision D29.

`candidate-qwen3-embedding-0.6b.json` is a template for one EMB candidate. Before use, set `revision` to
the exact model revision being served, and serve it from an OpenAI-compatible `/embeddings` endpoint
(set `OPEN_ALVARY_EMBEDDER_URL`). Then:

    python -m retrieval.indexer prepare retrieval/profiles/<profile>.json   (administrator connection)
    python -m retrieval.indexer work                                        (repeat until nothing waits)
    python -m retrieval.indexer validate <generation>
    python -m retrieval.indexer activate <generation>

and run the service with `OPEN_ALVARY_VECTOR_PROFILE=<profile id>`. Activation for a scope also needs its
EVAL/EMB gate to pass (RSV-12): run the scope's benchmark with and without the profile and compare.
