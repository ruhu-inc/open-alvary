"""Revoke a person's connection of a tool, as the console will (MAC-07, MAC-09).

    python -m trials.idp_check.revoke            # the latest person who signed in with Claude Code

Two steps, as in the design: Auth0 forgets the consent (so the tool cannot quietly get new passes),
and our own grant check refuses every pass resting on the earlier sign-in. Prints no token.
"""

import hashlib
import json
import time

from trials.idp_check.auth0 import Management
from trials.idp_check.server import REVOKED

CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"


def main():
    m = Management()
    client = m.post("clients/cimd/preview", {"external_client_id": CLAUDE_CODE})["client_id"]
    logins = [
        e
        for e in m.get("logs", per_page=50, sort="date:-1")
        if e.get("type") == "s" and e.get("client_id") == client
    ]
    if not logins:
        raise SystemExit("no Claude Code sign-in found in the recent log")
    user = logins[0]["user_id"]
    grants = m.get("grants", user_id=user)
    for grant in grants:
        if grant.get("clientID") == client:
            m.call("DELETE", f"grants/{grant['id']}")
    key = f"{hashlib.sha256(user.encode()).hexdigest()[:12]}|{CLAUDE_CODE}"
    revoked = json.loads(REVOKED.read_text()) if REVOKED.exists() else {}
    revoked[key] = int(time.time())
    REVOKED.write_text(json.dumps(revoked, indent=1))
    print(
        f"Auth0 consents removed: {sum(g.get('clientID') == client for g in grants)}; our grant revoked for {key.split('|')[0]}"
    )


if __name__ == "__main__":
    main()
