"""A collection's documents linked by what their texts say of one another: the earlier instruments each
repeals, amends or is made under, the later ones that repeal or amend it, the instruments made under
it, and whether it is current. Each country finds a citation's instrument in its own way (Rwanda by kind,
number and year: ingestion.rwanda.link_works; Nigeria by title, year and number: ingestion.ng_cited.link_works);
what follows from the links is the same for all.
"""

REVERSE = {"repeals": "repealed_by", "amends": "amended_by"}


def _latest(work: str, repealed_by: dict[str, list[str]], years: dict[str, int]) -> str:
    """The law in force at the end of a line of repeals: what repealed it, what repealed that, …"""
    seen = {work}
    while repealed_by.get(work):
        later = max(repealed_by[work], key=lambda w: (years.get(w) or 0, w))
        if later in seen:
            break
        seen.add(later)
        work = later
    return work


def link(
    documents: list[dict],
    finder,
    applies=lambda d: not d.get("part"),
    title_of=lambda d: d.get("title"),
    year_of=lambda d: d.get("year"),
    listing: str | None = None,
) -> list[dict]:
    """Each document that ``applies`` with its links ("relations") to the earlier instruments it repeals,
    amends or is made under, found in the collection by their citations (``finder`` of the documents gives
    the function that finds a citation's works, none where the collection lacks it or cannot tell), and to
    the later ones that repeal or amend it; the instruments made under it ("enables"); and whether it is
    current ("currency"): repealed by a later text in the collection (with the words of its provision that
    does, and their page), else as the country's ``listing`` files it ("listed" on the document), else
    unknown, with the latest law in its line, what has amended it, and any law it was made under that is
    since repealed. A citation of an instrument the collection lacks stays as printed, without a target.
    A country without a listing has none named, and nothing of it is ever in force by this step."""
    whole = [{**d, "work": d.get("work") or d["id"]} for d in documents if applies(d)]
    find = finder(whole)
    titles: dict[str, str] = {}
    years: dict[str, int] = {}
    listed: dict[str, str] = {}
    for d in whole:
        if d.get("language") == "en" or d["work"] not in titles:
            titles[d["work"]] = title_of(d) or d["work"]
        if year_of(d):
            years[d["work"]] = year_of(d)
        if d.get("listed"):
            listed[d["work"]] = d["listed"]
    forward: dict[str, list[dict]] = {}
    backward: dict[str, list[dict]] = {}
    enables: dict[str, set[str]] = {}
    provisions: dict[tuple[str, str], dict] = {}  # (changed, changing): the provision that changes it
    for d in whole:
        if d["work"] in forward or not d.get("cites"):
            continue
        links = []
        for cite in d["cites"]:
            targets = list(dict.fromkeys(find(cite, d)))
            if d["work"] in targets:
                continue
            note = {"note": cite["note"]} if cite.get("note") else {}
            links.append(
                {
                    "kind": cite["kind"],
                    "target": cite["target"],
                    **({"target_id": targets[0]} if targets else {}),
                    **note,
                }
            )
            for target in targets:  # one instrument held in several copies: each is what the text names
                if cite["kind"] == "made_under":
                    enables.setdefault(target, set()).add(
                        d["work"]
                    )  # listed apart: a law may enable hundreds
                    continue
                back = {
                    "kind": REVERSE[cite["kind"]],
                    "target": titles[d["work"]],
                    "target_id": d["work"],
                    **note,
                }
                if back not in backward.setdefault(target, []):  # said twice by one text: one link
                    backward[target].append(back)
                if cite["kind"] == "repeals" and cite.get("words"):
                    provisions[target, d["work"]] = {k: cite[k] for k in ("words", "page") if cite.get(k)}
        forward[d["work"]] = links
    repealed_by = {
        work: [link["target_id"] for link in links if link["kind"] == "repealed_by"]
        for work, links in backward.items()
    }

    def named(work: str) -> dict:
        return {"work": work, "title": titles.get(work, work)}

    def currency(work: str) -> dict:
        by = repealed_by.get(work) or []
        out: dict = {
            "status": "repealed" if by else listed.get(work, "unknown"),
            "basis": "text" if by else "listing" if work in listed else None,
        }
        if by:  # each with the words of its provision that repeals this, and their page, where read
            out["repealed_by"] = [named(w) | provisions.get((work, w), {}) for w in by]
            latest = _latest(work, repealed_by, years)
            if latest not in by:  # repealed in turn: the law in force now is further down the line
                out["latest"] = named(latest)
            if listed.get(work) == "in_force":
                out["disagrees"] = f"{listing or 'The listing'} files it among the laws in force"
        amended = [link["target_id"] for link in backward.get(work, []) if link["kind"] == "amended_by"]
        if amended:
            out["amended_by"] = [named(w) for w in amended]
        parents = [
            {**named(link["target_id"]), "repealed_by": [named(w) for w in repealed_by[link["target_id"]]]}
            for link in forward.get(work, [])
            if link["kind"] == "made_under" and repealed_by.get(link.get("target_id", ""))
        ]
        if parents:
            out["parents_repealed"] = parents
        return {k: v for k, v in out.items() if v is not None}

    out = []
    for d in documents:
        d = {k: v for k, v in d.items() if k not in ("relations", "enables", "currency")}
        work = d.get("work") or d["id"]
        if applies(d):
            links = forward.get(work, []) + backward.get(work, [])
            d |= {"relations": links} if links else {}
            d |= {"enables": sorted(enables[work])} if work in enables else {}
            d["currency"] = currency(work)
        out.append(d)
    return out
