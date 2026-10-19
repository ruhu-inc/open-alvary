"""Whether a reviewed document is approved, decided once for the workbench and for publishing
(operations.publish): its pages, its details (shared by a work's language versions) and whether
the latest approval still covers both exactly."""

from workbench import content, details, publication, record_view, routing


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
        elif place.code == "ng":
            values, absent = record_view.from_legacy(
                *details.initial(details.suggest(reading, act, self.library.manifest_for(act["id"])))
            )
        else:
            values = record_view.base_values(act, self.library.manifest_for(act["id"]))
            absent = dict(act.get("suggested_absent") or {})
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
            # CPR-01 under the owner's decision: only the routed pages and the audit sample must be checked.
            issues: dict[int, int] = {}
            for issue in [*built.errors, *built.warnings]:
                if issue.page is not None:
                    issues[issue.page] = issues.get(issue.page, 0) + 1
            plan = routing.requirement(document["id"], reading, latest, policy, issues)
            unchecked = [
                p for p in plan["required_pages"] if (latest.get(p) or {}).get("status") != "checked"
            ]
            readiness["routing"] = plan
            readiness["unchecked_pages"] = unchecked
            readiness["ready"] = (
                not unchecked
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
