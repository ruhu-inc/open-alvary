"""Google credentials from an Azure managed identity (workload identity federation), for the jobs that
reach Google Cloud from Azure: reading pages with Document AI and copying the buckets. No Google key
exists anywhere: Google's security token service exchanges the identity's Entra token for a short-
lived Google one. google-auth asks for the Entra token by running this module (executable-sourced
credentials), because Container Apps has no instance metadata service for google-auth's own Azure
source.

The credential configuration (the secret google-wif-config, made once by the owner:
deployment/azure/README.md) names the command

    python -m ingestion.google_federation token

which prints the answer google-auth reads. The job sets GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES=1,
and ALVARY_GOOGLE_AUDIENCE to the Entra application the token is for (its Application ID URI).
"""

import json
import os
import sys

from ingestion.corpus_store import AzureToken

JWT = "urn:ietf:params:oauth:token-type:jwt"


class GoogleAudience(AzureToken):
    """The managed identity's token for the federation's audience instead of Azure Storage."""

    def __init__(self, audience: str, **options):
        super().__init__(**options)
        self.RESOURCE = audience


def answer(token: str, expires: float) -> dict:
    return {
        "version": 1,
        "success": True,
        "token_type": JWT,
        "id_token": token,
        "expiration_time": int(expires),
    }


def main(argv=None, source=None) -> int:
    if (argv if argv is not None else sys.argv[1:]) != ["token"]:
        print("python -m ingestion.google_federation token", file=sys.stderr)
        return 2
    try:
        token, expires = (source or GoogleAudience(os.environ["ALVARY_GOOGLE_AUDIENCE"]))._fetch()
    except Exception as error:  # google-auth reads the failure from the answer; never a token in it
        print(
            json.dumps(
                {"version": 1, "success": False, "code": type(error).__name__, "message": str(error)[:200]}
            )
        )
        return 1
    print(json.dumps(answer(token, expires)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
