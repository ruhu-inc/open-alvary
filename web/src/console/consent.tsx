// Approving an app (MAC-07): the sign-in service sends people here when Claude, ChatGPT or another
// tool asks to read Open Alvary for them. The page names the app and how far it is known, says
// where it returns, and asks which organisation the connection is for.

import React, { useEffect, useState } from "react";
import { ApiError } from "./api";
import { Mark, Pill, Problem } from "./ui";

type Session = {
  client: {
    client_id: string;
    name: string;
    label: "verified_publisher" | "from_host" | "unverified";
    publisher: string | null;
    host: string | null;
    returns_to: string;
    loopback: boolean;
    resource: "mcp" | "api";
  };
  email: string;
  account: boolean;
  policies_pending: boolean;
  organisations: { id: string; name: string; eligible: boolean; reason: string | null }[];
};

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch("/console/oauth/api" + path, {
    credentials: "same-origin",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    ...init,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(response.status, data?.error?.code ?? "unknown", data?.error?.message ?? "Something went wrong.");
  }
  return data as T;
}

export function Consent() {
  const params = new URLSearchParams(location.search);
  const token = params.get("session_token") ?? "";
  const state = params.get("state") ?? "";
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [choice, setChoice] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    document.title = "Approve an app · Open Alvary";
    call<Session>("/session?session_token=" + encodeURIComponent(token)).then(
      (data) => {
        setSession(data);
        setChoice(data.organisations.find((o) => o.eligible)?.id ?? "");
      },
      setError,
    );
  }, [token]);
  async function decide(decision: "allow" | "deny") {
    setBusy(true);
    setError(null);
    try {
      const result = await call<{ redirect: string }>("/decision", {
        method: "POST",
        body: JSON.stringify({ session_token: token, state, decision, organisation: choice }),
      });
      location.assign(result.redirect);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  }
  if (!session) {
    return (
      <div className="solo">
        <main className="solo-card" id="main" tabIndex={-1}>
          <Mark size={36} />
          {error ? <Problem error={error} /> : <p className="fine">Loading…</p>}
        </main>
      </div>
    );
  }
  const { client } = session;
  const eligible = session.organisations.filter((o) => o.eligible);
  return (
    <div className="solo">
      <main className="solo-card wide" id="main" tabIndex={-1}>
        <Mark size={36} />
        <h1>{client.name} wants to connect to Open Alvary</h1>
        <div className="row">
          {client.label === "verified_publisher" ? (
            <Pill tone="good">Verified publisher · {client.publisher}</Pill>
          ) : client.label === "from_host" ? (
            <Pill tone="warn">From {client.host}</Pill>
          ) : (
            <Pill tone="bad">Unverified app</Pill>
          )}
          <span className="fine">Signed in as {session.email || "your account"}</span>
        </div>
        {client.label === "unverified" ? (
          <div className="callout bad">We haven't reviewed this app. Approve it only if you trust it and started this yourself.</div>
        ) : null}
        <div className="card tint">
          <b>It will be able to</b>
          <span>Read the published Open Alvary corpus within your organisation's access.</span>
          <span className="fine">It can't change anything, see your keys or act for other organisations.</span>
        </div>
        {!session.account || session.policies_pending ? (
          <div className="callout">
            {session.account
              ? "Our terms have changed. Accept them in the console first, then connect again."
              : "You don't have an Open Alvary account yet. Create one in the console and get access, then connect again."}{" "}
            <a href="/console" target="_blank" rel="noreferrer">
              Open the console
            </a>
          </div>
        ) : eligible.length === 0 ? (
          <div className="callout">
            None of your organisations can connect apps yet: an organisation needs access, and your role must allow
            connecting apps.{" "}
            <a href="/console/access" target="_blank" rel="noreferrer">
              Request access
            </a>
          </div>
        ) : (
          <fieldset className="field">
            <legend>For which organisation?</legend>
            {session.organisations.map((o) => (
              <label key={o.id} className="choice">
                <input type="radio" name="organisation" disabled={!o.eligible} checked={choice === o.id} onChange={() => setChoice(o.id)} />
                <span>
                  {o.name}
                  {o.reason ? <small>{o.reason}</small> : null}
                </span>
              </label>
            ))}
          </fieldset>
        )}
        {client.loopback ? (
          <div className="callout">
            {client.name} finishes signing in on your own computer. Approve only if you started this from {client.name}{" "}
            just now.
          </div>
        ) : (
          <p className="fine">
            You'll go back to <b>{client.returns_to || "the app"}</b>.
          </p>
        )}
        <Problem error={error} />
        <div className="row">
          <button className="btn" type="button" disabled={busy || !choice} onClick={() => decide("allow")}>
            Approve
          </button>
          <button className="btn secondary" type="button" disabled={busy} onClick={() => decide("deny")}>
            Deny
          </button>
        </div>
        <p className="fine">You can disconnect it any time under Connected apps in the console.</p>
      </main>
    </div>
  );
}
