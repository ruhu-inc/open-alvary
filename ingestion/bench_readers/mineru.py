"""MinerU (advanced tier, MinerU2.5-Pro VLM) reading of one page image; prints Markdown.

Runs in its own environment (private-staging/bench/mineru-venv); inference is local.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

kit = Path(sys.executable).with_name("mineru-kit")
with tempfile.TemporaryDirectory() as scratch:
    out = Path(scratch) / "page.md"
    subprocess.run(
        [str(kit), "parse", sys.argv[1], "-o", str(out), "--tier", "advanced"],
        check=True,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
    print(out.read_text())
