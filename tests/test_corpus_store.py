"""The corpus bucket: files named by fingerprint, never overwritten; no network in these tests."""

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
