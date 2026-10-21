"""Document record contract (``document-record-1``): one shape for every legal document in Africa.

It covers constitutions, legislation, bills, subsidiary legislation and executive instruments,
judgments and other decisions, gazettes and their notices, treaties and the instruments of
regional bodies, soft law, parliamentary records and other official documents, from every
jurisdiction: the continent, regional communities, countries, and their states, provinces,
counties and municipalities.

The record has three layers:

* the core every document has: identity, place, who issued it, its language versions (what each is
  worth as law, and the document's rule where they differ), the events
  of its life (passed, assented, published, commenced; filed, heard, decided; adopted, ratified,
  entered into force), where it was published, its legal status, its links to other documents,
  the form and version of the text, and its classification;
* a profile per family of documents adding what only that family has: a bill's stage and
  sponsor, an instrument's enabling law, a judgment's court, parties, bench and outcome, a
  treaty's parties and ratifications, a regional instrument's legal effect;
* the jurisdiction's profile (schema/jurisdictions.py), saying how law is made and published
  there, which steps a law is expected to go through, and how it is numbered and cited.

Every detail is either given or accounted for: ``absent`` maps a detail's path to why it is not
given (not stated in the source, not applicable, not yet known, or withheld, e.g. to protect a
child's identity). ``missing()`` lists what is neither, for a reviewer to resolve before the
record is confirmed. Dates are ISO 8601 with the printed form kept, so a date printed in the
Ethiopian calendar or in French keeps its original wording. Identifiers are Akoma Ntoso FRBR
URIs as recommended for African materials (AKN4Africa): ``/akn/ng/act/2022/8``,
``/akn/ke/judgment/kehc/2019/5``, ``/akn/aa-ecowas/act/supplementary-act/2010/1``.
"""

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

SCHEMA_VERSION = "document-record-1"


class RecordModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


Text = Annotated[str, Field(min_length=1, max_length=4000)]
Short = Annotated[str, Field(min_length=1, max_length=300)]
LanguageTag = Annotated[str, Field(pattern=r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")]  # BCP 47
ScriptCode = Annotated[str, Field(pattern=r"^[A-Z][a-z]{3}$")]  # ISO 15924: Latn, Arab, Ethi, Tfng
# Akoma Ntoso place: ISO 3166-1 alpha-2, optionally a locality ("ng-la", "ke-001", "za-ec443"); regional
# bodies use the reserved "aa" ("aa-au", "aa-ecowas").
PlaceCode = Annotated[str, Field(pattern=r"^[a-z]{2}(?:-[a-z0-9]{1,12})?$")]

# --- families and types -----------------------------------------------------------------------------

# Each family: its Akoma Ntoso document type and the kinds of document in it.
FAMILIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "constitution": ("act", ("constitution", "constitutional_amendment")),
    "legislation": ("act", ("act", "law", "organic_law", "ordinance", "proclamation", "decree_law", "code")),
    "bill": ("bill", ("bill",)),
    "subsidiary_legislation": (
        "act",
        (
            "regulations",
            "rules",
            "order",
            "statutory_instrument",
            "legislative_instrument",
            "legal_notice",
            "government_notice",
            "board_notice",
            "by_law",
            "rules_of_court",
            "practice_direction",
        ),
    ),
    "executive_instrument": (
        "act",
        (
            "decree",
            "royal_decree",
            "executive_order",
            "presidential_order",  # Rwanda's Iteka rya Perezida, the DRC's ordonnance
            "prime_ministers_order",
            "ministerial_order",
            "instructions",
            "circular",
        ),
    ),
    "judgment": ("judgment", ("judgment", "ruling", "order", "advisory_opinion", "decision", "award")),
    "gazette": ("officialGazette", ("gazette", "supplement")),
    "gazette_notice": ("doc", ("notice",)),
    "treaty": ("act", ("treaty", "convention", "protocol", "charter", "agreement")),
    "regional_instrument": (
        "act",
        (
            "regulation",
            "directive",
            "decision",
            "supplementary_act",
            "uniform_act",
            "community_act",
            "protocol",
        ),
    ),
    "soft_law": (
        "statement",
        (
            "resolution",
            "guideline",
            "declaration",
            "model_law",
            "recommendation",
            "general_comment",
            "principles",
        ),
    ),
    "parliamentary_record": (
        "debate",
        ("debate", "votes_and_proceedings", "order_paper", "committee_report"),
    ),
    "official_document": (
        "doc",
        ("policy", "report", "guidance", "regulator_decision", "explanatory_note", "other"),
    ),
}
Family = Literal[*FAMILIES]

ABSENT = {
    "not_in_source": "Not stated in this copy",
    "not_applicable": "Not applicable",
    "unknown": "Not yet known",
    "withheld": "Withheld",  # e.g. a child's or a victim's identity
}
Absence = Literal[*ABSENT]

# --- core building blocks ---------------------------------------------------------------------------


class Absent(RecordModel):
    reason: Absence
    note: Short | None = None


class Evidence(RecordModel):
    """Where a detail was read or who supplied it."""

    source: Literal["document", "reviewer", "registry", "manifest", "external"]
    page: int | None = Field(default=None, ge=1)
    quote: Annotated[str, Field(max_length=400)] | None = None
    url: HttpUrl | None = None


class Dated(RecordModel):
    """A date as normalised (ISO 8601) and as printed, in the calendar it was printed in."""

    value: date | None = None
    printed: Short | None = None
    calendar: Literal["gregorian", "ethiopian", "islamic", "other"] = "gregorian"
    precision: Literal["day", "month", "year"] = "day"

    @model_validator(mode="after")
    def complete(self):
        if self.value is None and not self.printed:
            raise ValueError("a date needs its value or its printed form")
        if self.calendar != "gregorian" and not self.printed:
            raise ValueError("a date from another calendar keeps its printed form")
        return self


BODY_KINDS = (
    "legislature",
    "chamber",
    "head_of_state",
    "executive",
    "minister",
    "court",
    "tribunal",
    "regulator",
    "municipality",
    "regional_body",
    "international_organisation",
    "constituent_assembly",
    "electorate",
    "other",
)


class Body(RecordModel):
    """An institution: a legislature, a chamber, a ministry, a court, a regional organ."""

    name: Short
    kind: Literal[*BODY_KINDS]
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,80}$")] | None = None


class Person(RecordModel):
    """Someone acting in a document as printed: a signatory, a judge, a sponsor."""

    name: Short
    office: Short | None = (
        None  # "President of the Federal Republic of Nigeria", "Clerk to the National Assembly"
    )


class Place(RecordModel):
    code: PlaceCode
    name: Short
    level: Literal["continental", "regional", "national", "subnational", "local"]


NUMBER_SCHEMES = (
    "act",
    "bill",
    "law",
    "decree",
    "notice",
    "instrument",
    "case",
    "gazette",
    "treaty",
    "chapter",
    "other",
)


class Number(RecordModel):
    scheme: Literal[*NUMBER_SCHEMES]
    value: Short  # normalised: "8", "2023-012", "1234/2021", "E009 of 2022"
    printed: Short | None = None  # as printed: "Act No. 8", "Loi n° 2023-012", "Proclamation No. 1234/2021"


CITATION_KINDS = ("official", "neutral", "law_report", "revised_edition", "popular", "other")


class Citation(RecordModel):
    kind: Literal[*CITATION_KINDS]
    text: Short  # "Copyright Act, No. 8, 2022"; "[2019] KEHC 5 (KLR)"; "Cap. C28, LFN 2004"
    scheme: Short | None = None  # a law report or citation scheme: "KLR", "NWLR", "SALR"


class Identity(RecordModel):
    family: Family
    type: Annotated[str, Field(pattern=r"^[a-z_]+$")]
    local_subtype: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,20}$")] | None = (
        None  # "si", "ln", "gn", "by-law"
    )
    work_uri: Annotated[
        str, Field(pattern=r"^/akn/[a-z]{2}(?:-[a-z0-9]{1,12})?/[a-zA-Z]+(?:/[a-z0-9._-]+)+$")
    ]
    title: Short  # short title, or a case's short name: "Copyright Act, 2022"; "Kenya Airways v Mwangi"
    full_title: Text | None = None  # long title, or full case name with all parties
    alternative_titles: tuple[Short, ...] = ()
    numbers: tuple[Number, ...] = ()
    year: int | None = Field(default=None, ge=1800, le=2200)
    citations: tuple[Citation, ...] = ()

    @model_validator(mode="after")
    def known_type(self):
        doctype, kinds = FAMILIES[self.family]
        if self.type not in kinds:
            raise ValueError(f"{self.type} is not a kind of {self.family}: {', '.join(kinds)}")
        if self.work_uri.split("/")[3] != doctype:
            raise ValueError(
                f"a {self.family} is an Akoma Ntoso {doctype}, not {self.work_uri.split('/')[3]}"
            )
        return self


EXPRESSION_AUTHORITY = (
    "authoritative",  # the only authoritative language (e.g. Amharic for Ethiopian proclamations)
    "equally_authentic",  # one of several equally authentic texts (e.g. English and French in Cameroon)
    "official_translation",
    "unofficial_translation",
    "machine_translation",
)


class Expression(RecordModel):
    """A language version of the document, and what its text is worth as law where that is recorded."""

    language: LanguageTag
    script: ScriptCode | None = None
    direction: Literal["ltr", "rtl"] = "ltr"
    # As the document or another official source states it, and where; None when not recorded, which the
    # public catalogue answers as unknown. Never inferred from the language a text was read in.
    authority: Literal[*EXPRESSION_AUTHORITY] | None = None
    authority_evidence: Evidence | None = None
    this_text: bool = False  # the language version this record's text is
    translator: Short | None = None
    expression_uri: str | None = None

    @model_validator(mode="after")
    def direction_follows_script(self):
        if self.script in {"Arab", "Hebr", "Syrc", "Thaa", "Nkoo", "Adlm"} and self.direction != "rtl":
            raise ValueError(f"{self.script} is written right to left")
        return self

    @model_validator(mode="after")
    def evidence_of_an_authority(self):
        if self.authority_evidence is not None and self.authority is None:
            raise ValueError("evidence of a language version's authority needs the authority")
        return self


class LanguageRule(RecordModel):
    """The instrument's own rule for divergence between its language versions, where it states one: the
    OHADA Treaty (art. 42) has documents take effect in French before translation and the French version
    prevail where versions differ. ``prevails`` is the language whose version prevails, when the rule
    names one; a rule making every version equally authentic names none."""

    rule: Text  # as the instrument states it
    prevails: LanguageTag | None = None
    provision: Short | None = None  # where it is stated: "Article 42"
    evidence: Evidence


EVENTS = (
    # how legislation is made and changed
    "introduced",
    "first_reading",
    "second_reading",
    "committee_stage",
    "third_reading",
    "passed",
    "concurred",
    "certified",
    "presented_for_assent",
    "assented",
    "withheld_assent",
    "promulgated",
    "signed",
    "made",
    "laid",
    "approved",
    "adopted",
    "published",
    "commenced",
    "amended",
    "repealed",
    "expired",
    "suspended",
    "annulled",
    "struck_down",
    "lapsed",
    "withdrawn",
    "referendum",
    # how a case is decided
    "filed",
    "heard",
    "reserved",
    "decided",
    "delivered",
    "corrected",
    "reported",
    # how a treaty binds
    "opened_for_signature",
    "ratified",
    "acceded",
    "entered_into_force",
    "denounced",
    "domesticated",
    # a gazette
    "issued",
)


class Event(RecordModel):
    """One step in the document's life, dated, with who acted and on which part."""

    kind: Literal[*EVENTS]
    when: Dated
    step: Annotated[str, Field(pattern=r"^[a-z0-9_:-]{1,60}$")] | None = (
        None  # the jurisdiction's step it fulfils
    )
    by: Body | None = None
    person: Person | None = None
    scope: Short | None = None  # "sections 1 to 10", "Part III"
    note: Short | None = None
    evidence: Evidence | None = None


PUBLICATION_KINDS = (
    "gazette",
    "gazette_supplement",
    "journal_officiel",
    "law_report",
    "court_record",
    "official_website",
    "revised_edition",
    "other",
)


class Publication(RecordModel):
    kind: Literal[*PUBLICATION_KINDS]
    name: Short  # "Federal Republic of Nigeria Official Gazette", "Journal officiel de la République du Sénégal"
    number: Short | None = None
    volume: Short | None = None
    part: Short | None = None  # "Part A", "Legislative Supplement"
    issue: Literal["ordinary", "extraordinary", "special", "supplement"] | None = None
    when: Dated | None = None
    place: Short | None = None
    pages: Short | None = None
    notice: Short | None = None  # "Government Notice No. 24", "L.N. 34 of 2023"
    publisher: Short | None = None
    url: HttpUrl | None = None


LEGAL_STATUS = (
    "in_force",
    "partly_in_force",
    "not_yet_in_force",
    "repealed",
    "expired",
    "superseded",
    "suspended",
    "struck_down",
    "lapsed",
    "withdrawn",
    "pending",  # a bill still before the legislature
    "historical",
    "decided",  # a judgment: see the judgment profile for its standing as precedent
    "unknown",
)


class Status(RecordModel):
    legal: Literal[*LEGAL_STATUS]
    as_of: date | None = None
    note: Text | None = None
    evidence: Evidence | None = None


RELATIONS = (
    "repeals",
    "repealed_by",
    "amends",
    "amended_by",
    "consolidates",
    "consolidated_in",
    "commences",
    "commenced_by",
    "made_under",
    "enables",
    "implements",
    "implemented_by",
    "transposes",
    "transposed_by",
    "domesticates",
    "domesticated_by",
    "ratifies",
    "supersedes",
    "superseded_by",
    "enacted_as",
    "bill_for",
    "published_in",
    "contains",
    "cites",
    "appeal_from",
    "appealed_to",
    "affirms",
    "reverses",
    "overrules",
    "overruled_by",
    "follows",
    "distinguishes",
    "related",
)


class Reference(RecordModel):
    """Another document: its work URI when known, and how it is cited."""

    citation: Short
    work_uri: str | None = None
    place: PlaceCode | None = None


class Relation(RecordModel):
    kind: Literal[*RELATIONS]
    target: Reference
    scope: Short | None = None  # "section 4", "Part II"
    when: Dated | None = None
    note: Short | None = None
    evidence: Evidence | None = None


TEXT_FORMS = (
    "gazette_publication",  # as published in a gazette or journal officiel
    "certified_copy",  # e.g. the Bill as passed and certified by the Clerk, before gazetting
    "official_reprint",
    "revised_edition",
    "law_report",
    "court_copy",  # the signed judgment as issued by the court
    "official_website",
    "unofficial",
)


class Version(RecordModel):
    label: Short  # "As enacted", "Consolidated to 1 March 2024", "As delivered"
    point_in_time: date | None = None
    consolidated: bool = False


class Summary(RecordModel):
    kind: Literal["explanatory_memorandum", "headnote", "abstract", "editorial"]
    text: Text
    official: bool  # part of the official document, or written by editors


class TextInfo(RecordModel):
    form: Literal[*TEXT_FORMS]
    version: Version
    summary: Summary | None = None


HIERARCHY = (
    "constitutional",
    "primary",
    "subsidiary",
    "executive",
    "judicial",
    "regional",
    "international",
    "soft_law",
    "parliamentary",
    "administrative",
)


class Subject(RecordModel):
    scheme: Short  # "oa-subjects-1", or a jurisdiction's official classification
    code: Short
    label: Short


class Classification(RecordModel):
    hierarchy: Literal[*HIERARCHY]
    subjects: tuple[Subject, ...] = ()
    keywords: tuple[Short, ...] = ()


# --- family profiles --------------------------------------------------------------------------------


class Profile(RecordModel):
    pass


class LegislationProfile(Profile):
    """Constitutions and their amendments, and primary legislation."""

    family: Literal["constitution", "legislation"]
    bill: Reference | None = None  # the Bill it was enacted from
    commencement: Literal["on_assent", "on_publication", "fixed_date", "by_notice", "staggered"] | None = None
    revised_edition: Citation | None = None  # its chapter in a revised edition: "Cap. C28, LFN 2004"


class BillProfile(Profile):
    family: Literal["bill"]
    stage: Literal[
        "introduced",
        "first_reading",
        "second_reading",
        "committee",
        "report",
        "third_reading",
        "passed",
        "assented",
        "published",
        "withdrawn",
        "lapsed",
        "negatived",
    ]
    originating_chamber: Body | None = None
    sponsor: Person | None = None
    sponsor_kind: Literal["government", "private_member", "committee", "other"] | None = None
    committee: Body | None = None
    enacted_as: Reference | None = None


class SubsidiaryProfile(Profile):
    """Instruments made under the authority of another law."""

    family: Literal["subsidiary_legislation"]
    made_under: tuple[Reference, ...] = Field(min_length=1)  # the enabling law and section
    maker: Body | None = None
    signed_by: Person | None = None
    parliamentary_control: Literal["laid", "affirmative", "negative", "none"] | None = None


class ExecutiveProfile(Profile):
    """Decrees, executive and ministerial orders made under the constitution or a law."""

    family: Literal["executive_instrument"]
    authority: Body
    basis: tuple[Reference, ...] = ()  # constitutional article or law relied on
    signed_by: Person | None = None
    countersigned_by: tuple[Person, ...] = ()


COURT_LEVELS = (
    "apex",
    "appellate",
    "superior",
    "subordinate",
    "specialised",
    "customary",
    "regional",
    "continental",
)


class Court(RecordModel):
    name: Short
    code: Annotated[str, Field(pattern=r"^[a-z0-9]{2,16}$")] | None = (
        None  # the court's citation code: "kehc", "zacc"
    )
    level: Literal[*COURT_LEVELS]
    division: Short | None = None
    seat: Short | None = None


PARTY_ROLES = (
    "appellant",
    "respondent",
    "applicant",
    "petitioner",
    "plaintiff",
    "claimant",
    "defendant",
    "accused",
    "prosecutor",
    "complainant",
    "interested_party",
    "intervener",
    "amicus_curiae",
    "state",
    "other",
)


class Party(RecordModel):
    name: Short  # as cited; "[Withheld]" when anonymised
    role: Literal[*PARTY_ROLES]
    anonymised: bool = False
    represented_by: tuple[Short, ...] = ()


class Judge(RecordModel):
    name: Short
    role: Literal["presiding", "member", "author", "concurring", "dissenting", "sole"]


OUTCOMES = (
    "allowed",
    "partly_allowed",
    "dismissed",
    "struck_out",
    "upheld",
    "set_aside",
    "varied",
    "remitted",
    "granted",
    "refused",
    "declared",
    "settled",
    "withdrawn",
    "other",
)


class JudgmentProfile(Profile):
    """Judgments, rulings, orders and opinions of courts and tribunals, national and regional."""

    family: Literal["judgment"]
    court: Court
    case_numbers: tuple[Short, ...] = ()
    parties: tuple[Party, ...] = ()
    bench: tuple[Judge, ...] = ()
    opinions: tuple[Judge, ...] = ()  # who wrote the majority, concurring and dissenting opinions
    outcome: tuple[Literal[*OUTCOMES], ...] = ()
    orders: tuple[Text, ...] = ()
    issues: tuple[Short, ...] = ()
    reportable: bool | None = None
    anonymisation: Literal["none", "partial", "full"] = "none"
    anonymisation_reason: Literal["child", "sexual_offence", "family", "security", "other"] | None = None
    precedent: Literal["binding", "persuasive", "not_binding", "unknown"] = "unknown"
    treatment: Literal["good_law", "overruled", "reversed", "doubted", "unknown"] = "unknown"


class GazetteProfile(Profile):
    family: Literal["gazette", "gazette_notice"]
    gazette: Publication
    contents: tuple[Reference, ...] = ()  # an issue's notices and supplements
    issuing_authority: Body | None = None  # for a notice


class TreatyParty(RecordModel):
    party: Short  # a state (place code) or an organisation
    place: PlaceCode | None = None
    signed: Dated | None = None
    ratified: Dated | None = None
    acceded: Dated | None = None
    denounced: Dated | None = None  # the state's own denunciation or withdrawal, as it took effect
    reservations: Text | None = None


class TreatyProfile(Profile):
    family: Literal["treaty"]
    adopted_by: Body | None = None
    adopted_at: Short | None = None  # place of adoption
    parties: tuple[TreatyParty, ...] = ()
    depositary: Short | None = None
    entry_into_force_rule: Text | None = None  # "thirty days after the fifteenth ratification"
    authentic_languages: tuple[LanguageTag, ...] = ()


LEGAL_EFFECTS = ("directly_applicable", "requires_transposition", "binding_on_addressees", "non_binding")


class RegionalProfile(Profile):
    """Instruments of regional communities: ECOWAS, EAC, SADC, OHADA, UEMOA, CEMAC and others."""

    family: Literal["regional_instrument"]
    organ: Body  # the Assembly, Council of Ministers, Authority of Heads of State, Commission
    legal_effect: Literal[*LEGAL_EFFECTS]
    member_states: tuple[PlaceCode, ...] = ()  # empty: every member
    transposition_deadline: date | None = None


class OtherProfile(Profile):
    """Soft law, parliamentary records and other official documents."""

    family: Literal["soft_law", "parliamentary_record", "official_document"]
    issuing_body: Body | None = None
    sitting: Short | None = None  # a parliamentary sitting or session


AnyProfile = Annotated[
    LegislationProfile
    | BillProfile
    | SubsidiaryProfile
    | ExecutiveProfile
    | JudgmentProfile
    | GazetteProfile
    | TreatyProfile
    | RegionalProfile
    | OtherProfile,
    Field(discriminator="family"),
]

# --- the record -------------------------------------------------------------------------------------

ABSENT_PATH = re.compile(r"^(?:[a-z_]+(?:\.[a-z_]+)*|event:[a-z_]+|step:[a-z0-9_:-]{1,60})$")


class DocumentRecord(RecordModel):
    schema_version: Literal["document-record-1"] = SCHEMA_VERSION
    identity: Identity
    place: Place
    issued_by: tuple[Body, ...] = ()
    expressions: tuple[Expression, ...] = Field(min_length=1)
    # Its rule for divergence between its language versions; absent["language_rule"] says why none is given.
    language_rule: LanguageRule | None = None
    events: tuple[Event, ...] = ()
    publications: tuple[Publication, ...] = ()
    status: Status
    relations: tuple[Relation, ...] = ()
    text: TextInfo
    classification: Classification
    profile: AnyProfile
    absent: dict[str, Absent] = {}  # detail path ("identity.full_title", "step:assent") -> why not given

    @model_validator(mode="after")
    def consistent(self):
        if self.profile.family != self.identity.family:
            raise ValueError(f"a {self.identity.family} record needs the {self.identity.family} profile")
        if not self.identity.work_uri.startswith(f"/akn/{self.place.code}/"):
            raise ValueError("the work URI must start with the record's place")
        if sum(e.this_text for e in self.expressions) != 1:
            raise ValueError("exactly one language version is this record's text")
        for path in self.absent:
            if not ABSENT_PATH.match(path):
                raise ValueError(f"not a detail path: {path}")
        return self


# What every family must give or account for before a record is confirmed (beyond the jurisdiction's
# expected steps). Paths name record fields; "event:<kind>" asks for an event of that kind.
REQUIRED_CORE = (
    "identity.full_title",
    "identity.numbers",
    "identity.citations",
    "issued_by",
    "publications",
    "text.summary",
)
REQUIRED: dict[str, tuple[str, ...]] = {
    "constitution": ("event:commenced",),
    "legislation": ("event:published", "event:commenced", "profile.bill", "profile.revised_edition"),
    "bill": ("event:introduced", "profile.sponsor", "profile.originating_chamber", "profile.committee"),
    "subsidiary_legislation": ("event:made", "event:published", "event:commenced", "profile.maker"),
    "executive_instrument": ("event:signed", "event:published", "profile.signed_by"),
    "judgment": (
        "event:decided",
        "profile.case_numbers",
        "profile.parties",
        "profile.bench",
        "profile.outcome",
    ),
    "gazette": ("event:issued",),
    "gazette_notice": ("event:published", "profile.issuing_authority"),
    "treaty": ("event:adopted", "event:entered_into_force", "profile.parties", "profile.depositary"),
    "regional_instrument": ("event:adopted", "event:published", "event:entered_into_force"),
    "soft_law": ("event:adopted", "profile.issuing_body"),
    "parliamentary_record": ("event:published", "profile.sitting"),
    "official_document": ("event:published", "profile.issuing_body"),
}


# And what only some kinds within a family must give: a constitution is adopted (by a constituent
# assembly or referendum); its amendments follow the jurisdiction's amendment procedure instead.
REQUIRED_BY_TYPE: dict[str, tuple[str, ...]] = {"constitution": ("event:adopted",)}


def _given(record: DocumentRecord, path: str) -> bool:
    if path.startswith("event:"):
        return any(e.kind == path[6:] for e in record.events)
    value = record
    for part in path.split("."):
        value = getattr(value, part, None)
    return value not in (None, (), "", [])


def missing(record: DocumentRecord, expected_steps: tuple[str, ...] = ()) -> list[str]:
    """Details neither given nor accounted for. `expected_steps` come from the jurisdiction's profile
    for this kind of document; each must be an event's `step` or be accounted for as "step:<id>"."""
    gaps = [
        path
        for path in (
            *REQUIRED_CORE,
            *REQUIRED[record.identity.family],
            *REQUIRED_BY_TYPE.get(record.identity.type, ()),
        )
        if not _given(record, path) and path not in record.absent
    ]
    done = {e.step for e in record.events if e.step}
    gaps += [
        f"step:{step}" for step in expected_steps if step not in done and f"step:{step}" not in record.absent
    ]
    return gaps


# ISO 639-1 to the three-letter ISO 639-2 (terminology) codes Akoma Ntoso uses in expression URIs;
# languages with no two-letter code are already three letters in BCP 47 ("zgh", "kab").
ISO_639_2 = {
    "en": "eng",
    "fr": "fra",
    "pt": "por",
    "ar": "ara",
    "es": "spa",
    "sw": "swa",
    "am": "amh",
    "ha": "hau",
    "yo": "yor",
    "ig": "ibo",
    "rw": "kin",
    "so": "som",
    "zu": "zul",
    "xh": "xho",
    "af": "afr",
    "st": "sot",
    "tn": "tsn",
    "ti": "tir",
    "om": "orm",
    "mg": "mlg",
    "rn": "run",
    "ny": "nya",
    "sn": "sna",
    "wo": "wol",
    "ln": "lin",
    "kg": "kon",
}


def work_uri(
    place: str,
    family: str,
    number: str,
    year: int | str,
    subtype: str | None = None,
    actor: str | None = None,
) -> str:
    """An Akoma Ntoso work URI: /akn/<place>/<doctype>[/<subtype>][/<actor>]/<year>/<number>."""
    doctype = FAMILIES[family][0]
    parts = ["", "akn", place, doctype, *(p for p in (subtype, actor) if p), str(year), slug(number)]
    return "/".join(parts)


def expression_uri(work: str, language: str, point_in_time: date | None = None) -> str:
    code = ISO_639_2.get(language.split("-")[0], language.split("-")[0])
    return f"{work}/{code}@{point_in_time.isoformat() if point_in_time else ''}"


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", value.lower()).strip("-") or "0"
