"""Names as Nigerian cases and laws are cited, from the capitals a court or Gazette prints them in.

"ABUGH TERSEER OTHNIEL -VS- THE NIGERIA POLICE FORCE & ORS" is cited "Abugh Terseer Othniel v. The Nigeria
Police Force & Anor" (two defendants), its judge "I.M.M. SAULAWA, JSC" as "I.M.M. Saulawa, JSC". The convention
agreed in review on 2026-11-16: the case name, a version's title, the parties and the bench in title case, as the
law reports write them; the full case name keeps the names as printed.

Text already in mixed case was typed by someone who chose its capitals, and is left as it is. A word whose
capitals cannot be decided (a short word that may be an acronym or a name: "UBA", "ADE") keeps them and is
returned as doubtful, for a reviewer to check against the page.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Acronyms and post-nominals kept in capitals: those the courts' catalogues print in capitals inside
# mixed-case text, or in brackets after a name, and never as a word (read 2026-11-16 from the Supreme
# Court's and the National Industrial Court's catalogues; names and words among them left out by hand).
ACRONYMS = frozenset(
    """
    ACN ACP ACM ACSC ADR AEDC AES AFBTE AGF AHNI AIDS ALSCON AMCON AMLSN AMO APC APER APLORI APM ASC ASCSN
    ASP ASSBIFI ASUP ASURI ASUU ATO AUPCTRE BEDC BGP BITRA BOC BPE CAC CAO CBN CCECC CDFIPSB CEC CFR CFTZ CISCON
    CJN CLO CMC CMRD CNPC CON CONRAISS CONTEC CORSIA CPN CRBC CRUDAN CRUTECH CSC DCP DDPP DHL DNA DPO DPP DPR
    DSP ECWA EEDC EFCC EHOAN EKEDC EKEDP ELA ENL EPSO ESBS ESCET ESPPC ESUT FAAN FBN FCDA FCMB FCT FEC FG FIIB
    FIRS FISSAN FLAILAS FMOJ FOCC FPSO FRN FSDH FTZE FUNAAB FUNNAB FUT FUTA GOCON GPC GTB HIV ICAO ICPC ICS
    ICT IDF IDSL IEI IGP IHS IKEDC IMT INEC IOSCO ISSAN ISSSAN ITAMLSN JAAC JEDC JOHESU JP JSC JCA KAEDC KEDCO
    LASACO LASU LAUTECH LFN LGA LGEA LLP LNG MAUTECH MDA MEPROSSAN MFB MFR MHWUN MOJ MOMTAN MOUN MPS MSF MTN
    MWUN MYTO NAAT NACA NACEST NAFDAC NAIRDA NANTS NAOC NAPO NAPPMED NAPTIP NASENI NASU NCAA NCAR NCDMB NDDC
    NDIC NDLEA NECO NELMCO NEMSA NERC NERDC NESI NEXIM NGOCE NHIA NHRC NICN NIIRA NIMASA NIMC NITDA NLC NMA
    NNPC NNPCL NSCDC NSITF NSML NSSHMB NTDC NTS NUAHP NUATE NUBIFIE NUBTE NUEE NUFBTE NUHPSW NUJ NULGE NUP
    NUPENG NURTW NUSDE NWLR NYSC OAU OAUSTECH OFR OHCSF OHCSGF OON OSGF PANS PARMC PDP PENGASSAN PENSGASSAN
    PFA PHB PHC PHEDC PHED PLASCHEMA PMAN PPMC PPMCA PRODA PSC PSN PSNL PTAD PTECSSAN RATTAWU RRBN RTEAN
    RTICUL SAN SDP SEC SEEPCO SEWUN SGS SIELTE SNECOU SPDC SSANIP SSANU SSAUTHRIAI SSAUTHRIAL SSC SSP SUBEB
    AG NTA HRH HRM VAT SIM
    TCN TEPAN TOAN TRCN TUC UACN UBA UIDC UK UNAAB UNICEM UNILAG UNIPORT USA UTTAWAN WAEC YEDC
    """.split()
)

# Written as the law reports write them, not in title case.
FORMS = {
    "PLC": "Plc",
    "LTD": "Ltd",
    "NIG": "Nig",
    "CO": "Co",
    "INC": "Inc",
    "CORP": "Corp",
    "ESQ": "Esq",
    "RTD": "Rtd",
    "PHD": "PhD",
    "ORS": "Ors",
    "ANOR": "Anor",
    # Titles and ranks, as written before a name.
    "MR": "Mr",
    "MRS": "Mrs",
    "MS": "Ms",
    "DR": "Dr",
    "PROF": "Prof",
    "ENGR": "Engr",
    "ALH": "Alh",
    "REV": "Rev",
    "GEN": "Gen",
    "COL": "Col",
    "MAJ": "Maj",
    "CAPT": "Capt",
    "SGT": "Sgt",
    "CPL": "Cpl",
    "LT": "Lt",
    "ST": "St",
    "JNR": "Jnr",
    "SNR": "Snr",
    "BRIG": "Brig",
    "MOH": "Moh",
}

# Lower case inside a name ("Federal Republic of Nigeria"), never first.
SMALL = frozenset("of and the for in on at by to a an or with from under into upon de".split())

# Short words written as words: they keep no capitals and raise no doubt.
WORDS = frozenset(
    """
    air all any arm art as bay big boy car dam day die end ex far fed gas her his hon inn job joy key law man
    may new no non old one own red re sea sir sky son sun ten to top two up way war
    aid awe bar can cup dim eco elf hot ice lot mix oil out pan pen pro tin
    act age ban bid box bus bye cut due etc fee fix gap kit low map net pay per put raw row run set six tax
    use via jan feb mar apr jun jul aug sep oct nov dec jos
    aba abu ada ade ado aji al ala ali ani aye ayo ben bob chi dan ede edo edu eke eko ene eni eso eyo ewu
    eze ibe idi ike ila ilu ime imo ini isa ita ivy jim joe kay ken kio mai mba obe obi oba odo odu oji ojo
    oke oko ola olu onu osu otu owo oyo ray sam tom udo ugo uka uwa uyo
    """.split()
)


ROMAN = re.compile(r"(?=[XVI])X{0,3}(?:IX|IV|V?I{0,3})")
INITIALS = re.compile(r"\.?[A-Z](?:\.[A-Z])*\.?")
ORDINAL = re.compile(r"(\d+)(ST|ND|RD|TH)")
VOWEL = re.compile(r"[AEIOUY]")
TOKEN = re.compile(r"(\s+|[-–—/&]|\.(?=\s|$)|[,;:()\[\]\"“”‘])")


def printed_in_capitals(text: str) -> bool:
    """Whether a name is printed in capitals: most of its letters are, a "PhD" or "Esq" aside."""
    upper = sum(c.isupper() for c in text)
    lower = sum(c.islower() for c in text)
    return upper >= 2 and upper >= 3 * lower


def _word(word: str, first: bool, doubtful: list[str]) -> str:
    """One word printed in capitals, as it is cited."""
    if not word or any(c.islower() for c in word) or not any(c.isalpha() for c in word):
        return word
    if "'" in word or "’" in word:
        mark = "'" if "'" in word else "’"
        head, _, tail = word.partition(mark)
        tail = tail.lower() if len(tail) == 1 else _word(tail, True, doubtful)  # "COMPANY'S", "O'NEIL"
        return _word(head, first, doubtful) + mark + tail
    if word in FORMS:
        return FORMS[word]
    if word in ACRONYMS or INITIALS.fullmatch(word) or ROMAN.fullmatch(word):
        return word
    ordinal = ORDINAL.fullmatch(word)
    if ordinal:
        return ordinal.group(1) + ordinal.group(2).lower()
    if any(c.isdigit() for c in word):
        return word
    lower = word.lower()
    if lower in SMALL and not first:
        return lower
    if not VOWEL.search(word):  # NNPC, FRN
        return word
    if len(word) <= 3 and lower not in WORDS and lower not in SMALL:
        doubtful.append(word)
        return word
    if word.startswith("MC") and len(word) > 3:
        return "Mc" + word[2:].capitalize()
    return word.capitalize()


def cited(text: str) -> tuple[str, list[str]]:
    """A name as it is cited, and the words whose capitals were kept because they could not be decided."""
    if not text or not printed_in_capitals(text):
        return text, []
    doubtful: list[str] = []
    out, first = [], True
    for part in TOKEN.split(text):
        if not part:
            continue
        if TOKEN.fullmatch(part):
            out.append(part)
            if part.strip() in ("(", "[", "&", "/", '"', "“", "-", "–", "—", ",", ";", ":"):
                first = part.strip() in ("(", "[", "&", '"', "“")
            continue
        out.append(_word(part, first, doubtful))
        first = False
    return "".join(out), doubtful


# Between the sides: "-VS-", "VS", "VS.", "VERSUS", a "v." already written, or a capital "V" or "V." after a
# word and before a word, never before an initial: "BARR. V. C. CHUKWUANI" is a name ("JOHN V. ADEBAYO",
# a middle initial before a surname, would be read as two sides; none is in the courts' catalogues).
VERSUS = re.compile(
    r"\s*-\s*VS\.?\s*-\s*|\s+(?:VS\.?|VERSUS)\s+|\s+(?-i:v)\.?\s+|(?<=[A-Za-z]{2})\s+(?-i:V)\.?\s+(?![A-Z]\.)",
    re.I,
)
OTHERS = re.compile(
    r"\s*(?:&|\band\b)\s*(?P<number>\d+\s*)?(?P<word>ORS|ANOR|OTHERS|OTHS?|ANOTHER)\.?\s*$", re.I
)


def sides_of(parties: list[dict] | None) -> list[int]:
    """How many parties each side has, in the order the rows give them (a side is a run of one role)."""
    counts: list[int] = []
    last = object()
    for row in parties or []:
        if row.get("role") != last:
            counts.append(0)
            last = row.get("role")
        counts[-1] += 1
    return counts


def case_name(title: str, parties: list[dict] | None = None) -> tuple[str, list[str]]:
    """A case's name as it is cited: "A v. B & Anor". Where a side is printed with its other parties
    ("& ORS") and the parties are known by side, "& Anor" or "& Ors" follows how many more that side has:
    the portal prints "& ORS" for two."""
    if not title:
        return title, []
    sides = [side for side in (part.strip(" -") for part in VERSUS.split(title.strip())) if side]
    counts = sides_of(parties)
    known = len(counts) == len(sides)
    out, doubtful = [], []
    for k, side in enumerate(sides):
        more = OTHERS.search(side)
        named, unsure = cited(side[: more.start()].strip() if more else side)
        doubtful += unsure
        if more and more.group("number"):  # "& 2 ORS": the number as printed
            named += f" & {more.group('number').strip()} Ors"
        elif more:
            printed = more.group("word").upper()
            two = counts[k] == 2 if known and counts[k] > 1 else printed in ("ANOR", "ANOTHER")
            named += " & Anor" if two else " & Ors"
        out.append(named)
    return " v. ".join(out), doubtful


# --- a reader's suggested details ----------------------------------------------------------------------

NAMED = {"profile.parties": ("name", "represented_by"), "profile.bench": ("name",)}


def is_cited_here(document: dict) -> bool:
    """Whether a document's names are cited this way: Nigeria's, in English (Rwanda's Kinyarwanda and
    French titles keep their own rules: ingestion.rw_gazette.sentence_case)."""
    return (document.get("jurisdiction") or "").lower() == "ng" and (document.get("language") or "en") == "en"


def title_of(title: str, values: dict) -> tuple[str, list[str]]:
    """A document's title as it is cited: a case's name (a judgment, or a title naming two sides), or a
    law's title."""
    if values.get("identity.family") == "judgment" or VERSUS.search(title or ""):
        return case_name(title, values.get("profile.parties"))
    return cited(title)


@lru_cache(maxsize=65536)
def listed_title(title: str) -> str:
    """A title as a list shows it, from the title alone: the workbench lists tens of thousands."""
    return title_of(title, {})[0]


def cited_values(values: dict) -> tuple[dict, dict[str, list[str]]]:
    """A reader's suggested details with the names as they are cited, and by field the words whose
    capitals were kept because they could not be decided. The full case name keeps the names as printed."""
    out, doubtful = dict(values), {}
    for key, columns in NAMED.items():
        rows = values.get(key)
        if not isinstance(rows, list):
            continue
        cased = []
        for row in rows:
            row = dict(row) if isinstance(row, dict) else row
            for column in columns:
                if isinstance(row, dict) and isinstance(row.get(column), str):
                    row[column], unsure = cited(row[column])
                    doubtful.setdefault(key, []).extend(unsure)
            cased.append(row)
        out[key] = cased
    if isinstance(values.get("identity.title"), str):
        out["identity.title"], unsure = title_of(values["identity.title"], values)
        doubtful.setdefault("identity.title", []).extend(unsure)
    return out, {key: sorted(set(words)) for key, words in doubtful.items() if words}


def cited_suggestions(values: dict, evidence: dict) -> tuple[dict, dict]:
    """The values and their evidence as cited: each field's evidence keeps the words as printed, and
    names the capitals kept for the reviewer to check ("doubtful")."""
    cased, doubtful = cited_values(values)
    evidence = dict(evidence)
    for key in (*NAMED, "identity.title"):
        if key in evidence and isinstance(evidence[key], dict) and "value" in evidence[key]:
            entry = {**evidence[key], "value": cited_values({**values, key: evidence[key]["value"]})[0][key]}
            if doubtful.get(key):
                entry["doubtful"] = doubtful[key]
            evidence[key] = entry
    return cased, evidence


def cited_document(document: dict) -> dict:
    """A Nigerian document as the workbench reads it, its reader's suggestions as cited: collections
    prepared before 2026-11-16 hold them as printed. Text already in mixed case is left as it is, so a
    collection prepared since is unchanged."""
    if not is_cited_here(document) or not isinstance(document.get("suggested"), dict):
        return document
    values, evidence = cited_suggestions(document["suggested"], document.get("suggested_evidence") or {})
    return {**document, "suggested": values, "suggested_evidence": evidence}
