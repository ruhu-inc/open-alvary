"""The corpus bucket: every country's original files, kept private in Cloud Storage.

    gs://open-alvary-corpus/sources/<country>/<source>/<sha256>.pdf    the file as fetched
    gs://open-alvary-corpus/sources/<country>/<source>/<sha256>.json   how and when it was fetched
    gs://open-alvary-corpus/state/<country>/…                          the sync ledger and catalogues

A file is named by its fingerprint and never overwritten (a second upload of the same bytes is a
no-op), so the bucket is an append-only record of what each source published. Access is through
Google Application Default Credentials; nothing here holds a key. The bucket is EU multi-region,
Autoclass (files nobody reads move to colder classes, with no retrieval fees), public access
prevented.
"""

import json
import os
import secrets
from urllib.parse import quote

BUCKET = os.environ.get("ALVARY_CORPUS_BUCKET", "open-alvary-corpus")
API = "https://storage.googleapis.com"


class CorpusStore:
    def __init__(self, bucket: str = BUCKET, session=None):
        self.bucket = bucket
        if session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
            )
            session = AuthorizedSession(credentials)
        self.session = session

    def uri(self, name: str) -> str:
        return f"gs://{self.bucket}/{name}"

    def exists(self, name: str) -> bool:
        response = self.session.get(f"{API}/storage/v1/b/{self.bucket}/o/{quote(name, safe='')}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    def put(
        self, name: str, data: bytes, content_type: str, metadata: dict | None = None, replace: bool = False
    ) -> bool:
        """Upload an object; unless `replace`, an object already there is kept (True if written)."""
        boundary = f"oa-{secrets.token_hex(16)}"  # random, so no file's bytes can contain it
        head = json.dumps({"name": name, "contentType": content_type, "metadata": metadata or {}})
        body = (
            (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{head}\r\n"
                f"--{boundary}\r\nContent-Type: {content_type}\r\n\r\n"
            ).encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        params = {"uploadType": "multipart"}
        if not replace:
            params["ifGenerationMatch"] = "0"  # only if no object of that name exists
        response = self.session.post(
            f"{API}/upload/storage/v1/b/{self.bucket}/o",
            params=params,
            data=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            timeout=600,
        )
        if response.status_code == 412:  # already there: the same fingerprint, the same bytes
            return False
        response.raise_for_status()
        return True

    def list(self, prefix: str) -> list[str]:
        """The names of the objects under a prefix."""
        names, token = [], None
        while True:
            params = {"prefix": prefix, "fields": "items(name),nextPageToken", "maxResults": 1000}
            if token:
                params["pageToken"] = token
            response = self.session.get(f"{API}/storage/v1/b/{self.bucket}/o", params=params, timeout=120)
            response.raise_for_status()
            page = response.json()
            names += [item["name"] for item in page.get("items", [])]
            token = page.get("nextPageToken")
            if not token:
                return names

    def get(self, name: str) -> bytes:
        response = self.session.get(
            f"{API}/storage/v1/b/{self.bucket}/o/{quote(name, safe='')}", params={"alt": "media"}, timeout=600
        )
        response.raise_for_status()
        return response.content


def source_name(country: str, source: str, sha256: str, suffix: str = ".pdf") -> str:
    return f"sources/{country}/{source}/{sha256}{suffix}"
