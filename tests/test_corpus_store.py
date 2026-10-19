"""The corpus store, in a Cloud Storage bucket or an Azure Blob container: files named by fingerprint,
never overwritten; no network in these tests."""

import json
import subprocess

import httpx
import pytest
from blob_support import BlobService

from ingestion import corpus_store


class Response:
    def __init__(self, status: int, content: bytes = b""):
        self.status_code, self.content = status, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Session:
    """Cloud Storage as a dictionary, with its precondition: ifGenerationMatch=0 means 'only if absent'."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.urls: list[str] = []

    def post(self, url, params=None, data=b"", headers=None, timeout=None):
        boundary = headers["Content-Type"].split("boundary=")[1].encode()
        parts = data.split(b"--" + boundary)
        head = parts[1].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
        import json

        name = json.loads(head)["name"]
        body = parts[2].split(b"\r\n\r\n", 1)[1][: -len(b"\r\n")]
        if params.get("ifGenerationMatch") == "0" and name in self.objects:
            return Response(412)
        self.objects[name] = body
        return Response(200)

    def request(self, method, url, **kwargs):
        self.urls.append(url)
        return self.post(url, **kwargs) if method == "POST" else self.get(url, **kwargs)

    def get(self, url, params=None, timeout=None):
        from urllib.parse import unquote

        name = unquote(url.rsplit("/o/", 1)[1])
        if name not in self.objects:
            return Response(404)
        return Response(200, self.objects[name])


def test_a_file_is_stored_once_under_its_fingerprint_and_read_back_as_stored():
    store = corpus_store.CorpusStore("test-bucket", session=Session())
    name = corpus_store.source_name("rw", "minijust-gazette", "ab" * 32)
    assert name == f"sources/rw/minijust-gazette/{'ab' * 32}.pdf"
    pdf = b"%PDF-1.4 example --oa-not-a-boundary \r\n bytes"
    assert store.put(name, pdf, "application/pdf", {"sha256": "ab" * 32}) is True
    assert store.put(name, b"%PDF-1.4 other", "application/pdf") is False  # never overwritten
    assert store.get(name) == pdf and store.exists(name) and not store.exists("sources/rw/none.pdf")
    assert store.put("state/rw/sync-ledger.json", b"{}", "application/json", replace=True) is True
    assert store.put("state/rw/sync-ledger.json", b'{"v": 2}', "application/json", replace=True) is True
    assert store.uri(name) == f"gs://test-bucket/{name}"


def test_a_dropped_connection_is_retried_and_a_lasting_one_raised(monkeypatch):
    import pytest
    import requests

    monkeypatch.setattr(corpus_store.CorpusStore, "RETRY_WAITS", (0, 0))
    session = Session()
    store = corpus_store.CorpusStore("test-bucket", session=session)
    store.put("state/rw/x.json", b"{}", "application/json", replace=True)
    failures = {"left": 2}
    real = session.request

    def flaky(method, url, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise requests.exceptions.ConnectionError("Remote end closed connection without response")
        return real(method, url, **kwargs)

    session.request = flaky
    assert store.get("state/rw/x.json") == b"{}"  # two drops, then through
    failures["left"] = 10
    with pytest.raises(requests.exceptions.ConnectionError):
        store.get("state/rw/x.json")


def test_the_address_chooses_cloud_storage_or_an_azure_blob_container():
    session = Session()
    for address in ("test-bucket", "gs://test-bucket", "gs://test-bucket/"):
        store = corpus_store.open_store(address, session=session)
        assert type(store) is corpus_store.CorpusStore and store.bucket == "test-bucket"
    assert store.put("state/rw/x.json", b"{}", "application/json") and store.get("state/rw/x.json") == b"{}"
    assert store.uri("state/rw/x.json") == "gs://test-bucket/state/rw/x.json"
    assert session.urls and all(url.startswith("https://storage.googleapis.com/") for url in session.urls)
    azure = corpus_store.open_store("az://oatest/corpus", token=lambda: "t")
    assert type(azure) is corpus_store.AzureBlobStore
    assert azure.uri("state/rw/x.json") == "az://oatest/corpus/state/rw/x.json"
    assert corpus_store.split("az://oatest/corpus/collections/rw/") == (
        "az://oatest/corpus",
        "collections/rw/",
    )
    assert corpus_store.split("gs://open-alvary-corpus/collections/rw/") == (
        "open-alvary-corpus",
        "collections/rw/",
    )
    # A store is a whole container, on its account's own host: anything else is refused.
    for address in (
        "az://oatest/corpus/collections/",
        "az://evil.example/corpus",
        "az://oatest/",
        "gs://",
        "s3://x",
    ):
        with pytest.raises(ValueError):
            corpus_store.open_store(address, token=lambda: "t")


def test_a_blob_is_stored_once_listed_page_by_page_and_read_back_as_stored():
    service = BlobService()
    store = service.store()
    name = corpus_store.source_name("rw", "minijust-gazette", "ab" * 32)
    pdf = b"%PDF-1.4 example \r\n bytes"
    url = "https://www.minijust.gov.rw/Gazette n° 1.pdf"
    assert store.put(name, pdf, "application/pdf", {"sha256": "ab" * 32, "url": url}) is True
    put = service.requests[-1]
    assert put.method == "PUT" and put.headers["If-None-Match"] == "*" and put.headers["Content-MD5"]
    assert (
        put.headers["x-ms-blob-type"] == "BlockBlob"
        and put.headers["x-ms-blob-content-type"] == "application/pdf"
    )
    held = service.blobs[("corpus", name)]
    assert held["metadata"] == {
        "sha256": "ab" * 32,
        "url": "https://www.minijust.gov.rw/Gazette n%C2%B0 1.pdf",
    }
    assert store.put(name, b"%PDF-1.4 other", "application/pdf") is False  # never overwritten
    assert service.answers[-1] == ("PUT", 409, "BlobAlreadyExists") and held["data"] == pdf
    assert store.get(name) == pdf and store.exists(name) and not store.exists("sources/rw/none.pdf")
    assert store.put("state/rw/sync-ledger.json", b"{}", "application/json", replace=True) is True
    assert store.put("state/rw/sync-ledger.json", b'{"v": 2}', "application/json", replace=True) is True
    assert "If-None-Match" not in service.requests[-1].headers
    assert store.get("state/rw/sync-ledger.json") == b'{"v": 2}'
    for n in range(5):
        store.put(f"state/rw/catalogue/{n}.json", b"{}", "application/json")
    service.requests.clear()
    assert store.list("state/rw/catalogue/") == [f"state/rw/catalogue/{n}.json" for n in range(5)]
    markers = [r.url.params.get("marker") for r in service.requests]
    assert markers == [None, "2", "4"]  # three pages, each asked for from where the last ended
    assert store.list("readings/") == []
    with pytest.raises(httpx.HTTPStatusError):
        store.get("sources/rw/none.pdf")


def test_a_container_under_an_immutability_policy_keeps_what_it_holds_and_damaged_bytes_are_refused():
    service = BlobService()
    service.immutable.add("corpus")
    store = service.store()
    assert store.put("sources/rw/a.pdf", b"%PDF-1.4 a", "application/pdf") is True
    assert store.put("sources/rw/a.pdf", b"%PDF-1.4 a", "application/pdf") is False
    with pytest.raises(httpx.HTTPStatusError):
        store.put("sources/rw/a.pdf", b"%PDF-1.4 b", "application/pdf", replace=True)
    assert service.answers[-1] == ("PUT", 409, "BlobImmutableDueToPolicy")
    service.blobs[("corpus", "sources/rw/a.pdf")]["data"] = b"%PDF-1.4 damaged on the way"
    with pytest.raises(OSError, match="arrived damaged"):
        store.get("sources/rw/a.pdf")


def test_a_dropped_connection_or_a_server_error_from_the_container_is_retried(monkeypatch):
    monkeypatch.setattr(corpus_store.AzureBlobStore, "RETRY_WAITS", (0, 0))
    service = BlobService()
    store = service.store()
    store.put("state/rw/x.json", b"{}", "application/json", replace=True)
    service.failures = [httpx.RemoteProtocolError("Server disconnected without sending a response"), 503]
    assert store.get("state/rw/x.json") == b"{}"  # two failures, then through
    service.failures = [503] * 10
    with pytest.raises(httpx.HTTPStatusError):
        store.get("state/rw/x.json")
    service.failures = [httpx.ConnectError("Connection refused")] * 10
    with pytest.raises(httpx.ConnectError):
        store.get("state/rw/x.json")


def test_a_container_app_signs_in_with_its_managed_identity_and_keeps_the_token_until_near_expiry(
    monkeypatch,
):
    monkeypatch.setattr(corpus_store.AzureBlobStore, "RETRY_WAITS", (0,))
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "identity-header")
    monkeypatch.setenv("AZURE_CLIENT_ID", "user-assigned-client")
    monkeypatch.delenv("AZURE_STORAGE_TOKEN", raising=False)
    now, asked, service = [1_800_000_000.0], [], BlobService()
    starting = [503]  # the identity endpoint, not yet ready as the container starts

    def handler(request):
        if request.url.host != "localhost":
            return service.handler(request)
        asked.append(request)
        if starting:
            return httpx.Response(starting.pop())
        token = {"access_token": f"identity-token-{len(asked)}", "expires_on": str(int(now[0]) + 3600)}
        return httpx.Response(
            200, json={**token, "resource": "https://storage.azure.com/", "token_type": "Bearer"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    token = corpus_store.AzureToken(client=client, clock=lambda: now[0])
    store = corpus_store.AzureBlobStore("oatest", "corpus", client=client, token=token)
    store.put("state/rw/x.json", b"{}", "application/json")
    assert store.get("state/rw/x.json") == b"{}" and store.exists("state/rw/x.json")
    assert len(asked) == 2  # the endpoint's 503 tried again; then one token for every request
    query = asked[-1].url.params
    assert query["resource"] == "https://storage.azure.com/" and query["api-version"] == "2019-08-01"
    assert (
        query["client_id"] == "user-assigned-client"
        and asked[-1].headers["X-IDENTITY-HEADER"] == "identity-header"
    )
    assert "Authorization" not in asked[-1].headers  # the token goes only to the container
    assert {r.headers["Authorization"] for r in service.requests} == {"Bearer identity-token-2"}
    now[0] += 3600 - 200  # within five minutes of its expiry: a new one
    store.exists("state/rw/x.json")
    assert len(asked) == 3 and service.requests[-1].headers["Authorization"] == "Bearer identity-token-3"


def test_a_virtual_machine_uses_its_metadata_service_and_an_operator_the_azure_cli(monkeypatch):
    for variable in ("IDENTITY_ENDPOINT", "IDENTITY_HEADER", "AZURE_CLIENT_ID", "AZURE_STORAGE_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    asked = []

    def vm(request):
        asked.append(request)
        return httpx.Response(
            200, json={"access_token": "vm-token", "expires_on": "1800003600", "expires_in": "3599"}
        )

    vm_token = corpus_store.AzureToken(client=httpx.Client(transport=httpx.MockTransport(vm)))
    assert vm_token() == "vm-token" and asked[0].url.host == "169.254.169.254"
    assert asked[0].headers["Metadata"] == "true" and asked[0].url.params["api-version"] == "2018-02-01"

    probes, ran, now = [], [], [1_800_000_000.0]

    def away_from_azure(request):
        probes.append(request)
        raise httpx.ConnectError("No route to host")

    def az(command, **kwargs):
        ran.append(command)
        answer = {"accessToken": f"cli-token-{len(ran)}", "expires_on": int(now[0]) + 3600}
        return subprocess.CompletedProcess(command, 0, json.dumps(answer), "")

    laptop = corpus_store.AzureToken(
        client=httpx.Client(transport=httpx.MockTransport(away_from_azure)), run=az, clock=lambda: now[0]
    )
    assert laptop() == "cli-token-1" and laptop() == "cli-token-1"
    assert ran[0][:3] == ["az", "account", "get-access-token"] and "https://storage.azure.com/" in ran[0]
    now[0] += 3600
    assert laptop() == "cli-token-2" and len(probes) == 1  # the metadata service is asked about once
    monkeypatch.setenv("AZURE_STORAGE_TOKEN", "given-token")
    assert laptop() == "given-token"
    monkeypatch.delenv("AZURE_STORAGE_TOKEN")

    def no_cli(command, **kwargs):
        raise FileNotFoundError(command[0])

    nowhere = corpus_store.AzureToken(
        client=httpx.Client(transport=httpx.MockTransport(away_from_azure)), run=no_cli
    )
    with pytest.raises(RuntimeError, match="No Azure credential"):
        nowhere()


def test_a_server_error_from_the_container_is_waited_out_like_the_buckets_and_a_websites_is_not():
    from ingestion import rwanda

    def failed(url: str, status: int) -> httpx.HTTPStatusError:
        request = httpx.Request("GET", url)
        return httpx.HTTPStatusError(
            "failed", request=request, response=httpx.Response(status, request=request)
        )

    assert rwanda._network_trouble(failed("https://oatest.blob.core.windows.net/corpus/x.pdf", 503))
    assert not rwanda._network_trouble(failed("https://oatest.blob.core.windows.net/corpus/x.pdf", 403))
    assert not rwanda._network_trouble(failed("https://www.minijust.gov.rw/x.pdf", 503))
