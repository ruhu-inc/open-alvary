// Webhooks (SUB C3): monitor matches sent to the developer's own HTTPS endpoint, signed. The secret
// is shown once at creation and at each rotation; deliveries can be inspected and replayed.
import React, { useState } from "react";
import { Plus, RefreshCw, RotateCcw, Send, ShieldCheck, Trash2, Webhook } from "lucide-react";
import { ApiError, api, go } from "./api";
import { CopyField, Dialog, Loading, Pill, Problem, ago, useAsync, when } from "./ui";
import { Heading, type PageProps } from "./pages";

type Endpoint = {
  id: string;
  revision: number;
  url: string;
  description: string | null;
  state: "pending_verification" | "active" | "paused" | "failing" | "disabled";
  watch_ids: string[];
  signing_keys: { kid: string; created_at: string; retires_at: string | null }[];
  failure: { consecutive_failures: number; first_failure_at: string | null; last_error: string | null };
  last_delivery: { state: string; last_status: number | null; created_at: string } | null;
  created_at: string;
  verified_at: string | null;
};
type Listing = { endpoints: Endpoint[]; limit: number; used: number };
type Monitor = { id: string; name: string };
type Delivery = {
  id: string;
  event_id: string;
  event_type: string | null;
  state: string;
  attempts: number;
  last_status: number | null;
  last_error: string | null;
  replay_of: string | null;
  created_at: string;
  next_attempt_at: string;
};
type Verification = { verified: boolean; status: number | null; error: string | null };

const STATE: Record<Endpoint["state"], [string, "good" | "warn" | "bad" | undefined]> = {
  active: ["Active", "good"],
  pending_verification: ["Not verified", "warn"],
  failing: ["Failing", "warn"],
  paused: ["Paused", "bad"],
  disabled: ["Disabled", "bad"],
};

const SAMPLE = `# Python: verify each delivery, and answer the one-time challenge.
import hashlib, hmac, json, time

def verified(secret: str, body: bytes, headers) -> bool:
    stamp = headers["Open-Alvary-Timestamp"]
    if abs(time.time() - int(stamp)) > 300:
        return False
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s.strip().removeprefix("v1="))
               for s in headers["Open-Alvary-Signature"].split(","))

def handle(secret, body, headers):
    if not verified(secret, body, headers):
        return 401, ""
    message = json.loads(body)
    if message["type"] == "endpoint.verification":
        return 200, json.dumps({"challenge": message["challenge"]})
    # message["type"] == "change": message["event"] is the change; deduplicate by
    # (event["feed_epoch"], event["event_id"], event["event_revision"]).
    return 200, ""`;

export function WebhooksPage({ org }: PageProps) {
  const listing = useAsync(() => api<Listing>(`/organisations/${org.id}/webhooks`), [org.id]);
  const monitors = useAsync(() => api<{ watches: Monitor[] }>(`/organisations/${org.id}/watches`), [org.id]);
  const [adding, setAdding] = useState(false);
  const [secret, setSecret] = useState<{ title: string; value: string; verification?: Verification } | null>(null);
  const [viewing, setViewing] = useState<Endpoint | null>(null);
  const [rotating, setRotating] = useState<Endpoint | null>(null);
  const [deleting, setDeleting] = useState<Endpoint | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  if (listing.loading && !listing.data) return <Loading />;
  if (listing.error) return <Problem error={listing.error} />;
  const { endpoints, limit, used } = listing.data!;
  const names = Object.fromEntries((monitors.data?.watches ?? []).map((m) => [m.id, m.name]));
  async function act(work: () => Promise<unknown>, done?: string) {
    setError(null);
    setNotice(null);
    try {
      await work();
      if (done) setNotice(done);
      listing.reload();
    } catch (e) {
      setError(e);
    }
  }
  return (
    <>
      <Heading
        title="Webhooks"
        description="Send your monitors' matches to your own HTTPS endpoint, signed so you can check each one came from Open Alvary."
        action={
          <button className="btn" type="button" disabled={used >= limit} onClick={() => setAdding(true)}>
            <Plus size={16} aria-hidden="true" /> Add endpoint
          </button>
        }
      />
      {limit === 0 ? (
        <div className="callout">
          <p>Webhooks come with paid plans. Monitors and their inbox and email work on every plan.</p>
          <button className="btn secondary" type="button" onClick={() => go("/console/access")}>
            See plans
          </button>
        </div>
      ) : (
        <p className="fine">
          {used} of {limit} {limit === 1 ? "endpoint" : "endpoints"} on your plan. Each receives the matches of the
          monitors you choose.
        </p>
      )}
      <Problem error={error} onDismiss={() => setError(null)} />
      {notice ? (
        <p className="callout" role="status">
          {notice}
        </p>
      ) : null}
      {endpoints.length ? (
        endpoints.map((e) => {
          const [label, tone] = STATE[e.state];
          return (
            <div className="card" key={e.id}>
              <div className="card-head">
                <div>
                  <h2 className="endpoint-url">
                    <code>{e.url}</code> <Pill tone={tone}>{label}</Pill>
                  </h2>
                  <p className="fine">
                    {e.description ? e.description + " · " : ""}
                    {e.watch_ids.map((id) => names[id] ?? "a monitor").join(", ")}
                    {e.last_delivery ? ` · last delivery ${ago(e.last_delivery.created_at)} (${e.last_delivery.state})` : ""}
                  </p>
                  {e.failure.last_error && e.state !== "active" ? <p className="fine bad-text">{e.failure.last_error}</p> : null}
                </div>
              </div>
              <div className="row">
                {e.state !== "active" ? (
                  <button
                    className="btn secondary"
                    type="button"
                    onClick={() =>
                      act(async () => {
                        const answer = await api<{ result: Verification }>(`/organisations/${org.id}/webhooks/${e.id}/verify`, {
                          method: "POST",
                        });
                        if (!answer.result.verified) throw new ApiError(0, "not_verified", answer.result.error ?? "Verification failed.");
                      }, "Verified. Deliveries resume.")
                    }
                  >
                    <ShieldCheck size={16} aria-hidden="true" /> Verify
                  </button>
                ) : null}
                <button
                  className="btn secondary"
                  type="button"
                  onClick={() =>
                    act(async () => {
                      const answer = await api<{ delivered: boolean; error: string | null }>(
                        `/organisations/${org.id}/webhooks/${e.id}/test`,
                        { method: "POST" },
                      );
                      if (!answer.delivered) throw new ApiError(0, "not_delivered", answer.error ?? "The test was not accepted.");
                    }, "Test delivered.")
                  }
                >
                  <Send size={16} aria-hidden="true" /> Send test
                </button>
                <button className="btn secondary" type="button" onClick={() => setViewing(e)}>
                  Deliveries
                </button>
                <button className="btn secondary" type="button" onClick={() => setRotating(e)}>
                  <RefreshCw size={16} aria-hidden="true" /> Rotate secret
                </button>
                <button className="link danger" type="button" onClick={() => setDeleting(e)}>
                  <Trash2 size={14} aria-hidden="true" /> Delete
                </button>
              </div>
            </div>
          );
        })
      ) : limit ? (
        <div className="empty">
          <Webhook size={28} strokeWidth={1.5} aria-hidden="true" />
          <h2>No endpoints yet</h2>
          <p className="lede">Add an HTTPS endpoint and choose which monitors it receives.</p>
        </div>
      ) : null}
      <details className="card">
        <summary>
          <b>How to receive deliveries</b>
        </summary>
        <p className="fine">
          Each delivery is a POST with a JSON body and the headers <code>Open-Alvary-Delivery</code>,{" "}
          <code>Open-Alvary-Timestamp</code>, <code>Open-Alvary-Key</code> and <code>Open-Alvary-Signature</code>. Answer
          2xx within 10 seconds. We retry after 1 minute, 5 minutes, 30 minutes, 2 hours and 8 hours, and stop after 24
          hours; answering 410 disables the endpoint. Redirects are not followed.
        </p>
        <pre className="code">{SAMPLE}</pre>
      </details>
      {adding ? (
        <AddEndpoint
          org={org.id}
          monitors={monitors.data?.watches ?? []}
          onClose={() => setAdding(false)}
          onCreated={(value, verification) => {
            setAdding(false);
            setSecret({ title: "Your endpoint's signing secret", value, verification });
            listing.reload();
          }}
        />
      ) : null}
      {secret ? (
        <Dialog title={secret.title} onClose={() => setSecret(null)}>
          <p>Copy it now: it won't be shown again. Use it to check each delivery's signature.</p>
          <CopyField value={secret.value} label="Signing secret" />
          {secret.verification ? (
            secret.verification.verified ? (
              <p className="fine">The endpoint answered the challenge and is active.</p>
            ) : (
              <p className="callout">
                {/* One child: the callout's grid would put each code element on a row of its own. */}
                <span>
                  Not verified yet: {secret.verification.error} Your endpoint must answer the first POST (type{" "}
                  <code>endpoint.verification</code>) with 2xx and the body <code>{'{"challenge": "<the challenge>"}'}</code>.
                  Then choose Verify.
                </span>
              </p>
            )
          ) : null}
          <div className="dialog-actions">
            <button className="btn" type="button" onClick={() => setSecret(null)}>
              I've saved it
            </button>
          </div>
        </Dialog>
      ) : null}
      {viewing ? <Deliveries org={org.id} endpoint={viewing} onClose={() => setViewing(null)} /> : null}
      {rotating ? (
        <Dialog title="Rotate the signing secret?" onClose={() => setRotating(null)}>
          <p>
            A new secret is made now. The current one keeps signing beside it for 24 hours, so you can switch without
            missing deliveries.
          </p>
          <div className="dialog-actions">
            <button className="btn secondary" type="button" onClick={() => setRotating(null)}>
              Cancel
            </button>
            <button
              className="btn"
              type="button"
              onClick={() => {
                const id = rotating.id;
                setRotating(null);
                act(async () => {
                  const answer = await api<{ secret: string }>(`/organisations/${org.id}/webhooks/${id}/rotate`, {
                    method: "POST",
                  });
                  setSecret({ title: "Your new signing secret", value: answer.secret });
                });
              }}
            >
              Rotate secret
            </button>
          </div>
        </Dialog>
      ) : null}
      {deleting ? (
        <Dialog title="Delete this endpoint?" onClose={() => setDeleting(null)}>
          <p>
            Deliveries to <code>{deleting.url}</code> stop now and its secrets and delivery records are deleted. Your monitors
            are kept.
          </p>
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
                act(() => api(`/organisations/${org.id}/webhooks/${id}`, { method: "DELETE" }));
              }}
            >
              Delete endpoint
            </button>
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

function AddEndpoint({
  org,
  monitors,
  onClose,
  onCreated,
}: {
  org: string;
  monitors: Monitor[];
  onClose: () => void;
  onCreated: (secret: string, verification: Verification) => void;
}) {
  const [url, setUrl] = useState("https://");
  const [description, setDescription] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);
  async function save(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const answer = await api<{ secret: string; verification: Verification }>(`/organisations/${org}/webhooks`, {
        method: "POST",
        body: { url, description, watch_ids: chosen },
      });
      onCreated(answer.secret, answer.verification);
    } catch (e) {
      setError(e);
    } finally {
      setSaving(false);
    }
  }
  return (
    <Dialog title="Add a webhook endpoint" onClose={onClose}>
      <form className="form" onSubmit={save}>
        <div className="field">
          <label htmlFor="hook-url">Endpoint URL</label>
          <input id="hook-url" type="url" required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.org/open-alvary" />
          <small>HTTPS on port 443, reachable from the internet. We send a challenge to it straight away.</small>
        </div>
        <div className="field">
          <label htmlFor="hook-description">Description (optional)</label>
          <input id="hook-description" type="text" maxLength={200} value={description} onChange={(e) => setDescription(e.target.value)} />
        </div>
        <fieldset className="field plain">
          <legend>Monitors it receives</legend>
          {monitors.length ? (
            <div className="country-list">
              {monitors.map((m) => (
                <label key={m.id} className={"choice" + (chosen.includes(m.id) ? " on" : "")}>
                  <input
                    type="checkbox"
                    checked={chosen.includes(m.id)}
                    onChange={() => setChosen(chosen.includes(m.id) ? chosen.filter((c) => c !== m.id) : [...chosen, m.id])}
                  />
                  <span>{m.name}</span>
                </label>
              ))}
            </div>
          ) : (
            <p className="fine">
              Create a monitor first.{" "}
              <button className="link" type="button" onClick={() => go("/console/monitor")}>
                Go to Monitor
              </button>
            </p>
          )}
        </fieldset>
        <Problem error={error} />
        <div className="dialog-actions">
          <button className="btn secondary" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="btn" type="submit" disabled={saving || !chosen.length}>
            {saving ? "Adding and verifying…" : "Add endpoint"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function Deliveries({ org, endpoint, onClose }: { org: string; endpoint: Endpoint; onClose: () => void }) {
  const list = useAsync(() => api<{ deliveries: Delivery[] }>(`/organisations/${org}/webhooks/${endpoint.id}/deliveries`), [endpoint.id]);
  const [error, setError] = useState<unknown>(null);
  return (
    <Dialog title="Recent deliveries" onClose={onClose}>
      <p className="fine">
        <code>{endpoint.url}</code> · kept 7 days
      </p>
      <Problem error={error} onDismiss={() => setError(null)} />
      {list.data ? (
        list.data.deliveries.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Change</th>
                  <th>Result</th>
                  <th>
                    <span className="sr-only">Replay</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.data.deliveries.map((d) => (
                  <tr key={d.id}>
                    <td>
                      {when(d.created_at, true)}
                      <small className="mono">{d.id}</small>
                    </td>
                    <td>{d.event_type ?? "change"}{d.replay_of ? <small>Replay</small> : null}</td>
                    <td>
                      {d.state}
                      {d.attempts ? ` · ${d.attempts} ${d.attempts === 1 ? "attempt" : "attempts"}` : ""}
                      {d.last_error ? <small>{d.last_error}</small> : d.last_status ? <small>HTTP {d.last_status}</small> : null}
                    </td>
                    <td className="actions">
                      <button
                        className="link"
                        type="button"
                        onClick={() =>
                          api(`/organisations/${org}/webhooks/${endpoint.id}/deliveries/${d.id}/replay`, { method: "POST" }).then(
                            () => list.reload(),
                            setError,
                          )
                        }
                      >
                        <RotateCcw size={14} aria-hidden="true" /> Replay
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="fine">No deliveries yet.</p>
        )
      ) : (
        <Loading />
      )}
      <div className="dialog-actions">
        <button className="btn secondary" type="button" onClick={onClose}>
          Close
        </button>
      </div>
    </Dialog>
  );
}
