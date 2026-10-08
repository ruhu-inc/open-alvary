DROP TRIGGER journal_actor_denial ON {{policy}}.actor;
DROP TRIGGER journal_privacy_denial ON {{policy}}.controller_record;
DROP TRIGGER journal_privacy_denial ON {{policy}}.privacy_review;
DROP FUNCTION {{policy}}.journal_actor_denial();
DROP FUNCTION {{policy}}.journal_privacy_denial();
DROP INDEX {{policy}}.serving_epoch_one_active;
CREATE UNIQUE INDEX serving_epoch_one_active ON {{policy}}.serving_epoch ((true)) WHERE state IN ('opening','open');
ALTER TABLE {{policy}}.serving_epoch
    DROP CONSTRAINT serving_epoch_state, DROP CONSTRAINT serving_epoch_sequence_order,
    DROP CONSTRAINT serving_epoch_close_revision, DROP CONSTRAINT serving_epoch_active,
    DROP CONSTRAINT serving_epoch_open, DROP CONSTRAINT serving_epoch_closing,
    DROP CONSTRAINT serving_epoch_closed, DROP CONSTRAINT serving_epoch_abandoned,
    ADD CHECK (state IN ('opening','open','closed','abandoned')),
    ADD CHECK (close_sequence > open_sequence),
    ADD CHECK (close_revision >= 0),
    ADD CHECK ((state IN ('open','closed')) = (open_sequence IS NOT NULL) OR state = 'abandoned'),
    ADD CHECK ((state = 'closed') = (close_sequence IS NOT NULL AND close_revision IS NOT NULL)),
    ADD CHECK ((state IN ('closed','abandoned')) = (closed_at IS NOT NULL));
ALTER TABLE {{policy}}.serving_epoch DROP COLUMN open_revision;
