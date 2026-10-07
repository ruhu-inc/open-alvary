"""Foundation handoff scenarios; original synthetic data, no acquisition or approval."""

from hashlib import sha256

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_postgres import pg_engine as pg_engine
from test_postgres_policy import foundation as foundation

from api.catalogue import TargetCatalogue
from rights.database import PolicyRepository
from schema.identity import mint

pytestmark = pytest.mark.postgres


def test_issue_and_three_notices_share_evidence_without_merging_identity_or_permission(foundation):
    f = foundation
    issue, *notices = [mint("wrk") for _ in range(4)]
    issue_scope, item_scope = mint("scp"), mint("scp")
    manifestation = mint("man")
    raw = b"Synthetic issue only.\nNotice A: test.\nNotice B: test.\nNotice C: test."
    with f.engine.begin() as conn:
        conn.execute(
            text("INSERT INTO authorities(id,jurisdiction_id,payload) VALUES ('synthetic-issuer','ng','{}')")
        )
        for scope, kind in ((issue_scope, "gazette"), (item_scope, "guidance")):
            conn.execute(
                text("""
                INSERT INTO identity_collections(id,jurisdiction_id,document_class,provider_key)
                VALUES (:id,'ng',:kind,'synthetic-issue-provider')
                """),
                {"id": scope, "kind": kind},
            )
        conn.execute(
            text("""
            INSERT INTO identity_manifestations(id,raw_hash,media_type,size_bytes,provenance_reference)
            VALUES (:id,:hash,'text/plain',:size,'Original synthetic fixture; no retrieved legal artifact')
            """),
            {"id": manifestation, "hash": sha256(raw).hexdigest(), "size": len(raw)},
        )
        for index, work in enumerate((issue, *notices)):
            scope = issue_scope if index == 0 else item_scope
            conn.execute(
                text("""
                INSERT INTO identity_works(id,jurisdiction_id,authority_id,document_class,title,citation,
                    reference_url,reference_kind,catalogue_language_id,legacy_legal_status,collection_id)
                VALUES (:id,'ng','synthetic-issuer',:kind,:title,:citation,
                    'https://example.test/fixture','document','en','unknown',:scope)
                """),
                {
                    "id": work,
                    "kind": "gazette" if index == 0 else "guidance",
                    "title": f"Synthetic {'issue' if index == 0 else 'notice'} {index}",
                    "citation": f"SYNTHETIC-{index}",
                    "scope": scope,
                },
            )
            conn.execute(
                text("""
                INSERT INTO identity_expressions(id,work_id,language_id,edition_kind,edition_key,
                    edition_date_unknown_reason,authenticity_unknown_reason)
                VALUES (:id,:work,'en','original','synthetic-edition',
                    'No legal edition date established','Synthetic fixture; authenticity not assessed')
                """),
                {"id": mint("exp"), "work": work},
            )
            conn.execute(
                text("INSERT INTO identity_work_manifestations VALUES (:work,:manifestation)"),
                {"work": work, "manifestation": manifestation},
            )
            # A byte relationship cannot publish a work or manufacture a decision.
            conn.execute(
                text(f"""
                INSERT INTO {f.name(conn, "corpus", "metadata_projection")}
                    (work_id,collection_id,title,citation,reference_url)
                VALUES (:work,:scope,'Synthetic metadata','SYNTHETIC','https://example.test/fixture')
                """),
                {"work": work, "scope": scope},
            )
        assert conn.scalar(text("SELECT count(*) FROM identity_work_manifestations")) == 4
        assert conn.scalar(text("SELECT count(DISTINCT work_id) FROM identity_expressions")) == 4
        assert conn.scalar(text("SELECT count(*) FROM versions")) == 0
    with pytest.raises(IntegrityError), f.engine.begin() as conn:
        conn.execute(
            text("UPDATE identity_works SET collection_id=:scope WHERE id=:id"),
            {"scope": issue_scope, "id": notices[0]},
        )
    with f.as_role("serving") as conn:
        catalogue = TargetCatalogue(PolicyRepository(conn, f.ns))
        assert catalogue.page().items == ()
        assert all(catalogue.metadata(work) is None for work in (issue, *notices))
        assert not PolicyRepository(conn, f.ns).public_decision(issue_scope, "redistribute_text").allowed


def test_nonempty_foundation_downgrade_preserves_decisions_audit_and_revision(foundation):
    f = foundation
    f.decision(redistribute_metadata="deny")
    with pytest.raises(RuntimeError, match="requires forward repair"), f.engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0002")
    with f.engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0003"
    with f.as_role("review") as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'collection_decision')}")) == 1
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')}")) == 1
