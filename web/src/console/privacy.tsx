// Your data (MAC-14): download it, or delete the account; owners download an organisation's records.
import React, { useState } from "react";
import { Download, Trash2 } from "lucide-react";
import { api, download, type Me, type Organisation } from "./api";
import { Dialog, Problem } from "./ui";

type Plan = {
  blocking: { id: string; name: string; members: number }[];
  deleted: { id: string; name: string }[];
  left: { id: string; name: string; role: string }[];
};

export function YourData({ me }: { me: Me }) {
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(null);
  async function exportData() {
    setError(null);
    setBusy(true);
    try {
      await download("/me/export");
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function startDeletion() {
    setError(null);
    try {
      setPlan(await api<Plan>("/me/deletion"));
    } catch (e) {
      setError(e);
    }
  }
  return (
    <div className="card">
      <h2>Your data</h2>
      <p className="lede">
        Download what Open Alvary holds about you, or delete your account. See the{" "}
        <a href="/privacy" target="_blank" rel="noopener noreferrer">
          privacy notice
        </a>{" "}
        for how long things are kept.
      </p>
      <Problem error={error} onDismiss={() => setError(null)} />
      <div className="row">
        <button className="btn secondary" type="button" onClick={exportData} disabled={busy}>
          <Download size={16} aria-hidden="true" /> {busy ? "Preparing…" : "Download my data"}
        </button>
        <button className="btn danger" type="button" onClick={startDeletion}>
          <Trash2 size={16} aria-hidden="true" /> Delete my account
        </button>
      </div>
      {plan ? <DeleteAccount me={me} plan={plan} onClose={() => setPlan(null)} /> : null}
    </div>
  );
}

function DeleteAccount({ me, plan, onClose }: { me: Me; plan: Plan; onClose: () => void }) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [deleting, setDeleting] = useState(false);
  async function remove(event: React.FormEvent) {
    event.preventDefault();
    setDeleting(true);
    setError(null);
    try {
      await api("/me/delete", { method: "POST", body: { confirm: typed } });
      location.assign("/console/sign-in?deleted=1");
    } catch (e) {
      setError(e);
      setDeleting(false);
    }
  }
  if (plan.blocking.length) {
    return (
      <Dialog title="Your account can't be deleted yet" onClose={onClose}>
        <p>
          You're the only owner of {plan.blocking.map((o) => o.name).join(", ")}, which{" "}
          {plan.blocking.length === 1 ? "has" : "have"} other members. Make someone else an owner (Members), or delete{" "}
          {plan.blocking.length === 1 ? "it" : "them"} (Settings), then come back.
        </p>
        <div className="dialog-actions">
          <button className="btn" type="button" onClick={onClose}>
            OK
          </button>
        </div>
      </Dialog>
    );
  }
  return (
    <Dialog title="Delete your account?" onClose={onClose}>
      <form className="form" onSubmit={remove}>
        <ul className="plain-list">
          <li>Your keys and connected apps stop working now, and you're signed out everywhere.</li>
          {plan.deleted.length ? (
            <li>
              Deleted with it, as you're the only member: <b>{plan.deleted.map((o) => o.name).join(", ")}</b>.
            </li>
          ) : null}
          {plan.left.length ? <li>You leave: {plan.left.map((o) => o.name).join(", ")}.</li> : null}
          <li>Your monitors and email settings are deleted now; your profile and sign-in details within 30 days.</li>
          <li>Audit entries in organisations stay, without your name.</li>
        </ul>
        <div className="field">
          <label htmlFor="confirm-email">Type your email address to confirm</label>
          <input
            id="confirm-email"
            type="email"
            autoComplete="off"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={me.person.email}
          />
        </div>
        <Problem error={error} />
        <div className="dialog-actions">
          <button className="btn secondary" type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn danger"
            type="submit"
            disabled={deleting || typed.trim().toLowerCase() !== me.person.email.toLowerCase()}
          >
            {deleting ? "Deleting…" : "Delete my account"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

export function OrganisationData({ org }: { org: Organisation }) {
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  if (org.role !== "owner") return null;
  return (
    <div className="card">
      <h2>Organisation records</h2>
      <p className="lede">
        Download {org.name}'s records as owners see them: members, access, keys, connected apps, usage, the audit log,
        shared monitors and webhook endpoints. Members' private settings are not included.
      </p>
      <Problem error={error} onDismiss={() => setError(null)} />
      <div className="row">
        <button
          className="btn secondary"
          type="button"
          disabled={busy}
          onClick={async () => {
            setError(null);
            setBusy(true);
            try {
              await download(`/organisations/${org.id}/export`);
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
        >
          <Download size={16} aria-hidden="true" /> {busy ? "Preparing…" : "Download organisation records"}
        </button>
      </div>
    </div>
  );
}
