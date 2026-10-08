# Admission values (owner-delegated best practice, 2026-10-07)

`admission.example.json` holds the initial ACS/BUD values. They are derived from the
specification's own fixtures, not measured production traffic; re-measure with
`scripts/load_fixture.py` on the real host and revise before raising any public promise.

| Setting | Value | Derivation |
|---|---|---|
| capacity / refill_per_minute | 300 / 300 | D3 fixture: 25 browsers behind one address × 8 requests/min = 200/min; 1.5× headroom. |
| identity_concurrency | 100 | Fixture: 25 browsers × up to 4 parallel requests may arrive together; no 429 allowed. |
| global_concurrency (G) | 32 | Admitted work per deployment, below the public pool; extra arrivals wait in the queue. |
| queue_size / queue_deadline_seconds | 256 / 10 | Bounded ingress; deadline equals the 10-second retrieval budget. |
| lease_seconds | 30 | Covers a bounded request; leases release on completion. |
| db_connections C / operator_reserve R / other_workers O | 100 / 10 / 10 | PostgreSQL default max_connections; R keeps withdrawal possible under saturation; O covers backup, journal delivery and reconciliation. |
| public_pool_size P / api_replicas | 40 / 2 | P <= C − R − O = 80 leaves headroom; 20 connections per replica, no overflow. |
| trusted_proxies | 1 | Caddy sets X-Forwarded-For from the real client; nginx forwards it unchanged. |
| byte capacity / refill | 64 MiB / 32 MiB per min | ≥ 10 MiB single response (MIN); 25 browsers × 3 text reads of typical statutes fit without 429. |
| global byte capacity / refill | 1 GiB / 512 MiB per min | About 70 Mbit/s sustained egress ceiling for the first collection. |
| emission_lease_seconds | 60 | 10 MiB at the 2 Mbit/s minimum supported throughput ≈ 42 s plus margin. |

Reconciliation (`ByteLedger.reconcile`): fence 30 s (write timeout plus clock skew) and
conservative settlement after 24 hours (the specification's initial bound); run every minute.

Generate the HMAC secret with `openssl rand -hex 32`, store it with the deployment secrets, and
rotate it by moving the current value to `previous_hmac_secret` for at least one refill window.

Local evidence (2026-10-07, one API replica on a development Mac, PostgreSQL 16 in Docker,
these values with `api_replicas=1`): the 60-second D3 fixture made 200 requests with no 429,
p50 17.7 ms and p95 28.5 ms. Repeated while a second address flooded 800 requests in 10 seconds,
the fixture again had no 429 (p95 25.5 ms) while the flooding address received 349 × 200 and
451 × 429. This is not production evidence; repeat on the deployed host.
