"""A privacy block is lifted only by the command that lifts it (findings 2026-11-16, item 36).

An operator's confidential withdrawal sets a rights row's privacy_status to "blocked". A publishing run or
the seed that saved a copy of the catalogue read before the withdrawal could write the old status back
(api.store now refuses such a save). As the last guard, the database itself refuses any change that takes
a row out of "blocked", unless the transaction says it is the lift (python -m changes.admin
lift-privacy-block sets open_alvary.lift_privacy_block for its own transaction).
"""

from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

GUARD = """
CREATE FUNCTION privacy_block_kept() RETURNS trigger
LANGUAGE plpgsql SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
BEGIN
    IF OLD.payload->>'privacy_status' = 'blocked'
       AND NEW.payload->>'privacy_status' IS DISTINCT FROM 'blocked'
       AND coalesce(current_setting('open_alvary.lift_privacy_block', true), '') <> 'on' THEN
        RAISE EXCEPTION 'privacy block on % is lifted only by lift-privacy-block', OLD.source_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER rights_privacy_block_kept BEFORE UPDATE ON rights
    FOR EACH ROW EXECUTE FUNCTION privacy_block_kept();
"""


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(GUARD)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER rights_privacy_block_kept ON rights")
    op.execute("DROP FUNCTION privacy_block_kept()")
