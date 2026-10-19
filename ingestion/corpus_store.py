"""The corpus store: every country's original files, kept private in Cloud Storage or Azure Blob Storage.

    gs://open-alvary-corpus/sources/<country>/<source>/<sha256>.pdf    the file as fetched
    gs://open-alvary-corpus/sources/<country>/<source>/<sha256>.json   how and when it was fetched
    gs://open-alvary-corpus/state/<country>/…                          the sync ledger and catalogues

The store is chosen by its address (ALVARY_CORPUS_BUCKET): a Cloud Storage bucket, by name or as
"gs://<bucket>", or an Azure Blob container as "az://<storage-account>/<container>", which holds the
same names. A file is named by its fingerprint and never overwritten (a second upload of the same
bytes is a no-op), so the store is an append-only record of what each source published. Nothing
here holds a key: Cloud Storage is reached through Google Application Default Credentials, Azure
through a managed identity's token (an operator's own, from the Azure CLI). The Cloud Storage bucket
is EU multi-region, Autoclass (files nobody reads move to colder classes, with no retrieval fees),
public access prevented.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import string
import subprocess
import sys
import threading
import time
from email.utils import formatdate
from urllib.parse import quote, unquote
from xml.etree import ElementTree

import httpx

BUCKET = os.environ.get("ALVARY_CORPUS_BUCKET", "open-alvary-corpus")
API = "https://storage.googleapis.com"


def split(place: str) -> tuple[str, str]:
    """A place in a store as the store's address and the prefix of its names there:
    "gs://<bucket>/<prefix>" gives the bucket's name, "az://<storage-account>/<container>/<prefix>"
    the container's address."""
    if place.startswith("az://"):
        account, _, rest = place[len("az://") :].partition("/")
        container, _, prefix = rest.partition("/")
        return f"az://{account}/{container}", prefix
    if place.startswith("gs://"):
        bucket, _, prefix = place[len("gs://") :].partition("/")
        return bucket, prefix
    raise ValueError(f"Not a place in a bucket or container: {place!r}")


def open_store(address: str = BUCKET, **options):
    """The store at an address: "az://<storage-account>/<container>" is an Azure Blob container;
    "gs://<bucket>", or a bucket's bare name, is a Cloud Storage bucket. A store is a whole bucket or
    container: a prefix belongs to a place in it (split)."""
    store, prefix = split(address) if "://" in address else (address, "")
    if prefix.strip("/"):
        raise ValueError(f"A store is a whole bucket or container, not a folder in one: {address!r}")
    if store.startswith("az://"):
        account, _, container = store[len("az://") :].partition("/")
        return AzureBlobStore(account, container, **options)
    if not store:
        raise ValueError(f"No bucket named in {address!r}")
    return CorpusStore(store, **options)


class CorpusStore:
    """The store in a Cloud Storage bucket, through the JSON API."""

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

    # Waits, in seconds, before a request is tried again when the connection drops or the service
    # answers with a server error: about twenty minutes in all, then the error is raised.
    RETRY_WAITS = (5, 15, 30, 60, 120, 300, 600)

    def _request(self, method: str, url: str, **kwargs):
        import time

        import google.auth.exceptions
        import requests

        # A request never waits for ever: a connection the far end has dropped would otherwise hang.
        kwargs.setdefault("timeout", 120)
        for wait in (*self.RETRY_WAITS, None):
            try:
                response = self.session.request(method, url, **kwargs)
                if response.status_code < 500 or wait is None:
                    return response
                reason = f"server error {response.status_code}"
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                google.auth.exceptions.TransportError,
            ) as error:
                if wait is None:
                    raise
                reason = type(error).__name__
            print(f"bucket: {reason}; trying again in {wait} s", file=sys.stderr, flush=True)
            time.sleep(wait)
        raise RuntimeError("unreachable")

    def uri(self, name: str) -> str:
        return f"gs://{self.bucket}/{name}"

    def exists(self, name: str) -> bool:
        response = self._request("GET", f"{API}/storage/v1/b/{self.bucket}/o/{quote(name, safe='')}")
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
        response = self._request(
            "POST",
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
            response = self._request("GET", f"{API}/storage/v1/b/{self.bucket}/o", params=params, timeout=120)
            response.raise_for_status()
            page = response.json()
            names += [item["name"] for item in page.get("items", [])]
            token = page.get("nextPageToken")
            if not token:
                return names

    def describe(self, prefix: str) -> dict[str, dict]:
        """Each object under a prefix: its content type, MD5 (base64, as the service holds it; None
        for a composite object) and metadata."""
        found, token = {}, None
        while True:
            params = {
                "prefix": prefix,
                "fields": "items(name,contentType,md5Hash,metadata),nextPageToken",
                "maxResults": 1000,
            }
            if token:
                params["pageToken"] = token
            response = self._request("GET", f"{API}/storage/v1/b/{self.bucket}/o", params=params, timeout=120)
            response.raise_for_status()
            page = response.json()
            for item in page.get("items", []):
                found[item["name"]] = {
                    "content_type": item.get("contentType") or "application/octet-stream",
                    "md5": item.get("md5Hash"),
                    "metadata": item.get("metadata") or {},
                }
            token = page.get("nextPageToken")
            if not token:
                return found

    def get(self, name: str) -> bytes:
        response = self._request(
            "GET",
            f"{API}/storage/v1/b/{self.bucket}/o/{quote(name, safe='')}",
            params={"alt": "media"},
            timeout=600,
        )
        response.raise_for_status()
        return response.content


ACCOUNT = re.compile(r"[a-z0-9]{3,24}")
CONTAINER = re.compile(r"(?=.{3,63}$)[a-z0-9](?:-?[a-z0-9])+")


class AzureBlobStore:
    """The same store in an Azure Blob container, through the Blob service's REST API. Writes are
    create-only unless replaced on purpose, so a container with an immutability policy keeps them."""

    VERSION = "2023-11-03"  # the Blob service version every request asks for
    RETRY_WAITS = CorpusStore.RETRY_WAITS

    def __init__(self, account: str, container: str, client=None, token=None):
        # Checked, so the token only ever goes to a storage account's own host.
        if not ACCOUNT.fullmatch(account) or not CONTAINER.fullmatch(container):
            raise ValueError(f"Not an Azure storage account and container: {account!r}, {container!r}")
        self.account, self.container = account, container
        self.endpoint = f"https://{account}.blob.core.windows.net/{container}"
        self.client = client or httpx.Client(timeout=120)
        self.token = token or AzureToken()

    def _request(self, method: str, name: str | None = None, *, headers=None, timeout=120, **kwargs):
        url = self.endpoint if name is None else f"{self.endpoint}/{quote(name, safe='/')}"
        for wait in (*self.RETRY_WAITS, None):
            try:
                sent = {
                    "Authorization": f"Bearer {self.token()}",
                    "x-ms-version": self.VERSION,
                    "x-ms-date": formatdate(usegmt=True),
                    **(headers or {}),
                }
                response = self.client.request(method, url, headers=sent, timeout=timeout, **kwargs)
                if response.status_code < 500 or wait is None:
                    return response
                reason = f"server error {response.status_code}"
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as error:
                if wait is None:
                    raise
                reason = type(error).__name__
            except TokenUnavailable as error:
                if wait is None:
                    raise
                reason = str(error)
            print(f"container: {reason}; trying again in {wait} s", file=sys.stderr, flush=True)
            time.sleep(wait)
        raise RuntimeError("unreachable")

    def uri(self, name: str) -> str:
        return f"az://{self.account}/{self.container}/{name}"

    def exists(self, name: str) -> bool:
        response = self._request("HEAD", name)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    def put(
        self, name: str, data: bytes, content_type: str, metadata: dict | None = None, replace: bool = False
    ) -> bool:
        """Upload a blob; unless `replace`, a blob already there is kept (True if written). The
        service checks the bytes it received against their MD5."""
        headers = {
            "x-ms-blob-type": "BlockBlob",
            "Content-Type": content_type,
            "x-ms-blob-content-type": content_type,
            "Content-MD5": _md5(data),
            **{f"x-ms-meta-{key}": _header_value(value) for key, value in (metadata or {}).items()},
        }
        if not replace:
            headers["If-None-Match"] = "*"  # only if no blob of that name exists
        response = self._request("PUT", name, headers=headers, content=data, timeout=600)
        # Already there (the same fingerprint, the same bytes): refused by the condition, or by the
        # container's immutability policy if that is looked at first.
        there = response.status_code == 412 or (
            response.status_code == 409
            and response.headers.get("x-ms-error-code") in ("BlobAlreadyExists", "BlobImmutableDueToPolicy")
        )
        if there and not replace:
            return False
        response.raise_for_status()
        return True

    def list(self, prefix: str) -> list[str]:
        """The names of the blobs under a prefix."""
        names, marker = [], None
        while True:
            params = {"restype": "container", "comp": "list", "prefix": prefix, "maxresults": 1000}
            if marker:
                params["marker"] = marker
            response = self._request("GET", params=params)
            response.raise_for_status()
            page = ElementTree.fromstring(response.content)
            for found in page.iterfind("Blobs/Blob/Name"):
                # A name with characters XML cannot carry comes percent-encoded.
                names.append(unquote(found.text) if found.get("Encoded") == "true" else found.text)
            marker = page.findtext("NextMarker")
            if not marker:
                return names

    def describe(self, prefix: str) -> dict[str, dict]:
        """Each blob under a prefix: its content type, MD5 (base64) and metadata."""
        found, marker = {}, None
        while True:
            params = {
                "restype": "container",
                "comp": "list",
                "prefix": prefix,
                "maxresults": 1000,
                "include": "metadata",
            }
            if marker:
                params["marker"] = marker
            response = self._request("GET", params=params)
            response.raise_for_status()
            page = ElementTree.fromstring(response.content)
            for blob in page.iterfind("Blobs/Blob"):
                element = blob.find("Name")
                name = unquote(element.text) if element.get("Encoded") == "true" else element.text
                found[name] = {
                    "content_type": blob.findtext("Properties/Content-Type") or "application/octet-stream",
                    "md5": blob.findtext("Properties/Content-MD5") or None,
                    "metadata": {item.tag: unquote(item.text or "") for item in blob.findall("Metadata/*")},
                }
            marker = page.findtext("NextMarker")
            if not marker:
                return found

    def get(self, name: str) -> bytes:
        response = self._request("GET", name, timeout=600)
        response.raise_for_status()
        held = response.headers.get("Content-MD5")
        if held and held != _md5(response.content):
            raise OSError(f"{name} arrived damaged: its bytes do not match the MD5 the container holds")
        return response.content


def _md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()


def _header_value(value) -> str:
    """Metadata as a header value: characters a header cannot carry are percent-encoded."""
    return quote(str(value), safe=string.punctuation + " ")


class TokenUnavailable(Exception):
    """An identity endpoint could not give a token just now: worth trying again."""


class AzureToken:
    """A bearer token for Azure Storage, kept until shortly before it expires. In order: one given in
    AZURE_STORAGE_TOKEN; the managed identity of an Azure Container App (IDENTITY_ENDPOINT and
    IDENTITY_HEADER) or of a virtual machine (the instance metadata service), the user-assigned one
    named by AZURE_CLIENT_ID if set; an operator's own, from the Azure CLI after `az login`."""

    RESOURCE = "https://storage.azure.com/"
    IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"
    MARGIN = 300  # seconds before it expires that a token is replaced

    def __init__(self, client=None, run=subprocess.run, clock=time.time):
        # Never through a proxy: both identity endpoints are on the machine itself.
        self.client = client or httpx.Client(trust_env=False, timeout=httpx.Timeout(30, connect=2))
        self.run, self.clock = run, clock
        self._value, self._expires = None, 0.0
        self._vm = None  # whether the instance metadata service is here, once asked
        self._lock = threading.Lock()

    def __call__(self) -> str:
        if os.environ.get("AZURE_STORAGE_TOKEN"):
            return os.environ["AZURE_STORAGE_TOKEN"]
        with self._lock:
            if self._value is None or self.clock() > self._expires - self.MARGIN:
                self._value, self._expires = self._fetch()
            return self._value

    def _fetch(self) -> tuple[str, float]:
        query = {"resource": self.RESOURCE}
        if os.environ.get("AZURE_CLIENT_ID"):  # a user-assigned identity
            query["client_id"] = os.environ["AZURE_CLIENT_ID"]
        if os.environ.get("IDENTITY_ENDPOINT") and os.environ.get("IDENTITY_HEADER"):
            return self._identity(
                os.environ["IDENTITY_ENDPOINT"],
                {**query, "api-version": "2019-08-01"},
                {"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]},
            )
        if self._vm is not False:
            try:
                found = self._identity(
                    self.IMDS, {**query, "api-version": "2018-02-01"}, {"Metadata": "true"}
                )
            except (TokenUnavailable, httpx.HTTPStatusError):
                if self._vm:  # a virtual machine's metadata service, not answering just now
                    raise
                self._vm = False  # asked once: away from Azure there is no metadata service
            else:
                self._vm = True
                return found
        return self._cli()

    def _identity(self, url: str, params: dict, headers: dict) -> tuple[str, float]:
        """A managed identity's token, from the endpoint that gives it out."""
        try:
            response = self.client.get(url, params=params, headers=headers)
        except httpx.TransportError as error:
            raise TokenUnavailable(f"identity endpoint unreachable ({type(error).__name__})") from error
        if response.status_code == 429 or response.status_code >= 500:
            raise TokenUnavailable(f"identity endpoint answered {response.status_code}")
        response.raise_for_status()
        answer = response.json()
        try:
            expires = float(answer["expires_on"])  # seconds since 1970, as a string
        except (KeyError, ValueError):
            expires = self.clock() + float(answer.get("expires_in") or 600)
        return answer["access_token"], expires

    def _cli(self) -> tuple[str, float]:
        """An operator's token from the Azure CLI."""
        try:
            done = self.run(
                ["az", "account", "get-access-token", "--resource", self.RESOURCE, "-o", "json"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except FileNotFoundError:
            raise RuntimeError("No Azure credential: no managed identity here and no Azure CLI") from None
        if done.returncode != 0:
            raise RuntimeError(f"The Azure CLI gave no token; run `az login` ({done.stderr.strip()[:200]})")
        answer = json.loads(done.stdout)
        # A token from the CLI has at least five minutes left; newer versions say when it expires.
        return answer["accessToken"], float(answer.get("expires_on") or self.clock() + self.MARGIN + 60)


def source_name(country: str, source: str, sha256: str, suffix: str = ".pdf") -> str:
    return f"sources/{country}/{source}/{sha256}{suffix}"
