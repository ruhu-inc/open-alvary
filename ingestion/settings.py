"""Operator settings for ingestion tools, read from the environment or the git-ignored .env file.

Values already in the environment win. The file is parsed, never executed, and its values are
never printed, so cloud keys can live there instead of being exported in every shell.
"""

import os
import re
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$")


def load(path: Path | None = None) -> None:
    path = path or ENV_FILE
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        match = LINE.match(raw)
        if not match or raw.lstrip().startswith("#"):
            continue
        name, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value:
            os.environ.setdefault(name, value)


def require(*names: str) -> dict[str, str]:
    load()
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"Set {', '.join(missing)} in the environment or in {ENV_FILE.name}")
    return {name: os.environ[name] for name in names}
