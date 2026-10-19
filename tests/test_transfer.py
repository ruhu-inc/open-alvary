"""Copying one corpus store to another (operations/transfer.py): what the target lacks is written with
its content type and metadata, a ledger under state/ is replaced, a stopped run resumes, and verify
finds a damaged or missing copy."""

from blob_support import BlobService

from ingestion import corpus_store
from operations import transfer


def stores(monkeypatch):
    service = BlobService()
    monkeypatch.setattr(corpus_store, "open_store", lambda address: service.store(address.rsplit("/", 1)[-1]))
    return service, service.store("source"), service.store("target")


def test_the_copy_keeps_types_and_metadata_replaces_ledgers_and_resumes(monkeypatch):
    service, source, target = stores(monkeypatch)
    source.put(
        "corpus/sources/ng/a.pdf",
        b"%PDF the act",
        "application/pdf",
        {"sha256": "ab" * 32, "url": "https://x.gov.ng/a.pdf"},
    )
    source.put("corpus/readings/a.json", b'{"pages": []}', "application/json")
    source.put("corpus/state/ng/ledger.json", b'{"v": 2}', "application/json")
    target.put("copy/readings/a.json", b'{"pages": []}', "application/json")  # an earlier run's
    target.put("copy/state/ng/ledger.json", b'{"v": 1}', "application/json")

    report = transfer.copy("az://oatest/source/corpus/", "az://oatest/target/copy/")
    assert report == {"source_objects": 3, "copied": 2, "already_there": 1}
    copied = target.describe("copy/")
    assert copied["copy/sources/ng/a.pdf"]["content_type"] == "application/pdf"
    assert copied["copy/sources/ng/a.pdf"]["metadata"] == {
        "sha256": "ab" * 32,
        "url": "https://x.gov.ng/a.pdf",
    }
    assert target.get("copy/state/ng/ledger.json") == b'{"v": 2}'  # the ledger's latest bytes
    assert transfer.copy("az://oatest/source/corpus/", "az://oatest/target/copy/")["copied"] == 0
    assert transfer.verify("az://oatest/source/corpus/", "az://oatest/target/copy/")["ok"]


def test_verify_finds_what_is_missing_damaged_or_only_in_the_target(monkeypatch):
    service, source, target = stores(monkeypatch)
    source.put("a.pdf", b"one", "application/pdf")
    source.put("b.pdf", b"two", "application/pdf")
    target.put("a.pdf", b"one", "application/pdf")
    target.put("z.pdf", b"stray", "application/pdf")
    service.rewrite("target", "a.pdf", b"changed")
    report = transfer.verify("az://oatest/source", "az://oatest/target")
    assert report["missing"] == ["b.pdf"] and report["different"] == ["a.pdf"]
    assert report["only_in_target"] == ["z.pdf"] and not report["ok"]
    assert transfer.main(["verify", "az://oatest/source", "az://oatest/target"]) == 1


def test_a_buckets_objects_are_described_page_by_page_with_their_types_and_metadata():
    class Answer:
        def __init__(self, body):
            self.status_code, self.body = 200, body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    pages = {
        None: {"items": [{"name": "a.pdf", "contentType": "application/pdf", "md5Hash": "x==", "metadata": {"url": "u"}}],
               "nextPageToken": "p2"},
        "p2": {"items": [{"name": "b.json", "contentType": "application/json"}]},  # composite: no MD5
    }  # fmt: skip

    class Session:
        def request(self, method, url, params=None, **kwargs):
            assert method == "GET" and url.endswith("/b/bucket/o") and "md5Hash" in params["fields"]
            return Answer(pages[params.get("pageToken")])

    found = corpus_store.CorpusStore("bucket", session=Session()).describe("")
    assert found == {
        "a.pdf": {"content_type": "application/pdf", "md5": "x==", "metadata": {"url": "u"}},
        "b.json": {"content_type": "application/json", "md5": None, "metadata": {}},
    }
