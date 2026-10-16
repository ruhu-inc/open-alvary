"use strict";

// The Access section (MAC-11): access requests, organisations, suspensions, leaked credentials and
// the accounts audit log, for operators with the access_admin role. Every change needs a passkey
// check within 10 minutes; a refused change asks for the passkey and is sent again once.
// Uses app.js's helpers (el, api, main, toast, setCrumbs, message, pill). No inline handlers (CSP).

const b64 = {
  enc: (buffer) => btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""),
  dec: (text) => Uint8Array.from(atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4)), (c) => c.charCodeAt(0)),
};
const day = (value) => (value ? String(value).slice(0, 10) : "");
const moment = (value) => (value ? String(value).slice(0, 16).replace("T", " ") : "");

async function passkeyCheck() {
  const options = await api("/api/access/passkeys/options", { method: "POST", body: { purpose: "verify" } });
  if (!options.ok) throw new Error(message(options));
  const o = options.data;
  const credential = await navigator.credentials.get({
    publicKey: {
      challenge: b64.dec(o.challenge),
      rpId: o.rpId,
      allowCredentials: o.allowCredentials.map((c) => ({ type: c.type, id: b64.dec(c.id) })),
      userVerification: o.userVerification,
      timeout: o.timeout,
    },
  });
  const r = credential.response;
  const verified = await api("/api/access/passkeys/verify", {
    method: "POST",
    body: { credential: { id: credential.id, clientDataJSON: b64.enc(r.clientDataJSON), authenticatorData: b64.enc(r.authenticatorData), signature: b64.enc(r.signature) } },
  });
  if (!verified.ok) throw new Error(message(verified));
}

async function passkeyRegister(name, code) {
  const options = await api("/api/access/passkeys/options", { method: "POST", body: { purpose: "register" } });
  if (!options.ok) throw new Error(message(options));
  const o = options.data;
  const credential = await navigator.credentials.create({
    publicKey: {
      challenge: b64.dec(o.challenge),
      rp: o.rp,
      user: { ...o.user, id: b64.dec(o.user.id) },
      pubKeyCredParams: o.pubKeyCredParams,
      authenticatorSelection: o.authenticatorSelection,
      attestation: o.attestation,
      excludeCredentials: o.excludeCredentials.map((c) => ({ type: c.type, id: b64.dec(c.id) })),
      timeout: o.timeout,
    },
  });
  const r = credential.response;
  const key = r.getPublicKey ? r.getPublicKey() : null;
  if (!key) throw new Error("This browser cannot register a passkey here. Use a current Chrome, Edge, Safari or Firefox.");
  const saved = await api("/api/access/passkeys", {
    method: "POST",
    body: {
      name,
      code,
      credential: {
        id: credential.id,
        rawId: b64.enc(credential.rawId),
        clientDataJSON: b64.enc(r.clientDataJSON),
        authenticatorData: b64.enc(r.getAuthenticatorData()),
        publicKey: b64.enc(key),
        publicKeyAlgorithm: r.getPublicKeyAlgorithm(),
      },
    },
  });
  if (!saved.ok) throw new Error(message(saved));
}

// A change: sent once, and again after a passkey check if the last one is older than 10 minutes.
async function change(path, body, done) {
  let result = await api(path, { method: "POST", body });
  if (result.status === 403 && result.data && result.data.code === "passkey_required") {
    try {
      await passkeyCheck();
    } catch (error) {
      toast(error.name === "NotAllowedError" ? "Passkey check cancelled." : error.message);
      return null;
    }
    result = await api(path, { method: "POST", body });
  }
  if (!result.ok) {
    toast(message(result));
    return null;
  }
  toast("Done.");
  if (done) await done(result.data);
  return result.data;
}

// A small form whose fields are read when it is sent: reason (required) and evidence.
function form(title, fields, submit, onSend, { evidence = true, danger = false } = {}) {
  const inputs = {};
  const rows = fields.map(([key, label, input]) => {
    inputs[key] = input;
    return el("label", { class: "access-field" }, el("span", { text: label }), input);
  });
  inputs.reason = el("input", { type: "text", maxlength: "500", required: true, placeholder: "Why (recorded in the audit log)" });
  rows.push(el("label", { class: "access-field" }, el("span", { text: "Reason" }), inputs.reason));
  if (evidence) {
    inputs.evidence = el("input", { type: "text", maxlength: "500", placeholder: "Ticket, email date or document checked" });
    rows.push(el("label", { class: "access-field" }, el("span", { text: "Evidence" }), inputs.evidence));
  }
  const button = el("button", { class: danger ? "danger" : "primary", type: "submit", text: submit });
  return el("form", {
    class: "access-form",
    on: {
      submit: async (event) => {
        event.preventDefault();
        const values = {};
        for (const [key, input] of Object.entries(inputs)) values[key] = input.type === "checkbox" ? input.checked : input.value.trim();
        button.disabled = true;
        try { await onSend(values); } finally { button.disabled = false; }
      },
    },
  }, title ? el("h4", { text: title }) : null, ...rows, button);
}

// --- the passkey bar ------------------------------------------------------------------------------

async function passkeyBar() {
  const { data } = await api("/api/access/me");
  const box = el("div", { class: "card access-passkey" });
  if (data.needs_enrolment) {
    const code = el("input", { type: "text", placeholder: "XXXX-XXXX-XXXX-XXXX", autocomplete: "off" });
    const name = el("input", { type: "text", value: "Passkey", maxlength: "100" });
    box.append(
      el("h3", { text: "Register a passkey" }),
      el("p", { class: "muted", text: "Changes here need a passkey on this device or a security key. Enter the one-time code you were given (workbench.admin enrol)." }),
      el("form", { class: "access-form", on: { submit: async (event) => {
        event.preventDefault();
        try { await passkeyRegister(name.value.trim() || "Passkey", code.value); toast("Passkey registered."); route(); }
        catch (error) { toast(error.name === "NotAllowedError" ? "Cancelled." : error.message); }
      } } },
      el("label", { class: "access-field" }, el("span", { text: "Code" }), code),
      el("label", { class: "access-field" }, el("span", { text: "Name" }), name),
      el("button", { class: "primary", type: "submit", text: "Register passkey" })),
    );
    return box;
  }
  const status = data.fresh_until
    ? el("span", { text: `Passkey checked: changes allowed until ${moment(data.fresh_until).slice(11)} UTC.` })
    : el("span", { text: "Changes ask for your passkey (valid 10 minutes)." });
  const check = el("button", { class: "small", text: "Check passkey now", on: { click: async () => {
    try { await passkeyCheck(); toast("Passkey checked."); route(); } catch (error) { toast(error.message); }
  } } });
  const keys = el("details", {}, el("summary", { text: `${data.passkeys.length} passkey${data.passkeys.length === 1 ? "" : "s"}` }),
    el("ul", { class: "plain" }, data.passkeys.map((k) => el("li", {}, `${k.name} · added ${day(k.created_at)}${k.last_used_at ? ` · used ${day(k.last_used_at)}` : ""} `,
      el("button", { class: "small", text: "Remove", on: { click: async () => {
        if (!confirm(`Remove the passkey "${k.name}"?`)) return;
        let result = await api(`/api/access/passkeys/${encodeURIComponent(k.id)}`, { method: "DELETE" });
        if (result.status === 403) { await passkeyCheck(); result = await api(`/api/access/passkeys/${encodeURIComponent(k.id)}`, { method: "DELETE" }); }
        toast(result.ok ? "Removed." : message(result)); route();
      } } })))),
    el("button", { class: "small", text: "Add a passkey", on: { click: async () => {
      try { await passkeyRegister(prompt("Name for this passkey", "Security key") || "Passkey", ""); toast("Passkey added."); route(); }
      catch (error) { toast(error.message); }
    } } }));
  box.append(el("div", { class: "access-status" }, status, " ", check), keys);
  return box;
}

// --- views ----------------------------------------------------------------------------------------

async function accessView(parts) {
  parts = parts.map((part) => part.split("?")[0]);
  setCrumbs(parts[0] ? el("a", { href: "#/access", text: "Access" }) : "Access", ...(parts[0] === "audit" ? ["Audit log"] : []));
  if (parts[0] === "org" && parts[1]) return organisationView(parts[1]);
  if (parts[0] === "audit") return accessAuditView();
  const [bar, requests, clients] = await Promise.all([passkeyBar(), api("/api/access/requests"), api("/api/access/clients")]);
  const search = el("input", { type: "search", placeholder: "Organisation name or ID, or a member's email" });
  const results = el("ul", { class: "plain" });
  search.addEventListener("input", async () => {
    if (search.value.trim().length < 2) return results.replaceChildren();
    const { data } = await api(`/api/access/search?q=${encodeURIComponent(search.value.trim())}`);
    results.replaceChildren(...(data.length ? data.map((o) => el("li", {}, el("a", { href: `#/access/org/${o.id}`, text: o.name }), ` · ${o.state} · ${o.id}`)) : [el("li", { class: "muted", text: "Nothing found." })]));
  });
  const credential = el("input", { type: "text", autocomplete: "off", placeholder: "The leaked key, its ID, or a grt_… app ID" });
  main(
    bar,
    el("div", { class: "card" }, el("h3", { text: `Open requests (${requests.data.length})` }),
      requests.data.length
        ? el("table", { class: "list" }, el("thead", {}, el("tr", {}, ["Organisation", "Plan asked", "Countries", "Purpose", "Contact", "Sent"].map((h) => el("th", { text: h })))),
          el("tbody", {}, requests.data.map((r) => el("tr", {},
            el("td", {}, el("a", { href: `#/access/org/${r.organisation_id}`, text: r.organisation })),
            el("td", { text: r.tier_requested || "—" }),
            el("td", { text: r.scope_requested.all ? "all" : r.scope_requested.jurisdictions.join(", ") }),
            el("td", { class: "access-purpose", text: r.purpose }),
            el("td", { text: `${r.contact_name} <${r.contact_email}>` }),
            el("td", { text: day(r.created_at) + (r.state === "needs_information" ? " · asked for more" : "") })))))
        : el("p", { class: "muted", text: "No open requests." })),
    el("div", { class: "card" }, el("h3", { text: "Find an organisation" }), search, results),
    el("div", { class: "card" }, el("h3", { text: "Revoke a leaked key or connected app" }),
      el("p", { class: "muted", text: "A pasted key is read only for its ID; it is never stored or logged." }),
      form("", [["credential", "Key or ID", credential]], "Revoke", (v) => change("/api/access/credentials/revoke", v, async () => { credential.value = ""; }), { danger: true })),
    el("div", { class: "card" }, el("h3", { text: "Verified publishers" }),
      el("p", { class: "muted", text: "Apps connecting with exactly this client metadata address are shown to people as from their publisher. Review the document first; apps already connected keep their label." }),
      table(["Name", "Publisher", "Client metadata", "Pinned", ""], clients.data.map((c) => el("tr", {},
        el("td", { text: c.name }), el("td", { text: c.publisher }),
        el("td", {}, el("a", { href: c.client_id, target: "_blank", rel: "noreferrer", text: c.client_id })),
        el("td", { text: c.unpinned_at ? `unpinned ${day(c.unpinned_at)} by ${c.unpinned_by}` : `${day(c.pinned_at)} by ${c.pinned_by}` }),
        el("td", {}, c.unpinned_at ? null : el("details", {}, el("summary", { text: "Unpin" }),
          form("Unpin", [], "Unpin", (v) => change("/api/access/clients/unpin", { ...v, client_id: c.client_id }, () => route()), { danger: true })))))),
      el("details", {}, el("summary", { text: "Pin a tool" }),
        form("", [
          ["client_id", "Client metadata (https)", el("input", { type: "text", placeholder: "https://…/client.json" })],
          ["name", "Name", el("input", { type: "text", maxlength: "100" })],
          ["publisher", "Publisher", el("input", { type: "text", maxlength: "100" })],
        ], "Pin", (v) => change("/api/access/clients", v, () => route())))),
    el("p", {}, el("a", { href: "#/access/audit", text: "The whole audit log →" })),
  );
}

function table(headers, rows) {
  if (!rows.length) return el("p", { class: "muted", text: "None." });
  return el("table", { class: "list" }, el("thead", {}, el("tr", {}, headers.map((h) => el("th", { text: h })))), el("tbody", {}, rows));
}

async function organisationView(id) {
  const [bar, view, tiers] = await Promise.all([passkeyBar(), api(`/api/access/organisations/${encodeURIComponent(id)}`), api("/api/access/tiers")]);
  if (!view.ok) return main(el("p", { text: message(view) }));
  const o = view.data;
  const again = () => route();
  const base = `/api/access/organisations/${encodeURIComponent(id)}`;
  const access = o.access || {};
  const open = o.requests.find((r) => r.state === "submitted" || r.state === "needs_information");
  const owned = o.members.some((m) => m.role === "owner" && m.state === "active");
  setCrumbs(el("a", { href: "#/access", text: "Access" }), o.name);

  const tier = el("select", {}, tiers.data.map((t) => el("option", { value: t.id, text: `${t.name}${t.countries ? ` (up to ${t.countries} countries)` : ""}` })));
  if (open && open.tier_requested) tier.value = open.tier_requested;
  const countries = el("input", { type: "text", placeholder: "ng, rw (empty: as asked, or all)", value: open && open.scope_requested.jurisdictions ? open.scope_requested.jurisdictions.join(", ") : "" });
  const until = el("input", { type: "date" });
  const waiver = el("input", { type: "checkbox" });
  waiver.checked = Boolean(open && open.reduced);

  main(...[
    bar,
    el("div", { class: "card" },
      el("h3", { text: o.name }),
      el("p", {}, pill(o.state === "active" ? "ok" : "warn", o.state), ` ${o.kind} · ${o.country || "no country"} · created ${day(o.created_at)} · ${o.id}`),
      el("p", { text: access.state === "active" ? `Access: ${access.tier ? access.tier.name : "?"}${access.scope && access.scope.jurisdictions ? ` for ${access.scope.jurisdictions.join(", ")}` : ""}${access.ends_at ? ` until ${day(access.ends_at)}` : ""}.` : `No active access (${access.state || "none"}).` })),
    open ? el("div", { class: "card" }, el("h3", { text: "Open request" }),
      el("p", { text: `${open.contact_name} <${open.contact_email}> · ${day(open.created_at)} · asks for ${open.tier_requested || "a plan"}${open.reduced ? " (free or reduced)" : ""}` }),
      el("p", { class: "access-purpose", text: open.purpose }), el("p", { class: "muted", text: `Volume: ${open.expected_volume}` }),
      el("div", { class: "access-row" },
        form("Ask for more information", [], "Ask", (v) => change(`${base}/requests/${open.id}`, { ...v, decision: "needs_information" }, again), { evidence: false }),
        form("Decline", [], "Decline", (v) => change(`${base}/requests/${open.id}`, { ...v, decision: "declined" }, again), { evidence: false, danger: true }))) : null,
    el("div", { class: "card" }, el("div", { class: "access-row" },
      form(open ? "Grant the request" : "Grant or change access", [["tier", "Plan", tier], ["countries", "Countries", countries], ["until", "Until", until], ["waiver", "Free or reduced", waiver]], "Grant",
        (v) => change(`${base}/grant`, { tier: v.tier, jurisdictions: v.countries ? v.countries.split(",").map((c) => c.trim()).filter(Boolean) : null, until: v.until || null, waiver: v.waiver, request_id: open ? open.id : null, reason: v.reason, evidence: v.evidence }, again)),
      access.state === "active" ? form("Revoke access", [], "Revoke", (v) => change(`${base}/revoke`, v, again), { danger: true }) : null,
      form(o.state === "suspended" ? "Restore the organisation" : "Suspend the organisation", [], o.state === "suspended" ? "Restore" : "Suspend",
        (v) => change(`${base}/state`, { ...v, change: o.state === "suspended" ? "restore" : "suspend" }, again), { danger: o.state !== "suspended" }))),
    el("div", { class: "card" }, el("h3", { text: "Members" }), owned ? null : el("p", { class: "warn", text: "No active person owns this organisation: you can make a member the owner, with evidence." }),
      table(["Name", "Email", "Role", "Account", "Second factor", ""], o.members.map((m) => el("tr", {},
        el("td", { text: m.display_name || "—" }), el("td", { text: m.email }), el("td", { text: m.role }), el("td", {}, pill(m.state === "active" ? "ok" : "warn", m.state)),
        el("td", { text: m.second_factor }),
        el("td", {}, el("details", {}, el("summary", { text: "Act" }),
          form(m.state === "suspended" ? "Restore this account" : "Suspend this account", [], m.state === "suspended" ? "Restore" : "Suspend",
            (v) => change(`/api/access/people/${encodeURIComponent(m.person_id)}/state`, { ...v, change: m.state === "suspended" ? "restore" : "suspend" }, again), { danger: m.state !== "suspended" }),
          !owned && m.state === "active" ? form("Make owner", [], "Make owner", (v) => change(`${base}/owner`, { ...v, membership_id: m.id }, again)) : null)))))),
    el("div", { class: "card" }, el("h3", { text: "Keys" }),
      table(["Name", "ID", "Kind", "Created", "Last used", "Expires", "State", ""], o.keys.map((k) => el("tr", {},
        el("td", { text: k.name }), el("td", { class: "muted", text: `${k.key_id.slice(0, 8)}…${k.last_four}` }), el("td", { text: k.kind }),
        el("td", { text: day(k.created_at) }), el("td", { text: day(k.last_used_at) || "never" }), el("td", { text: day(k.expires_at) }),
        el("td", { text: k.revoked_at ? `revoked (${k.revoked_reason})` : "live" }),
        el("td", {}, k.revoked_at ? null : el("details", {}, el("summary", { text: "Revoke" }),
          form("Revoke this key", [], "Revoke", (v) => change("/api/access/credentials/revoke", { ...v, credential: k.key_id }, again), { danger: true }))))))),
    el("div", { class: "card" }, el("h3", { text: "Connected apps" }),
      table(["App", "Label", "Resource", "Created", "Last used", "State", ""], o.connected_apps.map((g) => el("tr", {},
        el("td", { text: g.client_name }), el("td", { text: g.client_label }), el("td", { text: g.resource }), el("td", { text: day(g.created_at) }),
        el("td", { text: day(g.last_used_at) || "never" }), el("td", { text: g.revoked_at ? `revoked (${g.revoked_reason})` : "live" }),
        el("td", {}, g.revoked_at ? null : el("details", {}, el("summary", { text: "Revoke" }),
          form("Revoke this app", [], "Revoke", (v) => change("/api/access/credentials/revoke", { ...v, credential: g.id }, again), { danger: true }))))))),
    el("div", { class: "card" }, el("h3", { text: "Usage, last 30 days" }),
      table(["Day", "Class", "Requests", "Bytes"], o.usage.map((u) => el("tr", {}, el("td", { text: day(u.day) }), el("td", { text: u.class }), el("td", { text: String(u.requests) }), el("td", { text: String(u.bytes) }))))),
    el("div", { class: "card" }, el("h3", { text: "Plan history" }),
      table(["Plan", "Source", "Scope", "From", "To", "State", "Reason", "Evidence"], o.entitlements.map((e) => el("tr", {},
        el("td", { text: e.tier_id }), el("td", { text: e.source }), el("td", { text: e.scope.all ? "all" : (e.scope.jurisdictions || []).join(", ") }),
        el("td", { text: day(e.starts_at) }), el("td", { text: day(e.ends_at) }), el("td", { text: e.state }), el("td", { text: e.reason || "" }), el("td", { class: "muted", text: e.reference || "" }))))),
    el("div", { class: "card" }, el("h3", {}, "Audit log ", el("a", { class: "small", href: `#/access/audit?organisation=${o.id}`, text: "(all)" })), auditTable(o.audit)),
  ].filter(Boolean));
}

function auditTable(entries) {
  return table(["When (UTC)", "Who", "Action", "Target", "Details", "From"], entries.map((e) => el("tr", {},
    el("td", { text: moment(e.at) }), el("td", { text: e.actor_type === "person" ? e.actor : `${e.actor} (${e.actor_type})` }), el("td", { text: e.action }),
    el("td", { class: "muted", text: e.target_id ? `${e.target_type} ${e.target_id}` : "" }),
    el("td", { class: "muted", text: Object.keys(e.summary || {}).length ? JSON.stringify(e.summary) : "" }),
    el("td", { class: "muted", text: [e.client_family, e.network_prefix].filter(Boolean).join(" · ") }))));
}

async function accessAuditView() {
  const query = new URLSearchParams(location.hash.split("?")[1] || "");
  const action = el("input", { type: "search", placeholder: "Action starts with, e.g. key. or entitlement.", value: query.get("action") || "" });
  const body = el("div");
  let before = null;
  const more = el("button", { class: "small", text: "Older", on: { click: () => load(false) } });
  async function load(fresh) {
    if (fresh) { before = null; body.replaceChildren(); }
    const params = new URLSearchParams();
    if (query.get("organisation")) params.set("organisation", query.get("organisation"));
    if (action.value.trim()) params.set("action", action.value.trim());
    if (before) params.set("before", before);
    const { data } = await api(`/api/access/audit?${params}`);
    if (data.length) before = data[data.length - 1].seq;
    body.append(auditTable(data));
    more.hidden = data.length < 200;
  }
  action.addEventListener("change", () => load(true));
  main(el("div", { class: "card" }, el("h3", { text: query.get("organisation") ? `Audit log · ${query.get("organisation")}` : "Audit log, all organisations" }), action, body, more));
  await load(true);
}
