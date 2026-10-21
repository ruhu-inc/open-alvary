"""Whether a reviewed document is approved, decided once for the workbench and for publishing
(operations.publish): its pages, its details (shared by a work's language versions) and whether
the latest approval still covers both exactly."""

from workbench import content, details, publication, record_view, routing


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

    def described(self, act: dict, record: dict | None, reading: dict) -> dict:
        """The document's details in the record view: values, what is accounted for, its fields."""
        place, profile = record_view.place_for(act, self.library.manifest_for(act["id"]), self.profiles)
        if record:
            values, absent = record["details"], record["absent"]
            if not record_view.is_view(values):  # saved under the Nigerian DETAILS-1: read it in this view
                values, absent = record_view.from_legacy(values, absent)
        elif place.code == "ng":  # a first draft from the suggestions, without the legal facts
            values, absent = record_view.first_draft(
                *record_view.from_legacy(
                    *details.initial(details.suggest(reading, act, self.library.manifest_for(act["id"])))
                )
            )
        else:  # nothing marked absent by inference: suggested beside its field (record_view.suggestions)
            values, absent = record_view.base_values(act, self.library.manifest_for(act["id"])), {}
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
        return {
            "values": values,
            "absent": absent,
            "specs": specs,
            "place": place,
            "profile": profile,
            "family": family,
            "kind": kind,
            "missing": record_view.missing(values, absent, specs),
        }

    def built(self, act: dict, view: dict):
        """The validated record (document-record-1), or None while details are incomplete."""
        try:
            return record_view.to_record(
                view["values"],
                view["absent"],
                view["place"],
                view["profile"],
                record_view.slugify(act["id"][3:]),
            )
        except (ValueError, KeyError):
            return None

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
        built = publication.build(document, final, reading) if text or policy else None
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
