# Open Alvary Receipt Verifier (`oa-receipt-1`)

Zero-dependency ES module for verifying Open Alvary Law check receipts in browsers and Node 20+ using Web Crypto.

**License:** MIT (Copyright (c) 2026 Ruhu, Inc.)

## Files

- `receipt.js` — RFC 8785 (JCS) canonicalizer, `claimDigest(claim)`, `loadReceiptKeys()`, and `verifyReceipt(receipt, jwks, { claim, now })`.
- `oa-receipt-1.schema.json` — JSON Schema Draft 2020-12 for the `{ receipt, claim }` envelope.
- `vectors.json` — Real ES256-signed golden test vectors (`valid`, `tampered`, `retired_key`, `revoked_key`, `wrong_claim_digest`, `unsupported_version`).
- `receipt.test.mjs` — `node --test` conformance test suite.

## Usage

```js
import { loadReceiptKeys, verifyReceipt } from "./receipt.js";

const jwks = await loadReceiptKeys();
const result = await verifyReceipt(envelope.receipt, jwks, {
  claim: envelope.claim,
});

if (!result.ok) {
  console.error("Receipt did not verify:", result.reason, result.summary);
} else {
  console.log(result.status, result.ageDays, result.summary);
}
```

`verifyReceipt` always returns `{ ok, status, reason, ageDays, summary }` — never a bare boolean — so callers always inspect the receipt's `status` (such as `matches`, `words_found`, `wording_differs`, or `located`) rather than treating a valid signature alone as proof.

## Running the conformance tests

```bash
node --test clients/receipt/receipt.test.mjs
```
