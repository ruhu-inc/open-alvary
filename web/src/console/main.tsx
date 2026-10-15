// The developer console (MAC-10) at /console: sign-in, first-sign-in terms, and the console shell.

import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/dm-sans/latin-400.css";
import "@fontsource/dm-sans/latin-500.css";
import "@fontsource/dm-sans/latin-600.css";
import "@fontsource/libre-caslon-display/latin-400.css";
import "./console.css";
import { ApiError, api, go, setCsrf, signOut, type Me, type Organisation } from "./api";
import { Consent } from "./consent";
import { AccessPage, Apps, Connect, Keys, MembersPage, Overview, Security, Settings, Usage } from "./pages";
import { Loading, Mark, Problem } from "./ui";

const NAV: ([string, string] | string)[] = [
  ["/console", "Overview"],
  ["/console/keys", "API keys"],
  ["/console/apps", "Connected apps"],
  ["/console/usage", "Usage"],
  "Organisation",
  ["/console/members", "Members"],
  ["/console/access", "Access and plan"],
  ["/console/settings", "Settings"],
  "Your account",
  ["/console/security", "Security"],
];

const TITLES: Record<string, string> = {
  "/console": "Overview",
  "/console/keys": "API keys",
  "/console/apps": "Connected apps",
  "/console/connect": "Connect an app",
  "/console/usage": "Usage",
  "/console/members": "Members",
  "/console/access": "Access and plan",
  "/console/settings": "Settings",
  "/console/security": "Security",
  "/console/sign-in": "Sign in",
  "/console/welcome": "Welcome",
};

function usePath() {
  const [path, setPath] = useState(location.pathname.replace(/\/$/, "") || "/console");
  useEffect(() => {
    const update = () => setPath(location.pathname.replace(/\/$/, "") || "/console");
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  useEffect(() => {
    document.title = `${TITLES[path] ?? "Console"} · Open Alvary`;
    document.querySelector<HTMLElement>("main")?.focus({ preventScroll: true });
  }, [path]);
  return path;
}

function App() {
  const path = usePath();
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [version, setVersion] = useState(0);
  const signingIn = path === "/console/sign-in" || path === "/console/oauth/consent";
  useEffect(() => {
    if (signingIn) return;
    api<Me>("/me").then(
      (data) => {
        setCsrf(data.csrf);
        setMe(data);
        setError(null);
        const welcomed = data.policies.pending.length === 0 && data.organisations.length > 0;
        if (!welcomed && path !== "/console/welcome") go("/console/welcome");
      },
      (e) => setError(e),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version, signingIn]);
  if (path === "/console/oauth/consent") return <Consent />;
  if (signingIn) return <SignIn />;
  if (error) {
    if (error instanceof ApiError && error.code === "credential_required") return <Loading />;
    return (
      <div className="solo">
        <div className="solo-card">
          <Problem error={error} />
        </div>
      </div>
    );
  }
  if (!me) return <Loading />;
  const refresh = () => setVersion((v) => v + 1);
  if (path === "/console/welcome") return <Welcome me={me} onDone={() => (refresh(), go("/console"))} />;
  return <Shell me={me} path={path} refresh={refresh} />;
}

function Shell({ me, path, refresh }: { me: Me; path: string; refresh: () => void }) {
  const [orgId, setOrgId] = useState(() => {
    try {
      return localStorage.getItem("oa_console_org");
    } catch {
      return null;
    }
  });
  const [menu, setMenu] = useState(false);
  const org: Organisation | undefined = me.organisations.find((o) => o.id === orgId) ?? me.organisations[0];
  if (!org) return <Loading />;
  function choose(id: string) {
    setOrgId(id);
    try {
      localStorage.setItem("oa_console_org", id);
    } catch {
      /* the choice lasts for this page only */
    }
  }
  const props = { me, org, refresh };
  const page = (() => {
    switch (path) {
      case "/console":
        return <Overview {...props} />;
      case "/console/keys":
        return <Keys {...props} />;
      case "/console/apps":
        return <Apps {...props} />;
      case "/console/connect":
        return <Connect {...props} />;
      case "/console/usage":
        return <Usage {...props} />;
      case "/console/members":
        return <MembersPage {...props} />;
      case "/console/access":
        return <AccessPage {...props} />;
      case "/console/settings":
        return <Settings {...props} />;
      case "/console/security":
        return <Security me={me} refresh={refresh} />;
      default:
        return (
          <div className="page-head">
            <h1>This page doesn't exist</h1>
            <button className="btn secondary" type="button" onClick={() => go("/console")}>
              Go to the overview
            </button>
          </div>
        );
    }
  })();
  const initials = (me.person.display_name ?? me.person.email)
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");
  return (
    <div className="shell">
      <a className="skip" href="#main">
        Skip to content
      </a>
      <nav className="side" aria-label="Console">
        <a className="brand" href="/console" onClick={(e) => (e.preventDefault(), go("/console"))}>
          <Mark size={28} />
          <span>
            open alvary <small>console</small>
          </span>
        </a>
        {me.organisations.length > 1 ? (
          <label className="org">
            <span className="sr-only">Organisation</span>
            <select value={org.id} onChange={(e) => choose(e.target.value)}>
              {me.organisations.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <div className="org">
            <b>{org.name}</b>
          </div>
        )}
        <button className="menu-toggle" type="button" aria-expanded={menu} aria-controls="console-nav" onClick={() => setMenu(!menu)}>
          Menu
        </button>
        <div id="console-nav" className={"nav" + (menu ? " open" : "")}>
          {NAV.map((item) =>
            typeof item === "string" ? (
              <div className="nav-group" key={item}>
                {item}
              </div>
            ) : (
              <a
                key={item[0]}
                href={item[0]}
                aria-current={path === item[0] || (path === "/console/connect" && item[0] === "/console/apps") ? "page" : undefined}
                onClick={(e) => {
                  e.preventDefault();
                  setMenu(false);
                  go(item[0]);
                }}
              >
                {item[1]}
              </a>
            ),
          )}
        </div>
        <div className="side-foot">
          <a href="/developers">Documentation</a>
          <a href="/">Back to reading</a>
        </div>
      </nav>
      <div className="main-column">
        <div className="topbar">
          <a href="/about">Help</a>
          <SignOut />
          <span className="avatar" title={me.person.email} aria-label={`Signed in as ${me.person.email}`}>
            {initials}
          </span>
        </div>
        <main id="main" tabIndex={-1} className="content">
          {page}
        </main>
      </div>
    </div>
  );
}

function SignOut() {
  return (
    <button className="link" type="button" onClick={signOut}>
      Sign out
    </button>
  );
}

const SIGN_IN_ERRORS: Record<string, string> = {
  expired: "That sign-in took too long or was opened in another browser. Start again.",
  cancelled: "The sign-in was cancelled. Start again when you're ready.",
  method_not_supported: "Sign in with Google or an email code.",
  email_in_use: "An account already uses this email. Sign in the way you did before, then add this method under Security.",
  account_inactive: "This account is suspended or closed. Contact us if you think this is a mistake.",
  wrong_account: "That was a different account. Sign in with the one you were using.",
  unavailable: "Sign-in isn't available right now. Try again in a few minutes.",
  signed_out: "Your session ended. Sign in again.",
  sign_in_refused: "The sign-in couldn't be verified. Try again.",
  second_factor_required: "The second factor wasn't completed. Try again.",
  invalid: "Something was wrong with that sign-in link. Start again.",
};

function SignIn() {
  const params = new URLSearchParams(location.search);
  const error = params.get("error");
  const returnTo = params.get("return_to") ?? "/console";
  const start = (method: string) =>
    `/console/auth/start?method=${method}&return_to=${encodeURIComponent(returnTo.startsWith("/console") ? returnTo : "/console")}`;
  return (
    <div className="solo">
      <main className="solo-card" id="main" tabIndex={-1}>
        <Mark size={40} />
        <h1>Sign in to Open Alvary</h1>
        <p className="fine">For API, MCP and bulk access. Reading the corpus on the website needs no account.</p>
        {error ? (
          <div className="callout bad" role="alert">
            {SIGN_IN_ERRORS[error] ?? SIGN_IN_ERRORS.invalid}
          </div>
        ) : null}
        <a className="provider" href={start("google")}>
          <GoogleIcon />
          Continue with Google
        </a>
        <a className="provider" href={start("email")}>
          Continue with email
        </a>
        <p className="fine">We email you a code. No password to remember.</p>
        <p className="fine">
          <a href="/">Read the corpus without signing in</a>
        </p>
        <p className="fine">
          By continuing you agree to the <a href="/terms">terms of service</a> and the{" "}
          <a href="/acceptable-use">acceptable use policy</a>, and confirm you have read the{" "}
          <a href="/privacy">privacy notice</a>.
        </p>
      </main>
    </div>
  );
}

function Welcome({ me, onDone }: { me: Me; onDone: () => void }) {
  const pending = new Set(me.policies.pending);
  const [name, setName] = useState(me.person.display_name ?? "");
  const [orgName, setOrgName] = useState(me.person.display_name ?? "");
  const [agree, setAgree] = useState({ terms: !pending.has("terms") && !pending.has("acceptable_use"), privacy: !pending.has("privacy") });
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const firstTime = me.organisations.length === 0;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (pending.size) await api("/me/policies", { method: "POST", body: { versions: me.policies.current } });
      if (firstTime) await api("/me/welcome", { method: "POST", body: { display_name: name, organisation_name: orgName || name } });
      onDone();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="solo">
      <main className="solo-card wide" id="main" tabIndex={-1}>
        <Mark size={40} />
        <h1>{firstTime ? "Welcome to Open Alvary" : "Our terms have changed"}</h1>
        <p className="fine">
          {firstTime ? "Two things before you start. You can change both later." : "Review and accept them to continue."}
        </p>
        <form className="form" onSubmit={submit}>
          {firstTime ? (
            <>
              <div className="field">
                <label htmlFor="welcome-name">Your name</label>
                <input
                  id="welcome-name"
                  type="text"
                  autoComplete="name"
                  required
                  maxLength={100}
                  value={name}
                  onChange={(e) => {
                    if (orgName === name) setOrgName(e.target.value);
                    setName(e.target.value);
                  }}
                />
              </div>
              <div className="field">
                <label htmlFor="welcome-org">Organisation name</label>
                <input id="welcome-org" type="text" required maxLength={120} value={orgName} onChange={(e) => setOrgName(e.target.value)} />
                <small>Access, keys and usage belong to an organisation. This one is yours; rename it or invite colleagues any time.</small>
              </div>
            </>
          ) : null}
          <label className="choice">
            <input type="checkbox" required checked={agree.terms} onChange={(e) => setAgree({ ...agree, terms: e.target.checked })} />
            <span>
              I agree to the{" "}
              <a href="/terms" target="_blank" rel="noreferrer">
                terms of service
              </a>{" "}
              and the{" "}
              <a href="/acceptable-use" target="_blank" rel="noreferrer">
                acceptable use policy
              </a>
              .
            </span>
          </label>
          <label className="choice">
            <input type="checkbox" required checked={agree.privacy} onChange={(e) => setAgree({ ...agree, privacy: e.target.checked })} />
            <span>
              I have read the{" "}
              <a href="/privacy" target="_blank" rel="noreferrer">
                privacy notice
              </a>
              .
            </span>
          </label>
          <Problem error={error} />
          <button className="btn wide" type="submit" disabled={busy}>
            {busy ? "Saving…" : "Continue to the console"}
          </button>
        </form>
      </main>
    </div>
  );
}

function GoogleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.6 5.4 2.7 13.3l7.9 6.1C12.5 13.6 17.8 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.1 24.6c0-1.6-.1-3.1-.4-4.6H24v9h12.4c-.5 2.9-2.2 5.3-4.6 6.9l7.4 5.7c4.3-4 6.9-9.9 6.9-17z" />
      <path fill="#FBBC05" d="M10.6 28.6c-.5-1.4-.8-3-.8-4.6s.3-3.2.8-4.6l-7.9-6.1C1 16.6 0 20.2 0 24s1 7.4 2.7 10.7l7.9-6.1z" />
      <path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.4-5.7c-2.1 1.4-4.8 2.3-8.5 2.3-6.2 0-11.5-4.1-13.4-9.9l-7.9 6.1C6.6 42.6 14.6 48 24 48z" />
    </svg>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
