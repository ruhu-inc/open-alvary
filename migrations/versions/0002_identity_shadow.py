"""Add PostgreSQL identity shadows and a transactional legacy maintenance fence.

No publication or live authority cutover is enabled. SQLite remains the pilot.
This revision owns its DDL and never imports mutable runtime contracts.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

LEGACY = (
    "languages",
    "jurisdictions",
    "authorities",
    "sources",
    "rights",
    "versions",
    "structure",
    "citations",
    "amendments",
    "translations",
)


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE TABLE identity_migration_state (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            phase text NOT NULL DEFAULT 'legacy' CHECK (phase IN ('legacy','frozen','rehearsal')),
            epoch bigint NOT NULL DEFAULT 0 CHECK (epoch >= 0),
            source_checkpoint text CHECK (source_checkpoint ~ '^[a-f0-9]{64}$'),
            reconciled_hash text CHECK (reconciled_hash ~ '^[a-f0-9]{64}$'),
            operator_evidence text,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (phase <> 'rehearsal' OR
                (source_checkpoint IS NOT NULL AND reconciled_hash = source_checkpoint
                 AND coalesce(length(btrim(operator_evidence)), 0) > 0))
        );
        INSERT INTO identity_migration_state(singleton) VALUES (true);

        CREATE TABLE identity_collections (
            id text PRIMARY KEY CHECK (id ~ '^scp_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
            jurisdiction_id varchar(160) NOT NULL REFERENCES jurisdictions(id),
            document_class text NOT NULL CHECK (document_class IN
                ('constitution','act','regulation','judgment','gazette','guidance','other')),
            provider_key text NOT NULL CHECK (length(btrim(provider_key)) BETWEEN 1 AND 160),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (jurisdiction_id,document_class,provider_key),
            UNIQUE (id,jurisdiction_id,document_class)
        );
        ALTER TABLE authorities ADD CONSTRAINT uq_authority_jurisdiction UNIQUE (id,jurisdiction_id);
        CREATE TABLE identity_works (
            id text PRIMARY KEY CHECK (id ~ '^wrk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
            jurisdiction_id varchar(160) NOT NULL REFERENCES jurisdictions(id),
            authority_id varchar(160) NOT NULL,
            document_class text NOT NULL CHECK (document_class IN
                ('constitution','act','regulation','judgment','gazette','guidance','other')),
            title text NOT NULL CHECK (length(btrim(title)) > 0),
            citation text NOT NULL CHECK (length(btrim(citation)) > 0),
            reference_url text NOT NULL CHECK (reference_url ~ '^https?://'),
            reference_kind text NOT NULL CHECK (reference_kind IN ('document','landing_page','discovery')),
            catalogue_language_id varchar(160) NOT NULL REFERENCES languages(id),
            legacy_legal_status text NOT NULL CHECK (legacy_legal_status IN
                ('unknown','current','historical','repealed','superseded')),
            status_evidence_url text CHECK (status_evidence_url ~ '^https?://'),
            catalogue_note text,
            identity_status text NOT NULL DEFAULT 'legacy_unreviewed'
                CHECK (identity_status IN ('legacy_unreviewed','identified')),
            collection_id text,
            collection_unknown_reason text,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (authority_id,jurisdiction_id) REFERENCES authorities(id,jurisdiction_id),
            FOREIGN KEY (collection_id,jurisdiction_id,document_class)
                REFERENCES identity_collections(id,jurisdiction_id,document_class),
            CHECK ((collection_id IS NULL AND coalesce(length(btrim(collection_unknown_reason)), 0) > 0)
                OR (collection_id IS NOT NULL AND collection_unknown_reason IS NULL))
        );
        CREATE TABLE identity_expressions (
            id text PRIMARY KEY CHECK (id ~ '^exp_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
            work_id text NOT NULL REFERENCES identity_works(id),
            language_id varchar(160) NOT NULL REFERENCES languages(id),
            edition_kind text NOT NULL CHECK (edition_kind IN
                ('original','official_consolidation','unofficial_consolidation','translation')),
            edition_key text NOT NULL CHECK (length(btrim(edition_key)) BETWEEN 1 AND 160),
            edition_as_of date,
            edition_date_unknown_reason text,
            authenticity text NOT NULL DEFAULT 'unknown' CHECK (authenticity IN ('unknown','official','unofficial')),
            authenticity_unknown_reason text,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (work_id,language_id,edition_key),
            CHECK ((edition_as_of IS NULL AND coalesce(length(btrim(edition_date_unknown_reason)), 0) > 0)
                OR (edition_as_of IS NOT NULL AND edition_date_unknown_reason IS NULL)),
            CHECK ((authenticity = 'unknown' AND coalesce(length(btrim(authenticity_unknown_reason)), 0) > 0)
                OR (authenticity <> 'unknown' AND authenticity_unknown_reason IS NULL))
        );
        CREATE TABLE identity_manifestations (
            id text PRIMARY KEY CHECK (id ~ '^man_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
            raw_hash text NOT NULL CHECK (raw_hash ~ '^[a-f0-9]{64}$'),
            media_type text NOT NULL CHECK (media_type ~ '^[a-z0-9.+-]+/[a-z0-9.+-]+$'),
            size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
            provenance_reference text NOT NULL CHECK (length(btrim(provenance_reference)) > 0),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE identity_work_manifestations (
            work_id text NOT NULL REFERENCES identity_works(id),
            manifestation_id text NOT NULL REFERENCES identity_manifestations(id),
            PRIMARY KEY (work_id,manifestation_id)
        );
        CREATE TABLE identity_observations (
            id text PRIMARY KEY CHECK (id ~ '^obs_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
            manifestation_id text NOT NULL REFERENCES identity_manifestations(id),
            provider_key text NOT NULL CHECK (length(btrim(provider_key)) BETWEEN 1 AND 160),
            retrieval_key text NOT NULL CHECK (length(btrim(retrieval_key)) BETWEEN 1 AND 160),
            requested_url text NOT NULL CHECK (requested_url ~ '^https?://'),
            final_url text NOT NULL CHECK (final_url ~ '^https?://'),
            observed_at timestamptz NOT NULL,
            response_status integer NOT NULL CHECK (response_status BETWEEN 100 AND 599),
            processing_config_hash text NOT NULL CHECK (processing_config_hash ~ '^[a-f0-9]{64}$'),
            acquisition_reference text NOT NULL CHECK (length(btrim(acquisition_reference)) > 0),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (provider_key,retrieval_key)
        );
        CREATE TABLE identity_legacy_sources (
            source_id varchar(160) PRIMARY KEY REFERENCES sources(id),
            work_id text NOT NULL UNIQUE REFERENCES identity_works(id),
            expression_deferred_reason text NOT NULL CHECK (length(btrim(expression_deferred_reason)) > 0),
            source_digest text NOT NULL CHECK (source_digest ~ '^[a-f0-9]{64}$')
        );
        ALTER TABLE versions ADD CONSTRAINT uq_version_source UNIQUE (id,source_id);
        CREATE TABLE identity_legacy_versions (
            version_id varchar(160) PRIMARY KEY REFERENCES versions(id),
            source_id varchar(160) NOT NULL REFERENCES identity_legacy_sources(source_id),
            raw_hash text NOT NULL CHECK (raw_hash ~ '^[a-f0-9]{64}$'),
            content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
            normalization_status text NOT NULL DEFAULT 'legacy_unqualified'
                CHECK (normalization_status = 'legacy_unqualified'),
            deferred_reason text NOT NULL CHECK (length(btrim(deferred_reason)) > 0),
            source_digest text NOT NULL CHECK (source_digest ~ '^[a-f0-9]{64}$'),
            FOREIGN KEY (version_id,source_id) REFERENCES versions(id,source_id)
        );
        CREATE TABLE identity_legacy_snapshots (
            entity_type text NOT NULL CHECK (entity_type IN
                ('languages','jurisdictions','authorities','sources','rights','versions',
                 'structure','citations','amendments','translations')),
            legacy_id varchar(160) NOT NULL,
            payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
            digest text NOT NULL CHECK (digest ~ '^[a-f0-9]{64}$'),
            PRIMARY KEY (entity_type,legacy_id)
        );
        CREATE INDEX ix_identity_works_scope ON identity_works(jurisdiction_id,id);
        CREATE INDEX ix_identity_expressions_work ON identity_expressions(work_id,id);
        CREATE INDEX ix_identity_observations_manifestation ON identity_observations(manifestation_id,id);
    """)
    # Narrow security-definer checks allow row locks without granting UPDATE on state.
    # Fixed search_path/schema-qualified relations prevent caller name hijacking.
    namespace = op.get_bind().exec_driver_sql("SELECT current_schema()").scalar_one()
    quoted = op.get_bind().dialect.identifier_preparer.quote_identifier(namespace)
    op.execute(f"""
        CREATE FUNCTION {quoted}.identity_check_legacy_authority(is_write boolean) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE current_phase text;
        BEGIN
            SELECT phase INTO current_phase FROM {quoted}.identity_migration_state
                WHERE singleton FOR SHARE;
            IF current_phase IS NULL OR current_phase = 'rehearsal'
                OR (is_write AND current_phase <> 'legacy') THEN
                RAISE EXCEPTION 'Legacy authority is write-fenced' USING ERRCODE = '55000';
            END IF;
            END $$;
        CREATE FUNCTION {quoted}.identity_legacy_write_fence() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM {quoted}.identity_check_legacy_authority(true);
            RETURN NULL;
        END $$;
        REVOKE ALL ON FUNCTION {quoted}.identity_legacy_write_fence() FROM PUBLIC;
    """)
    for table in LEGACY:
        op.execute(f"""
            CREATE TRIGGER identity_write_fence BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE
            ON {table} FOR EACH STATEMENT EXECUTE FUNCTION {quoted}.identity_legacy_write_fence();
            ALTER TABLE {table} ENABLE ALWAYS TRIGGER identity_write_fence;
        """)


def downgrade():
    # A shadow removal is a maintenance operation, never a production rollback recipe.
    if op.get_bind().dialect.name != "postgresql":
        return
    phase = (
        op.get_bind()
        .exec_driver_sql("SELECT phase FROM identity_migration_state WHERE singleton FOR UPDATE")
        .scalar_one()
    )
    if phase != "legacy":
        raise RuntimeError("Cannot remove identity shadows while legacy authority is fenced")
    for table in LEGACY:
        op.execute(f"DROP TRIGGER identity_write_fence ON {table}")
    op.execute("DROP FUNCTION identity_legacy_write_fence()")
    op.execute("DROP FUNCTION identity_check_legacy_authority(boolean)")
    for table in (
        "identity_legacy_snapshots",
        "identity_legacy_versions",
        "identity_legacy_sources",
        "identity_observations",
        "identity_work_manifestations",
        "identity_manifestations",
        "identity_expressions",
        "identity_works",
        "identity_collections",
        "identity_migration_state",
    ):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE versions DROP CONSTRAINT uq_version_source")
    op.execute("ALTER TABLE authorities DROP CONSTRAINT uq_authority_jurisdiction")
