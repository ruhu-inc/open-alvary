GRANT INSERT ON {{policy}}.command_receipt,{{policy}}.operator_event,{{policy}}.evidence TO {{role_guard}};
GRANT UPDATE(active) ON {{policy}}.actor TO {{role_guard}};
GRANT INSERT ON {{policy}}.collection_decision,{{policy}}.verification_policy,{{policy}}.verification_material TO {{role_guard}};
GRANT EXECUTE ON FUNCTION {{policy}}.apply_operator_command(jsonb) TO {{role_review}};
