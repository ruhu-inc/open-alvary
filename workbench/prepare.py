"""Prepare page images for the review workbench (run on the operator machine, which has Docker).

    python -m workbench.prepare                 # every reconciled Act
    python -m workbench.prepare --act <id>

Images are rendered once at 150 dpi in the sealed tools container into
private-staging/<collection>/review-images/, which a deployed workbench mounts read-only.
"""

import argparse
import json

from workbench.content import Library


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--act")
    args = parser.parse_args(argv)
    library = Library()
    for act in library.documents():
        if args.act and act["id"] != args.act:
            continue
        pages = [
            page["page"]
            for page in json.loads((library.staging / f"{act['id']}.reading.json").read_text())["pages"]
        ]
        for page in pages:
            library.image(act["id"], page)
        print(f"{act['id']}: {len(pages)} page images ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
