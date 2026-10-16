"""Account email (ACS-07, MAC-04, MAC-08): invitations, access requests and decisions, sign-in changes.

Sent through Resend's HTTP API from Ruhu, Inc.'s verified domain (ALVARY_MAIL_FROM, by default
"Open Alvary <no-reply@ruhu.ai>"), with RESEND_API_KEY from Secret Manager. Without a
key, messages are only logged (local trials, tests). Mail never decides an outcome: the change is
already committed, a failed send is logged and retried a few times, and nothing waits for it, so
delivery timing cannot reveal whether an address has an account (ACS-02).
"""

import logging
import os
import time
from dataclasses import dataclass
from html import escape

import httpx

log = logging.getLogger("open_alvary.mail")
RESEND = "https://api.resend.com/emails"


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str  # the message; the HTML version is built from it
    headers: tuple[tuple[str, str], ...] = ()  # for example List-Unsubscribe (RFC 8058)


class Mailer:
    def __init__(self, api_key: str | None, sender: str, reply_to: str, client: httpx.Client | None = None):
        self.api_key, self.sender, self.reply_to = api_key, sender, reply_to
        self.http = client or httpx.Client(timeout=15)

    @classmethod
    def from_environment(cls) -> "Mailer":
        return cls(
            os.getenv("RESEND_API_KEY") or None,
            os.getenv("ALVARY_MAIL_FROM", "Open Alvary <no-reply@ruhu.ai>"),
            os.getenv("ALVARY_MAIL_REPLY_TO", "hello@ruhu.ai"),
        )

    def send(self, message: Message) -> bool:
        if not self.api_key:
            log.info("mail not sent (no RESEND_API_KEY): %s", message.subject)
            return False
        body = {
            "from": self.sender,
            "to": [message.to],
            "reply_to": self.reply_to,
            "subject": message.subject,
            "text": message.text,
            "html": html(message.text),
        }
        if message.headers:
            body["headers"] = dict(message.headers)
        for wait in (1, 4, 0):
            try:
                answer = self.http.post(
                    RESEND, json=body, headers={"Authorization": f"Bearer {self.api_key}"}
                )
                if answer.status_code < 300:
                    return True
                if answer.status_code < 500 and answer.status_code != 429:
                    break  # a refusal that a retry cannot fix
            except httpx.HTTPError:
                pass
            if wait:
                time.sleep(wait)
        log.warning("mail not delivered: %s", message.subject)  # never the address or the content
        return False


def html(text: str) -> str:
    paragraphs = "".join(f"<p>{escape(p).replace(chr(10), '<br>')}</p>" for p in text.strip().split("\n\n"))
    return (
        '<div style="font-family:Helvetica,Arial,sans-serif;font-size:15px;line-height:1.55;color:#22372f;'
        f'max-width:560px">{paragraphs}</div>'
    )


SIGN_OFF = "Open Alvary\nOperated by Ruhu, Inc. · https://open.alvary.ai"


def invitation(to: str, organisation: str, inviter: str, role: str, base: str) -> Message:
    return Message(
        to,
        f"You're invited to {organisation} on Open Alvary",
        f"{inviter} invited you to join {organisation} on Open Alvary as {role}.\n\n"
        f"To accept, sign in at {base}/console with this email address ({to}). The invitation is on the "
        "overview page and expires in 7 days.\n\n"
        "If you weren't expecting this, you can ignore it.\n\n" + SIGN_OFF,
    )


def request_received(to: str, organisation: str, base: str, plan: str | None = None) -> Message:
    asked = f"the {plan} plan" if plan else "more access to the Open Alvary API and MCP"
    return Message(
        to,
        "We received your Open Alvary request",
        f"Thank you. We received the request for {organisation} to move to {asked}.\n\n"
        f"We'll email you when it's decided, or if we need more information. You can follow it under Plan "
        f"and limits at {base}/console/access.\n\n" + SIGN_OFF,
    )


def request_for_operator(
    to: str, organisation: str, reduced: bool, base: str, plan: str | None = None
) -> Message:
    kind = (f"the {plan} plan" if plan else "more access") + (", free or reduced" if reduced else "")
    return Message(
        to,
        f"New Open Alvary access request: {organisation}",
        f"{organisation} asked for {kind}. List open requests with:\n\n"
        "python -m accounts.operator --operator <you> requests\n\n" + SIGN_OFF,
    )


def access_granted(to: str, organisation: str, until: str | None, base: str) -> Message:
    period = f" until {until}" if until else ""
    return Message(
        to,
        f"Access granted for {organisation}",
        f"{organisation} now has access to the Open Alvary API and MCP{period}.\n\n"
        f"Next: owners and admins set up a second factor, then create an API key or connect Claude, "
        f"ChatGPT or Codex at {base}/console.\n\n" + SIGN_OFF,
    )


def access_declined(to: str, organisation: str, reason: str, renewable: str, base: str) -> Message:
    return Message(
        to,
        f"Your Open Alvary access request for {organisation}",
        f"We can't grant access for {organisation} at this time.\n\nReason: {reason}\n\n"
        f"You can ask again after {renewable}, or sooner if you have new information, at "
        f"{base}/console/access.\n\n" + SIGN_OFF,
    )


def more_information(to: str, organisation: str, question: str, base: str) -> Message:
    return Message(
        to,
        f"We need more information: {organisation}",
        f"To decide on the access request for {organisation}, we need more information:\n\n{question}\n\n"
        f"Reply to this email or update the request at {base}/console/access.\n\n" + SIGN_OFF,
    )


def access_revoked(to: str, organisation: str, base: str) -> Message:
    return Message(
        to,
        f"Access ended for {organisation}",
        f"{organisation}'s access to the Open Alvary API and MCP has ended. Keys and connected apps are "
        f"kept, and work again if access is granted later.\n\nQuestions or an appeal: reply to this email. "
        f"Details at {base}/console/access.\n\n" + SIGN_OFF,
    )


def sign_in_changed(to: str, added: bool, method: str, base: str) -> Message:
    what = {"google": "Google", "email": "Email code"}.get(method, method)
    change = "added to" if added else "removed from"
    return Message(
        to,
        f"A sign-in method was {'added' if added else 'removed'}",
        f"{what} was {change} your Open Alvary account.\n\nIf this wasn't you, sign in at {base}/console, "
        "review Security, sign out everywhere, and tell us by replying to this email.\n\n" + SIGN_OFF,
    )


def digest(
    to: str,
    cadence: str,
    groups: list[tuple[str, list[dict], str | None]],
    extra: int,
    base: str,
    stop_all: str,
) -> Message:
    """A watch digest: per watch, each change's label, title, date, summary and link. No full text,
    no tracking. ``groups`` holds (watch name, events, link to stop this watch's email)."""
    count = sum(len(events) for _, events, _ in groups) + extra
    noun = "change" if count == 1 else "changes"
    subject = f"Open Alvary: {count} {noun} for your monitors" + (" this week" if cadence == "weekly" else "")
    parts = [f"Here is what changed in the catalogue for your monitors ({cadence} digest)."]
    for name, events, stop in groups:
        lines = [name]
        for e in events:
            title = (
                (e.get("work") or {}).get("title") or (e.get("jurisdiction") or {}).get("name") or "A record"
            )
            lines.append(
                f"- {LABELS.get(e['event_type'], e['event_type'])}: {title} ({e['committed_at'][:10]})"
            )
            lines.append(f"  {e['summary']}")
            if (e.get("work") or {}).get("url"):
                lines.append(f"  {e['work']['url']}")
        if stop:
            lines.append(f"Stop emails for this monitor: {stop}")
        parts.append("\n".join(lines))
    if extra:
        parts.append(f"And {extra} more in your inbox.")
    parts.append(
        f"See everything in your inbox: {base}/console/monitor\n"
        "These are changes to what Open Alvary holds, not every change in a country's law."
    )
    parts.append(f"Stop all digest emails: {stop_all}")
    return Message(
        to,
        subject,
        "\n\n".join(parts) + "\n\n" + SIGN_OFF,
        (("List-Unsubscribe", f"<{stop_all}>"), ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click")),
    )


def watches_paused(to: str, organisation: str, count: int, base: str) -> Message:
    noun = "monitor was" if count == 1 else "monitors were"
    return Message(
        to,
        f"{count} {noun} paused for {organisation}",
        f"{organisation}'s plan includes fewer monitors than it has, so {count} {noun} paused, newest first. "
        f"Nothing was deleted. Resume them after removing others, or see Plan and limits at "
        f"{base}/console/access.\n\n" + SIGN_OFF,
    )


def webhook_stopped(to: str, organisation: str, url: str, state: str, why: str, base: str) -> Message:
    action = "disabled" if state == "disabled" else "paused"
    return Message(
        to,
        f"A webhook endpoint was {action} for {organisation}",
        f"The webhook endpoint {url} {why}, so Open Alvary {action} it. Matches are kept in the console "
        f"inbox. Fix the endpoint, then verify it again under Webhooks at {base}/console/webhooks.\n\n"
        + SIGN_OFF,
    )


LABELS = {
    "catalogue_added": "Added",
    "metadata_corrected": "Corrected",
    "text_version_published": "Text published",
    "withdrawal": "Withdrawn",
    "coverage_changed": "Coverage",
}
