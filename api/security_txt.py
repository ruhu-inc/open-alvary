"""RFC 9116 security.txt (MAC-13): where to report a vulnerability, served on the website's domain and,
once activation B is live, the API's. The policy it points to is the website's /security page.

EXPIRES is renewed at least every six months; a test fails 30 days before it lapses, so it is never
served stale."""

from datetime import UTC, datetime

CONTACT = "hello@ruhu.ai"
EXPIRES = datetime(2027, 4, 30, tzinfo=UTC)
LANGUAGES = "en"


def security_txt(site_origin: str, api_origin: str | None = None) -> str:
    origins = dict.fromkeys(o for o in (site_origin, api_origin) if o)
    return "".join(
        f"{line}\n"
        for line in (
            f"Contact: mailto:{CONTACT}",
            f"Expires: {EXPIRES.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            f"Policy: {site_origin}/security#disclosure",
            *(f"Canonical: {origin}/.well-known/security.txt" for origin in origins),
            f"Preferred-Languages: {LANGUAGES}",
        )
    )
