// What each error code means (MAC-06). Every refusal from the API, MCP, the change feed and the
// website's data carries a problem type https://open.alvary.ai/docs/errors#<code>; each code has
// an anchor here.

import React from "react";
import { Link, PageTitle } from "./site";

const CODES: [number, string, string][] = [
  [400, "invalid_input", "The request is malformed: an unknown parameter, a value out of range, or a parameter sent twice."],
  [400, "beyond_bounds", "The request goes past a bound of the interface, such as a page after the 25th or a 501st result."],
  [400, "credential_not_accepted", "A key or token was sent to the website's own data, which takes none. Use /api/v1 with it."],
  [400, "invalid_filter", "A change feed filter is not valid: an unknown change type, too many values or a keyword query over 200 characters."],
  [400, "unsupported_filter", "The change feed cannot filter by this yet."],
  [400, "invalid_cursor", "The cursor or resume token is not valid, or was issued for other filters or another page size."],
  [401, "credential_required", "No key or token was sent. The WWW-Authenticate header says where to get one."],
  [401, "invalid_credentials", "The key or token is unknown, revoked or expired, or was issued for another resource."],
  [403, "insufficient_scope", "The token does not include corpus:read."],
  [403, "entitlement_required", "The organisation has no access. Ask for it in the console."],
  [403, "entitlement_lapsed", "The organisation's access ended or was revoked. Its keys are kept and work again when access is renewed."],
  [403, "jurisdiction_not_included", "The record or country is outside the organisation's plan."],
  [403, "account_inactive", "The person's account is suspended or closed."],
  [403, "organisation_suspended", "The organisation is suspended."],
  [404, "unavailable", "There is no such record, or it is not public."],
  [409, "stale_cursor", "The cursor expired. Continue from your last resume token."],
  [409, "feed_reset", "The change feed was reset. Take a new start=now token, reconcile from a current release, then replay from that token."],
  [410, "gone", "The anonymous API has closed. Use /api/v1 with a key or a connected app; the change feed stays open at /api/changes."],
  [410, "withdrawn", "The file was withdrawn and is no longer served."],
  [410, "replay_window_expired", "Changes after this position are no longer kept. Reconcile from a current release."],
  [413, "result_too_large", "The answer is too large for one response."],
  [429, "rate_limited", "An allowance is used up for now; Retry-After says when to try again. Outside your plan's countries, requests run at the Starter rate."],
  [503, "temporarily_unavailable", "Access cannot be checked right now, so the request is refused rather than let through. Try again after Retry-After."],
  [503, "capacity_exhausted", "The service is at capacity for now. Try again after Retry-After."],
];

export function ErrorsPage() {
  const statuses = [...new Set(CODES.map(([status]) => status))];
  return (
    <div className="page legal">
      <PageTitle label="DEVELOPERS" title="Errors.">
        Every refusal is a problem document (RFC 9457) with a status, a code and a type that links here. See the{" "}
        <Link to="/developers">developer pages</Link> for how to get access.
      </PageTitle>
      {statuses.map((status) => (
        <section key={status}>
          <h2>{status}</h2>
          <ul>
            {CODES.filter(([s]) => s === status).map(([, code, meaning]) => (
              <li key={code} id={code}>
                <code>{code}</code>: {meaning}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
