"use strict";

// Passkeys and the change helpers shared by the privileged sections, Access and Operators (MAC-11).
// A change is sent once, and again after a passkey check when the last one is older than 10 minutes.
// Uses app.js's helpers (el, api, toast, message). No inline handlers (strict CSP).

const b64 = {
  enc: (buffer) => btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""),
  dec: (text) => Uint8Array.from(atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4)), (c) => c.charCodeAt(0)),
};
const day = (value) => (value ? String(value).slice(0, 10) : "");
const moment = (value) => (value ? String(value).slice(0, 16).replace("T", " ") : "");

async function passkeyCheck() {
  const options = await api("/api/passkeys/options", { method: "POST", body: { purpose: "verify" } });
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
  const verified = await api("/api/passkeys/verify", {
    method: "POST",
    body: { credential: { id: credential.id, clientDataJSON: b64.enc(r.clientDataJSON), authenticatorData: b64.enc(r.authenticatorData), signature: b64.enc(r.signature) } },
  });
  if (!verified.ok) throw new Error(message(verified));
}

async function passkeyRegister(name, code) {
  const options = await api("/api/passkeys/options", { method: "POST", body: { purpose: "register" } });
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
  const saved = await api("/api/passkeys", {
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
function form(title, fields, submit, onSend, { evidence = true, danger = false, reason = true } = {}) {
  const inputs = {};
  const rows = fields.map(([key, label, input]) => {
    inputs[key] = input;
    return el("label", { class: "access-field" }, el("span", { text: label }), input);
  });
  if (reason) {
    inputs.reason = el("input", { type: "text", maxlength: "500", required: true, placeholder: "Why (recorded in the audit log)" });
    rows.push(el("label", { class: "access-field" }, el("span", { text: "Reason" }), inputs.reason));
  }
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
        for (const [key, input] of Object.entries(inputs)) values[key] = input.type === "checkbox" ? input.checked : input.value === undefined ? input.read() : input.value.trim();
        button.disabled = true;
        try { await onSend(values); } finally { button.disabled = false; }
      },
    },
  }, title ? el("h4", { text: title }) : null, ...rows, button);
}

// --- the passkey bar ------------------------------------------------------------------------------

async function passkeyBar() {
  const { data } = await api("/api/passkeys/me");
  const box = el("div", { class: "card access-passkey" });
  if (data.needs_enrolment) {
    const code = el("input", { type: "text", placeholder: "XXXX-XXXX-XXXX-XXXX", autocomplete: "off" });
    const name = el("input", { type: "text", value: "Passkey", maxlength: "100" });
    box.append(
      el("h3", { text: "Register a passkey" }),
      el("p", { class: "muted", text: "Changes here need a passkey on this device or a security key. Enter the one-time code an administrator gave you." }),
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
        let result = await api(`/api/passkeys/${encodeURIComponent(k.id)}`, { method: "DELETE" });
        if (result.status === 403) { await passkeyCheck(); result = await api(`/api/passkeys/${encodeURIComponent(k.id)}`, { method: "DELETE" }); }
        toast(result.ok ? "Removed." : message(result)); route();
      } } })))),
    el("button", { class: "small", text: "Add a passkey", on: { click: async () => {
      try { await passkeyRegister(prompt("Name for this passkey", "Security key") || "Passkey", ""); toast("Passkey added."); route(); }
      catch (error) { toast(error.message); }
    } } }));
  box.append(el("div", { class: "access-status" }, status, " ", check), keys);
  return box;
}

function table(headers, rows) {
  if (!rows.length) return el("p", { class: "muted", text: "None." });
  return el("table", { class: "list" }, el("thead", {}, el("tr", {}, headers.map((h) => el("th", { text: h })))), el("tbody", {}, rows));
}

