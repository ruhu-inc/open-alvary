GRANT SELECT ON {{policy}}.instance_binding, {{policy}}.serving_epoch, {{policy}}.recovery_floor TO {{role_guard}};
REVOKE SELECT ON {{policy}}.recovery_record FROM {{role_guard}};
