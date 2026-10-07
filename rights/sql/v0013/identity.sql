ALTER TABLE {{corpus}}.representation_revision
    DROP CONSTRAINT representation_revision_id_check;
ALTER TABLE {{corpus}}.representation_revision
    ADD CONSTRAINT representation_revision_id_check
    CHECK(id ~ '^(rep|prp)_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$');

-- Historical prp_ identities remain immutable and may still transition through lifecycle.
-- All new inserts, including calls through historical RPC aliases, use the proper prefix.
CREATE FUNCTION {{policy}}.representation_identity_insert() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    IF NEW.id !~ '^rep_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' THEN
        RAISE EXCEPTION 'New representation identity requires rep prefix' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION {{policy}}.representation_identity_insert() FROM PUBLIC;
CREATE TRIGGER representation_identity_insert BEFORE INSERT ON {{corpus}}.representation_revision
FOR EACH ROW EXECUTE FUNCTION {{policy}}.representation_identity_insert();
