"""A local inference server for open-weight embedding and reranker models (EMB-01, EMB-05: the
runnable local inference path). It answers the two APIs the retrieval service already speaks:

  POST /embeddings  {"model", "input": [...], "dimensions"?} → {"data": [{"index", "embedding"}]}  (OpenAI)
  POST /rerank      {"query", "texts": [...], "raw_scores": false} → [{"index", "score"}]       (TEI)

    python experiments/embeddings/serve.py --model Qwen/Qwen3-Embedding-0.6B --port 28101
    python experiments/embeddings/serve.py --model BAAI/bge-reranker-v2-m3 --kind rerank --port 28201

It needs torch and sentence-transformers (not the service's dependencies: run it from its own
environment). On Apple silicon it uses the GPU (mps). Weights come from the Hugging Face hub at the
revision given (pin it for any run that counts: EMB-05). Nothing is logged but request sizes.
"""

import argparse
import json
import math
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock


def load(args):
    import torch
    from sentence_transformers import CrossEncoder, SentenceTransformer

    device = args.device or ("mps" if torch.backends.mps.is_available() else "cpu")
    dtype = {"float16": torch.float16, "bfloat16": torch.bfloat16, "float32": torch.float32}[args.dtype]
    if args.kind == "rerank":
        model = CrossEncoder(args.model, device=device, revision=args.revision, max_length=args.max_length,
                             model_kwargs={"torch_dtype": dtype})  # fmt: skip
    else:
        model = SentenceTransformer(args.model, device=device, revision=args.revision,
                                    model_kwargs={"torch_dtype": dtype}, trust_remote_code=False)  # fmt: skip
        model.max_seq_length = args.max_length
    return model, device


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", default=None)
    parser.add_argument("--kind", choices=("embed", "rerank"), default="embed")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--device", default=None)
    parser.add_argument("--dtype", choices=("float16", "bfloat16", "float32"), default="float16")
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--batch", type=int, default=16)
    args = parser.parse_args(argv)
    model, device = load(args)
    lock = Lock()  # one batch at a time on the GPU

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # never log texts

        def answer(self, status: int, body) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.answer(200, {"model": args.model, "kind": args.kind, "device": device})

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                path = self.path.rstrip("/")
                if path.endswith("/embeddings") and args.kind == "embed":
                    texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
                    with lock:
                        vectors = model.encode(texts, batch_size=args.batch, normalize_embeddings=True)
                    if body.get("dimensions"):  # Matryoshka: keep the leading dimensions, renormalised
                        import numpy as np

                        vectors = vectors[:, : int(body["dimensions"])]
                        vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
                    data = [{"index": i, "embedding": [float(x) for x in v]} for i, v in enumerate(vectors)]
                    return self.answer(200, {"object": "list", "model": args.model, "data": data})
                if path.endswith("/rerank") and args.kind == "rerank":
                    pairs = [(body["query"], t) for t in body["texts"]]
                    with lock:
                        scores = model.predict(pairs, batch_size=args.batch)
                    raw = bool(body.get("raw_scores"))
                    found = [
                        {
                            "index": i,
                            "score": float(s) if raw or 0 <= s <= 1 else 1 / (1 + math.exp(-float(s))),
                        }
                        for i, s in enumerate(scores)
                    ]
                    return self.answer(200, sorted(found, key=lambda f: -f["score"]))
                return self.answer(404, {"error": "unknown route for this model"})
            except Exception as error:  # the caller degrades; nothing of the texts is echoed
                return self.answer(500, {"error": type(error).__name__})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"{args.model} ({args.kind}) on {device}, port {args.port}", file=sys.stderr, flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
