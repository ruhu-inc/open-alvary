"""Law check's page files for a given address.

The agent files under ``web/public/agents`` (the manifests, the two networks, the discovery files)
name the address the page is served from, because the challenge runtime wants whole addresses: the
MCP server the agents call, the peers the page may contact. As committed they name the live site,
``https://open.alvary.ai``. A copy served anywhere else must name that place, or its agents would call
the live site and not the server beside them.

    .venv/bin/python -m scripts.law_check_site --origin http://127.0.0.1:8771 --out /tmp/agents

writes such a copy. ``scripts.law_check_local`` serves one without writing it.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "web" / "public" / "agents"
PUBLISHED_ORIGIN = "https://open.alvary.ai"
# The files that carry the address. Scripts and the vendored runtime never do: the runtime is served
# byte for byte, and the page's own scripts use addresses relative to the page.
ADDRESSED = {".json"}


def addressed_files() -> list[Path]:
    """Every agent file that names the published address, runtime excluded."""
    runtime = AGENTS_DIR / "runtime"
    return sorted(
        path
        for path in AGENTS_DIR.rglob("*")
        if path.is_file()
        and path.suffix in ADDRESSED
        and runtime not in path.parents
        and PUBLISHED_ORIGIN in path.read_text(encoding="utf-8")
    )


def file_for_origin(path: Path, origin: str) -> bytes:
    """The file's bytes as served from ``origin``: unchanged unless it is one that carries the address."""
    data = path.read_bytes()
    if path.suffix not in ADDRESSED or (AGENTS_DIR / "runtime") in path.parents:
        return data
    return data.decode("utf-8").replace(PUBLISHED_ORIGIN, origin.rstrip("/")).encode("utf-8")


def write_site(origin: str, out: Path) -> list[Path]:
    """Copy the page's files to ``out`` for ``origin``; returns the files whose address changed."""
    if out.exists():
        shutil.rmtree(out)
    changed = []
    for path in sorted(AGENTS_DIR.rglob("*")):
        if not path.is_file():
            continue
        target = out / path.relative_to(AGENTS_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = file_for_origin(path, origin)
        target.write_bytes(data)
        if data != path.read_bytes():
            changed.append(target)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Write Law check's page files for an address.")
    parser.add_argument(
        "--origin", required=True, help="Where the copy will be served, e.g. http://127.0.0.1:8771"
    )
    parser.add_argument("--out", type=Path, required=True, help="Directory to write (replaced if it exists)")
    args = parser.parse_args()
    changed = write_site(args.origin, args.out)
    print(f"Wrote {args.out} for {args.origin}; {len(changed)} files name that address.")


if __name__ == "__main__":
    main()
