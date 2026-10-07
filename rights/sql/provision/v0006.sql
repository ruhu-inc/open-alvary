GRANT INSERT ON {{policy}}.staging_lifecycle_event,{{policy}}.lifecycle_receipt TO {{role_guard}};
GRANT EXECUTE ON FUNCTION {{policy}}.reconcile_staging(jsonb) TO {{role_acquisition}};
