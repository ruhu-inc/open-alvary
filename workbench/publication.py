"""The text and structure a document's approval binds and publishing publishes (TXP-01).

Built from the document's final pages each time it is asked for (15 ms for a 152-page Act), so
what the reviewer sees before approving, what the approval records and what publishing writes
come from the same code. Errors block approval; warnings are shown beside the pages they concern.
A document of a jurisdiction and type with no structure rules has an error for that alone.
"""

import re
from collections import Counter
from dataclasses import dataclass

from workbench import content, serialize, structure

# Because nobody proofreads, a word doubled while editing would publish unnoticed (TXP-06).
DOUBLED = re.compile(r"\b(\w{3,})\s+\1\b", re.I)
SHOWN = tuple("part chapter section article definition schedule table notice opinion order".split())


@dataclass
class Text:
    serialized: serialize.Serialized
    structure: structure.Structure
    issues: list[serialize.Issue]

    @property
    def errors(self) -> list[serialize.Issue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> list[serialize.Issue]:
        return [issue for issue in self.issues if issue.level == "warning"]

    @property
    def text_sha256(self) -> str:
        return self.serialized.content_hash

    @property
    def structure_sha256(self) -> str:
        return self.structure.structure_hash

    @property
    def profile_id(self) -> str:
        return self.serialized.profile_id

    @property
    def rules_id(self) -> str | None:
        return self.structure.rules_id


def build(document: dict, final: list[dict], reading: dict | None = None) -> Text:
    pages = [{"page": page["page"], "units": page["units"]} for page in final]
    serialized = serialize.serialize(pages)
    derived = structure.for_document(document, serialized, pages)
    issues = serialized.issues + derived.issues
    if reading is not None:
        issues += doubled_words(pages, reading)
    return Text(serialized, derived, issues)


def doubled_words(pages: list[dict], reading: dict) -> list[serialize.Issue]:
    """Words doubled in the reviewed text ("gratuity gratuity") that the machine reading did not have."""
    machine = {
        page["page"]: " ".join(
            serialize.unit_words(u) for u in content.units_from(page) if u["kind"] != "table"
        )
        for page in reading["pages"]
    }
    found = []
    for page in pages:
        printed = machine.get(page["page"], "").casefold()
        for index, unit in enumerate(page["units"]):
            if unit.get("kind") == "table":
                continue
            for match in DOUBLED.finditer(serialize.unit_words(unit)):
                if match.group(0).casefold() not in printed:
                    found.append(
                        serialize.Issue(
                            "warning",
                            "doubled_word",
                            page["page"],
                            index,
                            f"“{match.group(0)}”: a word doubled in editing?",
                        )
                    )
    return found


def summary(text: Text) -> dict:
    """What the workbench shows before approval."""

    def listed(issues):
        return [
            {"level": i.level, "code": i.code, "page": i.page, "unit": i.unit, "message": i.message}
            for i in issues
        ]

    kinds = Counter(node.kind for node in text.structure.nodes)
    return {
        "profile": text.profile_id,
        "rules": text.rules_id,
        "text_sha256": text.text_sha256,
        "structure_sha256": text.structure_sha256,
        "bytes": len(text.serialized.text.encode()),
        "counts": {kind: kinds[kind] for kind in SHOWN if kinds[kind]},
        "errors": listed(text.errors),
        "warnings": listed(text.warnings),
    }


def canonical(text: Text) -> dict:
    """The exact bytes and nodes publishing would write, for inspection."""
    return {
        "profile": text.profile_id,
        "rules": text.rules_id,
        "text_sha256": text.text_sha256,
        "structure_sha256": text.structure_sha256,
        "text": text.serialized.text,
        "nodes": text.structure.canonical(),
    }
