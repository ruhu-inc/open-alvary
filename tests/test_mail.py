"""Account email (ACS-07): messages say what happened and what to do; sending never blocks a change."""

import json

import httpx

from accounts import mail


def recorder(statuses):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(statuses.pop(0) if statuses else 200, json={"id": "x"})

    return sent, httpx.Client(transport=httpx.MockTransport(handler))


def test_messages_are_sent_through_resend_with_our_sender_and_reply_address(monkeypatch):
    monkeypatch.setattr(mail.time, "sleep", lambda s: None)
    sent, client = recorder([])
    mailer = mail.Mailer("re_test", "Open Alvary <no-reply@ruhu.ai>", "hello@ruhu.ai", client=client)
    message = mail.invitation(
        "guest@example.org", "Legal Clinic", "Amina Uwase", "developer", "https://open.alvary.ai"
    )
    assert mailer.send(message)
    [body] = sent
    assert body["to"] == ["guest@example.org"] and body["reply_to"] == "hello@ruhu.ai"
    assert body["from"] == "Open Alvary <no-reply@ruhu.ai>"
    assert (
        "Amina Uwase invited you to join Legal Clinic" in body["text"] and "expires in 7 days" in body["text"]
    )
    assert "<p>" in body["html"] and "<script" not in mail.html("<script>x</script>")


def test_failures_are_retried_only_when_a_retry_can_help(monkeypatch):
    monkeypatch.setattr(mail.time, "sleep", lambda s: None)
    sent, client = recorder([500, 502, 200])
    mailer = mail.Mailer("re_test", "a@b.c", "r@b.c", client=client)
    assert mailer.send(mail.request_received("a@example.org", "Org", "https://x")) and len(sent) == 3
    sent, client = recorder([422])
    assert not mail.Mailer("re_test", "a@b.c", "r@b.c", client=client).send(
        mail.request_received("a@example.org", "Org", "https://x")
    )
    assert len(sent) == 1


def test_without_a_key_nothing_is_sent():
    sent, client = recorder([])
    assert not mail.Mailer(None, "a@b.c", "r@b.c", client=client).send(
        mail.access_granted("a@example.org", "Org", None, "https://x")
    )
    assert sent == []


def test_decisions_say_what_to_do_next():
    declined = mail.access_declined(
        "a@example.org", "Org", "Not enough detail", "14 November 2026", "https://x"
    )
    assert "Reason: Not enough detail" in declined.text and "after 14 November 2026" in declined.text
    granted = mail.access_granted("a@example.org", "Org", "14 January 2027", "https://x")
    assert "until 14 January 2027" in granted.text and "second factor" in granted.text
    changed = mail.sign_in_changed("a@example.org", True, "google", "https://x")
    assert changed.subject == "A sign-in method was added" and "Google was added" in changed.text
