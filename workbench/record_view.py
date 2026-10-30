"""The Details screen for any document: the record (schema/record.py) as fields a reviewer fills in.

Which fields a document has follows from its family (an Act, a Bill, a judgment, a treaty...) and
its jurisdiction's profile (schema/jurisdictions.py): an Act in Nigeria has a field for each step
of the Nigerian procedure (passed by the Senate, passed by the House, certified by the Clerk,
assented to, published, commenced), with the date and who signed. Drafts are kept as these flat
values; the record is built and validated from them when the reviewer confirms, and every required
detail must then be given or accounted for (not stated in this copy, not applicable, not yet known,
withheld).

Each field's key is its place in the record ("identity.title", "publication.number",
"step:passed:senate.date", "profile.court.name"), so a value maps to exactly one place.
"""

import re
from dataclasses import dataclass

from schema import record as R
from schema.jurisdictions import JurisdictionProfile

YES_NO = ("yes", "no")

# Details a record cannot exist without: they must be given, never accounted for as absent.
ESSENTIAL = {
    "identity.family",
    "identity.type",
    "identity.title",
    "text.form",
    "text.version.label",
    "status.legal",
    "expressions",
    "classification.hierarchy",
    "profile.stage",
    "profile.made_under",
    "profile.authority",
    "profile.court.name",
    "profile.court.level",
    "profile.organ",
    "profile.legal_effect",
}


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    group: str
    kind: str  # text | long | date | year | choice | list | relations | rows
    choices: tuple[str, ...] = ()
    required: bool = False
    help: str = ""
    columns: tuple[dict, ...] = ()  # for rows: {key, label, kind, choices}
    accountable: bool = False  # optional, yet a reviewer may say why it is not given

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "kind": self.kind,
            "choices": [{"value": c, "label": label_of(c)} for c in self.choices],
            "required": self.required,
            "accountable": self.accountable,
            "essential": self.key in ESSENTIAL,
            "help": self.help,
            "columns": [
                {**c, "choices": [{"value": v, "label": label_of(v)} for v in c.get("choices", ())]}
                for c in self.columns
            ],
        }


LABELS = {
    "gazette_publication": "As published in the gazette",
    "certified_copy": "Certified copy (e.g. the Bill as passed, before gazetting)",
    "official_reprint": "Official reprint",
    "revised_edition": "Revised edition",
    "law_report": "Law report",
    "court_copy": "Signed copy from the court",
    "official_website": "Official website",
    "unofficial": "Unofficial copy",
    "in_force": "In force",
    "partly_in_force": "Partly in force",
    "not_yet_in_force": "Not yet in force",
    "unknown": "Not yet checked",
    "authoritative": "Authoritative",
    "equally_authentic": "Equally authentic",
    "official_translation": "Official translation",
    "unofficial_translation": "Unofficial translation",
    "machine_translation": "Machine translation",
    "explanatory_memorandum": "Explanatory memorandum",
    "headnote": "Headnote",
    "ltr": "Left to right",
    "rtl": "Right to left",
    "on_assent": "On assent",
    "on_publication": "On publication",
    "fixed_date": "On a fixed date",
    "by_notice": "By notice",
    "staggered": "Part by part",
    "organic_law": "Organic law",
    "decree_law": "Decree-law",
    "presidential_order": "Presidential Order",
    "prime_ministers_order": "Prime Minister's Order",
    "ministerial_order": "Ministerial Order",
}


def label_of(value: str) -> str:
    return LABELS.get(value, value.replace("_", " ").capitalize())


GROUP_LABELS = {
    "identity": "Identity",
    "making": "How it was made",
    "publication": "Publication",
    "text": "The text",
    "status": "Status and related documents",
    "language": "Language versions",
    "classification": "Classification",
    "profile": "",  # named per family below
}
PROFILE_GROUP = {
    "constitution": "The Bill and commencement",
    "legislation": "The Bill and commencement",
    "bill": "The Bill",
    "subsidiary_legislation": "The instrument",
    "executive_instrument": "The instrument",
    "judgment": "The case",
    "gazette": "The gazette",
    "gazette_notice": "The notice",
    "treaty": "The treaty",
    "regional_instrument": "The regional instrument",
    "soft_law": "Issued by",
    "parliamentary_record": "The sitting",
    "official_document": "Issued by",
}
NUMBER_SCHEME = {
    "constitution": "act",
    "legislation": "act",
    "bill": "bill",
    "subsidiary_legislation": "instrument",
    "executive_instrument": "decree",
    "judgment": "case",
    "gazette": "gazette",
    "gazette_notice": "notice",
    "treaty": "treaty",
    "regional_instrument": "instrument",
}
ISSUER_KIND = {
    "constitution": "legislature",
    "legislation": "legislature",
    "bill": "chamber",
    "subsidiary_legislation": "minister",
    "executive_instrument": "head_of_state",
    "judgment": "court",
    "gazette": "executive",
    "gazette_notice": "executive",
    "treaty": "international_organisation",
    "regional_instrument": "regional_body",
}
HIERARCHY = {
    "constitution": "constitutional",
    "legislation": "primary",
    "bill": "parliamentary",
    "subsidiary_legislation": "subsidiary",
    "executive_instrument": "executive",
    "judgment": "judicial",
    "gazette": "administrative",
    "gazette_notice": "administrative",
    "treaty": "international",
    "regional_instrument": "regional",
    "soft_law": "soft_law",
    "parliamentary_record": "parliamentary",
    "official_document": "administrative",
}


def _profile_fields(family: str) -> list[FieldSpec]:
    g = "profile"
    if family in ("constitution", "legislation"):
        return [
            FieldSpec("profile.bill", "The Bill", g, "text", required=True, help="Its title as passed"),
            FieldSpec(
                "profile.commencement",
                "How it commences",
                g,
                "choice",
                ("on_assent", "on_publication", "fixed_date", "by_notice", "staggered"),
            ),
            FieldSpec(
                "profile.revised_edition",
                "Chapter in a revised edition",
                g,
                "text",
                required=family == "legislation",
                help="e.g. Cap. C28, LFN 2004",
            ),
        ]
    if family == "bill":
        return [
            FieldSpec(
                "profile.stage",
                "Stage reached",
                g,
                "choice",
                tuple(R.BillProfile.model_fields["stage"].annotation.__args__),
                required=True,
            ),
            FieldSpec("profile.originating_chamber", "Chamber it started in", g, "text", required=True),
            FieldSpec("profile.sponsor", "Sponsor", g, "text", required=True),
            FieldSpec(
                "profile.sponsor_kind",
                "Sponsored by",
                g,
                "choice",
                ("government", "private_member", "committee", "other"),
            ),
            FieldSpec("profile.committee", "Committee", g, "text", required=True),
            FieldSpec("profile.enacted_as", "Enacted as", g, "text"),
        ]
    if family == "subsidiary_legislation":
        return [
            FieldSpec(
                "profile.made_under",
                "Made under",
                g,
                "list",
                required=True,
                help="The enabling law and section",
            ),
            FieldSpec("profile.maker", "Made by", g, "text", required=True),
            FieldSpec("profile.signed_by", "Signed by", g, "text"),
            FieldSpec(
                "profile.parliamentary_control",
                "Parliamentary control",
                g,
                "choice",
                ("laid", "affirmative", "negative", "none"),
            ),
        ]
    if family == "executive_instrument":
        return [
            FieldSpec("profile.authority", "Made by", g, "text", required=True),
            FieldSpec(
                "profile.basis", "Legal basis", g, "list", help="Constitutional article or law relied on"
            ),
            FieldSpec("profile.signed_by", "Signed by", g, "text", required=True),
            FieldSpec("profile.countersigned_by", "Countersigned by", g, "list"),
        ]
    if family == "judgment":
        return [
            FieldSpec("profile.court.name", "Court", g, "text", required=True),
            FieldSpec(
                "profile.court.code", "Court code", g, "text", help="As in neutral citations, e.g. kehc"
            ),
            FieldSpec("profile.court.level", "Court level", g, "choice", R.COURT_LEVELS, required=True),
            FieldSpec("profile.court.division", "Division", g, "text"),
            FieldSpec("profile.court.seat", "Seat", g, "text"),
            FieldSpec("profile.case_numbers", "Case numbers", g, "list", required=True),
            FieldSpec(
                "profile.parties",
                "Parties",
                g,
                "rows",
                required=True,
                columns=(
                    {"key": "name", "label": "Name as cited", "kind": "text"},
                    {"key": "role", "label": "Role", "kind": "choice", "choices": R.PARTY_ROLES},
                    {"key": "anonymised", "label": "Withheld", "kind": "choice", "choices": YES_NO},
                    # Who appeared for the party, as the judgment's representation names them ("; " between).
                    {"key": "represented_by", "label": "Counsel", "kind": "text"},
                ),
            ),
            FieldSpec(
                "profile.bench",
                "Bench",
                g,
                "rows",
                required=True,
                columns=(
                    {"key": "name", "label": "Judge", "kind": "text"},
                    {
                        "key": "role",
                        "label": "Role",
                        "kind": "choice",
                        "choices": ("presiding", "member", "author", "concurring", "dissenting", "sole"),
                    },
                ),
            ),
            # Optional for now (owner, 2026-10-29): a judgment is approved with its details and text, its
            # outcome and summary added later.
            FieldSpec("profile.outcome", "Outcome", g, "list", help=", ".join(R.OUTCOMES)),
            FieldSpec("profile.orders", "Orders", g, "list"),
            FieldSpec("profile.issues", "Issues", g, "list"),
            FieldSpec(
                "profile.anonymisation",
                "Names withheld",
                g,
                "choice",
                ("none", "partial", "full"),
                help="In a sensitive case (a child, a sexual offence or harassment): partial withholds the "
                "parties marked Withheld, full every party. Published details show [Withheld] in their place; "
                "the text is not published.",
            ),
            FieldSpec(
                "profile.anonymisation_reason",
                "Why withheld",
                g,
                "choice",
                ("child", "sexual_offence", "family", "security", "other"),
            ),
            FieldSpec(
                "profile.precedent",
                "Standing as precedent",
                g,
                "choice",
                ("binding", "persuasive", "not_binding", "unknown"),
            ),
            FieldSpec(
                "profile.treatment",
                "Later treatment",
                g,
                "choice",
                ("good_law", "overruled", "reversed", "doubted", "unknown"),
            ),
        ]
    if family in ("gazette", "gazette_notice"):
        return [
            FieldSpec(
                "profile.contents", "Contents", g, "list", help="Notices and supplements in this issue"
            ),
            FieldSpec(
                "profile.issuing_authority", "Issued by", g, "text", required=family == "gazette_notice"
            ),
        ]
    if family == "treaty":
        return [
            FieldSpec("profile.adopted_by", "Adopted by", g, "text"),
            FieldSpec("profile.adopted_at", "Adopted at", g, "text"),
            FieldSpec(
                "profile.parties",
                "Parties",
                g,
                "rows",
                required=True,
                columns=(
                    {"key": "party", "label": "Party", "kind": "text"},
                    {"key": "place", "label": "Code", "kind": "text"},
                    {"key": "signed", "label": "Signed", "kind": "date"},
                    {"key": "ratified", "label": "Ratified", "kind": "date"},
                    {"key": "acceded", "label": "Acceded", "kind": "date"},
                    {"key": "denounced", "label": "Denounced", "kind": "date"},
                    {"key": "reservations", "label": "Reservations", "kind": "text"},
                ),
            ),
            FieldSpec(
                "profile.parties_complete_as_of",
                "Party list complete as of",
                g,
                "date",
                help="Only from the depositary's status list of that date, listing every party; until then, "
                "a state missing from the list is unknown, never not a party",
            ),
            FieldSpec(
                "profile.parties_complete_evidence.url",
                "Party list: depositary's status list",
                g,
                "text",
                help="https:// address of the status list that shows every party on that date",
            ),
            FieldSpec("profile.parties_complete_evidence.quote", "Party list: words as printed", g, "text"),
            FieldSpec("profile.depositary", "Depositary", g, "text", required=True),
            FieldSpec("profile.entry_into_force_rule", "When it enters into force", g, "long"),
            FieldSpec("profile.authentic_languages", "Authentic languages", g, "list"),
        ]
    if family == "regional_instrument":
        return [
            FieldSpec("profile.organ", "Adopted by (organ)", g, "text", required=True),
            FieldSpec(
                "profile.legal_effect",
                "Legal effect",
                g,
                "choice",
                ("directly_applicable", "requires_transposition", "binding_on_addressees", "non_binding"),
                required=True,
            ),
            FieldSpec(
                "profile.member_states",
                "Member states it binds",
                g,
                "list",
                help="Codes; empty for every member",
            ),
            FieldSpec("profile.transposition_deadline", "Transposition deadline", g, "date"),
        ]
    return [
        FieldSpec("profile.issuing_body", "Issued by", g, "text", required=family != "parliamentary_record"),
        FieldSpec("profile.sitting", "Sitting", g, "text", required=family == "parliamentary_record"),
    ]


def steps_for(profile: JurisdictionProfile | None, kind: str) -> list:
    if not profile:
        return []
    for procedure in profile.procedures:
        if kind in procedure.applies_to:
            return list(procedure.steps)
    return []


def fields(family: str, kind: str, profile: JurisdictionProfile | None) -> list[FieldSpec]:
    """Every field of the Details screen for this kind of document in this jurisdiction."""
    out = [
        FieldSpec("identity.family", "Family", "identity", "choice", tuple(R.FAMILIES), required=True),
        FieldSpec("identity.type", "Kind", "identity", "choice", R.FAMILIES[family][1], required=True),
        FieldSpec(
            "identity.title",
            "Short title" if family != "judgment" else "Case name",
            "identity",
            "text",
            required=True,
        ),
        FieldSpec(
            "identity.full_title",
            "Full title" if family != "judgment" else "Full case name",
            "identity",
            "long",
            required=True,
        ),
        FieldSpec(
            "identity.number.value",
            "Number",
            "identity",
            "text",
            required=True,
            help="As normalised, e.g. 8, 014/2025",
        ),
        FieldSpec(
            "identity.number.printed",
            "Number as printed",
            "identity",
            "text",
            help="e.g. Act No. 8, Law n° 014/2025",
        ),
        FieldSpec("identity.year", "Year", "identity", "year", required=True),
        FieldSpec(
            "identity.citation",
            "Citation" if family != "judgment" else "Neutral citation",
            "identity",
            "text",
            required=True,
        ),
        FieldSpec("identity.other_citations", "Other citations", "identity", "list"),
        FieldSpec(
            "identity.local_subtype", "Local subtype", "identity", "text", help="e.g. law, ol, po, mo, ln, si"
        ),
        FieldSpec("issued_by", "Issued by", "identity", "text", required=True),
    ]
    step_events = set()
    for step in steps_for(profile, kind):
        step_events.add(step.event)
        out.append(FieldSpec(f"step:{step.id}.date", step.label, "making", "date", required=True))
        if step.signed_by:
            out.append(
                FieldSpec(
                    f"step:{step.id}.person",
                    f"{step.label}: signed by",
                    "making",
                    "text",
                    help=f"Name as signed ({step.signed_by})",
                )
            )
    for path in R.REQUIRED[family] + R.REQUIRED_BY_TYPE.get(kind, ()):
        if path.startswith("event:") and path[6:] not in step_events:
            event = path[6:]
            out.append(FieldSpec(f"event:{event}.date", label_of(event), "making", "date", required=True))
    out += [
        FieldSpec("publication.kind", "Published in", "publication", "choice", R.PUBLICATION_KINDS),
        FieldSpec(
            "publication.name",
            "Publication",
            "publication",
            "text",
            required=True,
            help="e.g. Official Gazette of the Republic of Rwanda",
        ),
        FieldSpec("publication.number", "Number", "publication", "text"),
        FieldSpec("publication.volume", "Volume", "publication", "text"),
        FieldSpec("publication.part", "Part or supplement", "publication", "text"),
        FieldSpec(
            "publication.issue",
            "Issue",
            "publication",
            "choice",
            ("ordinary", "extraordinary", "special", "supplement"),
        ),
        FieldSpec("publication.date", "Date", "publication", "date"),
        FieldSpec("publication.place", "Place", "publication", "text"),
        FieldSpec("publication.pages", "Pages", "publication", "text"),
        FieldSpec("publication.notice", "Notice", "publication", "text"),
        FieldSpec("publication.publisher", "Publisher", "publication", "text"),
        FieldSpec(
            "publication.url",
            "Official copy online",
            "publication",
            "text",
            help="https:// address of the official text",
        ),
        FieldSpec("text.form", "Form of this text", "text", "choice", R.TEXT_FORMS, required=True),
        FieldSpec("text.version.label", "Version", "text", "text", required=True, help="e.g. As enacted"),
        FieldSpec("text.version.point_in_time", "Version date", "text", "date"),
        FieldSpec("text.version.consolidated", "Consolidated", "text", "choice", YES_NO),
        FieldSpec(
            "text.summary.kind",
            "Summary is",
            "text",
            "choice",
            ("explanatory_memorandum", "headnote", "abstract", "editorial"),
        ),
        # Optional for now (owner, 2026-10-29 for judgments, 2026-10-30 for every family): few texts print
        # one, so a document is approved with its details and text and its summary added later.
        FieldSpec("text.summary.text", "Summary", "text", "long"),
        FieldSpec("text.summary.official", "Summary part of the official text", "text", "choice", YES_NO),
        FieldSpec("status.legal", "Legal status", "status", "choice", R.LEGAL_STATUS, required=True),
        FieldSpec("status.as_of", "Status checked on", "status", "date"),
        FieldSpec("status.note", "Status note", "status", "long"),
        FieldSpec("relations", "Related documents", "status", "relations"),
        FieldSpec(
            "expressions",
            "Language versions",
            "language",
            "rows",
            required=True,
            columns=(
                {"key": "language", "label": "Language (BCP 47)", "kind": "text"},
                {"key": "script", "label": "Script", "kind": "text"},
                {"key": "direction", "label": "Direction", "kind": "choice", "choices": ("ltr", "rtl")},
                {
                    "key": "authority",
                    "label": "Authority",
                    "kind": "choice",
                    "choices": R.EXPRESSION_AUTHORITY,
                },
                {"key": "authority_page", "label": "Authority: page", "kind": "text"},
                {"key": "authority_words", "label": "Authority: words as printed", "kind": "text"},
                {"key": "authority_url", "label": "Authority: address, if stated elsewhere", "kind": "text"},
                {"key": "this_text", "label": "This text", "kind": "choice", "choices": YES_NO},
            ),
            help="Leave the authority empty when nothing states it: it is published as unknown.",
        ),
        FieldSpec(
            "language_rule.rule",
            "Rule where language versions differ",
            "language",
            "long",
            accountable=True,
            help="As the document states it, e.g. the French version prevails (OHADA Treaty, art. 42)",
        ),
        FieldSpec(
            "language_rule.prevails",
            "Version that prevails",
            "language",
            "text",
            help="BCP 47, e.g. fr; empty when the rule names none",
        ),
        FieldSpec("language_rule.provision", "Rule stated in", "language", "text", help="e.g. Article 42"),
        FieldSpec("language_rule.evidence.page", "Rule: page", "language", "text"),
        FieldSpec("language_rule.evidence.quote", "Rule: words as printed", "language", "long"),
        FieldSpec(
            "language_rule.evidence.url",
            "Rule: address, if stated elsewhere",
            "language",
            "text",
            help="https:// address of the instrument that states it",
        ),
        FieldSpec(
            "classification.hierarchy",
            "Place in the legal hierarchy",
            "classification",
            "choice",
            R.HIERARCHY,
            required=True,
        ),
        FieldSpec("classification.subjects", "Subjects", "classification", "list"),
        FieldSpec("classification.keywords", "Keywords", "classification", "list"),
    ]
    return out + _profile_fields(family)


def groups(family: str) -> list[dict]:
    order = ("identity", "making", "publication", "text", "status", "language", "classification", "profile")
    return [{"key": g, "label": PROFILE_GROUP[family] if g == "profile" else GROUP_LABELS[g]} for g in order]


def empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def missing(values: dict, absent: dict, specs: list[FieldSpec]) -> list[str]:
    return [s.key for s in specs if s.required and empty(values.get(s.key)) and s.key not in absent]


# --- building the record ------------------------------------------------------------------------------


def _dated(value):
    return R.Dated(value=value) if value else None


def _yes(value) -> bool:
    return value in (True, "yes")


def _person(text: str | None, office: str | None = None):
    if not text:
        return None
    for marker in (", President of", ", Clerk to", ", Prime Minister", ", Minister"):
        at = text.find(marker)
        if at > 0:
            return R.Person(name=text[:at], office=text[at + 2 :])
    return R.Person(name=text, office=office)


ABSENT_TO_RECORD = {
    "identity.number.value": "identity.numbers",
    "identity.citation": "identity.citations",
    "publication.name": "publications",
    "text.summary.text": "text.summary",
    "language_rule.rule": "language_rule",
}


def _record_absent(absent: dict) -> dict:
    out = {}
    for key, reason in absent.items():
        if key.startswith("step:") and key.endswith(".date"):
            path = key[: -len(".date")]
        elif key.startswith("event:") and key.endswith(".date"):
            path = key[: -len(".date")]
        else:
            path = ABSENT_TO_RECORD.get(key, key)
        if R.ABSENT_PATH.match(path):
            out[path] = R.Absent(reason=reason)
    return out


def to_record(values: dict, absent: dict, place: R.Place, profile: JurisdictionProfile | None, slug: str):
    """The validated record from a confirmed set of values (raises ValueError when incomplete)."""
    family = values["identity.family"]
    kind = values["identity.type"]
    number = values.get("identity.number.value")
    year = values.get("identity.year")
    actor = values.get("profile.court.code") if family == "judgment" else None
    subtype = values.get("identity.local_subtype") or None
    work = R.work_uri(place.code, family, number or slug, year or "0", subtype=subtype, actor=actor)
    chambers = {c.id: c.name for c in profile.legislature.chambers} if profile and profile.legislature else {}
    events = []
    for step in steps_for(profile, kind):
        when = values.get(f"step:{step.id}.date")
        if when:
            events.append(
                R.Event(
                    kind=step.event,
                    when=R.Dated(value=when),
                    step=step.id,
                    by=R.Body(name=chambers[step.chamber], kind="chamber", id=step.chamber)
                    if step.chamber
                    else None,
                    person=_person(values.get(f"step:{step.id}.person"), step.signed_by),
                )
            )
    for key, when in values.items():
        if key.startswith("event:") and key.endswith(".date") and when:
            events.append(R.Event(kind=key[6:-5], when=R.Dated(value=when)))
    publication = None
    if values.get("publication.name"):
        publication = R.Publication(
            kind=values.get("publication.kind") or "gazette",
            name=values["publication.name"],
            number=values.get("publication.number"),
            volume=values.get("publication.volume"),
            part=values.get("publication.part"),
            issue=values.get("publication.issue"),
            when=_dated(values.get("publication.date")),
            place=values.get("publication.place"),
            pages=values.get("publication.pages"),
            notice=values.get("publication.notice"),
            publisher=values.get("publication.publisher"),
            url=values.get("publication.url") or None,
        )
    expressions = tuple(
        R.Expression(
            language=row["language"],
            script=row.get("script") or None,
            direction=row.get("direction") or "ltr",
            authority=row.get("authority") or None,  # not given: unknown, never assumed
            authority_evidence=_evidence(
                row.get("authority_page"), row.get("authority_words"), row.get("authority_url")
            ),
            this_text=_yes(row.get("this_text")),
        )
        for row in values.get("expressions") or []
    )
    language_rule = _language_rule(values)
    summary = None
    if values.get("text.summary.text"):
        summary = R.Summary(
            kind=values.get("text.summary.kind") or "explanatory_memorandum",
            text=values["text.summary.text"],
            official=_yes(values.get("text.summary.official", "yes")),
        )
    citations = []
    if values.get("identity.citation"):
        citations.append(
            R.Citation(
                kind="neutral" if family == "judgment" else "official", text=values["identity.citation"]
            )
        )
    citations += [R.Citation(kind="other", text=c) for c in values.get("identity.other_citations") or []]
    return R.DocumentRecord(
        identity=R.Identity(
            family=family,
            type=kind,
            local_subtype=subtype,
            work_uri=work,
            title=values["identity.title"],
            full_title=values.get("identity.full_title"),
            numbers=(
                R.Number(
                    scheme=NUMBER_SCHEME.get(family, "other"),
                    value=number,
                    printed=values.get("identity.number.printed"),
                ),
            )
            if number
            else (),
            year=int(year) if year else None,
            citations=tuple(citations),
        ),
        place=place,
        issued_by=(R.Body(name=values["issued_by"], kind=ISSUER_KIND.get(family, "other")),)
        if values.get("issued_by")
        else (),
        expressions=expressions,
        language_rule=language_rule,
        events=tuple(events),
        publications=(publication,) if publication else (),
        status=R.Status(
            legal=values.get("status.legal") or "unknown",
            as_of=values.get("status.as_of") or None,
            note=values.get("status.note") or None,
        ),
        relations=tuple(
            R.Relation(
                kind=link["kind"],
                target=R.Reference(citation=link["target"], work_uri=link.get("target_id")),
                note=link.get("note"),
            )
            for link in values.get("relations") or []
        ),
        text=R.TextInfo(
            form=values["text.form"],
            version=R.Version(
                label=values["text.version.label"],
                point_in_time=values.get("text.version.point_in_time") or None,
                consolidated=_yes(values.get("text.version.consolidated")),
            ),
            summary=summary,
        ),
        classification=R.Classification(
            hierarchy=values.get("classification.hierarchy") or HIERARCHY[family],
            subjects=tuple(
                R.Subject(scheme="free", code=R.slug(s), label=s)
                for s in values.get("classification.subjects") or []
            ),
            keywords=tuple(values.get("classification.keywords") or []),
        ),
        profile=_profile(family, values),
        absent=_with_step_events(_record_absent(absent), steps_for(profile, kind), events),
    )


def _evidence(page, words, url):
    """Where a detail is stated, from what the reviewer gave: a page and the words of this document, or of
    another instrument at the address given; None when nothing is given."""
    if not (page or words or url):
        return None
    if page and not str(page).strip().isdigit():
        raise ValueError(f"page {page!r}: give the page's number, such as 3")
    return R.Evidence(
        source="external" if url else "document",
        page=int(page) if page else None,
        quote=words or None,
        url=url or None,
    )


def _language_rule(values: dict):
    """The document's rule where its language versions differ, with where it is stated; None when not
    given (the reviewer may say why: not stated in this copy, not applicable)."""
    keys = [k for k in values if k.startswith("language_rule.") and values[k]]
    if not values.get("language_rule.rule"):
        if keys:
            raise ValueError("give the rule where language versions differ, or leave its details empty")
        return None
    evidence = _evidence(
        values.get("language_rule.evidence.page"),
        values.get("language_rule.evidence.quote"),
        values.get("language_rule.evidence.url"),
    )
    if evidence is None:
        raise ValueError("say where the language rule is stated: its page, its words or an address")
    return R.LanguageRule(
        rule=values["language_rule.rule"],
        prevails=values.get("language_rule.prevails") or None,
        provision=values.get("language_rule.provision") or None,
        evidence=evidence,
    )


def _parties_complete(values: dict) -> dict:
    """The date a treaty's party list is complete as of, with the depositary's status list that shows it;
    neither when not given."""
    when = values.get("profile.parties_complete_as_of") or None
    url = values.get("profile.parties_complete_evidence.url") or None
    words = values.get("profile.parties_complete_evidence.quote") or None
    if not (when or url or words):
        return {}
    if not (when and url):
        raise ValueError(
            "a party list complete as of a date needs the date and the address of the depositary's status "
            "list that shows it"
        )
    return {
        "parties_complete_as_of": when,
        "parties_complete_evidence": R.Evidence(source="external", quote=words, url=url),
    }


def _with_step_events(absent: dict, steps: list, events: list) -> dict:
    """A step accounted for also accounts for its event (an Act whose commencement is not stated in
    this copy has no commencement event to show either)."""
    given = {e.kind for e in events}
    out = dict(absent)
    for step in steps:
        reason = absent.get(f"step:{step.id}")
        if reason and step.event not in given:
            out.setdefault(f"event:{step.event}", reason)
    return out


def _profile(family: str, v: dict):
    refs = lambda key: tuple(R.Reference(citation=c) for c in v.get(key) or [])  # noqa: E731
    if family in ("constitution", "legislation"):
        return R.LegislationProfile(
            family=family,
            bill=R.Reference(citation=v["profile.bill"]) if v.get("profile.bill") else None,
            commencement=v.get("profile.commencement") or None,
            revised_edition=R.Citation(kind="revised_edition", text=v["profile.revised_edition"])
            if v.get("profile.revised_edition")
            else None,
        )
    if family == "bill":
        return R.BillProfile(
            family=family,
            stage=v["profile.stage"],
            originating_chamber=R.Body(name=v["profile.originating_chamber"], kind="chamber")
            if v.get("profile.originating_chamber")
            else None,
            sponsor=R.Person(name=v["profile.sponsor"]) if v.get("profile.sponsor") else None,
            sponsor_kind=v.get("profile.sponsor_kind") or None,
            committee=R.Body(name=v["profile.committee"], kind="chamber")
            if v.get("profile.committee")
            else None,
            enacted_as=R.Reference(citation=v["profile.enacted_as"]) if v.get("profile.enacted_as") else None,
        )
    if family == "subsidiary_legislation":
        return R.SubsidiaryProfile(
            family=family,
            made_under=refs("profile.made_under"),
            maker=R.Body(name=v["profile.maker"], kind="minister") if v.get("profile.maker") else None,
            signed_by=_person(v.get("profile.signed_by")),
            parliamentary_control=v.get("profile.parliamentary_control") or None,
        )
    if family == "executive_instrument":
        return R.ExecutiveProfile(
            family=family,
            authority=R.Body(name=v["profile.authority"], kind="executive"),
            basis=refs("profile.basis"),
            signed_by=_person(v.get("profile.signed_by")),
            countersigned_by=tuple(R.Person(name=p) for p in v.get("profile.countersigned_by") or []),
        )
    if family == "judgment":
        return R.JudgmentProfile(
            family=family,
            court=R.Court(
                name=v["profile.court.name"],
                code=v.get("profile.court.code") or None,
                level=v["profile.court.level"],
                division=v.get("profile.court.division") or None,
                seat=v.get("profile.court.seat") or None,
            ),
            case_numbers=tuple(v.get("profile.case_numbers") or ()),
            parties=tuple(
                R.Party(
                    name=p["name"],
                    role=p["role"],
                    anonymised=_yes(p.get("anonymised")),
                    represented_by=tuple(
                        n.strip() for n in str(p.get("represented_by") or "").split(";") if n.strip()
                    ),
                )
                for p in v.get("profile.parties") or []
            ),
            bench=tuple(R.Judge(name=j["name"], role=j["role"]) for j in v.get("profile.bench") or []),
            outcome=tuple(v.get("profile.outcome") or ()),
            orders=tuple(v.get("profile.orders") or ()),
            issues=tuple(v.get("profile.issues") or ()),
            anonymisation=v.get("profile.anonymisation") or "none",
            anonymisation_reason=v.get("profile.anonymisation_reason") or None,
            precedent=v.get("profile.precedent") or "unknown",
            treatment=v.get("profile.treatment") or "unknown",
        )
    if family in ("gazette", "gazette_notice"):
        return R.GazetteProfile(
            family=family,
            gazette=R.Publication(
                kind="gazette",
                name=v.get("publication.name") or "Gazette",
                number=v.get("publication.number"),
                when=_dated(v.get("publication.date")),
            ),
            contents=refs("profile.contents"),
            issuing_authority=R.Body(name=v["profile.issuing_authority"], kind="executive")
            if v.get("profile.issuing_authority")
            else None,
        )
    if family == "treaty":
        return R.TreatyProfile(
            family=family,
            adopted_by=R.Body(name=v["profile.adopted_by"], kind="international_organisation")
            if v.get("profile.adopted_by")
            else None,
            adopted_at=v.get("profile.adopted_at") or None,
            parties=tuple(
                R.TreatyParty(
                    party=p["party"],
                    place=p.get("place") or None,
                    signed=_dated(p.get("signed")),
                    ratified=_dated(p.get("ratified")),
                    acceded=_dated(p.get("acceded")),
                    denounced=_dated(p.get("denounced")),
                    reservations=p.get("reservations") or None,
                )
                for p in v.get("profile.parties") or []
            ),
            **_parties_complete(v),
            depositary=v.get("profile.depositary") or None,
            entry_into_force_rule=v.get("profile.entry_into_force_rule") or None,
            authentic_languages=tuple(v.get("profile.authentic_languages") or ()),
        )
    if family == "regional_instrument":
        return R.RegionalProfile(
            family=family,
            organ=R.Body(name=v["profile.organ"], kind="regional_body"),
            legal_effect=v["profile.legal_effect"],
            member_states=tuple(v.get("profile.member_states") or ()),
            transposition_deadline=v.get("profile.transposition_deadline") or None,
        )
    return R.OtherProfile(
        family=family,
        issuing_body=R.Body(name=v["profile.issuing_body"], kind="other")
        if v.get("profile.issuing_body")
        else None,
        sitting=v.get("profile.sitting") or None,
    )


# --- the Nigerian details (DETAILS-1) in this view ------------------------------------------------------

LEGACY = {
    "short_title": "identity.title",
    "long_title": "identity.full_title",
    "number": "identity.number.value",
    "year": "identity.year",
    "citation": "identity.citation",
    "enacting_body": "issued_by",
    "bill_title": "profile.bill",
    "passed_senate": "step:passed:senate.date",
    "passed_house": "step:passed:house.date",
    "authenticated_on": "step:certified.date",
    "authenticated_by": "step:certified.person",
    "assent_date": "step:assented.date",
    "assented_by": "step:assented.person",
    "commencement_date": "step:commenced.date",
    "gazette_name": "publication.name",
    "gazette_number": "publication.number",
    "gazette_volume": "publication.volume",
    "gazette_date": "publication.date",
    "gazette_place": "publication.place",
    "gazette_issue": "publication.issue",
    "gazette_notice": "publication.notice",
    "gazette_pages": "publication.pages",
    "official_publisher": "publication.publisher",
    "official_source_url": "publication.url",
    "text_form": "text.form",
    "version_label": "text.version.label",
    "consolidation": "text.version.consolidated",
    "summary": "text.summary.text",
    "subjects": "classification.subjects",
    "legal_status": "status.legal",
    "status_checked_on": "status.as_of",
    "status_note": "status.note",
    "relations": "relations",
}
LEGACY_VALUES = {
    "text.form": {
        "gazette_supplement": "gazette_publication",
        "authenticated_bill": "certified_copy",
        "other": "unofficial",
    },
    "text.version.consolidated": {"not_consolidated": "no", "consolidated": "yes"},
    "status.legal": {"current": "in_force"},
}
FAMILY_OF_TYPE = {
    "act": "legislation",
    "bill": "bill",
    "regulation": "subsidiary_legislation",
    "order": "subsidiary_legislation",
    "judgment": "judgment",
    "gazette": "gazette",
    "treaty": "treaty",
    "protocol": "treaty",  # also a regional instrument's kind: a protocol is a treaty unless filed otherwise
    "other": "official_document",
}


def family_for(kind: str | None) -> str | None:
    """The record's family for a kind: the one written for it, else the only family listing it."""
    if not kind:
        return None
    if kind in FAMILY_OF_TYPE:
        return FAMILY_OF_TYPE[kind]
    listed = [family for family, (_, kinds) in R.FAMILIES.items() if kind in kinds]
    return listed[0] if len(listed) == 1 else None


def usable_suggestions(document: dict) -> tuple[dict, list[str]]:
    """The details a collection's reader suggested, ready for the first draft, and what was put right
    on the way, said to the reviewer. A family that does not go with the kind (Rwanda's reader filed
    every agreement, regulation and ordinance as an executive instrument) is put right from the kind,
    and the rest of the suggestions kept: until 2026-10-29 all of them were dropped, silently."""
    suggested = {k: v for k, v in (document.get("suggested") or {}).items() if v not in (None, "", [])}
    family, kind = suggested.get("identity.family"), suggested.get("identity.type")
    if not suggested or (family in R.FAMILIES and kind in R.FAMILIES[family][1]):
        return suggested, []
    right = family_for(kind)
    if right:
        return {**suggested, "identity.family": right}, [
            f"The reader filed this {label_of(kind).lower()} under {label_of(family or 'no family')}; "
            f"it belongs under {label_of(right)} and is filed there. Check the family and kind."
        ]
    rest = {k: v for k, v in suggested.items() if k not in ("identity.family", "identity.type")}
    return rest, [
        f'The suggested kind "{kind}" is not one of the record\'s kinds: choose the family and kind.'
    ]


def from_legacy(details: dict, absent: dict) -> tuple[dict, dict]:
    """A Nigerian DETAILS-1 record (or its suggestions) in this view."""
    values, gaps = {}, {}
    relations = details.get("relations") or []
    constitutional = any(
        link["kind"] == "amends" and "constitution" in link["target"].lower() for link in relations
    )
    kind = details.get("document_type", "act")
    values["identity.family"] = (
        "constitution" if constitutional else FAMILY_OF_TYPE.get(kind, "official_document")
    )
    values["identity.type"] = (
        "constitutional_amendment"
        if constitutional
        else (
            kind
            if kind in R.FAMILIES[values["identity.family"]][1]
            else R.FAMILIES[values["identity.family"]][1][0]
        )
    )
    for old, new in LEGACY.items():
        if old in details and details[old] not in (None, "", []):
            value = details[old]
            if old == "relations":
                value = [
                    {**link, "kind": {"cross_reference": "related"}.get(link["kind"], link["kind"])}
                    for link in value
                ]
            values[new] = LEGACY_VALUES[new].get(value, value) if new in LEGACY_VALUES else value
        if old in absent:
            gaps[new] = absent[old]
    if values.get("identity.number.value"):
        values["identity.number.printed"] = f"Act No. {values['identity.number.value']}"
    if details.get("gazette_name"):
        values["publication.kind"] = "gazette_supplement"
        if details.get("gazette_date"):
            values["step:published.date"] = details["gazette_date"]
    elif "gazette_name" in absent:
        gaps["step:published.date"] = absent["gazette_name"]
    if values.get("text.summary.text"):
        values["text.summary.kind"] = "explanatory_memorandum"
        values["text.summary.official"] = "yes"
    language = details.get("language")
    if language:
        authority = {  # as the reviewer gave it; not given, unknown
            "original": "authoritative",
            "official_translation": "official_translation",
            "unofficial_translation": "unofficial_translation",
        }.get(details.get("language_status"))
        row = {"language": language, "direction": "ltr", "this_text": "yes"}
        if authority:
            row["authority"] = authority
        values["expressions"] = [row]
    values["classification.hierarchy"] = HIERARCHY[values["identity.family"]]
    if values["identity.family"] == "legislation" and "profile.revised_edition" not in values:
        gaps.setdefault("profile.revised_edition", "not_applicable")
    return values, gaps


def is_view(details: dict) -> bool:
    return "identity.family" in details


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# --- validation, suggestions and the document's place ---------------------------------------------------

LIMITS = {"text": 300, "long": 4000}
DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate(values: dict, absent: dict, specs: list[FieldSpec]) -> tuple[dict, dict]:
    """Clean submitted values against this document's fields; ValueError names the first problem."""
    from datetime import date

    if not isinstance(values, dict) or not isinstance(absent, dict):
        raise ValueError("details and absent must be objects")
    by_key = {s.key: s for s in specs}
    unknown = (set(values) | set(absent)) - set(by_key)
    if unknown:
        raise ValueError(f"unknown details: {', '.join(sorted(unknown))}")
    clean = {}
    for key, value in values.items():
        if empty(value):
            continue
        spec = by_key[key]
        label = spec.label
        if spec.kind in LIMITS:
            if not isinstance(value, str) or len(value.strip()) > LIMITS[spec.kind]:
                raise ValueError(f"{label}: text of at most {LIMITS[spec.kind]} characters")
            value = value.strip()
        elif spec.kind == "date":
            if not isinstance(value, str) or not DATE_ISO.match(value):
                raise ValueError(f"{label}: a date as YYYY-MM-DD")
            date.fromisoformat(value)
        elif spec.kind == "year":
            if not re.fullmatch(r"1[89]\d\d|20\d\d|2100", str(value)):
                raise ValueError(f"{label}: a four-digit year")
            value = str(value)
        elif spec.kind == "choice":
            if value not in spec.choices:
                raise ValueError(f"{label}: one of {', '.join(spec.choices)}")
        elif spec.kind == "list":
            if (
                not isinstance(value, list)
                or len(value) > 100
                or not all(isinstance(v, str) and 0 < len(v.strip()) <= 1000 for v in value)
            ):
                raise ValueError(f"{label}: a list of at most 100 entries")
            value = [v.strip() for v in value]
        elif spec.kind == "relations":
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError(f"{label}: a list of at most 100 links")
            for link in value:
                if (
                    not isinstance(link, dict)
                    or link.get("kind") not in R.RELATIONS
                    or not str(link.get("target", "")).strip()
                ):
                    raise ValueError(f"{label}: each link has a kind ({', '.join(R.RELATIONS)}) and a target")
        elif spec.kind == "rows":
            columns = {c["key"]: c for c in spec.columns}
            if not isinstance(value, list) or len(value) > 200:
                raise ValueError(f"{label}: a list of at most 200 rows")
            for row in value:
                if not isinstance(row, dict) or set(row) - set(columns):
                    raise ValueError(f"{label}: rows have only {', '.join(columns)}")
                for column, cell in row.items():
                    choices = columns[column].get("choices")
                    if choices and cell not in (None, "", *choices):
                        raise ValueError(
                            f"{label}: {columns[column]['label']} is one of {', '.join(choices)}"
                        )
        clean[key] = value
    for key, reason in absent.items():
        if reason not in R.ABSENT:
            raise ValueError(f"{key}: absence must be one of {', '.join(R.ABSENT)}")
        if key in clean:
            raise ValueError(f"{key}: given and marked absent")
        if key in ESSENTIAL:
            raise ValueError(f"{by_key[key].label}: must be given; it cannot be marked absent")
    return clean, dict(absent)


def suggestions_view(suggestions: dict) -> dict:
    """Suggestions read from a Nigerian document (DETAILS-1 keys) under this view's keys."""
    out = {}
    for old, new in LEGACY.items():
        hint = suggestions.get(old)
        if not hint:
            continue
        value = hint.get("value")
        if old == "relations" and value:
            value = [
                {**link, "kind": {"cross_reference": "related"}.get(link["kind"], link["kind"])}
                for link in value
            ]
        elif new in LEGACY_VALUES and value in LEGACY_VALUES[new]:
            value = LEGACY_VALUES[new][value]
        out[new] = {**hint, **({"value": value} if "value" in hint else {})}
    if "gazette_date" in suggestions:
        out["step:published.date"] = suggestions["gazette_date"]
    return out


# --- first drafts: legal facts suggested, never filled in ------------------------------------------------

# What a document is worth as law and when: each language version's authority, its rule where they
# differ, when it commences, what it does to other laws, whether it is current, whether a treaty's party
# list is complete. A reader may suggest each, with the words that state it; none is a field's value
# until a reviewer chooses it (owner decision 2026-10-21). The details as printed (titles, numbers, a
# date printed for a step of its making, the gazette and its pages) are filled in for the reviewer to
# check; what a reader infers rather than reads where it is printed for its field is suggested too,
# never filled in.
LEGAL = {
    "step:commenced.date",
    "event:commenced.date",
    "profile.commencement",
    "relations",
    "status.legal",
    "status.as_of",
    "status.note",
    "profile.parties_complete_as_of",
    "profile.parties_complete_evidence.url",
    "profile.parties_complete_evidence.quote",
}
AUTHORITY = ("authority", "authority_page", "authority_words", "authority_url")


def is_legal(key: str) -> bool:
    return key in LEGAL or key.startswith("language_rule.")


def first_draft(values: dict, absent: dict) -> tuple[dict, dict]:
    """A first draft from what a reader filled in: the details as printed, for the reviewer to check;
    the legal facts, a version's authority among them, left for the reviewer to choose; the legal
    status not yet checked (unknown, which states nothing)."""
    draft = {key: value for key, value in values.items() if not is_legal(key)}
    if isinstance(draft.get("expressions"), list):
        draft["expressions"] = [
            {key: cell for key, cell in row.items() if key not in AUTHORITY} for row in draft["expressions"]
        ]
    draft["status.legal"] = "unknown"
    return draft, {key: reason for key, reason in absent.items() if not is_legal(key)}


def suggestions(document: dict) -> dict:
    """The legal facts suggested for a document, keyed as its fields are, each with the words that state
    it and their page, or where else it was read: when it commences and what each language version is
    worth (under "expressions", the authority by language), read from its text; whether it is current,
    as a later text of its collection or its listing says: where both speak, the later text, a primary
    source, leads with the words of its provision that repeals it ("source": which law, and the page), and
    the listing, a secondary source, is noted beside it ("secondary"). With them, what its reader inferred
    rather than read where it is printed for its field (a date printed once for several steps, a step that
    did not happen), with the words it rests on. Shown beside the empty fields for the reviewer to choose;
    never filled in."""
    out = {
        # The details as printed, filled into the first draft, with the page and words each was read from
        # (the National Industrial Court's reader gives them; shown beside the filled field).
        **(document.get("suggested_evidence") or {}),
        **(document.get("suggested_inferred") or {}),
        **(document.get("suggested_legal") or {}),
    }
    listing = out.get("status.legal") or {}
    listed = listing.get("value")
    said = [(out.get("status.note") or {}).get("value")]
    links = document.get("relations") or []
    current = document.get("currency") or {}
    repealers = current.get("repealed_by") or [
        {"title": link["target"]} for link in links if link["kind"] == "repealed_by"
    ]
    lead = None
    if repealers:
        names = "; ".join(r["title"] for r in repealers)
        quoted = next((r for r in repealers if r.get("words")), None)
        page = f" (p. {quoted['page']})" if quoted and quoted.get("page") else ""
        said = [f"Repealed by {names}, as its text says{page}."]
        lead = {"value": "repealed", "page": None}  # the page is the other law's, under "source"
        if quoted:
            source = {key: quoted[key] for key in ("work", "title", "page") if quoted.get(key)}
            lead |= {"evidence": quoted["words"], "source": source}
        if listed not in (None, "unknown"):
            lead["secondary"] = {"value": listed, "evidence": listing.get("evidence")}
            if listed == "repealed":
                said.append(f"A secondary source agrees: {listing.get('evidence')}")
            else:
                said.append(f"A secondary source says otherwise: {listing.get('evidence')}")
                said.append("Check which is right.")
        if current.get("latest"):
            said.append(
                f"That law was repealed in turn; the law in force now is {current['latest']['title']}."
            )
    for parent in current.get("parents_repealed") or []:
        names = "; ".join(r["title"] for r in parent["repealed_by"])
        said.append(
            f"Made under {parent['title']}, which {names} has since repealed: check it is still in force."
        )
    note = " ".join(filter(None, said))
    if note:
        out["status.note"] = {"value": note, "page": None, "evidence": note}
    if lead:
        out["status.legal"] = {**lead, "evidence": lead.get("evidence") or note}
    elif note and "status.legal" in out:
        out["status.legal"] = {**out["status.legal"], "evidence": note}
    return out


def reader_suggested(document: dict) -> bool:
    """Whether the document's collection reader suggested its details in this view's keys (Rwanda's
    gazette reader, the National Industrial Court's page reader): its first draft is drawn from those,
    never from the DETAILS-1 suggestions written for Nigerian Acts."""
    suggested = document.get("suggested") or {}
    family = suggested.get("identity.family")
    return family in R.FAMILIES and suggested.get("identity.type") in R.FAMILIES[family][1]


def base_values(document: dict, manifest: dict) -> dict:
    """A first draft for a document from a jurisdiction we have no extractor for yet, with the details
    its collection's reader filled in, if any (Rwanda's gazette reader); never its legal facts."""
    kind = document.get("type", "act")
    family = family_for(kind) or "official_document"
    kinds = R.FAMILIES[family][1]
    values = {
        "identity.family": family,
        "identity.type": document.get("type") if document.get("type") in kinds else kinds[0],
        "identity.title": document.get("title", ""),
        "classification.hierarchy": HIERARCHY[family],
        "status.legal": "unknown",
        "text.version.label": "As enacted" if family in ("legislation", "constitution") else "As published",
    }
    if document.get("year"):
        values["identity.year"] = str(document["year"])
    # Details read from the document itself by its collection's extractor (Rwanda's gazette reader).
    # Its links to other laws and whether it is current are legal facts: suggestions(), not values.
    values.update(usable_suggestions(document)[0])
    if document.get("language") and isinstance(values.get("expressions"), list):
        values["expressions"] = [
            {**row, "this_text": "yes" if row.get("language") == document["language"] else "no"}
            for row in values["expressions"]
        ]
    return first_draft({k: v for k, v in values.items() if v}, {})[0]


def first_absent(values: dict, keys=()) -> dict:
    """What a first draft accounts for without a value where its source is certain to print none, rather
    than each of thousands waiting for a reviewer to say it (owner, 2026-10-30); the reviewer can still
    give one. Of the fields shown (``keys``):
    - a judgment that prints no neutral citation (the National Industrial Court's portal prints none);
    - a law's bill, which its gazetted text never names, and its revised edition, which it is not in yet,
      as Nigeria's Acts are drafted (workbench.details).
    What a reader inferred stays a suggestion beside its field, never marked here (owner, 2026-10-21)."""
    out = {}
    if values.get("identity.family") == "judgment" and not values.get("identity.citation"):
        out["identity.citation"] = "not_in_source"
    if "profile.bill" in keys and empty(values.get("profile.bill")):
        out["profile.bill"] = "not_in_source"
    if "profile.revised_edition" in keys and empty(values.get("profile.revised_edition")):
        out["profile.revised_edition"] = "not_applicable"
    return out


def place_for(document: dict, manifest: dict, profiles: dict) -> tuple[R.Place, JurisdictionProfile | None]:
    code = document.get("jurisdiction") or manifest.get("jurisdiction") or "ng"
    profile = profiles.get(code)
    level = profile.level if profile else "national"
    return R.Place(code=code, name=profile.name if profile else code.upper(), level=level), profile
