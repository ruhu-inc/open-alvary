// The developer console (MAC-10) at /console: sign-in, first-sign-in terms, and the console shell.

import React, { useEffect, useRef, useState } from "react";
import {
  BarChart3,
  Bell,
  BookOpen,
  Check,
  ChevronsUpDown,
  Gauge,
  Globe,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Menu,
  Plug,
  Plus,
  Settings,
  ShieldCheck,
  Users,
  Webhook,
  X,
  type LucideIcon,
} from "lucide-react";
import { createRoot } from "react-dom/client";
import "@fontsource/dm-sans/latin-400.css";
import "@fontsource/dm-sans/latin-500.css";
import "@fontsource/dm-sans/latin-600.css";
import "@fontsource/libre-caslon-display/latin-400.css";
import "./console.css";
import { ApiError, api, go, setCsrf, signOut, type Me, type Organisation } from "./api";
import { Consent } from "./consent";
import { AccessPage, Apps, Connect, Keys, MembersPage, Overview, Security, Settings as Settings_, Usage } from "./pages";
import { Dialog, External, Loading, Mark, Problem } from "./ui";
import { OrganisationData, YourData } from "./privacy";
import { DigestSettings, Unsubscribe, WatchesPage } from "./watches";
import { WebhooksPage } from "./webhooks";

type NavItem = { to: string; label: string; icon: LucideIcon };
const NAV: { group?: string; items: NavItem[] }[] = [
  {
    items: [
      { to: "/console", label: "Dashboard", icon: LayoutDashboard },
      { to: "/console/monitor", label: "Monitor", icon: Bell },
      { to: "/console/keys", label: "API keys", icon: KeyRound },
      { to: "/console/apps", label: "Connected apps", icon: Plug },
      { to: "/console/webhooks", label: "Webhooks", icon: Webhook },
      { to: "/console/usage", label: "Usage", icon: BarChart3 },
    ],
  },
  {
    group: "Organisation",
    items: [
      { to: "/console/members", label: "Members", icon: Users },
      { to: "/console/access", label: "Plan and limits", icon: Gauge },
      { to: "/console/settings", label: "Settings", icon: Settings },
    ],
  },
];

const TITLES: Record<string, string> = {
  "/console": "Dashboard",
  "/console/monitor": "Monitor",
  "/console/unsubscribe": "Email settings",
  "/console/keys": "API keys",
  "/console/apps": "Connected apps",
  "/console/webhooks": "Webhooks",
  "/console/connect": "Connect an app",
  "/console/usage": "Usage",
  "/console/members": "Members",
  "/console/access": "Plan and limits",
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
  // A digest's unsubscribe link works without signing in: the token is the credential.
  const unsubscribing = path.startsWith("/console/unsubscribe/");
  useEffect(() => {
    if (signingIn || unsubscribing) return;
    api<Me>("/me").then(
      (data) => {
        setCsrf(data.csrf);
        setMe(data);
        remember({ name: data.person.display_name ?? "", email: data.person.email });
        setError(null);
        const welcomed = data.policies.pending.length === 0 && data.organisations.length > 0;
        if (!welcomed && path !== "/console/welcome") go("/console/welcome");
      },
      (e) => setError(e),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version, signingIn]);
  if (unsubscribing) return <Unsubscribe token={decodeURIComponent(path.slice("/console/unsubscribe/".length))} />;
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
        return (
          <>
            <Settings_ {...props} />
            <OrganisationData org={org} />
          </>
        );
      case "/console/monitor":
        return <WatchesPage {...props} />;
      case "/console/webhooks":
        return <WebhooksPage {...props} />;
      case "/console/security":
        return (
          <>
            <Security me={me} refresh={refresh} />
            <DigestSettings />
            <YourData me={me} />
          </>
        );
      default:
        return (
          <div className="page-head">
            <h1>This page doesn't exist</h1>
            <button className="btn secondary" type="button" onClick={() => go("/console")}>
              Go to the dashboard
            </button>
          </div>
        );
    }
  })();
  const current = (to: string) => path === to || (path === "/console/connect" && to === "/console/apps");
  return (
    <div className="shell">
      <a className="skip" href="#main">
        Skip to content
      </a>
      <aside className="side">
        <div className="side-top">
          <a className="brand" href="/console" onClick={(e) => (e.preventDefault(), go("/console"))}>
            <Mark size={26} />
            <span>
              open alvary <small>console</small>
            </span>
          </a>
          <button
            className="menu-toggle"
            type="button"
            aria-expanded={menu}
            aria-controls="console-nav"
            onClick={() => setMenu(!menu)}
          >
            {menu ? <X size={20} aria-hidden="true" /> : <Menu size={20} aria-hidden="true" />}
            <span className="sr-only">Menu</span>
          </button>
        </div>
        <div id="console-nav" className={"side-body" + (menu ? " open" : "")}>
          <OrgSwitcher me={me} org={org} onChoose={(id) => (choose(id), setMenu(false))} />
          <nav className="nav" aria-label="Console">
            {NAV.map((section, i) => (
              <div key={i} className="nav-section">
                {section.group ? <div className="nav-group">{section.group}</div> : null}
                {section.items.map(({ to, label, icon: Icon }) => (
                  <a
                    key={to}
                    href={to}
                    aria-current={current(to) ? "page" : undefined}
                    onClick={(e) => {
                      e.preventDefault();
                      setMenu(false);
                      go(to);
                    }}
                  >
                    <Icon size={17} strokeWidth={1.8} aria-hidden="true" />
                    {label}
                  </a>
                ))}
              </div>
            ))}
          </nav>
          <div className="side-foot">
            <External href="/docs" className="foot-link">
              <BookOpen size={17} strokeWidth={1.8} aria-hidden="true" />
              Documentation
            </External>
            <External href="/" className="foot-link">
              <Globe size={17} strokeWidth={1.8} aria-hidden="true" />
              Open Alvary website
            </External>
            <AccountMenu me={me} active={path === "/console/security"} />
          </div>
        </div>
      </aside>
      <div className="main-column">
        <header className="topbar">
          <nav className="crumbs" aria-label="You are here">
            <span>{path === "/console/security" ? "Your account" : org.name}</span>
            <span aria-hidden="true">/</span>
            <b>{TITLES[path] ?? "Console"}</b>
          </nav>
          <div className="topbar-links">
            <External href="/docs">Docs</External>
            <External href="/docs/help">Help</External>
          </div>
        </header>
        <main id="main" tabIndex={-1} className="content">
          {page}
        </main>
      </div>
    </div>
  );
}

function initials(text: string) {
  return text
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");
}

// The organisation switcher (as in the Claude and OpenAI consoles): the current organisation and
// its plan, the others to switch to, and a way to make a new one.
function OrgSwitcher({ me, org, onChoose }: { me: Me; org: Organisation; onChoose: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    const key = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", key);
    };
  }, [open]);
  return (
    <div className="switcher" ref={ref}>
      <button className="switcher-button" type="button" aria-expanded={open} aria-haspopup="menu" onClick={() => setOpen(!open)}>
        <span className="org-badge" aria-hidden="true">
          {initials(org.name)}
        </span>
        <span className="switcher-text">
          <b>{org.name}</b>
          <small>{planName(org)}</small>
        </span>
        <ChevronsUpDown size={16} aria-hidden="true" />
      </button>
      {open ? (
        <div className="popover" role="menu">
          <div className="popover-label">Organisations</div>
          {me.organisations.map((o) => (
            <button
              key={o.id}
              role="menuitemradio"
              aria-checked={o.id === org.id}
              type="button"
              onClick={() => (setOpen(false), onChoose(o.id))}
            >
              <span className="org-badge small" aria-hidden="true">
                {initials(o.name)}
              </span>
              <span className="switcher-text">
                <b>{o.name}</b>
                <small>{planName(o)}</small>
              </span>
              {o.id === org.id ? <Check size={16} aria-hidden="true" /> : null}
            </button>
          ))}
          <div className="popover-rule" />
          <button role="menuitem" type="button" onClick={() => (setOpen(false), setCreating(true))}>
            <Plus size={16} aria-hidden="true" />
            Create organisation
          </button>
        </div>
      ) : null}
      {creating ? <CreateOrganisation onClose={() => setCreating(false)} onCreated={onChoose} /> : null}
    </div>
  );
}

function planName(org: Organisation) {
  if (org.access.state !== "active") return "No access";
  if (org.access.starter) return "Starter · free";
  return org.access.tier?.name ?? org.access.tier?.id ?? "Access";
}

function CreateOrganisation({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState("company");
  const [error, setError] = useState<unknown>(null);
  async function create(event: React.FormEvent) {
    event.preventDefault();
    try {
      const made = await api<{ id: string }>("/organisations", { method: "POST", body: { name, kind } });
      onCreated(made.id);
      location.assign("/console");
    } catch (e) {
      setError(e);
    }
  }
  return (
    <Dialog title="Create an organisation" onClose={onClose}>
      <form className="form" onSubmit={create}>
        <p className="fine">
          An organisation holds its own keys, connected apps, members and limits. It starts on the free starter plan.
        </p>
        <div className="field">
          <label htmlFor="new-org-name">Name</label>
          <input id="new-org-name" type="text" required maxLength={120} value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="new-org-kind">Type</label>
          <select id="new-org-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="company">Company</option>
            <option value="academic">Academic</option>
            <option value="non_profit">Non-profit</option>
            <option value="government">Government</option>
            <option value="individual">Individual</option>
            <option value="other">Other</option>
          </select>
        </div>
        <Problem error={error} />
        <div className="dialog-actions">
          <button className="btn secondary" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="btn" type="submit">
            Create organisation
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// The account menu at the foot of the sidebar: who is signed in, security, sign out.
function AccountMenu({ me, active }: { me: Me; active: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  const name = me.person.display_name ?? me.person.email;
  return (
    <div className="account" ref={ref}>
      {open ? (
        <div className="popover up" role="menu">
          <div className="popover-label">{me.person.email}</div>
          <button role="menuitem" type="button" onClick={() => (setOpen(false), go("/console/security"))}>
            <ShieldCheck size={16} aria-hidden="true" />
            Security and profile
          </button>
          <button role="menuitem" type="button" onClick={signOut}>
            <LogOut size={16} aria-hidden="true" />
            Sign out
          </button>
        </div>
      ) : null}
      <button
        className={"account-button" + (active ? " current" : "")}
        type="button"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen(!open)}
      >
        <span className="avatar" aria-hidden="true">
          {initials(name)}
        </span>
        <span className="switcher-text">
          <b>{name}</b>
          <small>{me.person.email}</small>
        </span>
        <ChevronsUpDown size={16} aria-hidden="true" />
      </button>
    </div>
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

// The last account used in this browser, for "Continue as …" (a convenience on the person's own
// device, as at Google and GitHub: name, email and how they signed in; never a secret).
type Remembered = { name: string; email: string; method?: "google" | "email" };
function recall(): Remembered | null {
  try {
    const raw = localStorage.getItem("oa_console_account");
    return raw ? (JSON.parse(raw) as Remembered) : null;
  } catch {
    return null;
  }
}
function remember(update: Partial<Remembered>) {
  try {
    localStorage.setItem("oa_console_account", JSON.stringify({ ...recall(), ...update }));
  } catch {
    /* storage unavailable: the sign-in page offers every method */
  }
}
function forget() {
  try {
    localStorage.removeItem("oa_console_account");
  } catch {
    /* nothing to forget */
  }
}

function SignIn() {
  const params = new URLSearchParams(location.search);
  const error = params.get("error");
  const returnTo = params.get("return_to") ?? "/console";
  const deleted = params.get("deleted") === "1";
  // After deleting an account, this browser forgets it too.
  const [known, setKnown] = useState(() => (deleted ? (forget(), null) : recall()));
  useEffect(() => {
    // Still signed in (the console session lasts up to 30 days): go straight back in.
    if (error) return;
    fetch("/console/api/v1/me", { credentials: "same-origin" }).then(
      (r) => r.ok && location.replace(returnTo.startsWith("/console") ? returnTo : "/console"),
      () => undefined,
    );
  }, [error, returnTo]);
  const start = (method: string, hint?: string) =>
    `/console/auth/start?method=${method}&return_to=${encodeURIComponent(returnTo.startsWith("/console") ? returnTo : "/console")}` +
    (hint ? `&login_hint=${encodeURIComponent(hint)}` : "");
  return (
    <div className="solo">
      <main className="solo-card" id="main" tabIndex={-1}>
        <Mark size={40} />
        <h1>{known?.email ? "Welcome back" : "Sign in to Open Alvary"}</h1>
        <p className="fine">For API and MCP access. Reading the corpus on the website needs no account.</p>
        {error ? (
          <div className="callout bad" role="alert">
            {SIGN_IN_ERRORS[error] ?? SIGN_IN_ERRORS.invalid}
          </div>
        ) : null}
        {deleted ? (
          <p className="callout" role="status">
            Your account was deleted and you're signed out. We've emailed you a confirmation.
          </p>
        ) : null}
        {known?.email && known.method ? (
          <>
            <a className="provider remembered" href={start(known.method, known.email)}>
              <span className="avatar" aria-hidden="true">
                {(known.name || known.email).slice(0, 1).toUpperCase()}
              </span>
              <span className="switcher-text">
                <b>Continue as {known.name || known.email}</b>
                <small>
                  {known.email} · {known.method === "google" ? "Google" : "email code"}
                </small>
              </span>
            </a>
            <button className="link" type="button" onClick={() => (forget(), setKnown(null))}>
              Use another account
            </button>
          </>
        ) : (
          <>
            <a className="provider" href={start("google")} onClick={() => remember({ method: "google" })}>
              <GoogleIcon />
              Continue with Google
            </a>
            <a className="provider" href={start("email")} onClick={() => remember({ method: "email" })}>
              Continue with email
            </a>
            <p className="fine">We email you a code. No password to remember.</p>
          </>
        )}
        <p className="fine">
          <External href="/">Read the corpus without signing in</External>
        </p>
        <p className="fine">
          By continuing you agree to the <External href="/terms">terms of service</External> and the{" "}
          <External href="/acceptable-use">acceptable use policy</External>, and confirm you have read the{" "}
          <External href="/privacy">privacy notice</External>.
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
                <label htmlFor="welcome-name">Full name</label>
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
              <External href="/terms">terms of service</External>{" "}
              and the{" "}
              <External href="/acceptable-use">acceptable use policy</External>
              .
            </span>
          </label>
          <label className="choice">
            <input type="checkbox" required checked={agree.privacy} onChange={(e) => setAgree({ ...agree, privacy: e.target.checked })} />
            <span>
              I have read the{" "}
              <External href="/privacy">privacy notice</External>
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
