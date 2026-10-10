"use strict";

// Review workbench client. Everything is rendered with DOM APIs and textContent; the only HTML
// inserted is the server-escaped machine reading. No inline handlers (strict CSP).

const app = { me: null, csrf: "", dirty: false, view: null };
const KINDS = [["paragraph", "Paragraph"], ["list_item", "List item"], ["heading", "Heading"]];
const KIND_LABEL = {
  differs: "reads differently",
  only_in_primary: "not seen by the check readings",
  missing_from_primary: "only in the check readings",
  marker_conflict: "check readings give different numbers",
  moved: "placed differently",
};
const DECISION_LABEL = { keep: "kept as read", use_check: "used check reading", edited: "edited by hand", dismissed: "dismissed" };

// --- small helpers -----------------------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "on") for (const [event, fn] of Object.entries(value)) node.addEventListener(event, fn);
    else if (key === "value") node.value = value;
    else if (key === "checked") node.checked = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function toast(message) {
  const box = document.getElementById("toast");
  box.textContent = message;
  box.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove("show"), 3500);
}

async function api(path, { method = "GET", body } = {}) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  if (method !== "GET") options.headers["X-CSRF-Token"] = app.csrf;
  const response = await fetch(path, options);
  let data = null;
  try { data = await response.json(); } catch { data = null; }
  if (response.status === 401 && path !== "/api/me") {
    app.me = null;
    await showSignIn();
    throw new Error("signed out");
  }
  return { ok: response.ok, status: response.status, data };
}

function message(result) {
  const detail = result.data || {};
  return detail.message || (typeof detail === "string" ? detail : `Request failed (${result.status})`);
}

function setCrumbs(...parts) {
  const crumbs = document.getElementById("crumbs");
  crumbs.replaceChildren(...parts.flatMap((part, i) => (i ? [" › ", part] : [part])));
}

function main(...children) {
  document.getElementById("main").replaceChildren(...children);
  window.scrollTo(0, 0);
}

const pill = (cls, text) => el("span", { class: `pill ${cls}`, text });

// --- sign-in -------------------------------------------------------------------------------------

async function loadMe() {
  const result = await api("/api/me");
  if (!result.ok) return result.data || {};
  app.me = result.data;
  app.csrf = result.data.csrf;
  const who = document.getElementById("who");
  who.replaceChildren(
    el("span", { text: `${result.data.name} · ${result.data.roles.join(", ")}` }),
    el("button", { class: "small", text: "Sign out", on: { click: signOut } }),
  );
  return null;
}

async function signOut() {
  if (app.dirty && !confirm("Discard unsaved changes and sign out?")) return;
  await api("/auth/logout", { method: "POST" });
  app.dirty = false;
  location.hash = "#/";
  location.reload();
}

async function showSignIn(info) {
  info = info || (await api("/api/me")).data || {};
  document.getElementById("who").replaceChildren();
  setCrumbs("Sign in");
  const card = el("div", { class: "card signin" }, el("h2", { text: "Operators only" }));
  if (info.oidc) card.append(el("p", {}, el("a", { class: "button", href: "/auth/login" }, el("button", { class: "primary", text: "Sign in with your organisation account" }))));
  if (info.dev_login) {
    const email = el("input", { type: "email", placeholder: "operator email", required: true });
    card.append(
      el("p", { class: "muted", text: "Local trial sign-in (this computer only):" }),
      el("form", { on: { submit: async (event) => {
        event.preventDefault();
        const result = await api("/auth/dev", { method: "POST", body: { email: email.value } });
        if (!result.ok) return toast(message(result));
        await start();
      } } }, email, el("button", { class: "primary", text: "Sign in" })),
    );
  }
  if (!info.oidc && !info.dev_login) card.append(el("p", { class: "muted", text: "Sign-in is not configured on this server." }));
  main(card);
}

// --- dashboard -------------------------------------------------------------------------------------

async function dashboard() {
  setCrumbs("Acts");
  const { data: acts } = await api("/api/acts");
  const rows = acts.map((act) => {
    const share = act.pages ? Math.round((100 * act.checked) / act.pages) : 0;
    const state = act.approval_current ? pill("ok", "approved")
      : act.approval === "approved" ? pill("warn", "approval out of date")
      : act.open_high_flags ? pill("high", `${act.open_high_flags} high flags open`) : pill("medium", "in review");
    return el("tr", {},
      el("td", {}, el("a", { href: `#/act/${encodeURIComponent(act.id)}`, text: act.title })),
      el("td", { text: `${act.checked} / ${act.pages}` }),
      el("td", {}, el("div", { class: "bar-meter", title: `${share}% of pages checked` }, el("span", { style: null }))),
      el("td", {}, state),
    );
  });
  const table = el("table", { class: "list" },
    el("thead", {}, el("tr", {}, ...["Act", "Pages checked", "Progress", "State"].map((t) => el("th", { text: t })))),
    el("tbody", {}, rows));
  // Width set after creation: CSP forbids inline style attributes in markup, not CSSOM.
  table.querySelectorAll(".bar-meter > span").forEach((bar, i) => {
    bar.style.width = `${acts[i].pages ? (100 * acts[i].checked) / acts[i].pages : 0}%`;
  });
  main(el("div", { class: "card" },
    el("h2", { text: "Acts ready for review" }),
    acts.length ? table : el("p", { class: "muted", text: "No reconciled Acts yet. Run the batch reading pipeline first." })));
}

// --- one Act: progress, pages, approval ------------------------------------------------------------

async function actView(actId) {
  const { data } = await api(`/api/acts/${encodeURIComponent(actId)}`);
  const act = data.act;
  setCrumbs(el("a", { href: "#/", text: "Acts" }), act.title);
  const ready = data.readiness;
  const grid = el("div", { class: "pages" }, data.pages.map((page) => {
    const classes = [page.status || "", page.open_high ? "high" : "", page.stale ? "stale" : ""].join(" ");
    const tip = `${page.status || "not reviewed"}${page.open_high ? `, ${page.open_high} high flags open` : ""}${page.stale ? ", reading changed" : ""}`;
    return el("a", { class: classes, href: `#/act/${encodeURIComponent(actId)}/page/${page.page}`, title: tip, text: String(page.page) });
  }));
  const nextTodo = data.pages.find((p) => p.status !== "checked" || p.open_high || p.stale);
  const head = data.approvals[0];
  const current = head && head.decision === "approved" && head.content_sha256 === data.content_sha256;
  main(
    el("div", { class: "card" },
      el("h2", { text: act.title }),
      el("p", { class: "muted", text: `Primary reading: Document AI ${data.readers.primary.version.version}. Checks: ${Object.keys(data.readers.checks).join(", ")}.` }),
      el("p", {},
        pill(ready.unchecked_pages.length ? "medium" : "ok", `${ready.pages - ready.unchecked_pages.length} of ${ready.pages} pages checked`), " ",
        pill(ready.open_high_flags.length ? "high" : "ok", `${ready.open_high_flags.length} high flags open`), " ",
        ready.stale_pages.length ? pill("warn", `${ready.stale_pages.length} pages to recheck`) : "", " ",
        current ? pill("ok", "approved") : head && head.decision === "approved" ? pill("warn", "approval out of date") : ""),
      nextTodo ? el("p", {}, el("a", { href: `#/act/${encodeURIComponent(actId)}/page/${nextTodo.page}` }, el("button", { class: "primary", text: `Continue reviewing at page ${nextTodo.page}` }))) : null,
      el("h3", { text: "Pages" }),
      el("p", { class: "muted", text: "Green: checked. Yellow: needs attention. Red border: high flags open. Dashed: the machine reading changed since review." }),
      grid,
    ),
    approvalPanel(actId, data, current),
  );
}

function approvalPanel(actId, data, current) {
  const ready = data.readiness;
  const canApprove = app.me.roles.includes("approver");
  const panel = el("div", { class: "card" }, el("h3", { text: "Approval" }));
  const blockers = el("ul", { class: "ready" });
  if (ready.unchecked_pages.length) blockers.append(el("li", { text: `Pages not yet checked: ${summarise(ready.unchecked_pages)}` }));
  if (ready.open_high_flags.length) blockers.append(el("li", {}, "High flags open on pages ", ...unique(ready.open_high_flags.map((f) => f.page)).flatMap((p, i) => [i ? ", " : "", el("a", { href: `#/act/${encodeURIComponent(actId)}/page/${p}`, text: String(p) })])));
  if (ready.stale_pages.length) blockers.append(el("li", { text: `Pages to recheck because the reading changed: ${summarise(ready.stale_pages)}` }));
  if (ready.missing_checks.length) blockers.append(el("li", { text: `Check readings missing: ${ready.missing_checks.join(", ")} — rerun the batch` }));
  panel.append(ready.ready ? el("p", {}, pill("ok", "Ready for approval")) : el("div", {}, el("p", { text: "Before approval:" }), blockers));
  if (canApprove) {
    const basis = el("textarea", { rows: 3, value: (data.approvals[0] && data.approvals[0].rights_basis) || data.act.rights_basis || "" });
    const boxes = Object.entries(data.materials).map(([key, label]) => {
      const box = el("input", { type: "checkbox", "data-key": key });
      return { key, box, row: el("label", {}, box, " ", label) };
    });
    const note = el("textarea", { rows: 2, placeholder: "Note for the record (optional)" });
    const expected = data.approvals.length ? data.approvals[0].revision : 0;
    const send = async (decision) => {
      const materials = Object.fromEntries(boxes.map(({ key, box }) => [key, box.checked]));
      const result = await api(`/api/acts/${encodeURIComponent(actId)}/approval`, { method: "POST", body: { expected_revision: expected, decision, rights_basis: basis.value, materials, note: note.value } });
      if (!result.ok) return toast(message(result));
      toast(decision === "approved" ? "Approved and recorded" : "Approval revoked");
      actView(actId);
    };
    panel.append(
      el("h4", { text: "Rights basis" }), basis,
      el("h4", { text: "Verification (confirm each against the gazette)" }), el("div", { class: "materials" }, boxes.map((b) => b.row)),
      note,
      el("p", {},
        el("button", { class: "primary", text: "Approve this Act", disabled: !ready.ready, on: { click: () => send("approved") } }), " ",
        current ? el("button", { text: "Revoke approval", on: { click: () => confirm("Revoke the approval of this Act?") && send("revoked") } }) : null),
    );
  } else {
    panel.append(el("p", { class: "muted", text: "Approval needs the approver role." }));
  }
  if (data.approvals.length) {
    panel.append(el("h4", { text: "Approval record" }), el("ul", {}, data.approvals.map((a) => el("li", {},
      `${a.created_at.slice(0, 16).replace("T", " ")} · ${a.decision} by ${a.author} · content ${a.content_sha256.slice(0, 12)}… `,
      a.content_sha256 === data.content_sha256 ? pill("ok", "matches current content") : pill("warn", "content changed since")))));
  }
  panel.append(el("p", {},
    el("a", { href: `/api/acts/${encodeURIComponent(actId)}/export`, target: "_blank", rel: "noopener", text: "Reviewed content (JSON)" }), " · ",
    el("a", { href: `/api/acts/${encodeURIComponent(actId)}/text`, target: "_blank", rel: "noopener", text: "Plain text" }), " · ",
    el("a", { href: `#/audit/${encodeURIComponent(actId)}`, text: "Activity log" })));
  return panel;
}

function unique(values) { return [...new Set(values)]; }
function summarise(pages) { return pages.length > 20 ? `${pages.slice(0, 20).join(", ")} and ${pages.length - 20} more` : pages.join(", "); }

// --- one page: image, flags, editor -------------------------------------------------------------------

function tokenSpans(text) { return [...String(text || "").matchAll(/\S+/g)].map((m) => ({ start: m.index, end: m.index + m[0].length })); }

function machineTokenMap(units) {
  // Mirrors the server's page text order: marker and text, then notes; tables: caption then cells.
  const map = [];
  units.forEach((unit, u) => {
    if (unit.kind === "table") {
      if (unit.caption) tokenSpans(unit.caption).forEach((_, i) => map.push({ u, field: "caption", i }));
      unit.rows.forEach((row, r) => row.forEach((cell, c) => tokenSpans(cell).forEach((_, i) => map.push({ u, field: `cell:${r}:${c}`, i }))));
      return;
    }
    if (unit.marker) tokenSpans(unit.marker).forEach((_, i) => map.push({ u, field: "marker", i }));
    tokenSpans(unit.text).forEach((_, i) => map.push({ u, field: "text", i }));
    (unit.notes || []).forEach((note, n) => tokenSpans(note).forEach((_, i) => map.push({ u, field: `note:${n}`, i })));
  });
  return map;
}

function replaceTokens(text, from, to, replacement) {
  const spans = tokenSpans(text);
  if (from === to) {
    const at = from < spans.length ? spans[from].start : text.length;
    const pad = from < spans.length ? `${replacement} ` : ` ${replacement}`;
    return (text.slice(0, at) + pad + text.slice(at)).replace(/ {2,}/g, " ").trim();
  }
  const out = text.slice(0, spans[from].start) + replacement + text.slice(spans[to - 1].end);
  return out.replace(/ {2,}/g, " ").trim();
}

function quickFix(view, flag, reader) {
  // A check reading can be applied in one click only when the flagged words sit in one unit's
  // text and that unit is still exactly as the machine read it.
  const words = flag.checks[reader] || "";
  if (flag.kind === "marker_conflict") {
    const target = view.units.findIndex((u) => u.kind !== "table" && u.text.startsWith(flag.primary));
    if (target < 0 || !/^\S{1,12}$/.test(words)) return null;
    return () => { view.units[target].marker = words; view.units[target].marker_source = [reader]; };
  }
  const [from, to] = flag.tokens;
  const map = view.tokenMap;
  const anchor = from < map.length ? map[from] : map[map.length - 1];
  if (!anchor || anchor.field !== "text") return null;
  for (let i = from; i < to; i++) if (!map[i] || map[i].u !== anchor.u || map[i].field !== "text") return null;
  const machine = view.machineUnits[anchor.u];
  const working = view.units[anchor.u];
  if (!working || working.kind === "table" || working.text !== machine.text) return null;
  const localFrom = from < map.length ? anchor.i : tokenSpans(machine.text).length;
  const localTo = localFrom + (to - from);
  if (flag.kind === "missing_from_primary" && !words) return null;
  return () => { working.text = replaceTokens(working.text, localFrom, localTo, words); };
}

async function pageView(actId, pageNumber) {
  const [actResult, pageResult] = await Promise.all([
    api(`/api/acts/${encodeURIComponent(actId)}`),
    api(`/api/acts/${encodeURIComponent(actId)}/pages/${pageNumber}`),
  ]);
  if (!pageResult.ok) return main(el("p", { text: message(pageResult) }));
  const detail = actResult.data;
  const page = pageResult.data;
  const latest = page.latest;
  const view = {
    actId, pageNumber, detail, page,
    machineUnits: page.machine_units,
    tokenMap: machineTokenMap(page.machine_units),
    units: structuredClone(latest ? latest.units : page.machine_units),
    resolutions: structuredClone(latest ? latest.resolutions : {}),
    comment: latest ? latest.comment : "",
    expected: latest ? latest.revision : 0,
    tab: "edit",
    showLow: false,
  };
  app.view = view;
  app.dirty = false;
  setCrumbs(el("a", { href: "#/", text: "Acts" }), el("a", { href: `#/act/${encodeURIComponent(actId)}`, text: detail.act.title }), `Page ${pageNumber}`);
  renderPage(view);
}

function changed(view) { app.dirty = true; view.dirtyBadge.hidden = false; }

function renderPage(view) {
  const { detail, page, actId, pageNumber } = view;
  const numbers = detail.pages.map((p) => p.page);
  const index = numbers.indexOf(pageNumber);
  const go = (n) => { location.hash = `#/act/${encodeURIComponent(actId)}/page/${n}`; };
  const nextTodo = detail.pages.find((p) => p.page > pageNumber && (p.status !== "checked" || p.open_high || p.stale));
  const picker = el("select", { on: { change: (e) => go(e.target.value) } }, numbers.map((n) => el("option", { value: n, text: `Page ${n}`, selected: n === pageNumber })));
  view.dirtyBadge = el("span", { class: "pill warn", text: "unsaved changes", hidden: true });
  const latest = page.latest;
  const pagebar = el("div", { class: "pagebar" },
    el("button", { text: "← Previous", disabled: index <= 0, on: { click: () => go(numbers[index - 1]) } }),
    picker,
    el("button", { text: "Next →", disabled: index >= numbers.length - 1, on: { click: () => go(numbers[index + 1]) } }),
    nextTodo ? el("button", { text: `Next to review (${nextTodo.page})`, on: { click: () => go(nextTodo.page) } }) : null,
    latest ? pill(latest.status === "checked" ? "ok" : "warn", `revision ${latest.revision}: ${latest.status} by ${latest.author}`) : pill("medium", "not reviewed"),
    view.dirtyBadge,
  );
  const viewer = el("div", { class: "viewer" },
    el("img", { src: `/api/acts/${encodeURIComponent(actId)}/pages/${pageNumber}/image`, alt: `Gazette page ${pageNumber}`, on: { click: (e) => e.currentTarget.parentElement.classList.toggle("zoom") } }));
  const right = el("div");
  if (latest && latest.stale) right.append(el("div", { class: "card", text: "The machine reading of this page changed after it was reviewed (a re-read). Compare the Machine reading tab and save again to confirm." }));
  const tabs = el("div", { class: "tabs" });
  const body = el("div");
  const tabNames = [["edit", "Review and edit"], ["machine", "Machine reading"], ["checks", "Check readings"], ["history", `History (${page.history.length})`]];
  const show = (name) => {
    view.tab = name;
    tabs.replaceChildren(...tabNames.map(([key, label]) => el("button", { class: key === name ? "on" : "", text: label, on: { click: () => show(key) } })));
    if (name === "edit") body.replaceChildren(flagsPanel(view), editor(view), saveBar(view));
    if (name === "machine") { const box = el("div", { class: "card layout" }); box.innerHTML = page.machine_html; body.replaceChildren(box); }
    if (name === "checks") body.replaceChildren(...Object.entries(page.checks).map(([name2, text]) => el("div", {}, el("h4", { text: name2 }), el("pre", { class: "check", text }))));
    if (name === "history") body.replaceChildren(page.history.length ? el("ul", {}, page.history.map((h) => el("li", { text: `Revision ${h.revision} · ${h.status} · ${h.author} · ${h.created_at.slice(0, 16).replace("T", " ")}${h.comment ? ` — ${h.comment}` : ""}` }))) : el("p", { class: "muted", text: "Not reviewed yet." }));
  };
  right.append(pagebar, tabs, body);
  view.showTab = show;
  main(el("div", { class: "review" }, viewer, right));
  show(view.tab);
}

function flagsPanel(view) {
  const flags = view.page.flags.filter((f) => view.showLow || f.severity !== "low");
  const lowCount = view.page.flags.filter((f) => f.severity === "low").length;
  const box = el("div", { class: "flags-panel" });
  const header = el("p", { class: "muted" },
    `${view.page.flags.filter((f) => f.severity === "high").length} high, ${view.page.flags.filter((f) => f.severity === "medium").length} medium flags. `,
    lowCount ? el("label", {}, el("input", { type: "checkbox", checked: view.showLow, on: { change: (e) => { view.showLow = e.target.checked; rerenderEdit(view); } } }), ` show ${lowCount} punctuation-only`) : null);
  box.append(header);
  for (const flag of flags) {
    const resolution = view.resolutions[flag.id];
    const resolve = (decision, reader = "") => { view.resolutions[flag.id] = { decision, reader, note: "" }; changed(view); rerenderEdit(view); };
    const reads = el("div", { class: "reads" }, el("span", { class: "muted", text: "Main reading" }), el("q", { text: flag.primary || "—" }), el("span"));
    for (const [reader, words] of Object.entries(flag.checks)) {
      const fix = resolution ? null : quickFix(view, flag, reader);
      reads.append(
        el("span", { class: "muted", text: reader }),
        el("q", { text: words || "—" }),
        fix ? el("button", { class: "small", text: words ? `Use ${reader}` : "Remove words", on: { click: () => { fix(); resolve("use_check", reader); } } }) : el("span"),
      );
    }
    const actions = resolution
      ? el("div", { class: "actions" }, pill("ok", DECISION_LABEL[resolution.decision] + (resolution.reader ? ` (${resolution.reader})` : "")), el("button", { class: "small", text: "Undo", on: { click: () => { delete view.resolutions[flag.id]; changed(view); rerenderEdit(view); } } }))
      : el("div", { class: "actions" },
          el("button", { class: "small", text: "Keep as read (printed so)", on: { click: () => resolve("keep") } }),
          el("button", { class: "small", text: "I edited it below", on: { click: () => resolve("edited") } }),
          el("button", { class: "small", text: "Dismiss (not a problem)", on: { click: () => resolve("dismissed") } }));
    box.append(el("div", { class: `flag ${flag.severity} ${resolution ? "resolved" : ""}` },
      el("div", {}, pill(flag.severity, flag.severity), " ", KIND_LABEL[flag.kind] || flag.kind),
      reads, actions));
  }
  if (!flags.length) box.append(el("p", { class: "muted", text: "No flags on this page. Compare the text below with the image." }));
  return box;
}

function rerenderEdit(view) {
  // Redraw only the editing tab: the page image stays put and keeps its zoom.
  if (view.tab !== "edit" || !view.showTab) return;
  const scroll = window.scrollY;
  view.showTab("edit");
  window.scrollTo(0, scroll);
}

function depthOf(unit) {
  const lead = (unit.marker || unit.text || "").trimStart();
  if (/^\d{1,3}[A-Z]?\./.test(lead)) return 0;
  if (/^\(\d{1,3}[A-Z]?\)/.test(lead)) return 1;
  if (/^\([ivxl]{2,6}\)/.test(lead)) return 3;
  if (/^\([a-z]{1,2}\)/.test(lead)) return 2;
  return null;
}

function editor(view) {
  const list = el("div", { class: "editor" });
  view.units.forEach((unit, u) => list.append(unitCard(view, unit, u)));
  list.append(el("p", {}, el("button", { class: "small", text: "+ Add paragraph at end", on: { click: () => { view.units.push({ kind: "paragraph", text: "" }); changed(view); rerenderEdit(view); } } })));
  return list;
}

function autosize(area) { area.style.height = "auto"; area.style.height = `${area.scrollHeight + 2}px`; }

function unitCard(view, unit, u) {
  const move = (delta) => { const target = u + delta; if (target < 0 || target >= view.units.length) return; [view.units[u], view.units[target]] = [view.units[target], view.units[u]]; changed(view); rerenderEdit(view); };
  const remove = () => { if (confirm("Delete this unit?")) { view.units.splice(u, 1); changed(view); rerenderEdit(view); } };
  const insert = () => { view.units.splice(u + 1, 0, { kind: unit.kind === "table" ? "paragraph" : unit.kind, text: "" }); changed(view); rerenderEdit(view); };
  const tools = [
    el("button", { class: "small", title: "Move up", text: "↑", on: { click: () => move(-1) } }),
    el("button", { class: "small", title: "Move down", text: "↓", on: { click: () => move(1) } }),
    el("button", { class: "small", title: "Insert a unit after this one", text: "+", on: { click: insert } }),
    el("button", { class: "small", title: "Delete this unit", text: "✕", on: { click: remove } }),
  ];
  if (unit.kind === "table") {
    const caption = el("input", { type: "text", class: "grow", value: unit.caption || "", placeholder: "Table caption", on: { input: (e) => { unit.caption = e.target.value; changed(view); } } });
    const headers = el("input", { type: "number", min: 0, max: unit.rows.length, value: unit.header_rows || 0, title: "Header rows", on: { input: (e) => { unit.header_rows = Number(e.target.value) || 0; changed(view); } } });
    const grid = el("textarea", { class: "text", value: unit.rows.map((r) => r.join("\t")).join("\n"), title: "One row per line, cells separated by tabs", on: { input: (e) => { unit.rows = e.target.value.split("\n").map((line) => line.split("\t")); changed(view); autosize(e.target); } } });
    grid.addEventListener("keydown", (e) => { if (e.key === "Tab") { e.preventDefault(); const at = grid.selectionStart; grid.setRangeText("\t", at, grid.selectionEnd, "end"); grid.dispatchEvent(new Event("input")); } });
    requestAnimationFrame(() => autosize(grid));
    return el("div", { class: "unit" }, el("div", { class: "row" }, el("strong", { text: "Table" }), caption, el("label", { class: "muted" }, "header rows ", headers), ...tools), grid,
      el("p", { class: "muted", text: "One row per line; Tab separates cells." }));
  }
  const kind = el("select", { on: { change: (e) => { unit.kind = e.target.value; changed(view); } } }, KINDS.map(([value, label]) => el("option", { value, text: label, selected: unit.kind === value })));
  const marker = el("input", { type: "text", class: "marker", value: unit.marker || "", placeholder: "number", title: "Section or paragraph number, when not part of the text", on: { input: (e) => { unit.marker = e.target.value.trim() || undefined; delete unit.marker_source; changed(view); } } });
  const text = el("textarea", { class: "text", value: unit.text, rows: 2, on: { input: (e) => { unit.text = e.target.value; changed(view); autosize(e.target); } } });
  const notes = el("textarea", { class: "notes", value: (unit.notes || []).join("\n"), rows: 1, placeholder: "Margin notes, one per line", on: { input: (e) => { const lines = e.target.value.split("\n").map((s) => s.trim()).filter(Boolean); if (lines.length) unit.notes = lines; else delete unit.notes; changed(view); autosize(e.target); } } });
  const split = () => {
    // The text box keeps its cursor position after the button takes focus.
    const at = text.selectionStart;
    if (at <= 0 || at >= unit.text.length) return toast("Place the cursor in this unit's text where it should split");
    const rest = unit.text.slice(at).trim();
    unit.text = unit.text.slice(0, at).trim();
    view.units.splice(u + 1, 0, { kind: unit.kind, text: rest });
    changed(view); rerenderEdit(view);
  };
  const merge = () => {
    const next = view.units[u + 1];
    if (!next || next.kind === "table") return toast("There is no text unit after this one to merge");
    unit.text = `${unit.text} ${next.marker ? `${next.marker} ` : ""}${next.text}`.replace(/ {2,}/g, " ").trim();
    if (next.notes) unit.notes = [...(unit.notes || []), ...next.notes];
    view.units.splice(u + 1, 1);
    changed(view); rerenderEdit(view);
  };
  requestAnimationFrame(() => { autosize(text); autosize(notes); });
  const level = depthOf(unit);
  const source = Array.isArray(unit.marker_source) ? unit.marker_source.join(", ") : unit.marker_source;
  return el("div", { class: `unit ${level !== null ? `d${level}` : ""}` },
    el("div", { class: "row" }, kind, marker,
      source ? el("span", { class: "muted", text: `number restored from ${source}` }) : null,
      el("span", { class: "grow" }),
      el("button", { class: "small", title: "Split at the cursor", text: "Split", on: { click: split } }),
      el("button", { class: "small", title: "Merge with the next unit", text: "Merge ↓", on: { click: merge } }),
      ...tools),
    text, notes);
}

function saveBar(view) {
  const comment = el("textarea", { rows: 1, value: view.comment, placeholder: "Comment for this revision (optional)", on: { input: (e) => { view.comment = e.target.value; changed(view); } } });
  const save = async (status, thenNext = false) => {
    const high = view.page.flags.filter((f) => f.severity === "high" && !view.resolutions[f.id]);
    if (status === "checked" && high.length && !confirm(`${high.length} high flag(s) are not resolved. Save as checked anyway? (The Act cannot be approved until they are.)`)) return;
    const result = await api(`/api/acts/${encodeURIComponent(view.actId)}/pages/${view.pageNumber}`, {
      method: "POST",
      body: { expected_revision: view.expected, units: view.units, status, resolutions: view.resolutions, comment: view.comment },
    });
    if (result.status === 409) { toast("Someone saved this page first. Reloading their version."); app.dirty = false; return pageView(view.actId, view.pageNumber); }
    if (!result.ok) return toast(message(result));
    app.dirty = false;
    toast(`Saved revision ${result.data.revision}`);
    const numbers = view.detail.pages.map((p) => p.page);
    const next = numbers[numbers.indexOf(view.pageNumber) + 1];
    if (thenNext && next) location.hash = `#/act/${encodeURIComponent(view.actId)}/page/${next}`;
    else pageView(view.actId, view.pageNumber);
  };
  const restore = () => {
    if (!confirm("Replace the text below with the machine reading? (Nothing is saved until you save.)")) return;
    view.units = structuredClone(view.machineUnits);
    changed(view); rerenderEdit(view);
  };
  const canEdit = app.me.roles.includes("reviewer");
  return el("div", { class: "savebar" },
    comment,
    el("button", { class: "primary", text: "Save as checked", disabled: !canEdit, on: { click: () => save("checked") } }),
    el("button", { text: "Checked → next page", disabled: !canEdit, on: { click: () => save("checked", true) } }),
    el("button", { text: "Save: needs attention", disabled: !canEdit, on: { click: () => save("attention") } }),
    el("button", { class: "small", text: "Restore machine reading", on: { click: restore } }),
    canEdit ? null : el("span", { class: "muted", text: "Editing needs the reviewer role." }));
}

// --- activity log ---------------------------------------------------------------------------------

async function auditView(actId) {
  const { data } = await api(`/api/audit?act=${encodeURIComponent(actId)}`);
  setCrumbs(el("a", { href: "#/", text: "Acts" }), el("a", { href: `#/act/${encodeURIComponent(actId)}`, text: actId }), "Activity");
  main(el("div", { class: "card" }, el("h3", { text: "Activity log" }),
    el("table", { class: "list" }, el("tbody", {}, data.map((e) => el("tr", {},
      el("td", { text: e.at.slice(0, 19).replace("T", " ") }), el("td", { text: e.actor }), el("td", { text: e.action }),
      el("td", { text: e.page ? `page ${e.page}` : "" }), el("td", { class: "muted", text: JSON.stringify(e.detail) })))))));
}

// --- routing -------------------------------------------------------------------------------------

let lastHash = location.hash;
async function route() {
  if (app.dirty && location.hash !== lastHash && !confirm("You have unsaved changes on this page. Leave without saving?")) {
    history.replaceState(null, "", lastHash);
    return;
  }
  app.dirty = false;
  lastHash = location.hash;
  if (!location.hash.includes("/page/") || !document.querySelector(".review")) main(el("p", { class: "muted", text: "Loading…" }));
  const parts = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  try {
    if (parts[0] === "act" && parts[2] === "page") return await pageView(parts[1], Number(parts[3]));
    if (parts[0] === "act" && parts[1]) return await actView(parts[1]);
    if (parts[0] === "audit" && parts[1]) return await auditView(parts[1]);
    return await dashboard();
  } catch (error) {
    if (error.message !== "signed out") toast(error.message);
  }
}

window.addEventListener("beforeunload", (event) => { if (app.dirty) { event.preventDefault(); event.returnValue = ""; } });
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "s" && app.view && location.hash.includes("/page/")) {
    event.preventDefault();
    document.querySelector(".savebar .primary")?.click();
  }
});

async function start() {
  const signedOut = await loadMe();
  if (signedOut) return showSignIn(signedOut);
  if (!start.listening) { window.addEventListener("hashchange", route); start.listening = true; }
  route();
}

start();
