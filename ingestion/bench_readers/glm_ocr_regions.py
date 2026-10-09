"""GLM-OCR reading of one page image, the way its own pipeline works; prints Markdown.

Stage 1 finds regions with PP-DocLayoutV3 (via PaddleOCR, in private-staging/bench/paddle-venv).
Stage 2 sends each region to the local Ollama `glm-ocr` model with the prompt for its kind.
Figures, seals and page furniture are skipped, using the SDK's generation settings. Nothing leaves the machine.
"""

import base64
import contextlib
import io
import sys

import httpx
from paddleocr import LayoutDetection
from PIL import Image

# As the official SDK, except margin notes ("aside_text") are kept: they are part of an Act.
SKIP = {"image", "figure", "chart", "seal", "header", "footer", "header_image", "footer_image", "number"}
PROMPTS = {
    "table": "Table Recognition:",
    "formula": "Formula Recognition:",
    "display_formula": "Formula Recognition:",
}


def recognise(region: Image.Image, prompt: str) -> str:
    buffer = io.BytesIO()
    region.save(buffer, format="PNG")
    response = httpx.post(
        "http://127.0.0.1:11434/api/generate",
        json={
            "model": "glm-ocr",
            "prompt": prompt,
            "images": [base64.b64encode(buffer.getvalue()).decode()],
            "stream": False,
            "options": {  # the official SDK's generation settings (glmocr/config.yaml)
                "temperature": 0,
                "top_p": 0.00001,
                "top_k": 1,
                "repeat_penalty": 1.1,
                "num_predict": 8192,
                "num_ctx": 16384,
            },
        },
        timeout=900,
    )
    response.raise_for_status()
    return response.json()["response"].strip()


page = Image.open(sys.argv[1]).convert("RGB")
with contextlib.redirect_stdout(sys.stderr):
    layout = LayoutDetection(model_name="PP-DocLayoutV3")
    boxes = [
        box for result in layout.predict(sys.argv[1], batch_size=1) for box in result.json["res"]["boxes"]
    ]
blocks = []
for box in boxes:  # PP-DocLayoutV3 returns boxes in reading order
    label = box["label"].lower()
    if label in SKIP:
        continue
    x1, y1, x2, y2 = (int(v) for v in box["coordinate"])
    text = recognise(
        page.crop((max(x1 - 4, 0), max(y1 - 4, 0), x2 + 4, y2 + 4)), PROMPTS.get(label, "Text Recognition:")
    )
    blocks.append(text)
    print(f"{label}: {text[:60]!r}", file=sys.stderr)
print("\n\n".join(blocks))
