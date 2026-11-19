"""Whether a reviewed document is approved, decided once for the workbench and for publishing
(operations.publish): its pages, its details (shared by a work's language versions) and whether
the latest approval still covers both exactly."""

import os

from pydantic import ValidationError

from ingestion import cited_names
from workbench import content, details, publication, record_view, routing

# Families eligible for online legal-status research and ALVARY_REVIEW_STATUS_GATE (design §8).
STATUS_RESEARCH_FAMILIES = frozenset(
    {
        "constitution",
        "legislation",
        "subsidiary_legislation",
        "executive_instrument",
        "treaty",
        "regional_instrument",
    }
)
STATUS_RESEARCH_PENDING_MESSAGE = "Legal status: research pending."


def status_gate_enabled() -> bool:
    """Whether ALVARY_REVIEW_STATUS_GATE is switched on (off by default)."""
    return os.environ.get("ALVARY_REVIEW_STATUS_GATE", "off").strip().lower() == "on"


def status_research_pending(record: dict | None, act: dict | None = None) -> bool:
    """Whether a work's confirmed Details are still waiting for online legal-status research.

    Pending is a state the workbench can see (design §8): the latest detail revision is confirmed,
    its identity.family is in STATUS_RESEARCH_FAMILIES, status.legal is "unknown" (or unset), and
    status.note is empty. Writing what each source answered in status.note ends the pending state.
    """
    if not record or record.get("status") != "confirmed":
        return False
    raw = record.get("details") or {}
    values = raw if record_view.is_view(raw) else record_view.from_legacy(raw, record.get("absent") or {})[0]
    family = values.get("identity.family") or record_view.family_for((act or {}).get("type"))
    if family not in STATUS_RESEARCH_FAMILIES:
        return False
    legal = values.get("status.legal") or "unknown"
    note = (values.get("status.note") or "").strip()
    return legal == "unknown" and note == ""


def to_do(readiness: dict) -> dict[int, str]:
    """What each page still needs, as a reviewer is shown it: ``review`` (the whole page against the
    image) or ``flags`` (its flags resolved); a page needing nothing is left out. Audit pages are
    reviewed blind (CPR-01): a sampled page is a ``review`` page exactly like a ``full`` one."""
    out = dict.fromkeys(readiness["unresolved_flag_pages"], "flags")
    for flag in readiness["open_high_flags"]:
        out.setdefault(flag["page"], "flags")
    out.update(dict.fromkeys([*readiness["unchecked_pages"], *readiness["stale_pages"]], "review"))
    return out


def blind(readiness: dict) -> dict:
    """Readiness as anyone reviewing pages sees it: what is left to do, never which pages are the audit
    sample or why each page was routed (the plan stays with the approval)."""
    return {key: value for key, value in readiness.items() if key != "routing"}


class Review:
    def __init__(self, store, library, profiles):
        self.store, self.library, self.profiles = store, library, profiles

    def details_key(self, document: dict) -> str:
        """Whose details these are: the work, shared by its language versions (Rwanda's three
        texts of one law), or the document itself."""
        return document.get("work") or document["id"]

    def version_title(self, act: dict, language: str | None) -> str:
        """The title a language version of this work was prepared with ("" where it has none here)."""
        # A document that names no language and no other versions (a Nigerian Act) is its one version.
        if language and (language == act.get("language") or not (act.get("language") or act.get("versions"))):
            return self.prepared_title(act)
        other = (act.get("versions") or {}).get(language or "")
        return self.library.listed(other).get("title", "") if other and self.library.has(other) else ""

    def prepared_title(self, act: dict) -> str:
        """The title a document was prepared with; a Nigerian one as cases and laws are cited, where its
        collection holds it in the capitals it is printed in (prepared before 2026-11-16)."""
        title = act.get("title") or ""
        if not cited_names.is_cited_here(act):
            return title
        if "suggested" not in act:  # as its manifest lists it
            return cited_names.listed_title(title)
        return cited_names.title_of(title, act["suggested"])[0]

    def shown_title(self, act: dict) -> str:
        """A document's title as the workbench heads and lists it: its version's title given in the
        details, else the title it was prepared with."""
        record = self.store.latest_details(self.details_key(act))
        rows = [
            r for r in ((record or {}).get("details") or {}).get("expressions") or [] if isinstance(r, dict)
        ]
        language = act.get("language") or (rows[0].get("language") if len(rows) == 1 else None)
        given = next(
            ((row.get("title") or "").strip() for row in rows if row.get("language") == language), ""
        )
        return given or self.prepared_title(act) or act["id"]

    def described(self, act: dict, record: dict | None, reading: dict) -> dict:
        """The document's details in the record view: values, what is accounted for, its fields."""
        place, profile = record_view.place_for(act, self.library.manifest_for(act["id"]), self.profiles)
        dropped: dict = {}
        if record:
            values, absent = record["details"], record["absent"]
            if not record_view.is_view(values):  # saved under the Nigerian DETAILS-1: read it in this view
                values, absent = record_view.from_legacy(values, absent)
        elif place.code == "ng" and not record_view.reader_suggested(act):
            # A first draft from the DETAILS-1 suggestions (Nigerian Acts), without the legal facts; a
            # document whose collection's reader suggested its details (a judgment) drafts from those.
            values, absent = record_view.first_draft(
                *record_view.from_legacy(
                    *details.initial(details.suggest(reading, act, self.library.manifest_for(act["id"])))
                )
            )
            if isinstance(values.get("identity.title"), str):
                values = {**values, "identity.title": cited_names.cited(values["identity.title"])[0]}
            allowed = {
                spec.key
                for spec in record_view.fields(values["identity.family"], values["identity.type"], profile)
            }
            dropped = {
                key: {
                    "value": value,
                    "page": None,
                    "evidence": "Read from the document; no field for it in this form.",
                }
                for key, value in values.items()
                if key not in allowed
            }
            values = {key: value for key, value in values.items() if key in allowed}
            absent = {key: reason for key, reason in absent.items() if key in allowed}
        else:  # nothing marked absent by inference: suggested beside its field (record_view.suggestions)
            manifest = self.library.manifest_for(act["id"])
            values = record_view.base_values(act, manifest, profile)
            dropped = record_view.unshown_suggestions(act, manifest, profile)
            shown = record_view.fields(values["identity.family"], values["identity.type"], profile)
            absent = record_view.first_absent(values, {spec.key for spec in shown})
        if isinstance(values.get("expressions"), list):
            # Each version's title, where none was given: the title its document was prepared with.
            values = {
                **values,
                "expressions": [
                    {**row, **({"title": title} if title else {})}
                    for row in values["expressions"]
                    for title in [row.get("title") or self.version_title(act, row.get("language"))]
                ],
            }
        if act.get("language") and isinstance(values.get("expressions"), list):
            # One record for the work; each language version is "this text" on its own page.
            values = {
                **values,
                "expressions": [
                    {**row, "this_text": "yes" if row.get("language") == act["language"] else "no"}
                    for row in values["expressions"]
                ],
            }
        family, kind = values["identity.family"], values["identity.type"]
        specs = record_view.fields(family, kind, profile)
        values, absent, extra_left_out = record_view.for_form(values, absent, specs)
        left_out = {
            **{k: (v["value"] if isinstance(v, dict) else v) for k, v in dropped.items()},
            **extra_left_out,
        }
        notices = [] if record else list(record_view.usable_suggestions(act)[1])
        if note := record_view.left_out_notice(
            left_out,
            profile,
            None if record else (act.get("suggested_evidence") or {}),
            saved=bool(record),
        ):
            notices.append(note)
        return {
            "values": values,
            "absent": absent,
            "specs": specs,
            "place": place,
            "profile": profile,
            "family": family,
            "kind": kind,
            "missing": record_view.missing(values, absent, specs),
            "left_out": left_out,
            "unshown": dropped,
            # What was put right in the reader's suggestions, and any values this form has no field for.
            "notices": notices,
        }

    def built(self, act: dict, view: dict):
        """The validated record (document-record-1), or None while details are incomplete."""
        return self.record_and_problem(act, view)[0]

    def record_and_problem(self, act: dict, view: dict):
        """The validated record and None, or None and why the details do not make one (the publishing
        run skips a document whose record does not build, so approval waits for it: findings
        2026-11-16, item 28)."""
        try:
            return (
                record_view.to_record(
                    view["values"],
                    view["absent"],
                    view["place"],
                    view["profile"],
                    record_view.slugify(act["id"][3:]),
                ),
                None,
            )
        except ValidationError as error:
            first = error.errors()[0]
            where = ".".join(str(part) for part in first.get("loc", ()))
            return None, f"{where}: {first.get('msg', 'not valid')}" if where else first.get(
                "msg", "not valid"
            )
        except (ValueError, KeyError) as error:
            return None, str(error) or type(error).__name__

    def state(self, document: dict, text: bool = False) -> dict:
        """Everything that decides a document's review status, read once: its pages, its details,
        and whether the latest approval still covers both exactly. With text, also the canonical
        text and structure (workbench.publication), whose errors block approval; lists of many
        documents leave it out."""
        reading, latest = self.library.reading(document["id"]), self.store.latest_pages(document["id"])
        final = content.final_pages(reading, latest)
        record = self.store.latest_details(self.details_key(document))
        view = self.described(document, record, reading)
        sha, details_sha = content.content_sha256(final), content.details_sha256(record)
        approvals = self.store.approvals(document["id"])
        head = approvals[0] if approvals else None
        approved = bool(
            head
            and head["decision"] == "approved"
            and head["content_sha256"] == sha
            and head.get("details_sha256") == details_sha
            and record
            and record["status"] == "confirmed"
        )
        readiness = content.readiness(reading, latest, content.details_state(record, view["missing"]))
        policy = routing.load_policy()
        built = (
            publication.build(publication.as_confirmed(document, self.store), final, reading)
            if text or policy
            else None
        )
        if built is not None:
            readiness["text"] = {"errors": len(built.errors), "warnings": len(built.warnings)}
            readiness["ready"] = readiness["ready"] and not built.errors
        if policy is not None and built is not None:
            # CPR-01 under the owner's decision: the full pages and the audit sample must be checked, and
            # the flagged pages' flags resolved. A page someone marked as needing attention is checked
            # whole, whatever its route.
            issues: dict[int, int] = {}
            for issue in [*built.errors, *built.warnings]:
                if issue.page is not None:
                    issues[issue.page] = issues.get(issue.page, 0) + 1
            plan = routing.requirement(document["id"], reading, latest, policy, issues)
            pages = {page["page"]: page for page in reading["pages"]}
            attention = {p for p, r in latest.items() if r["status"] == "attention" and p in pages}
            unchecked = sorted(
                {p for p in plan["required_pages"] if (latest.get(p) or {}).get("status") != "checked"}
                | attention
            )
            unresolved = [
                p
                for p in plan["flag_pages"]
                if p not in attention and not routing.flags_resolved(pages[p], latest.get(p))
            ]
            readiness["routing"] = plan
            readiness["routed"] = True
            readiness["unchecked_pages"] = unchecked
            readiness["unresolved_flag_pages"] = unresolved
            readiness["ready"] = (
                not unchecked
                and not unresolved
                and not readiness["open_high_flags"]
                and not readiness["stale_pages"]
                and not readiness["missing_checks"]
                and readiness["details"]["confirmed"]
                and not built.errors
            )
        held = document.get("text_held")
        if held:
            # Its text cannot be published as it stands (owner, 2026-10-25): its details are reviewed and
            # approved alone, and no page waits for review.
            readiness.update(
                unchecked_pages=[],
                open_high_flags=[],
                stale_pages=[],
                unresolved_flag_pages=[],
                missing_checks=[],
                text_held=held,
                ready=readiness["details"]["confirmed"],
            )
        if record and record["status"] == "confirmed":
            # Confirmed details that still do not make a valid record are never published: say why here.
            problem = self.record_and_problem(document, view)[1]
            readiness["details"] = {**readiness["details"], "record_problem": problem}
            readiness["ready"] = readiness["ready"] and problem is None
        pending = status_research_pending(record, document)
        if pending and status_gate_enabled():
            readiness["status_pending"] = True
            readiness["status_pending_message"] = STATUS_RESEARCH_PENDING_MESSAGE
            readiness["details"] = {
                **readiness["details"],
                "status_pending": True,
                "status_pending_message": STATUS_RESEARCH_PENDING_MESSAGE,
            }
            readiness["ready"] = False
        else:
            readiness["status_pending"] = False
        return {
            "reading": reading,
            "latest": latest,
            "final": final,
            "record": record,
            "content_sha256": sha,
            "details_sha256": details_sha,
            "approvals": approvals,
            "head": head,
            "approved": approved,
            "view": view,
            "readiness": readiness,
            "text": built,
        }
