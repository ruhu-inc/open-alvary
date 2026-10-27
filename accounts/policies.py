"""The documents people accept at sign-up, by version (D25). A new version is a new date; people
accept it at their next sign-in before using the console. Organisations' owners are emailed at least
NOTICE_DAYS before a new version takes effect (terms of service, "Changes"), with what it changes:
python -m accounts.operator --operator <you> terms-notice VERSION --effective DATE --reason R."""

CURRENT = {
    "terms": "2026-10-27",
    "acceptable_use": "2026-10-27",
    "privacy": "2026-10-27",
}
PATHS = {"terms": "/terms", "acceptable_use": "/acceptable-use", "privacy": "/privacy"}
NOTICE_DAYS = 30

# What each version changes, as the owner approved it, for the notice emailed to owners.
CHANGES = {
    "2026-10-27": (
        "- Open Alvary is not the publisher of the laws, regulations and judgments in the corpus: the bodies"
        " that issue them publish them. Open Alvary makes copies of the official texts available, each"
        " checked against the official copy, and every record links to the official copy at its publisher,"
        " which prevails where a copy differs."
    ),
    "2026-10-22": (
        "- You may show your users the records and texts your organisation is entitled to, in apps you"
        " offer, within each record's rights and attribution. Don't give them your keys or resell the API"
        " or MCP itself.\n"
        "- What you send the API and MCP is used only to answer it. We don't keep it or share it, and we"
        " don't use it to train anything. Send only what a search needs, and leave out details that"
        " identify a client.\n"
        "- We lower a plan's limits only with 30 days' notice by email.\n"
        "- We email organisation owners at least 30 days before new terms take effect. Using keys or"
        " connected apps after that counts as accepting them.\n"
        "- We suspend the narrowest thing that deals with a problem, a key or a connection before a whole"
        " organisation, and tell you first unless the risk is urgent."
    ),
}
