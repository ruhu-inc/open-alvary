"""Uptime probe for the public site: homepage identity and API readiness, read-only.

Exit 0 when healthy, 1 otherwise; prints one safe status line (no response bodies).
"""

import argparse
import time

import httpx


def probe(base_url: str, timeout: float, transport=None) -> tuple[bool, str]:
    started = time.perf_counter()
    try:
        with httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, follow_redirects=True, transport=transport
        ) as client:
            home = client.get("/")
            if home.status_code != 200 or "Open Alvary" not in home.text:
                return False, f"homepage status {home.status_code}"
            ready = client.get("/api/healthz")
            if ready.status_code != 200 or ready.json() != {"status": "ok"}:
                return False, f"readiness status {ready.status_code}"
    except (httpx.HTTPError, ValueError) as error:
        return False, f"unreachable ({type(error).__name__})"
    return True, f"healthy in {round((time.perf_counter() - started) * 1000)} ms"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--timeout", type=float, default=10)
    healthy, detail = probe(**vars(parser.parse_args()))
    print(("PASS: " if healthy else "FAIL: ") + detail)
    raise SystemExit(0 if healthy else 1)
