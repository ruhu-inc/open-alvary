"""The check operation (RSV-08): before an answer that cites Open Alvary leaves a client, it confirms
each cited part is still public with the same bytes, says when a newer text of the same expression
exists (with the same locator's anchor there), and gives the part's currentness at a date. A part that
is not public now and one that never was are the same answer: unavailable (DAT-06)."""

import hashlib
from datetime import UTC, date, datetime

from api.catalogue import LegacyCatalogue, SourceUnavailable, expression_id
from retrieval.legal_time import record_currentness
from retrieval.pack import dataset_revision
from schema.public import CheckItem, CheckPage, CheckResult, NodeViewRecord


class Checker:
    def __init__(self, service: LegacyCatalogue, *, today: date | None = None):
        self.service = service
        self.today = today or datetime.now(UTC).date()
        self._nodes: dict[str, list[NodeViewRecord]] = {}
        self.jurisdictions: set[str] = set()  # of the parts found, for the plan's admission
        self.served: set[tuple[str, str]] = set()  # (version, content hash) of every text read

    def _structure(self, version) -> list[NodeViewRecord]:
        if version.id not in self._nodes:
            self._nodes[version.id] = self.service._ordered_nodes(version)
        return self._nodes[version.id]

    def _locate(self, item: CheckItem):
        """The item's (version, source, node), all public now; None otherwise."""
        try:
            if item.anchor_id is not None:
                row = self.service.repository.node(item.anchor_id)
                if row is None:
                    return None
                node = NodeViewRecord.model_validate(row)
                version, source, _ = self.service.version(node.version_id)
                return version, source, node
            version, source, _ = self.service.version(item.version_id)
        except SourceUnavailable:
            return None
        node = next((n for n in self._structure(version) if n.locator == item.locator), None)
        return (version, source, node) if node is not None else None

    def _newest(self, version, source) -> object:
        """The latest public text of the same expression that descends from ``version`` (by
        prior_version_id), or ``version`` itself."""
        expression = expression_id(version)
        _, rights = self.service._require(source.id)
        public = [v for v in self.service._approved(source, rights) if expression_id(v) == expression]
        children: dict[str, list] = {}
        for candidate in public:
            if candidate.prior_version_id:
                children.setdefault(candidate.prior_version_id, []).append(candidate)
        at, seen = version, {version.id}
        while children.get(at.id):
            following = sorted(children[at.id], key=lambda v: v.id)
            nxt = next((v for v in following if v.id not in seen), None)
            if nxt is None:
                break
            seen.add(nxt.id)
            at = nxt
        return at

    def check(self, items: tuple[CheckItem, ...]) -> CheckPage:
        results = []
        for index, item in enumerate(items):
            located = self._locate(item)
            if located is None:
                results.append(CheckResult(index=index, result="unavailable", anchor_id=item.anchor_id,
                                           version_id=item.version_id, locator=item.locator))  # fmt: skip
                continue
            version, source, node = located
            self.jurisdictions.add(source.jurisdiction_id)
            self.served.add((version.id, version.content_hash))
            raw = version.canonical_text.encode()
            span = raw[node.start_byte : node.end_byte]
            base = {"index": index, "anchor_id": node.id, "version_id": version.id, "locator": node.locator}
            if item.span_hash is not None and hashlib.sha256(span).hexdigest() != item.span_hash:
                results.append(CheckResult(result="span_mismatch", **base))
                continue
            currentness = record_currentness(source.legal_status, version, item.as_of, self.today)
            newest = self._newest(version, source)
            if newest.id == version.id:
                results.append(CheckResult(result="unchanged", currentness=currentness, **base))
                continue
            there = next((n for n in self._structure(newest) if n.locator == node.locator), None)
            newer = newest.canonical_text.encode()
            results.append(
                CheckResult(
                    result="newer_version",
                    newer_version_id=newest.id,
                    newer_anchor_id=there.id if there else None,
                    same_words=there is not None and newer[there.start_byte : there.end_byte] == span,
                    currentness=record_currentness(source.legal_status, newest, item.as_of, self.today),
                    **base,
                )
            )
        return CheckPage(dataset=dataset_revision(self.service.repository.connection), results=tuple(results))
