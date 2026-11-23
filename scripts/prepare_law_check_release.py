"""Generate local release artefacts for Law check (LC-2, LC-3).

Produces:
  1. ES256 (P-256) receipt signing key PEM (private — upload to Secret Manager, never commit)
  2. Public JWKS (`open-alvary-receipt-keys.json`) with `kid`, `valid_from`, `valid_to`, `revoked_at: null`
  3. `agents-admission-config.json` computed from `public_pool_size` and `api_replicas` (LC-3)

Usage:
  .venv/bin/python -m scripts.prepare_law_check_release \\
      --out /tmp/law-check-secrets \\
      --public-pool-size 18 \\
      --api-replicas 1 \\
      --kid oa-receipt-2026-12
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from api.receipts import ReceiptSigner


def compute_limits(public_pool_size: int, api_replicas: int) -> dict:
    if public_pool_size <= 0 or api_replicas <= 0:
        raise ValueError("public_pool_size and api_replicas must be positive integers.")
    p = public_pool_size // api_replicas
    g = p // 3
    if p < 15 or g < 5:
        raise ValueError(
            f"Per-instance pool p={p} gives G={g} (< 5). Raise public_pool_size inside the 50-connection "
            f"DB budget so p >= 15 (G >= 5), because the runtime sends up to 4 concurrent calls per visitor "
            f"and AdmissionConfig requires identity_concurrency < global_concurrency."
        )
    admission_config = {
        "database_budget": 50,
        "public_pool_size": public_pool_size,
        "api_replicas": api_replicas,
        "capacity": 40,
        "refill_per_minute": 20,
        "identity_concurrency": 4,
        "global_concurrency": g,
        "queue_size": g * 2,
    }
    return {
        "p": p,
        "G": g,
        "plan_concurrency": 2 * g,
        "admission_config": admission_config,
    }


def generate_artefacts(
    out_dir: Path,
    *,
    public_pool_size: int,
    api_replicas: int,
    kid: str,
    valid_days: int = 395,
) -> dict:
    limits = compute_limits(public_pool_size, api_replicas)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    valid_from = now.isoformat().replace("+00:00", "Z")
    valid_to = (now + timedelta(days=valid_days)).isoformat().replace("+00:00", "Z")

    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    signer = ReceiptSigner(pem, kid=kid, valid_from=valid_from, valid_to=valid_to)

    out_dir.mkdir(parents=True, exist_ok=True)
    key_path = out_dir / "receipt-signing-key.pem"
    jwks_path = out_dir / "receipt-keys.json"
    admission_path = out_dir / "agents-admission-config.json"

    key_path.write_bytes(pem)
    key_path.chmod(0o600)
    jwks_path.write_text(json.dumps(signer.jwks(), indent=2) + "\n", encoding="utf-8")
    admission_path.write_text(json.dumps(limits["admission_config"], indent=2) + "\n", encoding="utf-8")

    return {
        "key_path": str(key_path),
        "jwks_path": str(jwks_path),
        "admission_path": str(admission_path),
        "p": limits["p"],
        "G": limits["G"],
        "plan_concurrency": limits["plan_concurrency"],
        "kid": kid,
        "valid_from": valid_from,
        "valid_to": valid_to,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare Law check signing key, JWKS, and admission config.")
    parser.add_argument("--out", type=Path, required=True, help="Untracked output directory (e.g. /tmp/law-check-secrets)")
    parser.add_argument("--public-pool-size", type=int, required=True, help="Total public_pool_size from live limits config")
    parser.add_argument("--api-replicas", type=int, required=True, help="api_replicas from live limits config")
    parser.add_argument("--kid", default="oa-receipt-2026-12", help="Receipt signing key ID")
    parser.add_argument("--valid-days", type=int, default=395, help="Validity window in days (default 395)")
    args = parser.parse_args()

    result = generate_artefacts(
        args.out,
        public_pool_size=args.public_pool_size,
        api_replicas=args.api_replicas,
        kid=args.kid,
        valid_days=args.valid_days,
    )
    print(json.dumps(result, indent=2))
    print(
        f"\nNext: confirm accounts/tiers.json law-check.allowance.concurrency == {result['plan_concurrency']} "
        f"(2 * G where G={result['G']}, p={result['p']}), then upload the three files to Secret Manager."
    )


if __name__ == "__main__":
    main()
