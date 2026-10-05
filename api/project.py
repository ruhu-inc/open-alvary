"""Public project details; unset identity fields stay visibly unconfirmed locally."""

import os
from typing import Literal

from pydantic import BaseModel, HttpUrl


class ProjectInfo(BaseModel):
    name: str = "Open Alvary"
    stage: str = "Metadata catalogue pilot"
    repository_url: HttpUrl | None = None
    maintainer: str | None = None
    contact_email: str | None = None
    metadata_licence: Literal["pending", "CC-BY-4.0"] = "pending"
    software_licence: str = "MIT"


def project_info() -> ProjectInfo:
    return ProjectInfo(
        repository_url=os.getenv("PUBLIC_REPOSITORY_URL") or None,
        maintainer=os.getenv("PUBLIC_MAINTAINER") or None,
        contact_email=os.getenv("PUBLIC_CONTACT_EMAIL") or None,
        metadata_licence=os.getenv("METADATA_LICENCE", "pending"),
    )
