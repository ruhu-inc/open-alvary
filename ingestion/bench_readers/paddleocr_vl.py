"""PaddleOCR-VL-1.6 reading of one page image; prints Markdown (tables as HTML) to stdout.

Runs in its own environment (private-staging/bench/paddle-venv) against a local
`mlx_vlm.server --port 8111` on Apple Silicon. The page never leaves the machine.
"""

import contextlib
import sys

from paddleocr import PaddleOCRVL

with contextlib.redirect_stdout(sys.stderr):  # keep library logging off the result stream
    pipeline = PaddleOCRVL(
        pipeline_version="v1.6",
        vl_rec_backend="mlx-vlm-server",
        vl_rec_server_url="http://127.0.0.1:8111/",
        vl_rec_api_model_name="PaddlePaddle/PaddleOCR-VL-1.6",
    )
    pages = [res.markdown["markdown_texts"] for res in pipeline.predict(sys.argv[1])]
print("\n\n".join(pages))
