GRANT INSERT,UPDATE(state,payload,canonical_bytes) ON {{staging}}.canonical_proposal TO {{role_guard}};
GRANT EXECUTE ON FUNCTION {{policy}}.canonical_inputs(jsonb) TO {{role_acquisition}};
GRANT EXECUTE ON FUNCTION {{policy}}.store_canonical_proposal(jsonb,bytea,jsonb) TO {{role_acquisition}};
GRANT EXECUTE ON FUNCTION {{policy}}.read_canonical_proposal(uuid,text) TO {{role_acquisition}};
