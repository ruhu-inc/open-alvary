"""Initial v0.1 relational corpus. This revision is independent of runtime models."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None
RELATIONS = {
    "languages": {},
    "jurisdictions": {},
    "authorities": {"jurisdiction_id": "jurisdictions"},
    "sources": {"jurisdiction_id": "jurisdictions", "authority_id": "authorities", "language": "languages"},
    "rights": {"source_id": "sources"},
    "versions": {"source_id": "sources", "language": "languages"},
    "structure": {"source_id": "sources", "version_id": "versions"},
    "citations": {"source_id": "sources"},
    "amendments": {"source_id": "sources", "target_source_id": "sources"},
    "translations": {
        "source_version_id": "versions",
        "translated_version_id": "versions",
        "language": "languages",
    },
}


def upgrade():
    for name, refs in RELATIONS.items():
        op.create_table(
            name,
            sa.Column("id", sa.String(160), primary_key=True),
            *[
                sa.Column(key, sa.String(160), sa.ForeignKey(f"{target}.id"), nullable=False)
                for key, target in refs.items()
            ],
            sa.Column("payload", sa.JSON(), nullable=False),
        )
        for key in refs:
            op.create_index(f"ix_{name}_{key}", name, [key], unique=name == "rights" and key == "source_id")


def downgrade():
    for name in reversed(RELATIONS):
        op.drop_table(name)
