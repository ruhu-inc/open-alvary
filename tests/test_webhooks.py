"""SUB C3 without a database: endpoint URLs, address vetting, pinning, signatures and sealing."""

import time

import httpcore
import pytest

from changes.webhooks import (
    Refused,
    Sealer,
    Sender,
    _send,
    check_url,
    public,
    signature_headers,
    verify_signature,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/hook",
        "https://localhost/hook",
        "https://api.localhost/hook",
        "https://printer.local/hook",
        "https://metadata.google.internal/computeMetadata",
        "https://127.0.0.1/hook",
        "https://10.1.2.3/hook",
        "https://169.254.169.254/latest",
        "https://[::1]/hook",
        "https://user:pass@example.org/hook",
        "https://example.org:8443/hook",
        "https://example.org/hook#part",
        "ftp://example.org/hook",
    ],
)
def test_endpoints_must_be_public_https_on_443(url):
    with pytest.raises(Refused):
        check_url(url)


def test_a_good_url_is_kept_and_the_default_port_dropped():
    assert (
        check_url("https://hooks.example.org:443/open-alvary?x=1")
        == "https://hooks.example.org/open-alvary?x=1"
    )


def test_only_global_addresses_are_public():
    assert public("93.184.216.34") and public("2606:4700:4700::1111")
    for address in (
        "10.0.0.1",
        "192.168.1.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "::1",
        "::ffff:10.0.0.1",
        "224.0.0.1",
    ):
        assert not public(address), address


def test_every_attempt_resolves_again_and_refuses_any_private_answer():
    answers = iter([["93.184.216.34"], ["127.0.0.1"], ["93.184.216.34", "10.0.0.5"]])
    sent = []
    sender = Sender(
        resolver=lambda host: next(answers), transport=lambda *a: sent.append(a) or (200, {}, b"")
    )
    assert sender.post("https://hooks.example.org/x", b"{}", {}).status == 200
    assert sent[0][1] == "93.184.216.34"  # connected to the vetted address
    rebound = sender.post("https://hooks.example.org/x", b"{}", {})
    mixed = sender.post("https://hooks.example.org/x", b"{}", {})
    assert rebound.status is None and "not reachable" in rebound.error
    assert mixed.status is None and len(sent) == 1


def test_the_connection_goes_to_the_vetted_address_not_a_new_lookup(monkeypatch):
    seen = []

    def connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        seen.append((host, port))
        raise httpcore.ConnectError("stop here")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    with pytest.raises(httpcore.ConnectError):
        _send("https://hooks.example.org/x", "93.184.216.34", b"{}", {"Content-Type": "application/json"})
    assert seen == [("93.184.216.34", 443)]


def test_signatures_verify_with_the_secret_and_fail_when_anything_changes():
    body = b'{"type":"change"}'
    headers = signature_headers(body, [("2", "whsec_new"), ("1", "whsec_old")], "whd_x")
    stamp, signature = headers["Open-Alvary-Timestamp"], headers["Open-Alvary-Signature"]
    assert headers["Open-Alvary-Key"] == "2,1" and signature.count("v1=") == 2
    assert verify_signature("whsec_new", body, stamp, signature)
    assert verify_signature("whsec_old", body, stamp, signature)  # during a rotation, either works
    assert not verify_signature("whsec_new", body + b" ", stamp, signature)
    assert not verify_signature("whsec_other", body, stamp, signature)
    old = signature_headers(body, [("1", "whsec_old")], "whd_x", timestamp=int(time.time()) - 600)
    assert not verify_signature("whsec_old", body, old["Open-Alvary-Timestamp"], old["Open-Alvary-Signature"])


def test_secrets_are_sealed_and_open_only_with_their_key():
    sealer = Sealer({"k1": b"a" * 32}, "k1")
    sealed = sealer.seal("whsec_secret")
    assert "whsec_secret" not in sealed and sealer.open(sealed) == "whsec_secret"
    with pytest.raises(RuntimeError):
        Sealer({"k1": b"b" * 32}, "k1").open(sealed)


def test_a_persistent_deployment_without_keys_has_webhooks_off(monkeypatch):
    monkeypatch.delenv("ALVARY_WEBHOOK_KEYS", raising=False)
    monkeypatch.setenv("ALVARY_RUNTIME_MODE", "persistent")
    assert Sealer.from_environment() is None


def test_a_delivery_has_one_deadline_for_the_whole_attempt(monkeypatch):
    monkeypatch.setattr("changes.webhooks.TIMEOUT", 0.2)

    def slow(*args):
        time.sleep(1)
        return 200, {}, b""

    started = time.monotonic()
    outcome = Sender(resolver=lambda host: ["93.184.216.34"], transport=slow).post(
        "https://hooks.example.org/x", b"{}", {}
    )
    assert outcome.status is None and "No answer" in outcome.error and time.monotonic() - started < 0.8
