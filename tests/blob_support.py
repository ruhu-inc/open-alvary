"""Azure Blob Storage as a dictionary, behind httpx.MockTransport: Put Blob with its If-None-Match
condition, its MD5 check and a container's immutability policy; Get Blob and its properties; List
Blobs a few at a time, as the service may answer. No network."""

import base64
import hashlib
from xml.sax.saxutils import escape

import httpx

from ingestion import corpus_store


def md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


class BlobService:
    PAGE = 2  # blobs per listing page, fewer than asked for: the service may do that

    def __init__(self, account: str = "oatest"):
        self.account = account
        self.blobs: dict[tuple[str, str], dict] = {}
        self.immutable: set[str] = set()  # containers whose blobs cannot be written over
        self.requests: list[httpx.Request] = []
        self.answers: list[tuple[str, int, str | None]] = []  # method, status, error code
        self.failures: list = []  # statuses or errors met before the service answers

    def store(self, container: str = "corpus", **options) -> corpus_store.AzureBlobStore:
        options.setdefault("token", lambda: "test-token")
        return corpus_store.AzureBlobStore(self.account, container, client=self.client(), **options)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def rewrite(self, container: str, name: str, data: bytes) -> None:
        """Bytes changed behind the archive's back, the service's own MD5 with them."""
        self.blobs[(container, name)].update(data=data, md5=md5(data))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure)
        response = self._answer(request)
        self.answers.append((request.method, response.status_code, response.headers.get("x-ms-error-code")))
        return response

    def _answer(self, request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https" and request.url.host == f"{self.account}.blob.core.windows.net"
        assert request.headers["Authorization"].startswith("Bearer ")
        assert (
            request.headers["x-ms-version"] == corpus_store.AzureBlobStore.VERSION
            and request.headers["x-ms-date"]
        )
        container, _, name = request.url.path.lstrip("/").partition("/")
        if request.method == "GET" and not name:
            return self._list(container, request.url.params)
        blob = self.blobs.get((container, name))
        if request.method == "PUT":
            assert request.headers["x-ms-blob-type"] == "BlockBlob"
            data = request.content
            if request.headers.get("Content-MD5") and request.headers["Content-MD5"] != md5(data):
                return error(400, "Md5Mismatch")
            if blob is not None and request.headers.get("If-None-Match") == "*":
                return error(409, "BlobAlreadyExists")
            if blob is not None and container in self.immutable:
                return error(409, "BlobImmutableDueToPolicy")
            self.blobs[(container, name)] = {
                "data": data,
                "md5": md5(data),
                "content_type": request.headers["x-ms-blob-content-type"],
                "metadata": {
                    key[len("x-ms-meta-") :]: value
                    for key, value in request.headers.items()
                    if key.startswith("x-ms-meta-")
                },
            }
            return httpx.Response(201)
        if blob is None:
            return error(404, "BlobNotFound")
        headers = {"Content-Type": blob["content_type"], "Content-MD5": blob["md5"]}
        return httpx.Response(200, headers=headers, content=b"" if request.method == "HEAD" else blob["data"])

    def _list(self, container: str, params) -> httpx.Response:
        assert params["restype"] == "container" and params["comp"] == "list"
        names = sorted(n for c, n in self.blobs if c == container and n.startswith(params.get("prefix", "")))
        start = int(params.get("marker") or 0)
        page = names[start : start + self.PAGE]
        following = str(start + self.PAGE) if start + self.PAGE < len(names) else ""
        blobs = "".join(f"<Blob><Name>{escape(n)}</Name><Properties /></Blob>" for n in page)
        body = (
            '\ufeff<?xml version="1.0" encoding="utf-8"?>'  # led by a byte order mark, as the service does
            f'<EnumerationResults ServiceEndpoint="https://{self.account}.blob.core.windows.net/"'
            f' ContainerName="{container}"><Prefix>{escape(params.get("prefix", ""))}</Prefix>'
            f"<MaxResults>{params['maxresults']}</MaxResults><Blobs>{blobs}</Blobs>"
            f"<NextMarker>{following}</NextMarker></EnumerationResults>"
        )
        return httpx.Response(200, headers={"Content-Type": "application/xml"}, content=body.encode())


def error(status: int, code: str) -> httpx.Response:
    return httpx.Response(
        status, headers={"x-ms-error-code": code}, content=f"<Error><Code>{code}</Code></Error>".encode()
    )
