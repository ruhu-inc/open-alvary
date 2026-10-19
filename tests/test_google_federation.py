"""Google credentials from an Azure managed identity: the module answers google-auth's executable
source with the identity's token for the federation's audience, or a failure without any token."""

import json

import httpx

from ingestion import google_federation


def test_the_identitys_token_for_googles_audience_is_answered_as_google_auth_reads_it(monkeypatch, capsys):
    asked = []

    def identity(request):
        asked.append(request)
        return httpx.Response(200, json={"access_token": "entra-jwt", "expires_on": "1792430000"})

    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://identity.local/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "header-value")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-1")
    source = google_federation.GoogleAudience(
        "api://open-alvary-google", client=httpx.Client(transport=httpx.MockTransport(identity))
    )
    assert google_federation.main(["token"], source=source) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "version": 1, "success": True, "token_type": google_federation.JWT, "id_token": "entra-jwt",
        "expiration_time": 1792430000,
    }  # fmt: skip
    params = asked[0].url.params
    assert params["resource"] == "api://open-alvary-google" and params["client_id"] == "client-1"
    assert asked[0].headers["X-IDENTITY-HEADER"] == "header-value"


def test_a_failure_is_answered_without_a_token(monkeypatch, capsys):
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://identity.local/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "h")
    refused = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403, json={})))
    source = google_federation.GoogleAudience("api://open-alvary-google", client=refused)
    assert google_federation.main(["token"], source=source) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["success"] is False and "id_token" not in printed
