// Monitor (SUB C2; "watches" in the specification): follow countries, records and kinds of change; matches arrive in the inbox here
// and, if chosen, in a daily or weekly email. Also the digest settings and the unsubscribe page.
import React, { useEffect, useState } from "react";
import { Bell, Check, Pause, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { ApiError, api, go } from "./api";
import { Dialog, External, Loading, Mark, Pill, Problem, useAsync, when } from "./ui";
import { Heading, type PageProps } from "./pages";

type Filters = { jurisdiction: string[]; work: string[]; type: string[]; class: string[]; language: string[]; q: string | null };
type Watch = {
  id: string;
  revision: number;
  name: string;
  filters: Filters;
  state: "active" | "paused";
  visibility: "private" | "organisation";
  mine: boolean;
  email: "off" | "daily" | "weekly";
  unread: number;
  activated_at: string;
};
type Limits = { watches: number; digest: "off" | "weekly" | "daily"; shared: boolean; used: number };
type ChangeEvent = {
  event_id: string;
  event_type: string;
  committed_at: string;
  jurisdiction: { id: string; name: string } | null;
  work: { id: string; title: string; url: string } | null;
  summary: string;
};
type InboxItem = { id: string; watch: { id: string; name: string }; matched_at: string; read: boolean; event: ChangeEvent };
type Country = { id: string; name: string; works: number };

const TYPES: [string, string][] = [
  ["catalogue_added", "Added"],
  ["metadata_corrected", "Corrected"],
  ["text_version_published", "Text published"],
  ["withdrawal", "Withdrawn"],
  ["coverage_changed", "Coverage"],
];
const LABEL = Object.fromEntries(TYPES);

function describe(filters: Filters, countries: Country[]) {
  const name = (id: string) => countries.find((c) => c.id === id)?.name ?? id.toUpperCase();
  const parts = [
    filters.jurisdiction.length ? filters.jurisdiction.map(name).join(", ") : null,
    filters.work.length ? `${filters.work.length} ${filters.work.length === 1 ? "record" : "records"}` : null,
    filters.type.length ? filters.type.map((t) => LABEL[t] ?? t).join(", ") : null,
    filters.q ? `“${filters.q}”` : null,
  ].filter(Boolean);
  return parts.join(" · ") || "Everything";
}

// Countries with public records, from the website's own data path.
function useCountries() {
  const [countries, setCountries] = useState<Country[]>([]);
  useEffect(() => {
    fetch("/site/v1/change-coverage")
      .then((r) => (r.ok ? r.json() : { jurisdictions: [] }))
      .then((data) => setCountries(data.jurisdictions ?? []))
      .catch(() => setCountries([]));
  }, []);
  return countries;
}

export function WatchesPage({ org }: PageProps) {
  const [tab, setTab] = useState<"inbox" | "watches">("inbox");
  const watches = useAsync(() => api<{ watches: Watch[]; limits: Limits }>(`/organisations/${org.id}/watches`), [org.id]);
  const inbox = useAsync(() => api<{ items: InboxItem[] }>(`/organisations/${org.id}/inbox`), [org.id]);
  const countries = useCountries();
  const [editing, setEditing] = useState<Watch | "new" | null>(null);
  const [prefill, setPrefill] = useState<{ work?: string; title?: string } | null>(null);
  const [deleting, setDeleting] = useState<Watch | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    // "Monitor this record" on a record page arrives as /console/monitor?new=1&work=<id>.
    const params = new URLSearchParams(location.search);
    const work = params.get("work");
    if (params.get("new") && work) {
      fetch("/site/v1/sources/" + encodeURIComponent(work))
        .then((r) => (r.ok ? r.json() : null))
        .then((source) => {
          setPrefill({ work, title: source?.title });
          setEditing("new");
        });
      history.replaceState(null, "", "/console/monitor");
    }
  }, []);
  if (watches.loading && !watches.data) return <Loading />;
  if (watches.error) return <Problem error={watches.error} />;
  const { watches: list, limits } = watches.data!;
  const items = inbox.data?.items ?? [];
  const unread = items.filter((i) => !i.read).length;
  const full = limits.used >= limits.watches;
  async function act(work: () => Promise<unknown>) {
    setError(null);
    try {
      await work();
      watches.reload();
      inbox.reload();
    } catch (e) {
      setError(e);
    }
  }
  return (
    <>
      <Heading
        title="Monitor"
        description="Monitor countries, records and kinds of change. Matches arrive here, and by email if you choose."
        action={
          <button className="btn" type="button" disabled={full} onClick={() => (setPrefill(null), setEditing("new"))}>
            <Plus size={16} aria-hidden="true" /> New monitor
          </button>
        }
      />
      <p className="fine">
        {limits.used} of {limits.watches} monitors on your plan ·{" "}
        {limits.digest === "daily" ? "daily or weekly email" : "weekly email"}
        {limits.shared ? " · monitors can be shared with your organisation" : ""} ·{" "}
        <button className="link" type="button" onClick={() => go("/console/access")}>
          Plan and limits
        </button>
      </p>
      <Problem error={error} onDismiss={() => setError(null)} />
      <div className="tabs" role="tablist">
        <button role="tab" type="button" aria-selected={tab === "inbox"} onClick={() => setTab("inbox")}>
          Inbox {unread ? <Pill tone="good">{unread} new</Pill> : null}
        </button>
        <button role="tab" type="button" aria-selected={tab === "watches"} onClick={() => setTab("watches")}>
          Your monitors <span className="fine">({list.length})</span>
        </button>
      </div>
      {tab === "inbox" ? (
        items.length ? (
          <div className="card">
            <div className="card-head">
              <h2>Latest matches</h2>
              {unread ? (
                <button className="link" type="button" onClick={() => act(() => api(`/organisations/${org.id}/inbox/read`, { method: "POST", body: {} }))}>
                  <Check size={14} aria-hidden="true" /> Mark all read
                </button>
              ) : null}
            </div>
            <ol className="inbox">
              {items.map((item) => (
                <li key={item.id} className={item.read ? "" : "unread"}>
                  <div className="inbox-meta">
                    <Pill>{LABEL[item.event.event_type] ?? item.event.event_type}</Pill>
                    <span>{when(item.event.committed_at)}</span>
                    <span>· {item.watch.name}</span>
                  </div>
                  {item.event.work ? (
                    <External href={item.event.work.url} className="inbox-title">
                      {item.event.work.title}
                    </External>
                  ) : null}
                  <p>{item.event.summary}</p>
                </li>
              ))}
            </ol>
          </div>
        ) : (
          <div className="empty">
            <Bell size={28} strokeWidth={1.5} aria-hidden="true" />
            <h2>{list.length ? "No matches yet" : "Nothing monitored yet"}</h2>
            <p className="lede">
              {list.length
                ? "A monitor tells you about changes after it starts. New matches will appear here."
                : "Create a monitor to hear when laws in a country, or a record you follow, change."}
            </p>
            {!list.length ? (
              <button className="btn" type="button" onClick={() => setEditing("new")}>
                <Plus size={16} aria-hidden="true" /> New monitor
              </button>
            ) : null}
          </div>
        )
      ) : list.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Monitor</th>
                <th>Email</th>
                <th>State</th>
                <th>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {list.map((w) => (
                <tr key={w.id} className={w.state === "paused" ? "dim" : ""}>
                  <td>
                    {w.name} {w.visibility === "organisation" ? <Pill>Shared</Pill> : null}
                    <small>{describe(w.filters, countries)}</small>
                  </td>
                  <td>{w.email === "off" ? "Off" : w.email === "daily" ? "Daily" : "Weekly"}</td>
                  <td>{w.state === "active" ? <Pill tone="good">Active</Pill> : <Pill tone="warn">Paused</Pill>}</td>
                  <td className="actions">
                    {w.mine ? (
                      <>
                        <button className="link" type="button" onClick={() => setEditing(w)} aria-label={`Edit ${w.name}`}>
                          <Pencil size={14} aria-hidden="true" /> Edit
                        </button>
                        <button
                          className="link"
                          type="button"
                          onClick={() =>
                            act(() =>
                              api(`/organisations/${org.id}/watches/${w.id}`, {
                                method: "PATCH",
                                body: { state: w.state === "active" ? "paused" : "active", revision: w.revision },
                              }),
                            )
                          }
                        >
                          {w.state === "active" ? <Pause size={14} aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}
                          {w.state === "active" ? " Pause" : " Resume"}
                        </button>
                        <button className="link danger" type="button" onClick={() => setDeleting(w)} aria-label={`Delete ${w.name}`}>
                          <Trash2 size={14} aria-hidden="true" /> Delete
                        </button>
                      </>
                    ) : (
                      <EmailChoice org={org.id} watch={w} limits={limits} onChange={() => watches.reload()} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty">
          <Bell size={28} strokeWidth={1.5} aria-hidden="true" />
          <h2>Nothing monitored yet</h2>
          <button className="btn" type="button" onClick={() => setEditing("new")}>
            <Plus size={16} aria-hidden="true" /> New monitor
          </button>
        </div>
      )}
      {editing ? (
        <WatchDialog
          org={org.id}
          watch={editing === "new" ? null : editing}
          prefill={editing === "new" ? prefill : null}
          limits={limits}
          countries={countries}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            watches.reload();
            setTab("watches");
          }}
        />
      ) : null}
      {deleting ? (
        <Dialog title={`Delete “${deleting.name}”?`} onClose={() => setDeleting(null)}>
          <p>The monitor, its filters and its matches are deleted now. This can't be undone.</p>
          <div className="dialog-actions">
            <button className="btn secondary" type="button" onClick={() => setDeleting(null)}>
              Cancel
            </button>
            <button
              className="btn danger"
              type="button"
              onClick={() => {
                const id = deleting.id;
                setDeleting(null);
                act(() => api(`/organisations/${org.id}/watches/${id}`, { method: "DELETE" }));
              }}
            >
              Delete monitor
            </button>
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

function EmailChoice({ org, watch, limits, onChange }: { org: string; watch: Watch; limits: Limits; onChange: () => void }) {
  return (
    <label className="inline-select">
      <span className="sr-only">Email for {watch.name}</span>
      <select
        value={watch.email}
        onChange={(e) =>
          api(`/organisations/${org}/watches/${watch.id}/email`, { method: "POST", body: { email: e.target.value } }).then(onChange)
        }
      >
        <option value="off">No email</option>
        <option value="weekly">Weekly</option>
        <option value="daily" disabled={limits.digest !== "daily"}>
          Daily
        </option>
      </select>
    </label>
  );
}

function WatchDialog({
  org,
  watch,
  prefill,
  limits,
  countries,
  onClose,
  onSaved,
}: {
  org: string;
  watch: Watch | null;
  prefill: { work?: string; title?: string } | null;
  limits: Limits;
  countries: Country[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const start = watch?.filters;
  const [name, setName] = useState(watch?.name ?? prefill?.title ?? "");
  const [chosen, setChosen] = useState<string[]>(start?.jurisdiction ?? []);
  const [types, setTypes] = useState<string[]>(start?.type ?? []);
  const [works, setWorks] = useState<string[]>(start?.work ?? (prefill?.work ? [prefill.work] : []));
  const [q, setQ] = useState(start?.q ?? "");
  const [email, setEmail] = useState<Watch["email"]>(watch?.email ?? "weekly");
  const [shared, setShared] = useState(watch?.visibility === "organisation");
  const [preview, setPreview] = useState<ChangeEvent[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);
  const filters = { jurisdiction: chosen, work: works, type: types, q: q.trim() || undefined };
  const empty = !chosen.length && !works.length && !types.length && !q.trim();
  function toggle(list: string[], set: (v: string[]) => void, value: string) {
    set(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);
    setPreview(null);
  }
  async function showPreview() {
    setError(null);
    try {
      const answer = await api<{ events: ChangeEvent[] }>(`/organisations/${org}/watches/preview`, {
        method: "POST",
        body: { filters },
      });
      setPreview(answer.events.slice(0, 5));
    } catch (e) {
      setError(e);
    }
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const body = { name, filters, email, visibility: shared ? "organisation" : "private" };
      if (watch) {
        await api(`/organisations/${org}/watches/${watch.id}`, { method: "PATCH", body: { ...body, revision: watch.revision } });
      } else {
        await api(`/organisations/${org}/watches`, { method: "POST", body });
      }
      onSaved();
    } catch (e) {
      setError(e);
    } finally {
      setSaving(false);
    }
  }
  return (
    <Dialog title={watch ? "Edit monitor" : "New monitor"} onClose={onClose}>
      <form className="form" onSubmit={save}>
        <div className="field">
          <label htmlFor="watch-name">Name</label>
          <input id="watch-name" type="text" required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} placeholder="Nigerian legislation" />
        </div>
        {works.length ? (
          <div className="field">
            <span>Record</span>
            <div className="row">
              {works.map((w) => (
                <Pill key={w}>
                  {w === prefill?.work && prefill.title ? prefill.title : w}{" "}
                  <button className="link" type="button" aria-label="Stop following this record" onClick={() => toggle(works, setWorks, w)}>
                    ×
                  </button>
                </Pill>
              ))}
            </div>
          </div>
        ) : null}
        <fieldset className="field plain">
          <legend>Countries</legend>
          {countries.length ? (
            <div className="country-list">
              {countries.map((c) => (
                <label key={c.id} className={"choice" + (chosen.includes(c.id) ? " on" : "")}>
                  <input type="checkbox" checked={chosen.includes(c.id)} onChange={() => toggle(chosen, setChosen, c.id)} />
                  <span>
                    {c.name}
                    <small>{c.works} {c.works === 1 ? "record" : "records"}</small>
                  </span>
                </label>
              ))}
            </div>
          ) : (
            <p className="fine">No countries are published yet.</p>
          )}
          <small>None chosen means every country. A country includes its states and local law.</small>
        </fieldset>
        <fieldset className="field plain">
          <legend>Kinds of change</legend>
          <div className="row">
            {TYPES.map(([id, label]) => (
              <label key={id} className={"choice compact" + (types.includes(id) ? " on" : "")}>
                <input type="checkbox" checked={types.includes(id)} onChange={() => toggle(types, setTypes, id)} />
                <span>{label}</span>
              </label>
            ))}
          </div>
          <small>None chosen means every kind.</small>
        </fieldset>
        <div className="field">
          <label htmlFor="watch-q">Keyword</label>
          <input id="watch-q" type="text" maxLength={200} value={q} onChange={(e) => (setQ(e.target.value), setPreview(null))} placeholder="Words in the title or citation" />
        </div>
        <div className="grid2">
          <div className="field">
            <label htmlFor="watch-email">Email</label>
            <select id="watch-email" value={email} onChange={(e) => setEmail(e.target.value as Watch["email"])}>
              <option value="off">No email, inbox only</option>
              <option value="weekly">Weekly digest</option>
              <option value="daily" disabled={limits.digest !== "daily"}>
                Daily digest{limits.digest !== "daily" ? " (paid plans)" : ""}
              </option>
            </select>
          </div>
          <label className={"choice" + (limits.shared ? "" : " disabled")}>
            <input type="checkbox" checked={shared} disabled={!limits.shared} onChange={(e) => setShared(e.target.checked)} />
            <span>
              Share with my organisation
              <small>{limits.shared ? "Members see it and choose their own email." : "With the Team plan and above."}</small>
            </span>
          </label>
        </div>
        <div className="preview-box">
          <div className="card-head">
            <span className="fine">A monitor tells you about changes after it starts.</span>
            <button className="link" type="button" disabled={empty} onClick={showPreview}>
              Preview recent changes
            </button>
          </div>
          {preview ? (
            preview.length ? (
              <ul className="preview-list">
                {preview.map((e) => (
                  <li key={e.event_id}>
                    <b>{LABEL[e.event_type] ?? e.event_type}</b> · {when(e.committed_at)} · {e.summary}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="fine">No changes like this in the last 90 days.</p>
            )
          ) : null}
        </div>
        <Problem error={error} />
        <div className="dialog-actions">
          <button className="btn secondary" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="btn" type="submit" disabled={saving || empty || !name.trim()}>
            {saving ? "Saving…" : watch ? "Save monitor" : "Create monitor"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// ---------- the person's digest settings (on Security) ----------
type Prefs = { timezone: string; daily_time: string; weekly_day: number; email_on: boolean; bounced: boolean };
const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function timeZones(): string[] {
  const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
  return supported ? supported("timeZone") : ["UTC", "Africa/Kigali", "Africa/Lagos", "Africa/Nairobi", "Africa/Johannesburg"];
}

export function DigestSettings() {
  const prefs = useAsync(() => api<Prefs>("/me/notifications"), []);
  const [form, setForm] = useState<Prefs | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (prefs.data) setForm(prefs.data);
  }, [prefs.data]);
  if (!form) return null;
  async function save(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      setForm(await api<Prefs>("/me/notifications", { method: "PATCH", body: form }));
      setSaved(true);
    } catch (e) {
      setError(e);
    }
  }
  const zones = timeZones();
  return (
    <form className="card form" onSubmit={save} onChange={() => setSaved(false)}>
      <h2>Email digests</h2>
      <p className="lede">When your monitors' digests arrive. Each monitor chooses daily, weekly or no email.</p>
      {form.bounced ? (
        <p className="callout">Emails to your address bounced, so digests are off. Turn them on again to retry.</p>
      ) : null}
      <label className="choice">
        <input type="checkbox" checked={form.email_on} onChange={(e) => setForm({ ...form, email_on: e.target.checked })} />
        <span>
          Send digest emails
          <small>Matches always stay in your inbox in the console.</small>
        </span>
      </label>
      <div className="grid3">
        <div className="field">
          <label htmlFor="digest-zone">Time zone</label>
          <select id="digest-zone" value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })}>
            {(zones.includes(form.timezone) ? zones : [form.timezone, ...zones]).map((z) => (
              <option key={z}>{z}</option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="digest-time">Time</label>
          <input id="digest-time" type="time" value={form.daily_time} onChange={(e) => setForm({ ...form, daily_time: e.target.value })} />
        </div>
        <div className="field">
          <label htmlFor="digest-day">Weekly on</label>
          <select id="digest-day" value={form.weekly_day} onChange={(e) => setForm({ ...form, weekly_day: Number(e.target.value) })}>
            {DAYS.map((d, i) => (
              <option key={d} value={i + 1}>
                {d}
              </option>
            ))}
          </select>
        </div>
      </div>
      <Problem error={error} />
      <div className="row">
        <button className="btn secondary" type="submit">
          Save
        </button>
        {saved ? <span className="fine">Saved.</span> : null}
      </div>
    </form>
  );
}

// ---------- the link in each digest (no sign-in needed) ----------
export function Unsubscribe({ token }: { token: string }) {
  const [state, setState] = useState<{ scope?: "all" | "watch"; watch_name?: string | null; done?: boolean; error?: string }>({});
  useEffect(() => {
    api<{ scope: "all" | "watch"; watch_name: string | null }>(`/unsubscribe/${encodeURIComponent(token)}`).then(
      (data) => setState(data),
      (e) => setState({ error: e instanceof ApiError ? e.message : "We couldn't verify this link." }),
    );
  }, [token]);
  async function confirm() {
    const response = await fetch(`/console/unsubscribe/${encodeURIComponent(token)}`, { method: "POST" });
    setState(response.ok ? { ...state, done: true } : { error: "We couldn't verify this link. Sign in to change your email settings." });
  }
  const what = state.scope === "watch" ? `emails for the monitor “${state.watch_name}”` : "all digest emails";
  return (
    <div className="solo">
      <div className="solo-card">
        <Mark size={36} />
        <h1>{state.done ? "You're unsubscribed" : "Stop these emails?"}</h1>
        {state.error ? (
          <>
            <p className="lede">{state.error}</p>
            <a className="btn" href="/console/security">
              Sign in to email settings
            </a>
          </>
        ) : state.done ? (
          <p className="lede">We won't send {what} any more. Your monitors and inbox are kept; change this any time in the console.</p>
        ) : state.scope ? (
          <>
            <p className="lede">This stops {what}. Your monitors and their inbox stay as they are.</p>
            <button className="btn wide" type="button" onClick={confirm}>
              Unsubscribe
            </button>
          </>
        ) : (
          <Loading />
        )}
      </div>
    </div>
  );
}
