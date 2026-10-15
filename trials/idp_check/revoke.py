"""Revoke a person's connection of a tool, as the console will (MAC-07, MAC-09).

    python -m trials.idp_check.revoke                 # Claude Code, for the latest person who used it
    python -m trials.idp_check.revoke --client cli    # our command-line tool instead

Two steps, as in the design: our grant is marked revoked, so every pass resting on it is refused at
the next request (renewed ones included); and Auth0 forgets the consent. Prints no token.
"""

import argparse
import hashlib
import time

from trials.idp_check.auth0 import Management
from trials.idp_check.server import grants, save_grants

CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=["claude-code", "cli"], default="claude-code")
    args = parser.parse_args()
    m = Management()
    if args.client == "cli":
        from trials.idp_check.cli import client_id

        auth0_client = token_client = client_id()
    else:
        auth0_client = m.post("clients/cimd/preview", {"external_client_id": CLAUDE_CODE})["client_id"]
        token_client = CLAUDE_CODE
    logins = [
        e
        for e in m.get("logs", per_page=100, sort="date:-1")
        if e.get("type") == "s" and e.get("client_id") == auth0_client
    ]
    if not logins:
        raise SystemExit("no sign-in with that tool found in the recent log")
    user = logins[0]["user_id"]
    removed = 0
    for grant in m.get("grants", user_id=user):
        if grant.get("clientID") == auth0_client:
            m.call("DELETE", f"grants/{grant['id']}")
            removed += 1
    person = hashlib.sha256(user.encode()).hexdigest()[:12]
    data, revoked = grants(), 0
    for record in data.values():
        if (
            record["subject"] == person
            and record["client"] in (token_client, auth0_client)
            and not record["revoked_at"]
        ):
            record["revoked_at"] = int(time.time())
            revoked += 1
    save_grants(data)
    print(f"our grants revoked: {revoked}; Auth0 consents removed: {removed}")


if __name__ == "__main__":
    main()
