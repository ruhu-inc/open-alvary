// The console API (/console/api/v1): same-origin JSON with the session cookie and CSRF token.
// Refusals carry a stable code; the sign-in ones become a way back in rather than an error.

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
  ) {
    super(message);
  }
}

let csrf = "";
export function setCsrf(token: string) {
  csrf = token;
}

export async function api<T = unknown>(
  path: string,
  options: { method?: string; body?: unknown } = {},
): Promise<T> {
  const method = options.method ?? "GET";
  let response: Response;
  try {
    response = await fetch("/console/api/v1" + path, {
      method,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...(options.body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(method !== "GET" ? { "X-CSRF-Token": csrf } : {}),
      },
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    });
  } catch {
    throw new ApiError(0, "network", "We couldn't reach Open Alvary. Check your connection and try again.");
  }
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = data?.error ?? {};
    if (error.code === "credential_required") {
      location.assign(signInUrl());
    }
    throw new ApiError(
      response.status,
      error.code ?? "unknown",
      error.message ?? "Something went wrong. Try again.",
      error.request_id,
    );
  }
  return data as T;
}

// Moves the console to another page without reloading it.
export function go(to: string) {
  history.pushState(null, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.scrollTo(0, 0);
}

export async function signOut() {
  await fetch("/console/auth/sign-out", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf },
  }).catch(() => undefined);
  location.assign("/console/sign-in");
}

export function here(): string {
  return location.pathname + location.search;
}

export function signInUrl(): string {
  return "/console/sign-in?return_to=" + encodeURIComponent(here());
}

// A fresh sign-in or second factor at the identity provider, coming back to the same place.
export function stepUpUrl(code: string, returnTo = here()): string {
  const purpose = code === "second_factor_required" ? "second_factor" : "reauthenticate";
  return `/console/auth/start?purpose=${purpose}&return_to=${encodeURIComponent(returnTo)}`;
}

export const STEP_UP = new Set(["second_factor_required", "reauthentication_required"]);

// What the person typed before stepping out to the provider, restored when they come back.
export function saveDraft(key: string, value: unknown) {
  try {
    sessionStorage.setItem("oa_draft_" + key, JSON.stringify(value));
  } catch {
    /* storage unavailable: the person retypes */
  }
}
// Read without removing: a render React discards and restarts must find the draft again.
export function peekDraft<T>(key: string): T | null {
  try {
    const raw = sessionStorage.getItem("oa_draft_" + key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}
export function clearDraft(key: string) {
  try {
    sessionStorage.removeItem("oa_draft_" + key);
  } catch {
    /* nothing to clear */
  }
}

export type Access = {
  state: "none" | "active" | "lapsed" | "revoked" | "ended" | "scheduled";
  entitlement?: string;
  tier?: { id: string; includes: string[]; allowance: Record<string, number> };
  scope?: { all?: boolean; jurisdictions?: string[] };
  ends_at?: string | null;
};
export type Organisation = {
  id: string;
  name: string;
  kind: string;
  country: string | null;
  website: string | null;
  state: string;
  personal: boolean;
  role: "owner" | "admin" | "developer" | "viewer";
  access: Access;
  revision?: number;
  counts?: { members: number; pending_invitations: number };
};
export type Me = {
  person: { id: string; email: string; display_name: string | null; second_factor: string; created_at: string };
  policies: { pending: string[]; current: Record<string, string>; paths: Record<string, string> };
  organisations: Organisation[];
  invitations: { id: string; role: string; expires_at: string; organisation: string }[];
  csrf: string;
  fresh_until: string;
  second_factor_until: string | null;
  dev_login: boolean;
};
