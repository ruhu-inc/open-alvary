DROP TRIGGER representation_identity_insert ON {{corpus}}.representation_revision;
DROP FUNCTION {{policy}}.representation_identity_insert();
ALTER TABLE {{corpus}}.representation_revision
    DROP CONSTRAINT representation_revision_id_check;
ALTER TABLE {{corpus}}.representation_revision
    ADD CONSTRAINT representation_revision_id_check
    CHECK(id ~ '^prp_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$');
