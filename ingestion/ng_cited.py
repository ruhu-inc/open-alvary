"""What a Nigerian instrument does to the earlier ones it names, and how those are found in the collection.

A later text repeals, revokes or amends an earlier one in a provision of its own: "24. (1) The Money
Laundering (Prohibition) Act 2004 is repealed.", "13.—(1) The Food Grade (Table or Cooking) Salt Regulations
2005 is revoked.", "Section 12 of the Chiefs (Appointment and Deposition) (Federal Capital Territory) Act is
repealed." (a part only: an amendment), "The National Crop Varieties … Act Cap N27 … (in this Act referred
to as "the Principal Act") is amended as set out in this Act."; in its long title ("An Act to repeal the
Electoral Act, No. 13, 2022 and enacts the Electoral Act, 2026 …"); in its explanatory memorandum ("This
Bill repeals …"); or in a schedule of enactments repealed ("Short title | Extent of repeal", "The whole
Act."). Subsidiary legislation names the Act it is made under in its enacting words ("In exercise of the
powers conferred on it by section 70 of the Nigerian Communications Act, 2003 …").

cited() reads these from a document's reading, each with the provision's words as printed and their page;
link_works() finds each cited instrument among the collection's documents and gives every law its links,
the instruments made under it and whether it is current (ingestion.linked.link), as Rwanda's have.

Nigeria has no official listing of the laws in force, so nothing is ever in force by this step: a law is
repealed where a later text of the collection says so, and otherwise not yet known. These are suggestions
for a reviewer, with the words they rest on; none is a record's value until a reviewer chooses it.

A wrong link would tell a reviewer that a law in force is repealed, so a link is made only where little
can go wrong, and a citation in doubt stays as printed, without a target:
- an instrument is found by its title and year together, and by its number where both print one. A
  citation by chapter of a revised edition ("Cap. E14 Laws of the Federation of Nigeria, 2004") gives the
  edition's year, not the instrument's: it has no target;
- a document answers to the name its own text says it may be cited as, else to its title or its listing's
  where that is a title of its own kind and year; a draft or a Bill answers to none and repeals nothing;
- an Act is found only among Acts and subsidiary legislation among subsidiary legislation, each within
  its own legislature (a Region's law is not the Federation's), and never a text later than the one citing it;
- a citation's name and year must be read the same by the check reading of its page, where there is one;
- a conditional mention ("where … is repealed"), a part ("section 13 of …") and a repeal with a limit
  ("in so far as it applies to …") repeal nothing: the last two are amendments.
"""

import re

from ingestion import cited_names, linked

# The last word of an instrument's short title.
_KIND = (
    r"Act|Decree|Edict|Law|Ordinance|Proclamation|Regulations?|Rules|Order(?:\s+in\s+Council)?"
    r"|Bye-?[Ll]aws|By-?[Ll]aws|Guidelines"
)
KIND_WORD = re.compile(rf"\b(?:{_KIND})\b")
PRIMARY = re.compile(r"^(?:act|decree|edict|law|ordinance|proclamation)$", re.I)
PRIMARY_TYPES = {"act", "law", "proclamation"}
# The kinds of document that are laws here: every kind but a court's decision and a Gazette record.
NOT_LAW = {"judgment", "ruling", "gazette"}
# The legislatures whose instruments the bound volumes hold, by the short name their ids carry
# (ingestion.ng_documents.VOLUMES); none for the Federation's.
ISSUERS = {"wr", "er", "csn", "nn"}
# A document that is not law yet, or never was: it answers to no name and repeals nothing.
NOT_ENACTED = re.compile(r"\b(?:draft|bill|exposure|proposed|consultation)\b", re.I)

# What follows a short title: its year, with its number where one is printed ("…Act, No. 13, 2022",
# "…Act No. 69 of 1993", "…Act, 1995 No 3", "…Regulations of 2005"); or its chapter in a revised edition
# of the laws, whose year is the edition's ("…Act Cap E14 Laws of the Federation of Nigeria, 2004",
# "…Act, Cap. N3 LFN, 2004", "…Ordinance (Cap. 163)").
_YEAR = r"(?:18|19|20)\d\d"
DATED = re.compile(
    rf"[\s,]*(?:(?:Act\s+)?No\.?\s*(?P<n1>\d{{1,3}})[\s,]*)?(?:of\s+)?(?P<year>{_YEAR})\b"
    rf"(?:[\s,]*No\.?\s*(?P<n2>\d{{1,3}})\b)?",
    re.I,
)
_EDITION = (
    rf"(?:of\s+the\s+)?(?:Laws\s+of\s+(?:the\s+)?[A-Z][A-Za-z ,]{{3,60}}?|L\.?\s?F\.?\s?N\.?),?\s*{_YEAR}\)?"
)
CHAPTER = re.compile(
    rf"[\s,(]*(?:(?:Cap|CAP|Chapter)\b\.?\s*[A-Z]?\.?\s?\d+[A-Z]?[.)]?(?:[\s,.]*{_EDITION})?|{_EDITION})"
)

# A title's words, walked back from its last: capitalised words, a part in brackets, and the small words
# between them.
TOKEN = re.compile(r"\([^()]{0,120}\)|[A-Za-z][A-Za-z'’-]*\.?|\d[\w./-]*|\S")
SMALL = {"of", "and", "the", "for", "in", "on", "to", "&", "etc.", "etc", "other"}
JOINED = {"of", "for", "in", "on", "to", "and"}  # before "the" inside a title: "Court of the Federal …"
_PART = (
    r"sections?|sub-?sections?|paragraphs?|sub-?paragraphs?|parts?|schedules?|regulations?|rules?|orders?"
    r"|clauses?|articles?|chapters?|items?|forms?|tables?|appendi(?:x|ces)|annex(?:es|ures?)?|provisos?"
    r"|definitions?"
)
# A part of the instrument named after it: "Section 12 of the", "The First Schedule to the".
PART_OF = re.compile(rf"\b(?:{_PART})\b[^.;:]{{0,100}}?\b(?:of|to|in)\s+(?:the\s+)?$", re.I)
PART_WORD = re.compile(rf"\b(?:{_PART})\b", re.I)
PART_FIRST = re.compile(
    rf"^(?:(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth)\s+)?(?:{_PART})\b"
    r".{0,40}?\b(?:of|to)\s+the\s+(?=[A-Z])",
    re.I,
)
# An instrument named only as what changed the one acted on: "… as amended by the … Regulations, 1956".
CHANGED_BY = re.compile(
    r"(?:\bas\s+)?\b(?:amended|modified|replaced|substituted|inserted|affected|extended)\s+by\s+(?:the\s+)?$",
    re.I,
)
# What may stand between an instrument's citation and what is done to it: a name given it in brackets
# ("(in this Act referred to as "the Principal Act")"), "as amended".
_FILLER = r"\s+|,|\([^()]{0,300}\)|\bas\s+amended\b"
# … and such a name whose bracket the printer never closed: '(in this Act referred to as "the Principal Act" is'.
_UNCLOSED = r"\([^()]{0,200}\b(?:referred\s+to|hereinafter|called)\b[^()]{0,100}"
FILLER = re.compile(rf"(?:{_FILLER})*(?:{_UNCLOSED})?")
BETWEEN = re.compile(rf"(?:{_FILLER})*(?:;|\band\b|\bor\b)?\s*")
# What is done: "is repealed", "are hereby revoked", "is amended as set out in this Act".
ACTION = re.compile(r"\b(?:is|are)\s+(?:hereby\s+)?(repealed|revoked|amended)\b")
# Said of a case, not done: "where a provision of the tax law … is repealed or amended".
CONDITION = re.compile(r"\b(?:where|if|when|whenever|until|unless|whether|as\s+if|in\s+the\s+event)\b", re.I)
# A repeal with a limit is an amendment: "… is hereby revoked, in so far as it applies to Iseyin Town."
LIMITED = re.compile(
    r"\s*,?\s*(?:in\s+so\s+far\s+as|insofar\s+as|to\s+the\s+extent|except|save\b|other\s+than"
    r"|with\s+the\s+exception|in\s+(?:its|their)\s+application|as\s+(?:it|they)\s+appl|only\b)",
    re.I,
)
# Where a sentence begins ("No. 5", "Cap. E14" and "etc. Act" do not end one).
_NOT_END = (
    r"(?<!\bNo)(?<!Nos)(?<!Cap)(?<!CAP)(?<!etc)(?<!Etc)(?<!ETC)(?<!\bs)(?<!\bss)(?<!\bL)(?<!\bF)(?<!\bN)"
)
SENTENCE_START = re.compile(rf"{_NOT_END}[.;:]\s+(?=[\"“(]?[A-Z0-9(])")
SENTENCE_END = re.compile(rf"{_NOT_END}[.;](?=\s|$)")
# A section's number after the marginal note read before it: "Repeal of Act No. 4, 2004 24.—(1) The …".
SECTION_START = re.compile(r"(?:(?<=\s)|^)\d{1,3}[A-Z]?\.\s*[—–-]+\s*(?:\(\d{1,2}\)\s*)?(?=[\"“(]?[A-Z])")
NUMBERING = re.compile(r"(?:\b\d{1,3}[A-Z]?\.\s*(?:[—–-]\s*)?)?(?:\(\d{1,2}[a-z]?\)\s*)?$")
# … and not the end of the sentence before it: "… by deleting sections 34, 35A and 35C. (9) The …".
NOT_NUMBERING = re.compile(
    r"(?:,|\b(?:and|or|to|of|under|by|in|sections?|subsections?|paragraphs?|regulations?|rules?|orders?|No\.?))\s*$",
    re.I,
)
# Words that name no instrument: "the Principal Act", "the said Order".
NO_TITLE = re.compile(
    r"^(?:principal|said|repealed|former|old|new|main|parent|amended|same|aforesaid|relevant|enabling|original)"
    r"\s+\w+(?:\s+in\s+council)?$",
    re.I,
)
# The long title, and the explanatory memorandum: "An Act to repeal the … Act, 2007 and enact …".
STATED = re.compile(
    r"\b(?:(?:An\s+Act|A\s+Bill\s+for\s+an\s+Act)\s+to\s+(?P<long>repeal|amend)(?:\s+and\s+re-?enact)?"
    r"|This\s+(?:Act|Bill)\s+(?P<memo>repeals|amends))\s+",
    re.I,
)
NAMED_NEXT = re.compile(
    rf"(?:the\s+)?(?P<title>(?:[^.;:]|(?<=[Ee]tc)\.|(?<=ETC)\.){{2,160}}?\b(?:{_KIND}))"
    rf"(?=\s*(?:,|\(|No\b|Cap\b|Laws\b|L\.?F\.?N|{_YEAR}|and\s+(?:to\s+)?(?:re-?)?enacts?\b|and\s+for\b"
    r"|to\s+\w|;|\.|$))",
    re.I,
)
ANOTHER = re.compile(r"[\s,]*(?:and\s+)?(?=the\s)", re.I)
# A list opened by what is done to all of it: "The following enactments are repealed —", each item one
# instrument ("(a) The Electoral Act 2002 ;"), an item's own words aside ("… and all subsequent amendments
# thereto").
LISTED = re.compile(r"\bfollowing\b[^.;]{0,60}$", re.I)
OPENS_LIST = re.compile(r"(?:\s*\([^()]{0,80}\))?(?:\s*,?\s*namely)?\s*[:—–-]*\s*$", re.I)
ITEM_ENDS = re.compile(
    r"(?:\s+and\s+all\s+(?:subsequent\s+)?amendments(?:\s+thereto)?)?[\s;,.]*(?:\band\b|\bor\b)?[\s;,.]*$",
    re.I,
)
ITEM_BEGINS = re.compile(r"^(?:\(?[a-z0-9]{1,4}\)\s*|\d{1,2}\.\s*)*(?:the\s+)?$", re.I)
# The Act subsidiary legislation is made under, in its enacting words.
ENACTING = re.compile(r"\bIn\s+exercise\s+of\s+the\s+powers?\s+conferred\b[^.;]{0,160}?\bby\s+", re.I)
ENACTING_ENDS = re.compile(r"\band\s+of\s+all\s+other\b|\bhereby\b|\bmakes?\s+the\s+following\b|;", re.I)
# A document's own name, in its own words: "This Act may be cited as the Electoral Act, 2010."
CITED_AS = re.compile(rf"\b(?:may|shall)\s+be\s+cited\s+as\s+(?:the\s+)?(.{{3,200}}?{_YEAR})\b", re.I | re.S)
# A name in full: a title ending in its kind, "(No. 2)" where one of a year's is so told apart, its
# number where printed, and its year, and nothing after.
OWN = re.compile(
    rf"^\W*(?:the\s+)?(?P<title>.{{2,200}}?\b(?:{_KIND}))(?P<series>\s*\(No\.?\s*\d+\))?"
    rf"[\s,]*(?:No\.?\s*(?P<number>\d{{1,3}})[\s,]*(?:of\s+)?)?(?P<year>{_YEAR})\W*$",
    re.I | re.S,
)
NUMBERED_ID = re.compile(r"^ng-(?:act|si)-(\d+)-(?:18|19|20)\d\d(?:-[0-9a-f]{8})?$")
SCHEDULE_HEAD = re.compile(r"extent\s+of\s+(repeal|amendment)", re.I)
WHOLE = re.compile(r"^\W*(?:the\s+whole(?:\s+\w+)?|all)\W*$", re.I)
WORDS = 400  # the most of a provision quoted


def _key(title: str) -> str:
    """A title as compared, its letters and figures alone: "The Money Laundering (Prohibition) Act" and
    "MONEY LAUNDERING (PROHIBITION) ACT" alike, "Niger-Delta" and "Niger Delta", and a word broken at a
    line's end ("Corpora- tion")."""
    words = re.sub(r"[^a-z0-9]+", " ", title.lower().replace("&", " and ")).split()
    return "".join(words[1:] if words[:1] == ["the"] else words)


def named(text: str | None) -> dict | None:
    """A name in full as it is found by: its title's key and its year, and its number where printed; None
    for words that are not one instrument's name of its own kind and year, or are a draft's or a Bill's."""
    match = OWN.match(" ".join((text or "").split()))
    if not match or NOT_ENACTED.search(match.group("title")):
        return None
    out = {
        "key": _key(match.group("title") + (match.group("series") or "")),
        "year": int(match.group("year")),
    }
    if match.group("number"):
        out["number"] = str(int(match.group("number")))
    return out


def _texts(reading: dict):
    """Each unit of text with its page and kind: a paragraph, a heading, a list item with its marker."""
    for page in reading["pages"]:
        for node in page["nodes"]:
            if node.get("kind") in ("table", "furniture") or not node.get("text"):
                continue
            marker = f"{node['marker']} " if node.get("marker") else ""
            yield " ".join(f"{marker}{node['text']}".split()), page, node.get("kind")


def cited_as(reading: dict, document: dict) -> str | None:
    """The name the document's own text says it may be cited as, as printed; where its text gives several
    (an amending Act quotes the principal Act's), the one its title or listing bears out, else none."""
    found: dict[tuple, str] = {}
    for text, _, _ in _texts(reading):
        for match in CITED_AS.finditer(text):
            name = named(match.group(1))
            if name:
                found.setdefault((name["key"], name["year"]), " ".join(match.group(1).split()))
    if len(found) == 1:
        return next(iter(found.values()))
    for held in (document.get("title"), document.get("listed_title")):
        name = named(held)
        if name and (name["key"], name["year"]) in found:
            return found[name["key"], name["year"]]
    return None


def _tail(text: str, at: int) -> tuple[int, int | None, str | None]:
    """What follows a title's last word at ``at``: where the citation ends, its year and its number; no
    year for a chapter of a revised edition."""
    chapter = CHAPTER.match(text, at)
    if chapter and chapter.end() > at and re.search(r"Cap|CAP|Chapter|Laws|L\.?\s?F", chapter.group(0)):
        return chapter.end(), None, None
    dated = DATED.match(text, at)
    if dated:
        number = dated.group("n1") or dated.group("n2")
        return dated.end(), int(dated.group("year")), str(int(number)) if number else None
    return at, None, None


def citations(text: str) -> list[dict]:
    """The instruments a sentence names by their short titles, in order: each with where its title's run
    of words begins ("run"), where the title itself does ("start") and where the citation ends ("end")."""
    out: list[dict] = []
    floor = 0
    for kind in KIND_WORD.finditer(text):
        after = text[kind.end() :]
        if re.match(r"\s+(?:[A-Z][a-z]|\(\s*[A-Z])", after) and not re.match(
            r"\s+\(?\s*(?:No\b|Nos\b|Cap\b|Chapter\b|Laws\s+of\b|L\.?\s?F)", after
        ):
            continue  # the title goes on: "Public Order Act", "Law Reform (Contracts) Act"
        tokens = [m for m in TOKEN.finditer(text, floor, kind.start())]
        run = len(tokens)
        while run > 0:
            token = tokens[run - 1].group(0)
            nxt = tokens[run].group(0) if run < len(tokens) else ""
            if token.startswith("(") and len(token) > 1:
                pass
            elif token[0].isupper() or token in SMALL:
                pass
            elif token == "," and run < len(tokens) and nxt.lower() != "the":
                pass
            else:
                break
            run -= 1
        held = tokens[run:]
        # Its title begins after the last "the" that is not inside it ("… of the Federal Capital Territory …").
        for k in range(len(held) - 1, -1, -1):
            before = held[k - 1].group(0).lower() if k else ""
            if held[k].group(0).lower() == "the" and before not in JOINED:
                held = held[k:]
                break
        first = 0  # a title begins with a word of its own: not "and the", nor a paragraph's "(1)"
        while first < len(held) and (
            held[first].group(0).lower() in SMALL or held[first].group(0)[0] in ",("
        ):
            first += 1
        if first == len(held) or not any(t.group(0)[0].isupper() for t in held[first:]):
            continue  # no title before the word: "this Act", "Act No. 35 of 1992"
        run_at, start = held[0].start(), held[first].start()
        part = None
        while found := PART_FIRST.match(text[start : kind.end()]):
            part, start = found, start + found.end()  # "Paragraph (a) of the Second Schedule to the …"
        if NO_TITLE.match(" ".join(text[start : kind.end()].split())):
            continue
        end, year, number = _tail(text, kind.end())
        out.append(
            {
                "run": run_at,
                "start": start,
                "end": end,
                "title": " ".join(text[start : kind.end()].split()),
                "target": " ".join(text[start:end].split()).rstrip(" ,("),
                "year": year,
                "number": number,
                "part": " ".join(text[held[first].start() : start].split()) if part else None,
            }
        )
        floor = end
    return out


def _part_of(before: str):
    """The part named just before an instrument's title, where there is one: of several ("section 4 of
    the … Act 1969 and section 7 of …"), the last, which is this instrument's."""
    found = None
    for word in PART_WORD.finditer(before):
        match = PART_OF.match(before, word.start())
        if match and not KIND_WORD.search(before, word.end(), match.end()):
            found = match
    return found


def _part(words: str | None) -> str | None:
    """'section 12 of the ' -> 'Section 12'; 'The First Schedule to the ' -> 'First Schedule'."""
    words = re.sub(r"\s+(?:of|to|in)(?:\s+the)?\s*$", "", " ".join((words or "").split()), flags=re.I)
    words = re.sub(r"^the\s+", "", words, flags=re.I).strip(" ,")
    return (words[:1].upper() + words[1:])[:80] or None


def _parts(refs: list[str]) -> str | None:
    """'Section 2', 'Section 9', 'Section 24' -> 'Sections 2, 9 and 24' (the first ten, then how many more)."""
    refs = list(dict.fromkeys(r for r in refs if r))
    if not refs:
        return None
    word = refs[0].split()[0]
    if len(refs) > 1 and all(re.fullmatch(rf"{re.escape(word)}\s+[\w()]+", r) for r in refs):
        numbers = [r.split(None, 1)[1] for r in refs]
        shown, more = numbers[:10], len(numbers) - 10
        tail = f" and {more} more" if more > 0 else f" and {shown.pop()}"
        return f"{word}s {', '.join(shown)}{tail}"
    return "; ".join(refs[:5]) + (f" and {len(refs) - 5} more" if len(refs) > 5 else "")


def _acted(subject: str) -> list[tuple[dict, str | None]]:
    """The instruments a sentence's subject says something is done to, each with the part of it named
    (None for the whole): those listed before what is done, but not one named only as having changed
    another, nor any where other words stand between."""
    found = citations(subject)
    out: list[tuple[dict, str | None]] = []
    boundary, gap = len(subject), FILLER
    for cite in reversed(found):
        if not gap.fullmatch(subject[cite["end"] : boundary]):
            break
        before = subject[: cite["start"]]
        changer = CHANGED_BY.search(subject[: cite["run"]]) or CHANGED_BY.search(before)
        if changer:
            boundary, gap = changer.start(), FILLER
            continue
        part = _part_of(before) if not cite["part"] else None
        out.append((cite, _part(cite["part"] or (part.group(0) if part else None))))
        boundary, gap = (part.start() if part else cite["run"]), BETWEEN
    return out[::-1]


def _provision(text: str, begin: int, action_end: int) -> str:
    """The sentence of ``text`` from ``begin`` through what is done, with its number before it, as printed:
    whole where it is short enough, else up to what is done."""
    numbered = NUMBERING.search(text, 0, begin)
    if not NOT_NUMBERING.search(text, 0, numbered.start()):
        begin = numbered.start()
    elif (sub := re.search(r"\(\d{1,2}[a-z]?\)\s*$", text[:begin])) is not None:
        begin = sub.start()
    stop = SENTENCE_END.search(text, action_end)
    whole = text[begin : stop.end() if stop else len(text)].strip()
    if len(whole) <= WORDS:
        return whole
    cut = text[begin:action_end].strip()
    return f"{cut} …" if len(cut) < WORDS else f"… {cut[-(WORDS - 4) :]} …"


def _supported(cite: dict, page: dict) -> bool:
    """Whether the page's check reading reads the citation's title, year and number as the primary does:
    its words in their order, a marginal note's words allowed between them (the check reads a line across
    the page, margin and all). True for a citation with no year, which is found by none, and where the
    page has no check reading (its document is then blocked from approval until it has one). Every check
    reading there is must agree."""
    checks = [text for text in (page.get("checks") or {}).values() if isinstance(text, str) and text.strip()]
    if not checks or not cite.get("year"):
        return True
    wanted = re.findall(r"[a-z0-9]+", cite["title"].lower())
    after = [str(cite["year"]), *([str(cite["number"])] if cite.get("number") else [])]

    def reads(text: str) -> bool:
        read = re.findall(r"[a-z0-9]+", text.lower())
        for at in (k for k, word in enumerate(read) if word == wanted[0]):
            for word in wanted[1:]:
                at = next((k for k in range(at + 1, min(at + 9, len(read))) if read[k] == word), None)
                if at is None:
                    break
            if at is not None and all(word in read[at + 1 : at + 13] for word in after):
                return True
        return False

    return all(reads(text) for text in checks)


def _schedule(reading: dict):
    """The rows of a schedule of enactments repealed or amended: (verb, citation, extent, words, page)."""
    for page in reading["pages"]:
        for node in page["nodes"]:
            if node.get("kind") != "table":
                continue
            rows = [
                [" ".join(cell["text"].split()) for cell in row]
                for row in (node.get("header_rows") or []) + (node.get("body_rows") or [])
            ]
            head = next((k for k, row in enumerate(rows) if SCHEDULE_HEAD.search(" ".join(row))), None)
            if head is None or len(rows[head]) < 3:
                continue
            verb = SCHEDULE_HEAD.search(" ".join(rows[head])).group(1).lower()
            title_at = next(
                (k for k, cell in enumerate(rows[head]) if re.search(r"short\s+title", cell, re.I)), 1
            )
            for row in rows[head + 1 :]:
                if len(row) != len(rows[head]) or not row[title_at]:
                    continue
                found = citations(row[title_at])
                if len(found) != 1 or found[0]["end"] < len(row[title_at].rstrip(" .")):
                    continue
                cite = dict(found[0])
                numbered = re.search(
                    rf"\bNo\.?\s*(\d{{1,3}})\s+of\s+({_YEAR})|({_YEAR})\s+No\.?\s*(\d{{1,3}})", row[0]
                )
                if numbered and cite["year"] is None:
                    cite["year"] = int(numbered.group(2) or numbered.group(3))
                    cite["number"] = str(int(numbered.group(1) or numbered.group(4)))
                words = " | ".join(rows[head]) + " — " + " | ".join(row)
                yield verb, cite, row[-1], words if len(words) <= WORDS else " | ".join(row)[:WORDS], page


def _whole_title(title: str) -> bool:
    """Whether words read after "to repeal the" are a title and nothing else: every word capitalised or
    one of a title's small ones ("certain tax Acts and enacts the Nigeria Tax Act" is not)."""
    words = re.sub(r"\([^()]*\)", " ", title).replace(",", " ").split()
    return bool(words) and all(w[0].isupper() or w.lower() in SMALL for w in words) and words[0][0].isupper()


def cited(reading: dict, document: dict, passed: list | None = None) -> list[dict]:
    """The earlier instruments this one repeals, revokes or amends, and the Act it is made under, each by
    its citation as printed ("target"), with the title, year and number it is found by, the words of the
    provision that says so and their page, and for an amendment the parts changed ("note"). None for a
    draft or a Bill, which does nothing yet. What was read and passed over is noted in ``passed``, with
    the reason (a measure of what the rules leave out)."""
    passed = [] if passed is None else passed
    if NOT_ENACTED.search(document.get("title") or "") or NOT_ENACTED.search(
        document.get("listed_title") or ""
    ):
        passed.append(("a draft or a Bill", document.get("title")))
        return []
    own = {
        (name["key"], name["year"])
        for name in (named(document.get(k)) for k in ("cited_as", "title", "listed_title"))
        if name
    }
    found: dict[tuple, dict] = {}
    principal: dict[str, set] = {}  # the instrument its text calls "the Principal Act", by that last word
    loose: list[tuple[str, str]] = []  # the parts of "the Principal Act" its provisions change

    def add(cite: dict, kind: str, page: dict, words: str | None, part: str | None = None, first=True):
        if (_key(cite["title"]), cite["year"]) in own:
            return None
        if not _supported(cite, page):
            passed.append(("the check reading reads the citation otherwise", cite["target"]))
            return None
        # What it does to another instrument, and what it is made under, are kept apart.
        key = (_key(cite["title"]), cite["year"], kind == "made_under")
        if key in found and not first:
            return key
        link = found.setdefault(
            key,
            {
                "kind": kind,
                "target": cite["target"],
                "title": cite["title"],
                "year": cite["year"],
                "refs": [],
            },
        )
        if cite.get("number") and not link.get("number"):
            link["number"] = cite["number"]
        if kind == "repeals" and link["kind"] == "amends":
            link["kind"] = "repeals"  # the whole instrument, though some provision amends a part first
            link.pop("words", None), link.pop("page", None)
        if words and "words" not in link and kind == link["kind"]:
            link |= {"words": words[:WORDS], "page": page["page"]}
        if part:
            link["refs"].append(part)
        return key

    units = list(_texts(reading))
    for at, (text, page, _) in enumerate(units):
        for action in ACTION.finditer(text):
            starts = [m.end() for m in SENTENCE_START.finditer(text, 0, action.start())]
            begin = starts[-1] if starts else 0
            numbered = [m.start() for m in SECTION_START.finditer(text, begin, action.start())]
            begin = numbered[-1] if numbered else begin
            subject = text[begin : action.start()]
            words = _provision(text, begin, action.end())
            if LISTED.search(subject) and OPENS_LIST.fullmatch(text[action.end() :]):
                # "The following enactments are repealed —": each item after it that is one instrument.
                for item, item_page, kind in units[at + 1 : at + 41]:
                    if kind != "list_item":
                        break
                    named_in = citations(item)
                    if (
                        len(named_in) == 1
                        and ITEM_BEGINS.match(item[: named_in[0]["start"]])
                        and ITEM_ENDS.fullmatch(item[named_in[0]["end"] :])
                        and not named_in[0]["part"]
                    ):
                        quoted = f"{words} {item}"
                        quoted = (
                            quoted
                            if len(quoted) <= WORDS
                            else f"… {text[action.start() - 60 : action.end()]} — {item}"
                        )
                        add(
                            named_in[0],
                            "repeals" if action.group(1) != "amended" else "amends",
                            item_page,
                            quoted,
                        )
                continue
            acted = _acted(subject)
            limited = LIMITED.match(text, action.end())
            if not acted:
                ref = re.search(
                    rf"\b((?:{_PART})\s+[\w()]+)[^.;:]{{0,60}}?\bof\s+the\s+[Pp]rincipal\s+(\w+)\W*$",
                    subject,
                    re.I,
                )
                if ref:
                    loose.append((ref.group(2).lower(), _part(ref.group(1))))
                continue
            if CONDITION.search(subject[: acted[0][0]["run"]]):
                passed.append(("said of a case, not done", subject.strip()[-160:]))
                continue
            stop = SENTENCE_END.search(text, action.end())
            limit = _part(text[action.end() : stop.start() if stop else len(text)]) if limited else None
            for cite, part in acted:
                whole = action.group(1) in ("repealed", "revoked") and not part and not limited
                if not whole and action.group(1) != "amended":
                    passed.append(("a part or a limited repeal: an amendment", cite["target"]))
                key = add(cite, "repeals" if whole else "amends", page, words, part or limit)
                called = re.match(r"[\s,]*\([^()]{0,200}\bprincipal\s+(\w+)", subject[cite["end"] :], re.I)
                if key and called:
                    principal.setdefault(called.group(1).lower(), set()).add(key)
        for enacting in ENACTING.finditer(text):
            clause = text[enacting.end() : enacting.end() + 400]
            ends = ENACTING_ENDS.search(clause)
            under = citations(clause[: ends.start()] if ends else clause)
            if under:
                part = _part_of(clause[: under[0]["start"]])
                words = text[enacting.start() : enacting.end() + under[0]["end"]]
                add(under[0], "made_under", page, words, _part(part.group(0)) if part else None)
    # Its long title and its explanatory memorandum, where no provision was read to say the same.
    for text, page, _ in units:
        for stated in STATED.finditer(text):
            verb = (stated.group("long") or stated.group("memo")).lower()
            at = stated.end()
            while True:
                name = NAMED_NEXT.match(text, at)
                if (
                    not name
                    or NOT_ENACTED.search(name.group("title"))
                    or not _whole_title(name.group("title"))
                ):
                    break
                end, year, number = _tail(text, name.end())
                begins, part = name.start("title"), None
                while of := PART_FIRST.match(text[begins : name.end()]):
                    part, begins = (
                        of,
                        begins + of.end(),
                    )  # "… amends Parts A to D of the Schedule to the … Act"
                cite = {
                    "title": " ".join(text[begins : name.end()].split()),
                    "target": " ".join(text[begins:end].split()).rstrip(" ,("),
                    "year": year,
                    "number": number,
                }
                stop = SENTENCE_END.search(text, end)
                words = text[stated.start() : stop.end() if stop else len(text)].strip()
                words = words if len(words) <= WORDS else f"{text[stated.start() : end].strip()} …"
                whole = verb.startswith("repeal") and not part
                add(cite, "repeals" if whole else "amends", page, words, first=False)
                more = ANOTHER.match(text, end)
                if not more or more.end() == end:
                    break
                at = more.end()
    for verb, cite, extent, words, page in _schedule(reading):
        whole = verb == "repeal" and WHOLE.match(extent)
        add(cite, "repeals" if whole else "amends", page, words, None if whole else extent[:80], first=False)
    for word, ref in loose:  # "Section 2 of the Principal Act": of the one instrument so called
        if len(principal.get(word, ())) == 1:
            found[next(iter(principal[word]))]["refs"].append(ref)
    out = []
    for link in found.values():
        refs = link.pop("refs")
        note = _parts(refs) if link["kind"] != "repeals" else None
        out.append({k: v for k, v in link.items() if v is not None} | ({"note": note} if note else {}))
    return out


def evidence(reading: dict, document: dict) -> dict:
    """What a law's manifest entry keeps for linking: the name its text says it may be cited as, and the
    instruments it repeals, amends or is made under (cited); nothing for a court's decision or a Gazette
    record."""
    if document.get("type") in NOT_LAW:
        return {}
    name = cited_as(reading, document)
    document = {**document, **({"cited_as": name} if name else {})}
    cites = cited(reading, document)
    return {**({"cited_as": name} if name else {}), **({"cites": cites} if cites else {})}


def issuer(document: dict) -> str | None:
    """The legislature a bound volume's instrument is of, by its id ("ng-wr-law-12-1957": the Western
    Region's); None for the Federation's."""
    parts = document["id"].split("-")
    return parts[1] if len(parts) > 2 and parts[1] in ISSUERS else None


def identity(document: dict) -> dict | None:
    """What a document is found by: the names in full (named) its own text says it may be cited as and its
    title bears, and their year; its listing's name only where neither is one. A text whose own two names
    give different years ("… Regulations, 2018" in its citation, "… Regulations, 2019" on the Gazette's
    front page) is found by neither: its year is in doubt. With its number, where its volume or its
    Gazette's front page gives one. None for a document with no such name."""
    if NOT_ENACTED.search(document.get("title") or "") or NOT_ENACTED.search(
        document.get("listed_title") or ""
    ):
        return None
    said, borne = named(document.get("cited_as")), named(document.get("title"))
    names = [name for name in (said, borne) if name] or [
        name for name in [named(document.get("listed_title"))] if name
    ]
    if not names or len({name["year"] for name in names}) > 1:
        return None
    numbered = NUMBERED_ID.match(document.get("stored_as") or document["id"])
    number = (
        document.get("number")
        or (numbered.group(1) if numbered else None)
        or next((name["number"] for name in names if name.get("number")), None)
    )
    return {
        "keys": {name["key"] for name in names},
        "year": names[0]["year"],
        "number": str(number) if number else None,
        "issuer": issuer(document),
    }


def _primary(kind: str) -> bool:
    return bool(PRIMARY.match(kind))


def earliest(document: dict) -> int | None:
    """The earliest year a document may be of, by any of its names, its volume's year or its Gazette
    number's: a text it cites is never later than that."""
    stored = document.get("stored_as") or document["id"]
    numbered = (
        re.search(r"-((?:18|19|20)\d\d)(?:-[0-9a-f]{8})?$", stored) if NUMBERED_ID.match(stored) else None
    )
    names = (named(document.get(k)) for k in ("cited_as", "title", "listed_title"))
    years = [name["year"] for name in names if name]
    years += [int(document["year"])] if str(document.get("year") or "").isdigit() else []
    years += [int(numbered.group(1))] if numbered else []
    return min(years, default=None)


def _by_name(whole: list[dict], passed: list | None = None):
    """How a citation's documents are found among Nigeria's: by title and year together, within the same
    legislature, an Act among Acts and subsidiary legislation among subsidiary legislation, never one
    later than the text citing it, and not where both print a number and the numbers differ. Every copy of
    the instrument the collection holds is found; none where any of this is in doubt (noted in ``passed``
    with the reason)."""
    passed = [] if passed is None else passed
    index: dict[tuple, list[tuple[str, dict, dict]]] = {}
    for d in whole:
        found = identity(d)
        for key in (found or {}).get("keys", ()):
            index.setdefault((key, found["year"], found["issuer"]), []).append((d["work"], found, d))

    def find(cite: dict, source: dict) -> list[str]:
        if not cite.get("year") or not cite.get("title"):
            return []
        this = earliest(source)
        held = index.get((_key(cite["title"]), int(cite["year"]), issuer(source)), [])
        out = []
        for work, found, document in held:
            if work == source["work"]:
                return [work]  # itself: no link (ingestion.linked.link)
            if cite.get("number") and found["number"] and str(cite["number"]) != found["number"]:
                # Perhaps another instrument of that name and year.
                passed.append(("the numbers differ", source["id"], cite["target"], work))
                return []
            if _primary(cite["title"].split()[-1]) != (document.get("type") in PRIMARY_TYPES):
                # An Act's name at the head of a regulation made under it, or the like.
                passed.append(
                    ("an Act's name on another kind of document", source["id"], cite["target"], work)
                )
                continue
            if this is None or found["year"] > this:
                # The text citing it must be the later one, and its own year known.
                reason = (
                    "the citing text's own year is not known"
                    if this is None
                    else "the cited text is the later"
                )
                passed.append((reason, source["id"], cite["target"], work))
                return []
            out.append(work)
        return out

    return find


def link_works(documents: list[dict]) -> list[dict]:
    """Each Nigerian law with its links ("relations") to the earlier instruments it repeals, amends or is
    made under, found in the collection by their citations, and to the later ones that repeal or amend it;
    the instruments made under it ("enables"); and whether it is current ("currency"): repealed by a later
    text of the collection, with the words of its provision that does and their page, else unknown. Never
    in force: Nigeria has no official listing to say so. A court's decision has none of these."""
    return linked.link(
        documents,
        _by_name,
        applies=lambda d: d.get("type") not in NOT_LAW,
        # Named as its own text says it may be cited, else by its title as laws are cited (not in capitals).
        title_of=lambda d: d.get("cited_as") or cited_names.cited(d.get("title") or "")[0],
        year_of=lambda d: (identity(d) or {}).get("year") or d.get("year"),
    )
