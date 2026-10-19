// What each error code means (MAC-06). Every refusal from the API, MCP, the change feed and the
// website's data carries a problem type https://open.alvary.ai/docs/errors#<code>; each code has
// an anchor on the errors page, which is generated from this list.

export const CODES: [number, string, string][] = [
  [
    400,
    "invalid_input",
    "The request is malformed: an unknown parameter, a value out of range, or a parameter sent twice. `detail` names the parameter.",
  ],
  [
    400,
    "beyond_bounds",
    "The request goes past a bound of the interface, such as a page after the 25th or a 501st result.",
  ],
  [
    400,
    "credential_not_accepted",
    "A key or token was sent where none is taken (the website's own data), or in the address instead of the Authorization header. Use /api/v1 with the key in the header.",
  ],
  [
    400,
    "invalid_filter",
    "A change feed filter is not valid: an unknown change type, too many values or a keyword query over 200 characters.",
  ],
  [400, "unsupported_filter", "The change feed cannot filter by this yet."],
  [
    400,
    "invalid_cursor",
    "The cursor or resume token is not valid, or was issued for other filters or another page size.",
  ],
  [401, "credential_required", "No key or token was sent. The WWW-Authenticate header says where to get one."],
  [
    401,
    "invalid_credentials",
    "The key or token is unknown, revoked or expired, or was issued for another resource. Create a new key in the console.",
  ],
  [403, "insufficient_scope", "The token does not include corpus:read. Connect the app again."],
  [
    403,
    "entitlement_required",
    "The organisation has no access, or its access does not include what was asked for, such as bulk releases with full text. Ask for access in the console; bulk releases are granted by agreement.",
  ],
  [
    403,
    "entitlement_lapsed",
    "The organisation's access ended or was revoked. Its keys are kept and work again when access is renewed.",
  ],
  [403, "jurisdiction_not_included", "The record or country is outside the organisation's plan."],
  [403, "account_inactive", "The person's account is suspended or closed."],
  [403, "organisation_suspended", "The organisation is suspended."],
  [403, "wrong_address", "The request did not come through open.alvary.ai or api.open.alvary.ai. Use those addresses."],
  [404, "unavailable", "There is no such record or change, or it is not public."],
  [409, "stale_cursor", "The cursor expired (after 15 minutes). Continue from your last resume token."],
  [
    409,
    "feed_reset",
    "The change feed was reset. Take a new start=now token, reconcile from a current release, then replay from that token.",
  ],
  [
    410,
    "gone",
    "The anonymous API has closed. Use /api/v1 with a key or a connected app; the change feed stays open at /api/changes.",
  ],
  [410, "withdrawn", "The file was withdrawn and is no longer served."],
  [410, "replay_window_expired", "Changes after this position are no longer kept. Reconcile from a current release."],
  [413, "result_too_large", "The answer is too large for one response."],
  [
    429,
    "rate_limited",
    "An allowance is used up for now: requests a minute, requests at once, or data a day. Retry-After says when to try again. Outside your plan's countries, requests run at the Starter rate.",
  ],
  [
    503,
    "temporarily_unavailable",
    "Access cannot be checked right now, so the request is refused rather than let through. Try again after Retry-After.",
  ],
  [503, "capacity_exhausted", "The service is at capacity for now. Try again after Retry-After."],
];

const STATUS_NAMES: Record<number, string> = {
  400: "Bad request",
  401: "Unauthorised",
  403: "Forbidden",
  404: "Not found",
  409: "Conflict",
  410: "Gone",
  413: "Too large",
  429: "Too many requests",
  503: "Unavailable",
};

export function errorsMarkdown(): string {
  const statuses = [...new Set(CODES.map(([status]) => status))];
  return [
    "Every refusal is a problem document ([RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)) with the HTTP status, a `code` and a `type` that links here. Branch on `code`, never on the wording of `detail`.",
    "```json\n" +
      JSON.stringify(
        {
          type: "https://open.alvary.ai/docs/errors#rate_limited",
          title: "Request refused",
          status: 429,
          detail: "Too many requests. Wait for the time in Retry-After.",
          code: "rate_limited",
          retryable: true,
          request_id: "3f1c9a2e7b4d4f0e",
          retry_after_seconds: 12,
        },
        null,
        2,
      ) +
      "\n```",
    "`retryable` says whether the same request may succeed later; when it is true, wait for `Retry-After` (seconds) before trying again. Quote `request_id` when you write to us about a request. MCP tools give the same codes in the JSON-RPC error's `data.code`.",
    ...statuses.flatMap((status) => [
      `## ${status} ${STATUS_NAMES[status] ?? ""}`.trimEnd(),
      ...CODES.filter(([s]) => s === status).flatMap(([, code, meaning]) => [`### \`${code}\` {#${code}}`, meaning]),
    ]),
  ].join("\n\n");
}
