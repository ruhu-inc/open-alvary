"""A small Auth0 Management API client for the identity-provider checks.

Credentials come from the git-ignored .env (AUTH0_DOMAIN, AUTH0_CLIENT_ID, AUTH0_CLIENT_SECRET) and
are never printed. Every call returns parsed JSON or raises with Auth0's own error message.
"""

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


def environment(path=ROOT / ".env"):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip().strip("'\"")
    values.update({k: v for k, v in os.environ.items() if k.startswith(("AUTH0_", "IDP_CHECK_"))})
    return values


class Auth0Error(RuntimeError):
    pass


class Management:
    def __init__(self, env=None):
        env = env or environment()
        self.domain = env["AUTH0_DOMAIN"].removeprefix("https://").rstrip("/")
        self.issuer = f"https://{self.domain}/"
        answer = httpx.post(
            f"https://{self.domain}/oauth/token",
            timeout=20,
            data={
                "grant_type": "client_credentials",
                "client_id": env["AUTH0_CLIENT_ID"],
                "client_secret": env["AUTH0_CLIENT_SECRET"],
                "audience": f"https://{self.domain}/api/v2/",
            },
        )
        if answer.status_code != 200:
            raise Auth0Error(
                f"management token refused: {answer.status_code} {answer.json().get('error_description')}"
            )
        self.client = httpx.Client(
            base_url=f"https://{self.domain}/api/v2/",
            timeout=30,
            headers={"Authorization": f"Bearer {answer.json()['access_token']}"},
        )

    def call(self, method, path, body=None, **params):
        for wait in (1, 2, 4, 8, 0):  # the free plan's management API allows only a few calls a second
            answer = self.client.request(method, path, json=body, params=params or None)
            if answer.status_code != 429 or not wait:
                break
            time.sleep(wait)
        if answer.status_code >= 400:
            try:
                detail = answer.json()
            except ValueError:
                detail = answer.text[:300]
            raise Auth0Error(f"{method} {path}: {answer.status_code} {json.dumps(detail)[:500]}")
        return answer.json() if answer.content else None

    def get(self, path, **params):
        return self.call("GET", path, **params)

    def post(self, path, body):
        return self.call("POST", path, body)

    def patch(self, path, body):
        return self.call("PATCH", path, body)
