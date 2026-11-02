GRANT SELECT,INSERT,UPDATE ON {{accounts}}.traffic_day, {{accounts}}.traffic_record_day, {{accounts}}.traffic_record_month, {{accounts}}.traffic_visitors TO {{role_guard}};
GRANT EXECUTE ON FUNCTION {{policy}}.record_traffic(jsonb) TO {{role_serving}};
GRANT EXECUTE ON FUNCTION {{policy}}.traffic_salt(bytea) TO {{role_serving}};
