"""Read-only checks against a running pilot; never submits data or changes rights."""

import argparse
from hashlib import sha256

import httpx

SECURITY_HEADERS = [
    "content-security-policy",
    "x-content-type-options",
    "referrer-policy",
    "permissions-policy",
]


def verify(base_url):
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=15, follow_redirects=True) as client:
        home = client.get("/")
        home.raise_for_status()
        assert "Open Alvary" in home.text, "Homepage is missing project identity"
        for response in [home, client.get("/api/healthz")]:
            for header in SECURITY_HEADERS:
                assert header in response.headers, f"{response.url.path} lacks {header}"
        assert client.get("/api/healthz").json() == {"status": "ok"}
        coverage = client.get("/api/coverage")
        coverage.raise_for_status()
        assert coverage.json()["catalogued_sources"] >= 4
        assert coverage.json()["open_full_text_sources"] == 0, "Initial pilot must not imply cleared text"
        sources = client.get("/api/search", params={"q": "freedom"}).json()
        assert any(s["id"] == "ng-foi-act-2011" for s in sources["items"])
        versions = client.get("/api/sources/ng-foi-act-2011/versions").json()
        assert versions["items"] == []
        manifests = client.get("/api/releases").json()
        assert manifests, "No available releases"
        for manifest in manifests:
            base = "/api" + manifest["download_base"]
            for name, details in manifest["files"].items():
                response = client.get(base + "/" + name)
                response.raise_for_status()
                assert sha256(response.content).hexdigest() == details["sha256"], name
        initialize = client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
        )
        assert initialize.json()["result"]["serverInfo"]["name"] == "open-alvary", "MCP endpoint unavailable"
        assert client.post("/api/sources", json={}).status_code == 405, "Writes must stay refused"
        print(
            "PASS: homepage, security headers, MCP, health, pilot counts, search, withheld text and release hashes"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    verify(parser.parse_args().base_url)
