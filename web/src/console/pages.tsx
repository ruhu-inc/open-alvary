// The console's pages (MAC-10), in the words people use: access, keys, connected apps, members.

import React, { useEffect, useState } from "react";
import { BarChart3, BookOpen, ExternalLink, KeyRound, PauseCircle, Plug, Plus } from "lucide-react";
import { ApiError, api, clearDraft, go, peekDraft, saveDraft, type Me, type Organisation } from "./api";
import {
  COUNTRIES,
  CopyField,
  Dialog,
  External,
  maskedKey,
  Loading,
  Pill,
  Problem,
  ROLE_HELP,
  ROLE_NAMES,
  accessLabel,
  ago,
  number,
  useAsync,
  when,
} from "./ui";

export type PageProps = { me: Me; org: Organisation; refresh: () => void };

const API_ORIGIN =
  location.hostname === "open.alvary.ai" ? "https://api.open.alvary.ai" : location.origin;
const MCP_URL = API_ORIGIN + "/mcp";
const MANAGERS = new Set(["owner", "admin"]);
const BUILDERS = new Set(["owner", "admin", "developer"]);

export function Heading({ title, description, action }: { title: string; org?: Organisation; description?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="page-head">
      <div className="page-title">
        <h1>{title}</h1>
        {description ? <p className="lede">{description}</p> : null}
      </div>
      {action ? <div className="row">{action}</div> : null}
    </div>
  );
}

function AccessEnded({ org }: { org: Organisation }) {
  if (org.access.state === "active") return null;
  return (
    <div className="banner bad" role="alert">
      <p>
        <b>API and MCP access is paused for this organisation.</b> Keys and connected apps are kept, not deleted;
        requests are refused with <code>403 entitlement_lapsed</code> until access is restored. Reply to our email
        or write to hello@ruhu.ai.
      </p>
    </div>
  );
}

function NeedsAccess({ what }: { what: string; org?: Organisation }) {
  return (
    <div className="empty">
      <PauseCircle size={28} strokeWidth={1.5} aria-hidden="true" />
      <h2>{what} are paused</h2>
      <p className="lede">This organisation's access was stopped. Write to hello@ruhu.ai to restore it.</p>
    </div>
  );
}

// ---------- Dashboard ----------
export function Overview({ me, org }: PageProps) {
  const keys = useAsync(() => api<{ keys: Key[] }>(`/organisations/${org.id}/keys`), [org.id]);
  const apps = useAsync(() => api<{ clients: Grant[] }>(`/organisations/${org.id}/clients`), [org.id]);
  const usage = useAsync(() => api<{ days: UsageDay[] }>(`/organisations/${org.id}/usage?days=7`), [org.id]);
  const active = org.access.state === "active";
  const builder = BUILDERS.has(org.role);
  const activeKeys = (keys.data?.keys ?? []).filter((k) => k.state === "active");
  const connected = (apps.data?.clients ?? []).filter((c) => !c.revoked_at);
  const today = new Date().toISOString().slice(0, 10);
  const todayRequests = (usage.data?.days ?? []).filter((d) => d.day === today).reduce((a, d) => a + d.requests, 0);
  const firstName = (me.person.display_name ?? "").split(" ")[0];
  const limits = org.access.tier?.allowance ?? {};
  return (
    <>
      <Heading
        title={firstName ? `Welcome, ${firstName}` : "Dashboard"}
        description="Search and read Africa's published law from your own code and AI tools."
      />
      <AccessEnded org={org} />
      {me.invitations.map((invitation) => (
        <Invitation key={invitation.id} invitation={invitation} />
      ))}
      {active && builder ? (
        <section className="quick" aria-label="Quick start">
          <button className="quick-card" type="button" onClick={() => go("/console/keys?create=1")}>
            <KeyRound size={18} strokeWidth={1.7} aria-hidden="true" />
            <b>Create an API key</b>
            <span>Call the REST API or MCP from your code.</span>
          </button>
          <button className="quick-card" type="button" onClick={() => go("/console/connect?app=claude")}>
            <Plug size={18} strokeWidth={1.7} aria-hidden="true" />
            <b>Connect Claude or Codex</b>
            <span>Ask questions over the corpus from your AI tools.</span>
          </button>
          <External className="quick-card" href="/docs" icon={false}>
            <BookOpen size={18} strokeWidth={1.7} aria-hidden="true" />
            <b>
              Read the docs <ExternalLink size={13} strokeWidth={1.8} aria-hidden="true" className="ext-icon" />
            </b>
            <span>Endpoints, examples, limits and error codes.</span>
          </External>
        </section>
      ) : null}
      <div className="grid3">
        <div className="card stat">
          <span className="stat-label">Plan</span>
          <b>{active ? (org.access.tier?.name ?? "Starter") : "Paused"}</b>
          <span>
            {org.access.starter ? "Free" : org.access.ends_at ? `Until ${when(org.access.ends_at)}` : "No end date"} ·{" "}
            <button className="link" type="button" onClick={() => go("/console/access")}>
              {org.access.starter ? "Upgrade" : "Details"}
            </button>
          </span>
        </div>
        <div className="card stat">
          <span className="stat-label">Requests today</span>
          <b>{number(todayRequests)}</b>
          <span>
            Up to {number(limits.requests_per_minute ?? 0)} a minute ·{" "}
            <button className="link" type="button" onClick={() => go("/console/usage")}>
              Usage
            </button>
          </span>
        </div>
        <div className="card stat">
          <span className="stat-label">In use</span>
          <b>
            {activeKeys.length} {activeKeys.length === 1 ? "key" : "keys"} · {connected.length}{" "}
            {connected.length === 1 ? "app" : "apps"}
          </b>
          <span>
            <button className="link" type="button" onClick={() => go("/console/keys")}>
              Manage keys
            </button>
          </span>
        </div>
      </div>
      {activeKeys.length ? (
        <div className="card">
          <div className="card-head">
            <h2>Recent keys</h2>
            <button className="link" type="button" onClick={() => go("/console/keys")}>
              View all
            </button>
          </div>
          <table>
            <tbody>
              {activeKeys.slice(0, 3).map((k) => (
                <tr key={k.id}>
                  <td>
                    {k.name}
                    <small className="mono" dir="ltr">
                      {maskedKey(k.id, k.last_four)}
                    </small>
                  </td>
                  <td className="actions">{ago(k.last_used_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {MANAGERS.has(org.role) && active && !org.access.starter && me.person.second_factor === "none" && !me.second_factor_until ? (
        <div className="banner warn">
          <p>
            <b>Set up a second factor.</b> Owners and admins of organisations with approved access confirm keys and
            other sensitive changes with one.
          </p>
          <button className="btn" type="button" onClick={() => go("/console/security")}>
            Set up
          </button>
        </div>
      ) : null}
    </>
  );
}

function Invitation({ invitation }: { invitation: Me["invitations"][number] }) {
  const [error, setError] = useState<unknown>(null);
  async function accept() {
    try {
      const result = await api<{ organisation: string }>(`/invitations/${invitation.id}/accept`, { method: "POST" });
      localStorage.setItem("oa_console_org", result.organisation);
      location.assign("/console");
    } catch (e) {
      setError(e);
    }
  }
  return (
    <div className="banner warn">
      <p>
        You're invited to join <b>{invitation.organisation}</b> as {ROLE_NAMES[invitation.role]?.toLowerCase()}.
        The invitation expires on {when(invitation.expires_at)}.
      </p>
      <button className="btn" type="button" onClick={accept}>
        Join
      </button>
      <Problem error={error} />
    </div>
  );
}

// ---------- Access and plan ----------
type AccessRequest = {
  id: string;
  purpose: string;
  expected_volume: string;
  scope_requested: { all?: boolean; jurisdictions?: string[] };
  reduced: boolean;
  tier_requested: string | null;
  contact_name: string;
  contact_email: string;
  state: "submitted" | "needs_information" | "granted" | "declined" | "withdrawn";
  decision: { reason?: string } | null;
  renewable_after: string | null;
  created_at: string;
  updated_at: string;
};
type AccessDetails = {
  current: Organisation["access"];
  history: {
    id: string;
    tier_name: string;
    source: string;
    scope: { all?: boolean; jurisdictions?: string[] };
    starts_at: string;
    ends_at: string | null;
    state: string;
    reason: string | null;
  }[];
  requests: AccessRequest[];
  country_change_after: string | null;
};

// A country with published material, for choosing a plan's countries.
type Country = { id: string; name: string; sources: number; full_text: number };
type Scope = { all?: boolean; jurisdictions?: string[] };

type Plan = {
  id: string;
  name: string;
  description: string;
  includes: string[];
  allowance: Record<string, number>;
  listing: {
    order?: number;
    for?: string;
    price?: string;
    note?: string;
    action?: "included" | "upgrade" | "contact" | "apply";
    limits?: string;
  };
};

export function AccessPage({ me, org, refresh }: PageProps) {
  const details = useAsync(() => api<AccessDetails>(`/organisations/${org.id}/access`), [org.id]);
  const plans = useAsync(() => api<{ tiers: Plan[] }>("/tiers"), []);
  const countries = useAsync(() => api<{ countries: Country[] }>("/countries"), []);
  const [error, setError] = useState<unknown>(null);
  const [confirmEnd, setConfirmEnd] = useState(false);
  const [asking, setAsking] = useState<Plan | null>(null);
  const [changing, setChanging] = useState(false);
  if ((details.loading && !details.data) || (plans.loading && !plans.data)) return <Loading />;
  if (details.error) return <Problem error={details.error} />;
  const data = details.data!;
  const published = (plans.data?.tiers ?? []).filter((t) => t.listing.action);
  const open = data.requests.find((r) => ["submitted", "needs_information"].includes(r.state));
  const declined = data.requests.find((r) => r.state === "declined");
  const currentId = data.current.state === "active" ? data.current.tier?.id : null;
  const current = data.history.find((h) => h.state === "active");
  const owner = org.role === "owner";
  const published_countries = countries.data?.countries ?? [];
  const names = Object.fromEntries(published_countries.map((c) => [c.id, c.name]));
  const chosenLimit = data.current.tier?.allowance?.jurisdictions;
  async function withdraw(id: string) {
    try {
      await api(`/organisations/${org.id}/access-requests/${id}/withdraw`, {
        method: "POST",
      });
      details.reload();
    } catch (e) {
      setError(e);
    }
  }
  async function end() {
    try {
      await api(`/organisations/${org.id}/access/end`, { method: "POST" });
      setConfirmEnd(false);
      refresh();
      details.reload();
    } catch (e) {
      setError(e);
      setConfirmEnd(false);
    }
  }
  function planAction(plan: Plan) {
    const action = plan.listing.action;
    if (plan.id === currentId) return <span className="fine">Your current plan</span>;
    if (action === "included") return <span className="fine">Included for everyone</span>;
    if (!owner) return <span className="fine">Owners can change plans</span>;
    if (open) return <span className="fine">A request is open</span>;
    return (
      <button
        className={"btn" + (action === "upgrade" ? "" : " secondary")}
        type="button"
        onClick={() => setAsking(plan)}
      >
        {action === "contact" ? "Contact us" : action === "apply" ? "Apply" : "Request upgrade"}
      </button>
    );
  }
  const nameOf = (id?: string | null) =>
    published.find((p) => p.id === id)?.name ?? (id === data.current.tier?.id ? data.current.tier?.name : id);
  return (
    <>
      <Heading
        title="Plan and limits"
        org={org}
        description="Every plan reads the same published corpus as the website. Plans differ in their limits, shared by every key and app in the organisation."
      />
      <AccessEnded org={org} />
      <Problem error={error} onDismiss={() => setError(null)} />
      {currentId ? (
        <div className="card current-plan">
          <div>
            <span className="stat-label">Current plan</span>
            <h2>
              {nameOf(currentId)}{" "}
              <Pill tone="good">
                {data.current.starter ? "Free" : current?.source === "waiver" ? "Free or reduced" : "Active"}
              </Pill>
            </h2>
            <p className="fine">
              {number(data.current.tier?.allowance?.requests_per_minute ?? 0)} requests a minute ·{" "}
              {number(data.current.tier?.allowance?.concurrency ?? 0)} at once ·{" "}
              {scopeLabel(data.current.scope ?? { all: true }, names)}
              {!data.current.starter && current?.ends_at ? ` · until ${when(current.ends_at)}, then Starter` : ""}
            </p>
            {data.current.scope?.jurisdictions ? (
              <p className="fine">
                Other countries at the Starter rate.
                {owner && data.country_change_after
                  ? ` You can change countries again from ${when(data.country_change_after)}.`
                  : ""}
              </p>
            ) : null}
          </div>
          {!data.current.starter && owner ? (
            <div className="row">
              {chosenLimit && !data.country_change_after ? (
                <button className="btn secondary" type="button" onClick={() => setChanging(true)}>
                  Change countries
                </button>
              ) : null}
              <button className="btn secondary" type="button" onClick={() => setConfirmEnd(true)}>
                Go back to Starter
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
      {open ? (
        <RequestStatus
          request={open}
          names={names}
          plan={nameOf(open.tier_requested)}
          onWithdraw={() => withdraw(open.id)}
          owner={owner}
        />
      ) : null}
      <section aria-labelledby="plans-title" className="plans-section">
        <h2 id="plans-title" className="section-title">
          Plans
        </h2>
        <div className="plans">
          {published
            .filter((plan) => plan.listing.action !== "apply")
            .map((plan) => (
              <div key={plan.id} className={"plan-card" + (plan.id === currentId ? " current" : "")}>
                <div className="plan-top">
                  <h3>{plan.name}</h3>
                  {plan.id === currentId ? <Pill tone="good">Current</Pill> : null}
                </div>
                <p className="plan-for">{plan.listing.for}</p>
                <div>
                  <p className="plan-price">{plan.listing.price}</p>
                  <p className="plan-note">{plan.listing.note}</p>
                </div>
                <ul className="plan-limits">
                  {plan.listing.limits ? (
                    <li>{plan.listing.limits}</li>
                  ) : (
                    <>
                      <li>{number(plan.allowance.requests_per_minute ?? 0)} requests a minute</li>
                      <li>{number(plan.allowance.concurrency ?? 0)} requests at once</li>
                    </>
                  )}
                  {plan.allowance.jurisdictions ? (
                    <>
                      <li>
                        {plan.allowance.jurisdictions} {plan.allowance.jurisdictions === 1 ? "country" : "countries"} of
                        your choice
                      </li>
                      <li>Others at the Starter rate</li>
                    </>
                  ) : (
                    <li>{plan.listing.action === "contact" ? "All countries, including new ones" : "All countries"}</li>
                  )}
                  <li>REST API and MCP</li>
                </ul>
                <div className="plan-action">{planAction(plan)}</div>
              </div>
            ))}
        </div>
        {published
          .filter((plan) => plan.listing.action === "apply")
          .map((plan) => (
            <div key={plan.id} className={"card plan-strip" + (plan.id === currentId ? " current" : "")}>
              <div>
                <h3>
                  {plan.name}{" "}
                  <Pill tone={plan.id === currentId ? "good" : undefined}>
                    {plan.id === currentId ? "Current" : plan.listing.price}
                  </Pill>
                </h3>
                <p className="fine">{plan.description}</p>
              </div>
              {planAction(plan)}
            </div>
          ))}
        <p className="fine">
          A country includes its states, provinces and local law. African Union and regional law, such as the EAC,
          ECOWAS and OHADA, is in every plan. Prices for paid plans are coming; until then, we confirm each upgrade by
          email. Need something else? Write to hello@ruhu.ai.
        </p>
      </section>
      {declined && !open ? (
        <div className="card">
          <h3>Your last request was declined</h3>
          <p className="fine">
            {declined.decision?.reason ?? "No reason was recorded."}{" "}
            {declined.renewable_after ? `You can ask again after ${when(declined.renewable_after)}, or sooner with new information.` : ""}
          </p>
        </div>
      ) : null}
      {data.history.length ? (
        <div className="card">
          <h3>History</h3>
          <ul className="timeline">
            {data.history.map((h) => (
              <li key={h.id} className="on">
                <div>
                  <b>
                    {h.tier_name} · {stateName(h.state)}
                  </b>
                  <small>
                    {when(h.starts_at)} {h.ends_at ? `to ${when(h.ends_at)}` : ""} · {scopeLabel(h.scope, names)}
                  </small>
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {asking ? (
        <RequestForm
          me={me}
          org={org}
          plan={asking}
          countries={published_countries}
          current={data.current.scope?.jurisdictions ?? []}
          declined={declined}
          onCancel={() => setAsking(null)}
          onSent={() => {
            setAsking(null);
            details.reload();
            refresh();
          }}
        />
      ) : null}
      {changing && chosenLimit ? (
        <ChangeCountries
          org={org}
          countries={published_countries}
          limit={chosenLimit}
          current={data.current.scope?.jurisdictions ?? []}
          onCancel={() => setChanging(false)}
          onSaved={() => {
            setChanging(false);
            details.reload();
            refresh();
          }}
        />
      ) : null}
      {confirmEnd ? (
        <Dialog title="Go back to the Starter plan?" onClose={() => setConfirmEnd(false)}>
          <p>
            Your current plan ends now and the free Starter limits apply again. Keys and connected apps keep working.
          </p>
          <div className="dialog-actions">
            <button className="btn secondary" type="button" onClick={() => setConfirmEnd(false)}>
              Cancel
            </button>
            <button className="btn danger" type="button" onClick={end}>
              Go back to Starter
            </button>
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

function stateName(state: string) {
  return (
    {
      active: "Active",
      lapsed: "Ended",
      ended: "Ended",
      revoked: "Revoked",
      scheduled: "Scheduled",
    }[state] ?? state
  );
}

function scopeLabel(scope: Scope | undefined, names: Record<string, string> = {}) {
  return scope?.all || !scope?.jurisdictions
    ? "All countries"
    : scope.jurisdictions.map((j) => names[j] ?? j.toUpperCase()).join(", ");
}

// Choose up to `limit` countries; ones already chosen stay listed even without published material.
function CountryPicker({
  countries,
  limit,
  value,
  onChange,
}: {
  countries: Country[];
  limit: number;
  value: string[];
  onChange: (value: string[]) => void;
}) {
  const listed = [
    ...countries,
    ...value.filter((id) => !countries.some((c) => c.id === id)).map((id) => ({ id, name: id.toUpperCase(), sources: 0, full_text: 0 })),
  ];
  return (
    <fieldset className="field plain">
      <legend>
        Countries <span className="fine">· choose up to {limit}</span>
      </legend>
      {listed.length ? (
        <div className="country-list">
          {listed.map((c) => {
            const on = value.includes(c.id);
            return (
              <label key={c.id} className={"choice" + (on ? " on" : "")}>
                <input
                  type="checkbox"
                  checked={on}
                  disabled={!on && value.length >= limit}
                  onChange={(e) => onChange(e.target.checked ? [...value, c.id] : value.filter((v) => v !== c.id))}
                />
                <span>
                  {c.name}
                  <small>
                    {c.full_text ? `${number(c.full_text)} with full text` : c.sources ? "Metadata only" : "Not yet published"}
                    {c.sources ? ` · ${number(c.sources)} ${c.sources === 1 ? "source" : "sources"}` : ""}
                  </small>
                </span>
              </label>
            );
          })}
        </div>
      ) : (
        <p className="fine">No countries are published yet.</p>
      )}
      <small>Other countries still answer, at the Starter rate. You can change countries once a month.</small>
    </fieldset>
  );
}

function ChangeCountries({
  org,
  countries,
  limit,
  current,
  onCancel,
  onSaved,
}: {
  org: Organisation;
  countries: Country[];
  limit: number;
  current: string[];
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [chosen, setChosen] = useState(current);
  const [error, setError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);
  const same = chosen.length === current.length && chosen.every((c) => current.includes(c));
  async function save() {
    setSaving(true);
    setError(null);
    try {
      await api(`/organisations/${org.id}/access/countries`, { method: "POST", body: { jurisdictions: chosen } });
      onSaved();
    } catch (e) {
      setError(e);
    } finally {
      setSaving(false);
    }
  }
  return (
    <Dialog title="Change your plan's countries" onClose={onCancel}>
      <CountryPicker countries={countries} limit={limit} value={chosen} onChange={setChosen} />
      <p className="fine">The change applies at once. After it, the next change is possible in 30 days.</p>
      <Problem error={error} />
      <div className="dialog-actions">
        <button className="btn secondary" type="button" onClick={onCancel}>
          Cancel
        </button>
        <button className="btn" type="button" disabled={saving || same || !chosen.length} onClick={save}>
          {saving ? "Saving…" : "Save countries"}
        </button>
      </div>
    </Dialog>
  );
}

function RequestStatus({
  request,
  names,
  plan,
  onWithdraw,
  owner,
}: {
  request: AccessRequest;
  names: Record<string, string>;
  plan?: string | null;
  onWithdraw: () => void;
  owner: boolean;
}) {
  const asked = request.state === "needs_information";
  return (
    <div className="card request-status">
      <div>
        <h3>
          Your request{plan ? ` for ${plan}` : ""}{" "}
          <Pill tone="warn">{asked ? "Information needed" : "Being reviewed"}</Pill>
        </h3>
        <p className="fine">
          {asked
            ? (request.decision?.reason ?? "Check your email for our question.")
            : `Sent ${when(request.created_at)} · ${request.expected_volume}${request.scope_requested.jurisdictions ? ` · ${scopeLabel(request.scope_requested, names)}` : ""}${request.reduced ? " · free or reduced asked" : ""}. We reply by email.`}
        </p>
      </div>
      {owner ? (
        <button className="link" type="button" onClick={onWithdraw}>
          Withdraw
        </button>
      ) : null}
    </div>
  );
}

function RequestForm({
  me,
  org,
  plan,
  countries,
  current,
  declined,
  onSent,
  onCancel,
}: {
  me: Me;
  org: Organisation;
  plan: Plan;
  countries: Country[];
  current: string[];
  declined?: AccessRequest;
  onSent: () => void;
  onCancel: () => void;
}) {
  const apply = plan.listing.action === "apply";
  const [form, setForm] = useState({
    purpose: "",
    expected_volume: "Up to 1,000 a day",
    reduced: apply,
    contact_name: me.person.display_name ?? "",
    contact_email: me.person.email,
    new_information: false,
  });
  const limit = plan.allowance.jurisdictions;
  const [chosen, setChosen] = useState<string[]>(() =>
    (current.length ? current : countries.slice(0, 1).map((c) => c.id)).slice(0, limit ?? 0),
  );
  const [error, setError] = useState<unknown>(null);
  const [sending, setSending] = useState(false);
  const blocked = declined?.renewable_after && new Date(declined.renewable_after) > new Date();
  function set<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }
  async function send(event: React.FormEvent) {
    event.preventDefault();
    setSending(true);
    setError(null);
    try {
      await api(`/organisations/${org.id}/access-requests`, {
        method: "POST",
        body: { ...form, tier: plan.id, scope: limit ? { jurisdictions: chosen } : { all: true } },
      });
      onSent();
    } catch (e) {
      setError(e);
    } finally {
      setSending(false);
    }
  }
  const title =
    plan.listing.action === "contact"
      ? `Contact us about ${plan.name}`
      : apply
        ? "Apply for public-interest access"
        : `Upgrade to ${plan.name}`;
  return (
    <Dialog title={title} onClose={onCancel}>
      <form className="form" onSubmit={send}>
        <p className="fine">
          {apply
            ? "For research, government and legal aid. Tell us who you are and how the corpus serves the public; we decide case by case."
            : "Tell us how you'll use the corpus. We reply by email to confirm the plan for your organisation."}
        </p>
        <div className="field">
          <label htmlFor="purpose">{apply ? "Who you are and how you'll use it" : "How will you use it?"}</label>
          <textarea
            id="purpose"
            required
            maxLength={2000}
            value={form.purpose}
            onChange={(e) => set("purpose", e.target.value)}
            placeholder={
              apply
                ? "University legal clinic: students research Rwandan legislation with Claude."
                : "A legal research assistant for our firm's lawyers."
            }
          />
          <small>Don't include confidential client or case details.</small>
        </div>
        {limit ? <CountryPicker countries={countries} limit={limit} value={chosen} onChange={setChosen} /> : null}
        <div className="field">
          <label htmlFor="volume">Expected requests a day</label>
          <select id="volume" value={form.expected_volume} onChange={(e) => set("expected_volume", e.target.value)}>
            <option>Up to 1,000 a day</option>
            <option>1,000 to 50,000 a day</option>
            <option>More than 50,000 a day</option>
          </select>
        </div>
        <div className="grid2">
          <div className="field">
            <label htmlFor="contact-name">Contact name</label>
            <input
              id="contact-name"
              type="text"
              required
              autoComplete="name"
              value={form.contact_name}
              onChange={(e) => set("contact_name", e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="contact-email">Contact email</label>
            <input
              id="contact-email"
              type="email"
              required
              autoComplete="email"
              value={form.contact_email}
              onChange={(e) => set("contact_email", e.target.value)}
            />
          </div>
        </div>
        {blocked ? (
          <label className="choice">
            <input
              type="checkbox"
              checked={form.new_information}
              onChange={(e) => set("new_information", e.target.checked)}
            />
            <span>
              I have new information since the last decision
              <small>Otherwise you can ask again after {when(declined!.renewable_after)}.</small>
            </span>
          </label>
        ) : null}
        <Problem error={error} />
        <div className="dialog-actions">
          <button className="btn secondary" type="button" onClick={onCancel}>
            Cancel
          </button>
          <button className="btn" type="submit" disabled={sending || (!!limit && !chosen.length)}>
            {sending ? "Sending…" : "Send request"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// ---------- API keys ----------
type Key = {
  id: string;
  kind: "personal" | "service";
  name: string;
  last_four: string;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  created_by: string | null;
  mine: boolean;
  // Rotated: a new key has its place, and this one works only until expires_at (at most 24 hours).
  replaced: boolean;
  state: "active" | "expired" | "revoked";
};
type NewKey ={ id: string; name: string; secret: string; expires_at: string };

export function Keys({ org }: PageProps) {
  const list = useAsync(() => api<{ keys: Key[] }>(`/organisations/${org.id}/keys`), [org.id]);
  const [creating, setCreating] = useState(() => new URLSearchParams(location.search).has("create"));
  const [revealed, setRevealed] = useState<NewKey | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [confirm, setConfirm] = useState<Key | null>(null);
  const active = org.access.state === "active";
  const builder = BUILDERS.has(org.role);
  async function revoke(key: Key) {
    try {
      await api(`/organisations/${org.id}/keys/${key.id}/revoke`, {
        method: "POST",
      });
      setConfirm(null);
      list.reload();
    } catch (e) {
      setError(e);
      setConfirm(null);
    }
  }
  async function rotate(key: Key) {
    try {
      const created = await api<NewKey>(`/organisations/${org.id}/keys/${key.id}/rotate`, {
        method: "POST",
        body: { overlap_hours: 24 },
      });
      setRevealed(created);
      list.reload();
    } catch (e) {
      setError(e);
    }
  }
  const keys = list.data?.keys ?? [];
  return (
    <>
      <Heading
        title="API keys"
        org={org}
        description="Keys let your code call the REST API and MCP. Claude, ChatGPT and Codex don't need one: they sign in themselves."
        action={
          active && builder ? (
            <button className="btn" type="button" onClick={() => setCreating(true)}>
              <Plus size={16} aria-hidden="true" />
              Create key
            </button>
          ) : null
        }
      />
      <AccessEnded org={org} />
      <Problem error={error} onDismiss={() => setError(null)} />
      {list.loading && !list.data ? <Loading /> : null}
      {list.error ? <Problem error={list.error} /> : null}
      {keys.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Type</th>
                <th scope="col">Created by</th>
                <th scope="col">Expires</th>
                <th scope="col">Last used</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {keys.map((key) => (
                <tr key={key.id} className={key.state !== "active" ? "dim" : ""}>
                  <td>
                    {key.name}
                    <small className="mono" dir="ltr">
                      {maskedKey(key.id, key.last_four)}
                    </small>
                  </td>
                  <td>
                    <Pill>{key.kind === "personal" ? "Personal" : "Shared, for servers"}</Pill>
                  </td>
                  <td>{key.created_by ?? "—"}</td>
                  <td>
                    {key.state === "revoked" ? (
                      <Pill tone="bad">Revoked</Pill>
                    ) : key.state === "expired" ? (
                      <Pill tone="bad">Expired</Pill>
                    ) : key.replaced ? (
                      <>
                        <Pill tone="warn">Replaced</Pill> until {when(key.expires_at, true)}
                      </>
                    ) : (
                      when(key.expires_at)
                    )}
                  </td>
                  <td>{active || key.state !== "active" ? ago(key.last_used_at) : <Pill tone="bad">Paused</Pill>}</td>
                  <td className="actions">
                    {key.state === "active" && (key.mine || MANAGERS.has(org.role)) ? (
                      <>
                        {active && !key.replaced && (key.mine || key.kind === "service") ? (
                          <button className="link" type="button" onClick={() => rotate(key)}>
                            Rotate
                          </button>
                        ) : null}
                        <button className="link danger" type="button" onClick={() => setConfirm(key)}>
                          Revoke
                        </button>
                      </>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : !list.loading && active ? (
        <div className="empty">
          <KeyRound size={28} strokeWidth={1.5} aria-hidden="true" />
          <h2>No API keys yet</h2>
          <p className="lede">Create a key to call the REST API or MCP from your own code.</p>
          {builder ? (
            <button className="btn" type="button" onClick={() => setCreating(true)}>
              <Plus size={16} aria-hidden="true" />
              Create key
            </button>
          ) : null}
        </div>
      ) : null}
      {active ? (
        <div className="card">
          <h3>First request</h3>
          <div className="code" dir="ltr">
            {`curl ${API_ORIGIN}/api/v1/search?q=constitution \\\n  -H "Authorization: Bearer $OPEN_ALVARY_KEY"`}
          </div>
          <p className="fine">
            <External href="/docs/api">API reference, limits and errors</External>
          </p>
        </div>
      ) : null}
      {creating && active ? (
        <CreateKey
          org={org}
          onClose={() => {
            setCreating(false);
            history.replaceState(null, "", "/console/keys");
          }}
          onCreated={(key) => {
            setCreating(false);
            history.replaceState(null, "", "/console/keys");
            setRevealed(key);
            list.reload();
          }}
        />
      ) : null}
      {revealed ? <RevealKey created={revealed} onClose={() => setRevealed(null)} /> : null}
      {confirm ? (
        <Dialog title={`Revoke ${confirm.name}?`} onClose={() => setConfirm(null)}>
          <p>Requests with this key are refused from their next call. This can't be undone; create a new key if you need one.</p>
          <div className="dialog-actions">
            <button className="btn secondary" type="button" onClick={() => setConfirm(null)}>
              Cancel
            </button>
            <button className="btn danger" type="button" onClick={() => revoke(confirm)}>
              Revoke key
            </button>
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

function CreateKey({ org, onClose, onCreated }: { org: Organisation; onClose: () => void; onCreated: (key: NewKey) => void }) {
  type Draft = { name: string; kind: "personal" | "service"; days: number };
  const [name, setName] = useState(() => peekDraft<Draft>("key")?.name ?? "");
  const [kind, setKind] = useState<"personal" | "service">(() => peekDraft<Draft>("key")?.kind ?? "personal");
  const [days, setDays] = useState(() => peekDraft<Draft>("key")?.days ?? 90);
  useEffect(() => clearDraft("key"), []);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const expiry = (d: number) => when(new Date(Date.now() + d * 86400000).toISOString());
  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(
        await api<NewKey>(`/organisations/${org.id}/keys`, {
          method: "POST",
          body: { name, kind, days },
        }),
      );
    } catch (e) {
      // Keep the choices for the trip to the sign-in service and back.
      if (e instanceof ApiError) saveDraft("key", { name, kind, days });
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title="Create an API key" onClose={onClose}>
      <form className="form" onSubmit={create}>
        <div className="field">
          <label htmlFor="key-name">Name</label>
          <input id="key-name" type="text" required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} placeholder="laptop-dev" />
          <small>So you can tell keys apart later.</small>
        </div>
        <fieldset className="field">
          <legend>Type</legend>
          <label className="choice">
            <input type="radio" name="kind" checked={kind === "personal"} onChange={() => setKind("personal")} />
            <span>
              Personal
              <small>For your own scripts. Stops working if you leave the organisation.</small>
            </span>
          </label>
          {MANAGERS.has(org.role) ? (
            <label className="choice">
              <input type="radio" name="kind" checked={kind === "service"} onChange={() => setKind("service")} />
              <span>
                Shared, for servers
                <small>Belongs to the organisation and survives people leaving.</small>
              </span>
            </label>
          ) : null}
        </fieldset>
        <div className="field">
          <label htmlFor="key-days">Expires</label>
          <select id="key-days" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={30}>In 30 days ({expiry(30)})</option>
            <option value={90}>In 90 days ({expiry(90)})</option>
            <option value={365}>In 1 year ({expiry(365)})</option>
          </select>
        </div>
        <p className="fine">Can read the published corpus within this organisation's access. Nothing else.</p>
        <Problem error={error} />
        <div className="dialog-actions">
          <button className="btn secondary" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="btn" type="submit" disabled={busy}>
            {busy ? "Creating…" : "Create key"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function RevealKey({ created, onClose }: { created: NewKey; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const [warn, setWarn] = useState(false);
  function done() {
    if (!copied && !warn) {
      setWarn(true);
      return;
    }
    onClose();
  }
  return (
    <Dialog title="Copy your key now" onClose={done} closable={false}>
      <p>This is the only time it's shown. Store it in a password manager or your server's secret store.</p>
      <CopyField value={created.secret} label="Your new API key" onCopied={() => setCopied(true)} />
      <div className="field">
        <span>Try it</span>
        <div className="code" dir="ltr">
          {`export OPEN_ALVARY_KEY=${created.secret.slice(0, 14)}…\ncurl ${API_ORIGIN}/api/v1/search?q=constitution \\\n  -H "Authorization: Bearer $OPEN_ALVARY_KEY"`}
        </div>
      </div>
      {warn ? (
        <div className="callout" role="alert">
          You haven't copied the key. Once closed, it can't be shown again; you would need to create a new one.
        </div>
      ) : null}
      <div className="dialog-actions">
        {warn ? (
          <button className="btn secondary" type="button" onClick={onClose}>
            Close without copying
          </button>
        ) : null}
        <button className="btn" type="button" onClick={done}>
          I've stored it
        </button>
      </div>
    </Dialog>
  );
}

// ---------- Connected apps ----------
type Grant = {
  id: string;
  client_name: string;
  client_label: "verified_publisher" | "from_host" | "unverified";
  client_host: string | null;
  resource: "api" | "mcp";
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  person: string | null;
  mine: boolean;
};

export function Apps({ org }: PageProps) {
  const list = useAsync(() => api<{ clients: Grant[] }>(`/organisations/${org.id}/clients`), [org.id]);
  const [error, setError] = useState<unknown>(null);
  const active = org.access.state === "active";
  async function revoke(grant: Grant) {
    try {
      await api(`/organisations/${org.id}/clients/${grant.id}/revoke`, {
        method: "POST",
      });
      list.reload();
    } catch (e) {
      setError(e);
    }
  }
  const grants = (list.data?.clients ?? []).filter((g) => !g.revoked_at);
  return (
    <>
      <Heading
        title="Connected apps"
        org={org}
        description="Apps such as Claude, Claude Code and Codex that you've allowed to read the corpus for this organisation."
        action={
          active && BUILDERS.has(org.role) ? (
            <button className="btn" type="button" onClick={() => go("/console/connect")}>
              Connect an app
            </button>
          ) : null
        }
      />
      <AccessEnded org={org} />
      {org.access.state !== "active" ? <NeedsAccess what="Connected apps" org={org} /> : null}
      <Problem error={error} onDismiss={() => setError(null)} />
      {list.loading && !list.data ? <Loading /> : null}
      {grants.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">App</th>
                <th scope="col">Connected by</th>
                <th scope="col">Uses</th>
                <th scope="col">Connected</th>
                <th scope="col">Last used</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {grants.map((grant) => (
                <tr key={grant.id}>
                  <td>
                    {grant.client_name}
                    <small>{grant.client_label === "verified_publisher" ? "Verified publisher" : grant.client_label === "from_host" ? `From ${grant.client_host}` : "Unverified app"}</small>
                  </td>
                  <td>{grant.person ?? "—"}</td>
                  <td>{grant.resource === "mcp" ? "MCP" : "REST API"}</td>
                  <td>{when(grant.created_at)}</td>
                  <td>{active ? ago(grant.last_used_at) : <Pill tone="bad">Paused</Pill>}</td>
                  <td className="actions">
                    {grant.mine || MANAGERS.has(org.role) ? (
                      <button className="link danger" type="button" onClick={() => revoke(grant)}>
                        Disconnect
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : !list.loading && active ? (
        <div className="empty">
          <Plug size={28} strokeWidth={1.5} aria-hidden="true" />
          <h2>No apps connected</h2>
          <p className="lede">Connect Claude, Claude Code or Codex to ask questions over the corpus.</p>
          {BUILDERS.has(org.role) ? (
            <button className="btn" type="button" onClick={() => go("/console/connect")}>
              Connect an app
            </button>
          ) : null}
        </div>
      ) : null}
      <p className="fine">
        Disconnecting stops the app at its next request. Remove it in the app too, so it stops asking you to sign
        in.
      </p>
    </>
  );
}

const TABS = [
  ["claude", "Claude"],
  ["code", "Claude Code"],
  ["codex", "Codex"],
  ["rest", "Your own code"],
] as const;

export function Connect({ org }: PageProps) {
  const initial = new URLSearchParams(location.search).get("app");
  const [tab, setTab] = useState<string>(TABS.some(([id]) => id === initial) ? initial! : "claude");
  const command = `claude mcp add --transport http open-alvary ${MCP_URL}`;
  return (
    <>
      <Heading
        title="Connect an app"
        org={org}
        description="Apps sign in with your Open Alvary account and act for one organisation. You approve each one, and can disconnect it any time."
      />
      {org.access.state !== "active" ? <NeedsAccess what="Connected apps" org={org} /> : null}
      <div className="card">
        <div className="tabs" role="tablist">
          {TABS.map(([id, label]) => (
            <button
              key={id}
              role="tab"
              type="button"
              aria-selected={tab === id}
              onClick={() => {
                setTab(id);
                history.replaceState(null, "", `/console/connect?app=${id}`);
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <div role="tabpanel">
          {tab === "claude" ? (
            <ol className="steps">
              <li>
                In Claude, open <b>Settings → Connectors</b> and choose <b>Add custom connector</b>.
              </li>
              <li>
                Paste this address and add it:
                <CopyField value={MCP_URL} label="MCP address" />
              </li>
              <li>
                Choose <b>Connect</b>. Claude sends you here to sign in and approve; choose this organisation.
              </li>
            </ol>
          ) : null}
          {tab === "code" ? (
            <ol className="steps">
              <li>
                Add the server:
                <CopyField value={command} label="Command" />
              </li>
              <li>
                Run <code>/mcp</code> in Claude Code, choose <b>open-alvary</b> and <b>Authenticate</b>. Your browser
                opens here to approve.
              </li>
            </ol>
          ) : null}
          {tab === "codex" ? (
            <ol className="steps">
              <li>
                Add the server:
                <CopyField value={`codex mcp add open-alvary --url ${MCP_URL}`} label="Command" />
              </li>
              <li>
                Sign in:
                <CopyField value="codex mcp login open-alvary" label="Command" />
              </li>
              <li>Approve in the browser window that opens.</li>
            </ol>
          ) : null}
          {tab === "rest" ? (
            <>
              <p>Create an API key, then call the REST API. MCP clients that accept a header can use the same key.</p>
              <div className="code" dir="ltr">
                {`curl ${API_ORIGIN}/api/v1/sources/ng-foi-act-2011 \\\n  -H "Authorization: Bearer $OPEN_ALVARY_KEY"`}
              </div>
              <div className="row">
                <button className="btn secondary" type="button" onClick={() => go("/console/keys?create=1")}>
                  Create key
                </button>
              </div>
            </>
          ) : null}
        </div>
      </div>
    </>
  );
}

// ---------- Usage ----------
type UsageDay = {
  day: string;
  credential: string;
  class: string;
  requests: number;
  bytes: number;
  refusals: Record<string, number>;
};

const REFUSALS: Record<string, [string, string]> = {
  rate_limited: ["More requests than your limit allows", "Wait the seconds in Retry-After"],
  jurisdiction_not_included: ["A jurisdiction outside your access", "Change it under Plan and limits"],
  entitlement_lapsed: ["Access had ended", "Renew access"],
  entitlement_required: ["No access yet", "Request access"],
};

export function Usage({ org }: PageProps) {
  const usage = useAsync(() => api<{ days: UsageDay[] }>(`/organisations/${org.id}/usage?days=30`), [org.id]);
  const keys = useAsync(() => api<{ keys: Key[] }>(`/organisations/${org.id}/keys`), [org.id]);
  const apps = useAsync(() => api<{ clients: Grant[] }>(`/organisations/${org.id}/clients`), [org.id]);
  if (usage.loading && !usage.data) return <Loading />;
  if (usage.error) return <Problem error={usage.error} />;
  const days = usage.data!.days;
  const today = new Date().toISOString().slice(0, 10);
  const byDay = new Map<string, number>();
  const byCredential = new Map<string, number>();
  const refusals = new Map<string, number>();
  let todayRequests = 0;
  let todayBytes = 0;
  for (const d of days) {
    byDay.set(d.day, (byDay.get(d.day) ?? 0) + d.requests);
    if (d.day === today) {
      todayRequests += d.requests;
      todayBytes += d.bytes;
      byCredential.set(d.credential, (byCredential.get(d.credential) ?? 0) + d.requests);
    }
    for (const [code, count] of Object.entries(d.refusals)) refusals.set(code, (refusals.get(code) ?? 0) + count);
  }
  const names = new Map<string, string>();
  for (const k of keys.data?.keys ?? []) names.set(k.id, k.name);
  for (const g of apps.data?.clients ?? []) names.set(g.id, g.client_name);
  const series = Array.from({ length: 14 }, (_, i) => {
    const day = new Date(Date.now() - (13 - i) * 86400000).toISOString().slice(0, 10);
    return { day, requests: byDay.get(day) ?? 0 };
  });
  return (
    <>
      <Heading title="Usage" org={org} description="Requests by this organisation's keys and apps, counted per day (UTC)." />
      <AccessEnded org={org} />
      <div className="grid3">
        <div className="card stat">
          <b>{number(todayRequests)}</b>
          <span>requests today (UTC)</span>
        </div>
        <div className="card stat">
          <b>{size(todayBytes)}</b>
          <span>sent today</span>
        </div>
        <div className="card stat">
          <b>{number([...refusals.values()].reduce((a, b) => a + b, 0))}</b>
          <span>requests refused in 30 days</span>
        </div>
      </div>
      {days.length ? (
        <div className="card">
          <h3>Requests a day</h3>
          <Chart series={series} />
        </div>
      ) : (
        <div className="empty">
          <BarChart3 size={28} strokeWidth={1.5} aria-hidden="true" />
          <h2>No requests yet</h2>
          <p className="lede">Calls from your keys and connected apps appear here within a minute.</p>
          <button className="btn secondary" type="button" onClick={() => go("/console/keys")}>
            Go to API keys
          </button>
        </div>
      )}
      {refusals.size ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Refused</th>
                <th scope="col">Why</th>
                <th scope="col">What to do</th>
                <th scope="col">In 30 days</th>
              </tr>
            </thead>
            <tbody>
              {[...refusals.entries()].map(([code, count]) => (
                <tr key={code}>
                  <td>
                    <code>{code}</code>
                  </td>
                  <td>{REFUSALS[code]?.[0] ?? "—"}</td>
                  <td>{REFUSALS[code]?.[1] ?? "—"}</td>
                  <td>{number(count)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {byCredential.size ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">By key or app, today</th>
                <th scope="col">Requests</th>
              </tr>
            </thead>
            <tbody>
              {[...byCredential.entries()].map(([credential, count]) => (
                <tr key={credential}>
                  <td>
                    {names.get(credential) ?? "Removed"}
                    <small className="mono" dir="ltr">
                      {credential.startsWith("key_") ? maskedKey(credential) : `${credential.slice(0, 12)}…`}
                    </small>
                  </td>
                  <td>{number(count)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}

function Chart({ series }: { series: { day: string; requests: number }[] }) {
  const max = Math.max(10, ...series.map((s) => s.requests));
  const top = niceCeiling(max);
  const width = 560;
  const height = 160;
  const barWidth = width / series.length;
  const total = series.reduce((a, s) => a + s.requests, 0);
  return (
    <svg viewBox={`0 0 ${width} ${height + 24}`} width="100%" role="img" aria-label={`Requests a day for the last 14 days, ${number(total)} in all`}>
      <line x1="0" y1={height} x2={width} y2={height} className="axis" />
      <line x1="0" y1={height - (height - 16)} x2={width} y2={height - (height - 16)} className="grid" />
      <text x="0" y="12" className="chart-label">
        {number(top)}
      </text>
      {series.map((s, i) => {
        const h = Math.round((s.requests / top) * (height - 16));
        return (
          <rect key={s.day} x={i * barWidth + 4} y={height - h} width={barWidth - 8} height={h} rx="2" className={i === series.length - 1 ? "bar now" : "bar"}>
            <title>{`${s.day}: ${number(s.requests)}`}</title>
          </rect>
        );
      })}
      <text x="4" y={height + 18} className="chart-label">
        {when(series[0].day)}
      </text>
      <text x={width - 4} y={height + 18} className="chart-label" textAnchor="end">
        Today
      </text>
    </svg>
  );
}

function size(bytes: number) {
  if (bytes < 1000) return `${bytes} bytes`;
  if (bytes < 1e6) return `${(bytes / 1e3).toLocaleString("en-GB", { maximumFractionDigits: 1 })} KB`;
  return `${(bytes / 1e6).toLocaleString("en-GB", { maximumFractionDigits: 1 })} MB`;
}

function niceCeiling(value: number) {
  const power = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 5, 10]) if (step * power >= value) return step * power;
  return 10 * power;
}

// ---------- Members ----------
type Member = {
  id: string;
  role: string;
  joined_at: string;
  display_name: string | null;
  email: string | null;
  second_factor: string;
  you: boolean;
};
type Members = {
  members: Member[];
  invitations: {
    id: string;
    email: string;
    role: string;
    created_at: string;
    expires_at: string;
  }[];
  invite_roles: string[];
};

export function MembersPage({ org, refresh }: PageProps) {
  const data = useAsync(() => api<Members>(`/organisations/${org.id}/members`), [org.id]);
  const [error, setError] = useState<unknown>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("developer");
  const [transfer, setTransfer] = useState<Member | null>(null);
  const [leave, setLeave] = useState<Member | null>(null);
  const manager = MANAGERS.has(org.role);
  async function run(action: () => Promise<unknown>) {
    setError(null);
    try {
      await action();
      data.reload();
    } catch (e) {
      setError(e);
    }
  }
  if (data.loading && !data.data) return <Loading />;
  if (data.error) return <Problem error={data.error} />;
  const { members, invitations, invite_roles } = data.data!;
  const you = members.find((m) => m.you);
  return (
    <>
      <Heading title="Members" org={org} description="People in this organisation and what their role lets them do." />
      <Problem error={error} onDismiss={() => setError(null)} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Person</th>
              <th scope="col">Role</th>
              {manager ? <th scope="col">Second factor</th> : null}
              <th scope="col">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {members.map((m) => (
              <tr key={m.id}>
                <td>
                  {m.display_name ?? "—"}
                  {m.you ? " (you)" : ""}
                  {m.email ? <small>{m.email}</small> : null}
                </td>
                <td>
                  {canChange(org.role, m) ? (
                    <select
                      aria-label={`Role of ${m.display_name ?? "member"}`}
                      value={m.role}
                      onChange={(e) => run(() => api(`/organisations/${org.id}/members/${m.id}`, { method: "PATCH", body: { role: e.target.value } }))}
                    >
                      {(org.role === "owner" ? ["owner", "admin", "developer", "viewer"] : ["admin", "developer", "viewer"]).map((r) => (
                        <option key={r} value={r}>
                          {ROLE_NAMES[r]}
                        </option>
                      ))}
                    </select>
                  ) : (
                    ROLE_NAMES[m.role]
                  )}
                </td>
                {manager ? (
                  <td>
                    {m.second_factor !== "none" ? (
                      <Pill tone="good">On</Pill>
                    ) : ["owner", "admin"].includes(m.role) && !org.access.starter ? (
                      <Pill tone="warn">Needed</Pill>
                    ) : (
                      <span className="fine">Not set up</span>
                    )}
                  </td>
                ) : null}
                <td className="actions">
                  {m.you ? (
                    <button className="link" type="button" onClick={() => setLeave(m)}>
                      Leave
                    </button>
                  ) : manager && (m.role !== "owner" || org.role === "owner") ? (
                    <>
                      {org.role === "owner" ? (
                        <button className="link" type="button" onClick={() => setTransfer(m)}>
                          Make owner
                        </button>
                      ) : null}
                      <button
                        className="link danger"
                        type="button"
                        onClick={() =>
                          run(() =>
                            api(`/organisations/${org.id}/members/${m.id}`, {
                              method: "DELETE",
                            }),
                          )
                        }
                      >
                        Remove
                      </button>
                    </>
                  ) : null}
                </td>
              </tr>
            ))}
            {invitations.map((i) => (
              <tr key={i.id} className="dim">
                <td>
                  {i.email}
                  <small>Invited {when(i.created_at)} · expires {when(i.expires_at)}</small>
                </td>
                <td>{ROLE_NAMES[i.role]}</td>
                {manager ? <td>—</td> : null}
                <td className="actions">
                  <button
                    className="link danger"
                    type="button"
                    onClick={() =>
                      run(() =>
                        api(`/organisations/${org.id}/invitations/${i.id}`, {
                          method: "DELETE",
                        }),
                      )
                    }
                  >
                    Revoke invitation
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {invite_roles.length ? (
        <form
          className="card form"
          onSubmit={(e) => {
            e.preventDefault();
            run(async () => {
              await api(`/organisations/${org.id}/invitations`, {
                method: "POST",
                body: { email, role },
              });
              setEmail("");
            });
          }}
        >
          <h2>Invite someone</h2>
          <div className="grid2">
            <div className="field">
              <label htmlFor="invite-email">Email</label>
              <input id="invite-email" type="email" required autoComplete="off" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@university.ac.rw" />
            </div>
            <div className="field">
              <label htmlFor="invite-role">Role</label>
              <select id="invite-role" value={role} onChange={(e) => setRole(e.target.value)}>
                {invite_roles.map((r) => (
                  <option key={r} value={r}>
                    {ROLE_NAMES[r]}: {ROLE_HELP[r].toLowerCase()}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="row">
            <button className="btn" type="submit">
              Send invitation
            </button>
            <span className="fine">They accept by signing in with that address within 7 days.</span>
          </div>
        </form>
      ) : null}
      {transfer ? (
        <TransferDialog
          member={transfer}
          onClose={() => setTransfer(null)}
          onConfirm={(keep) =>
            run(async () => {
              await api(`/organisations/${org.id}/transfer-ownership`, { method: "POST", body: { member_id: transfer.id, keep_owner: keep } });
              setTransfer(null);
              refresh();
            })
          }
        />
      ) : null}
      {leave && you ? (
        <Dialog title={`Leave ${org.name}?`} onClose={() => setLeave(null)}>
          <p>Your personal keys and the apps you connected for this organisation stop working at once.</p>
          {you.role === "owner" ? <p className="fine">If you are its only owner, make someone else an owner first.</p> : null}
          <div className="dialog-actions">
            <button className="btn secondary" type="button" onClick={() => setLeave(null)}>
              Cancel
            </button>
            <button
              className="btn danger"
              type="button"
              onClick={() =>
                run(async () => {
                  await api(`/organisations/${org.id}/members/${you.id}`, {
                    method: "DELETE",
                  });
                  localStorage.removeItem("oa_console_org");
                  location.assign("/console");
                }).finally(() => setLeave(null))
              }
            >
              Leave
            </button>
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

function canChange(actorRole: string, member: Member) {
  if (member.you) return false;
  if (actorRole === "owner") return true;
  return actorRole === "admin" && member.role !== "owner";
}

function TransferDialog({ member, onClose, onConfirm }: { member: Member; onClose: () => void; onConfirm: (keep: boolean) => void }) {
  const [keep, setKeep] = useState(true);
  return (
    <Dialog title={`Make ${member.display_name ?? "this member"} an owner?`} onClose={onClose}>
      <p>Owners manage access and can delete the organisation.</p>
      <label className="choice">
        <input type="radio" name="keep" checked={keep} onChange={() => setKeep(true)} />
        <span>Stay an owner as well</span>
      </label>
      <label className="choice">
        <input type="radio" name="keep" checked={!keep} onChange={() => setKeep(false)} />
        <span>Become an admin</span>
      </label>
      <div className="dialog-actions">
        <button className="btn secondary" type="button" onClick={onClose}>
          Cancel
        </button>
        <button className="btn" type="button" onClick={() => onConfirm(keep)}>
          Make owner
        </button>
      </div>
    </Dialog>
  );
}

// ---------- Settings ----------
type AuditEvent = {
  seq: number;
  at: string;
  actor: string;
  action: string;
  target_id: string | null;
  summary: Record<string, unknown>;
};

const ACTIONS: Record<string, string> = {
  "organisation.created": "Created the organisation",
  "organisation.updated": "Changed organisation details",
  "access.requested": "Requested access",
  "access.withdrawn": "Withdrew the access request",
  "access.declined": "Declined the access request",
  "access.needs_information": "Asked for more information",
  "entitlement.granted": "Granted access",
  "entitlement.ended": "Ended access",
  "entitlement.revoked": "Revoked access",
  "key.created": "Created a key",
  "key.rotated": "Rotated a key",
  "key.revoked": "Revoked a key",
  "client.connected": "Connected an app",
  "client.revoked": "Disconnected an app",
  "member.invited": "Invited someone",
  "member.invitation_revoked": "Revoked an invitation",
  "member.joined": "Joined",
  "member.left": "Left",
  "member.removed": "Removed a member",
  "member.role_changed": "Changed a role",
  "ownership.transferred": "Transferred ownership",
};

export function Settings({ org, refresh }: PageProps) {
  const details = useAsync(() => api<Organisation>(`/organisations/${org.id}`), [org.id]);
  const manager = MANAGERS.has(org.role);
  const audit = useAsync(
    () => (manager ? api<{ events: AuditEvent[] }>(`/organisations/${org.id}/audit`) : Promise.resolve({ events: [] })),
    [org.id, manager],
  );
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  if (details.loading && !details.data) return <Loading />;
  if (details.error) return <Problem error={details.error} />;
  const o = details.data!;
  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setError(null);
    setSaved(false);
    try {
      await api(`/organisations/${org.id}`, {
        method: "PATCH",
        body: {
          revision: o.revision,
          name: form.get("name"),
          kind: form.get("kind"),
          country: (form.get("country") as string) || null,
          website: (form.get("website") as string) || null,
        },
      });
      setSaved(true);
      details.reload();
      refresh();
    } catch (e) {
      setError(e);
    }
  }
  async function remove() {
    try {
      await api(`/organisations/${org.id}`, { method: "DELETE" });
      localStorage.removeItem("oa_console_org");
      location.assign("/console");
    } catch (e) {
      setError(e);
      setConfirmDelete(false);
    }
  }
  return (
    <>
      <Heading title="Settings" org={org} description="This organisation's details, its audit log, and deleting it." />
      <Problem error={error} onDismiss={() => setError(null)} />
      <form className="card form" onSubmit={save}>
        <h2>Organisation</h2>
        <fieldset disabled={!manager} className="plain">
          <div className="grid2">
            <div className="field">
              <label htmlFor="org-name">Name</label>
              <input id="org-name" name="name" type="text" required maxLength={120} defaultValue={o.name} />
            </div>
            <div className="field">
              <label htmlFor="org-kind">Type</label>
              <select id="org-kind" name="kind" defaultValue={o.kind}>
                <option value="individual">Individual</option>
                <option value="company">Company</option>
                <option value="academic">Academic</option>
                <option value="non_profit">Non-profit</option>
                <option value="government">Government</option>
                <option value="other">Other</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="org-country">Country</label>
              <select id="org-country" name="country" defaultValue={o.country ?? ""}>
                <option value="">Not set</option>
                {COUNTRIES.map(([code, name]) => (
                  <option key={code} value={code}>
                    {name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="org-website">Website (optional)</label>
              <input id="org-website" name="website" type="url" defaultValue={o.website ?? ""} placeholder="https://" />
            </div>
          </div>
        </fieldset>
        {manager ? (
          <div className="row">
            <button className="btn secondary" type="submit">
              Save
            </button>
            <span className="fine" aria-live="polite">
              {saved ? "Saved." : ""}
            </span>
          </div>
        ) : (
          <p className="fine">Owners and admins change these details.</p>
        )}
      </form>
      {manager ? (
        <div className="card">
          <h2>Audit log</h2>
          <p className="fine">Every access change in this organisation. Owners and admins see it.</p>
          {audit.data?.events.length ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">When (UTC)</th>
                    <th scope="col">Who</th>
                    <th scope="col">What</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.data.events.map((e) => (
                    <tr key={e.seq}>
                      <td>{when(e.at, true)}</td>
                      <td>{e.actor}</td>
                      <td>
                        {ACTIONS[e.action] ?? e.action}
                        {e.target_id?.startsWith("key_") ? (
                          <small className="mono" dir="ltr">
                            {maskedKey(e.target_id)}
                          </small>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Loading />
          )}
        </div>
      ) : null}
      {org.role === "owner" ? (
        <div className="card">
          <h2>Delete this organisation</h2>
          <p className="fine">
            Every key and connected app stops working at once, its access ends, and members lose access to it.
          </p>
          <div className="row">
            <button className="btn danger" type="button" onClick={() => setConfirmDelete(true)}>
              Delete organisation
            </button>
          </div>
        </div>
      ) : null}
      {confirmDelete ? (
        <Dialog title={`Delete ${o.name}?`} onClose={() => setConfirmDelete(false)}>
          <p>This can't be undone. Keys and connected apps stop working at once, and access ends.</p>
          <div className="dialog-actions">
            <button className="btn secondary" type="button" onClick={() => setConfirmDelete(false)}>
              Cancel
            </button>
            <button className="btn danger" type="button" onClick={remove}>
              Delete organisation
            </button>
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

// ---------- Security ----------
type Identity = {
  id: string;
  method: string;
  label: string;
  linked_at: string;
  last_used_at: string | null;
};
type Session = {
  ref: string;
  created_at: string;
  last_seen_at: string;
  client_family: string | null;
  network_prefix: string | null;
};

export function Security({ me, refresh }: { me: Me; refresh: () => void }) {
  const identities = useAsync(() => api<{ identities: Identity[] }>("/me/identities"), []);
  const sessions = useAsync(() => api<{ sessions: Session[] }>("/me/sessions"), []);
  const [error, setError] = useState<unknown>(null);
  const [name, setName] = useState(me.person.display_name ?? "");
  const [saved, setSaved] = useState(false);
  const factorSeen = me.person.second_factor !== "none" || !!me.second_factor_until;
  async function unlink(identity: Identity) {
    try {
      await api(`/me/identities/${identity.id}`, { method: "DELETE" });
      identities.reload();
    } catch (e) {
      setError(e);
    }
  }
  return (
    <>
      <Heading title="Security" description="How you sign in, your second factor, your profile and where you're signed in." />
      <Problem error={error} onDismiss={() => setError(null)} />
      <div className="card">
        <h2>Second factor</h2>
        <p className="lede">
          A second proof that it's you, with Touch ID, a security key or an authenticator app. Owners and admins of
          organisations with approved or paid access need one to create keys, connect apps and make other sensitive
          changes; on the free Starter plan it's recommended.
        </p>
        <p>{factorSeen ? <Pill tone="good">Used in this session</Pill> : <Pill>Not used yet</Pill>}</p>
        <div className="row">
          <a className="btn" href={`/console/auth/start?purpose=second_factor&return_to=${encodeURIComponent("/console/security")}`}>
            {factorSeen ? "Confirm or add a factor" : "Set up a second factor"}
          </a>
          <span className="fine">You'll set it up on the sign-in page and come back here. Keep the recovery code it shows.</span>
        </div>
      </div>
      <div className="card">
        <h2>Sign-in methods</h2>
        {identities.data ? (
          <table>
            <tbody>
              {identities.data.identities.map((i) => (
                <tr key={i.id}>
                  <td>
                    {i.method === "google" ? "Google" : i.method === "email" ? "Email code" : i.method}
                    <small>
                      {i.label} · added {when(i.linked_at)}
                    </small>
                  </td>
                  <td className="actions">
                    {identities.data!.identities.length > 1 ? (
                      <button className="link" type="button" onClick={() => unlink(i)}>
                        Remove
                      </button>
                    ) : (
                      <span className="fine">Your only method</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Loading />
        )}
        <div className="row">
          <a className="btn secondary" href="/console/auth/start?purpose=link&method=google&return_to=%2Fconsole%2Fsecurity">
            Add Google
          </a>
          <a className="btn secondary" href="/console/auth/start?purpose=link&method=email&return_to=%2Fconsole%2Fsecurity">
            Add email code
          </a>
        </div>
      </div>
      <form
        className="card form"
        onSubmit={async (e) => {
          e.preventDefault();
          setSaved(false);
          try {
            await api("/me", { method: "PATCH", body: { display_name: name } });
            setSaved(true);
            refresh();
          } catch (err) {
            setError(err);
          }
        }}
      >
        <h2>Profile</h2>
        <div className="field">
          <label htmlFor="profile-name">Full name</label>
          <input id="profile-name" type="text" autoComplete="name" required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
          <small>Members of your organisations see it. Your email ({me.person.email}) is shown only to their owners and admins.</small>
        </div>
        <div className="row">
          <button className="btn secondary" type="submit">
            Save
          </button>
          <span className="fine" aria-live="polite">
            {saved ? "Saved." : ""}
          </span>
        </div>
      </form>
      <div className="card">
        <h2>Sessions</h2>
        {sessions.data ? (
          <table>
            <tbody>
              {sessions.data.sessions.map((s) => (
                <tr key={s.ref}>
                  <td>
                    {s.client_family ?? "Browser"}
                    <small>
                      {s.network_prefix ?? "Unknown network"} · signed in {when(s.created_at)}
                    </small>
                  </td>
                  <td>{ago(s.last_seen_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Loading />
        )}
        <div className="row">
          <button
            className="btn danger"
            type="button"
            onClick={async () => {
              try {
                await api("/me/sessions/revoke-all", { method: "POST" });
                location.assign("/console/sign-in");
              } catch (e) {
                setError(e);
              }
            }}
          >
            Sign out everywhere
          </button>
        </div>
      </div>
    </>
  );
}
