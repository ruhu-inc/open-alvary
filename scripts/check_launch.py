"""Fail closed on owner/deployment configuration; does not establish legal rights."""

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from api.project import project_info


def launch_errors(environ=None, data_licence_text=None):
    env = environ if environ is not None else os.environ
    errors = []
    url = urlsplit(env.get("PUBLIC_REPOSITORY_URL", ""))
    if url.scheme != "https" or url.netloc != "github.com" or not re.fullmatch(r"/[^/]+/[^/]+/?", url.path):
        errors.append("PUBLIC_REPOSITORY_URL must identify the public GitHub repository")
    if not env.get("PUBLIC_MAINTAINER", "").strip():
        errors.append("PUBLIC_MAINTAINER is required")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", env.get("PUBLIC_CONTACT_EMAIL", "")):
        errors.append("PUBLIC_CONTACT_EMAIL must be a monitored email address")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", env.get("PUBLIC_DOMAIN", "")):
        errors.append("PUBLIC_DOMAIN must be a hostname without scheme or path")
    if not re.fullmatch(r"[A-Za-z0-9]{32,}", env.get("POSTGRES_PASSWORD", "")):
        errors.append("POSTGRES_PASSWORD must be a generated alphanumeric secret of at least 32 characters")
    licence_text = data_licence_text if data_licence_text is not None else Path("DATA-LICENSE.md").read_text()
    if env.get("METADATA_LICENCE") != "CC-BY-4.0" or "Status: approved" not in licence_text:
        errors.append("Original metadata licence requires recorded owner approval")
    return errors


if __name__ == "__main__":
    errors = launch_errors()
    if errors:
        print("Launch blocked:\n" + "\n".join(f"- {e}" for e in errors))
        sys.exit(1)
    project_info()
    print(
        "Configuration checks passed. Verify DNS, monitored contact, public repository and live smoke checks."
    )
