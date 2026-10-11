"use strict";

// Review workbench client. Everything is rendered with DOM APIs and textContent; the only HTML
// inserted is the server-escaped machine reading. No inline handlers (strict CSP).

const app = { me: null, csrf: "", dirty: false, view: null };
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
  setCrumbs("");
  const panel = el("div", { class: "signin-panel" },
    el("div", { class: "signin-brand" }, el("span", { class: "mark", text: "OA" }), el("span", { text: "Open Alvary" })),
    el("h1", { text: "Review workbench" }),
    el("p", { class: "muted", text: "Check machine readings of legal documents against the original, correct them, and approve them for publication." }));
  if (info.oidc) {
    panel.append(el("a", { class: "signin-button", href: "/auth/login" }, googleMark(), el("span", { text: "Sign in with Google" })));
  }
  if (info.dev_login) {
    const email = el("input", { type: "email", placeholder: "you@organisation.org", required: true, autocomplete: "username", "aria-label": "Operator email" });
    const error = el("p", { class: "signin-error", role: "alert", hidden: true });
    panel.append(
      info.oidc ? el("div", { class: "signin-or", text: "or, on this computer only" }) : el("p", { class: "signin-note", text: "Local trial sign-in — this computer only, no password." }),
      el("form", { class: "signin-form", on: { submit: async (event) => {
        event.preventDefault();
        const result = await api("/auth/dev", { method: "POST", body: { email: email.value } });
        if (!result.ok) { error.textContent = result.status === 403 ? "That email is not on the operator list." : message(result); error.hidden = false; return; }
        await start();
      } } }, email, el("button", { class: "primary", text: "Continue" })),
      error);
  }
  if (!info.oidc && !info.dev_login) panel.append(el("p", { class: "signin-error", text: "Sign-in is not configured on this server. Ask an administrator." }));
  panel.append(el("ul", { class: "signin-facts" },
    el("li", { text: "Operators on the allow-list only" }),
    el("li", { text: "Every save and approval is recorded with your name" }),
    el("li", { text: "Sessions end after an hour without activity" })));
  main(el("div", { class: "signin-page" }, panel));
  const field = panel.querySelector("input[type=email]");
  if (field) field.focus();
}

function googleMark() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 18 18"); svg.setAttribute("width", "18"); svg.setAttribute("height", "18"); svg.setAttribute("aria-hidden", "true");
  [["#4285F4", "M17.6 9.2c0-.6-.1-1.2-.2-1.7H9v3.3h4.8a4.1 4.1 0 0 1-1.8 2.7v2.2h2.9c1.7-1.6 2.7-3.9 2.7-6.5z"],
   ["#34A853", "M9 18c2.4 0 4.5-.8 6-2.2l-2.9-2.2c-.8.5-1.8.9-3.1.9-2.4 0-4.4-1.6-5.1-3.8H.9v2.3A9 9 0 0 0 9 18z"],
   ["#FBBC05", "M3.9 10.7a5.4 5.4 0 0 1 0-3.4V5H.9a9 9 0 0 0 0 8l3-2.3z"],
   ["#EA4335", "M9 3.6c1.3 0 2.5.5 3.4 1.3l2.6-2.6A9 9 0 0 0 .9 5l3 2.3C4.6 5.2 6.6 3.6 9 3.6z"]].forEach(([fill, d]) => {
    const path = document.createElementNS(ns, "path"); path.setAttribute("fill", fill); path.setAttribute("d", d); svg.append(path);
  });
  return svg;
}

// --- dashboard -------------------------------------------------------------------------------------

const TYPE_LABEL = { act: "Act", bill: "Bill", regulation: "Regulation", order: "Order", judgment: "Judgment", gazette: "Gazette", treaty: "Treaty", other: "Document" };
const ACTION_LABEL = { page_saved: "saved", details_saved: "saved the details of", act_approved: "approved", act_revoked: "revoked approval of", signed_in: "signed in", signed_out: "signed out", operator_saved: "updated operator", sign_in_refused: "was refused sign-in" };

function statusOf(doc) {
  if (doc.approval === "approved") return ["ok", "Approved"];
  if (doc.approval === "out_of_date") return ["warn", "Approval out of date"];
  if (doc.ready) return ["ok", "Ready for approval"];
  if (!doc.checked && !doc.attention && !doc.last) return ["medium", "Not started"];
  return ["medium", "In review"];
}

function ago(iso) {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  const steps = [[60, "minute"], [3600, "hour"], [86400, "day"], [604800, "week"]];
  let unit = ["minute", 60];
  for (const [size, name] of steps) if (seconds >= size) unit = [name, size];
  const n = Math.floor(seconds / unit[1]);
  return `${n} ${unit[0]}${n === 1 ? "" : "s"} ago`;
}

function remember(key, value) {
  try { if (value === undefined) return localStorage.getItem(`oa-home:${key}`); localStorage.setItem(`oa-home:${key}`, value); } catch { return null; }
  return null;
}

function meter(share) {
  const bar = el("span");
  bar.style.width = `${share}%`; // CSSOM, allowed by the strict content policy
  return el("div", { class: "bar-meter", title: `${share}% checked` }, bar);
}

async function dashboard() {
  setCrumbs("Home");
  const { data } = await api("/api/overview");
  const t = data.totals;
  const share = t.pages ? Math.round((100 * t.checked) / t.pages) : 0;
  const tiles = el("div", { class: "tiles" },
    el("div", { class: "tile" }, el("span", { class: "big", text: String(t.documents) }), el("span", { text: "documents" })),
    el("div", { class: "tile" }, el("span", { class: "big", text: `${share}%` }), el("span", { text: `${t.checked.toLocaleString()} of ${t.pages.toLocaleString()} pages checked` }), meter(share)),
    el("div", { class: `tile ${t.open_high ? "alert" : ""}` }, el("span", { class: "big", text: String(t.open_high) }), el("span", { text: "high flags open" })),
    el("div", { class: "tile" }, el("span", { class: "big", text: String(t.approved) }), el("span", { text: `approved${t.ready ? ` · ${t.ready} ready for approval` : ""}` })));

  const resume = data.resume
    ? el("div", { class: "card resume" },
        el("div", {},
          el("div", { class: "muted", text: "Continue where you left off" }),
          el("strong", { text: `${data.resume.title}, page ${data.resume.page}` }),
          el("div", { class: "muted", text: `You last saved page ${data.resume.last_page} ${ago(data.resume.at)}.` })),
        el("a", { href: `#/doc/${encodeURIComponent(data.resume.id)}/page/${data.resume.page}` }, el("button", { class: "primary", text: "Continue →" })))
    : null;

  const reasons = (d) => [
    d.open_high ? pill("high", `${d.open_high} high flags`) : null,
    d.attention ? pill("warn", `${d.attention} pages need attention`) : null,
    d.stale ? pill("warn", `${d.stale} pages to recheck`) : null,
    d.approval === "out_of_date" ? pill("warn", "approval out of date") : null,
  ].filter(Boolean);
  const attention = data.documents.filter((d) => reasons(d).length).sort((a, b) => b.open_high - a.open_high).slice(0, 6);
  const needs = attention.length
    ? el("div", { class: "card" }, el("h3", { text: "Needs attention" }),
        el("ul", { class: "plain" }, attention.map((d) => el("li", {},
          el("a", { href: `#/doc/${encodeURIComponent(d.id)}`, text: d.title }), " ", ...reasons(d).flatMap((r) => [r, " "])))))
    : null;

  const search = el("input", { type: "search", placeholder: "Search documents", value: remember("q") || "" });
  const typeSelect = el("select", {}, el("option", { value: "", text: "All types" }), data.types.map((type) => el("option", { value: type, text: TYPE_LABEL[type] || type })));
  const statusSelect = el("select", {}, ["All statuses", "Not started", "In review", "Ready for approval", "Approved", "Approval out of date"].map((label, i) => el("option", { value: i ? label : "", text: label })));
  const sortSelect = el("select", {}, [["recent", "Recently active"], ["title", "Title"], ["flags", "Most high flags"], ["progress", "Least checked"]].map(([value, label]) => el("option", { value, text: label })));
  typeSelect.value = remember("type") || ""; statusSelect.value = remember("status") || ""; sortSelect.value = remember("sort") || "recent";
  const cards = el("div", { class: "doc-cards" });
  const render = () => {
    remember("q", search.value); remember("type", typeSelect.value); remember("status", statusSelect.value); remember("sort", sortSelect.value);
    const q = search.value.trim().toLowerCase();
    let list = data.documents.filter((d) => (!q || d.title.toLowerCase().includes(q) || d.id.includes(q))
      && (!typeSelect.value || d.type === typeSelect.value) && (!statusSelect.value || statusOf(d)[1] === statusSelect.value));
    const progress = (d) => (d.pages ? d.checked / d.pages : 0);
    const sorts = {
      recent: (a, b) => (b.last ? b.last.at : "").localeCompare(a.last ? a.last.at : "") || a.title.localeCompare(b.title),
      title: (a, b) => a.title.localeCompare(b.title),
      flags: (a, b) => b.open_high - a.open_high,
      progress: (a, b) => progress(a) - progress(b),
    };
    list = list.sort(sorts[sortSelect.value]);
    cards.replaceChildren(...(list.length ? list.map((d) => {
      const pct = Math.round(100 * progress(d));
      const [cls, label] = statusOf(d);
      return el("a", { class: "doc-card", href: `#/doc/${encodeURIComponent(d.id)}` },
        el("div", { class: "doc-meta" }, el("span", { class: "type", text: TYPE_LABEL[d.type] || d.type }), d.year ? el("span", { text: String(d.year) }) : null, d.jurisdiction ? el("span", { text: d.jurisdiction.toUpperCase() }) : null),
        el("strong", { class: "doc-title", text: d.title }),
        meter(pct),
        el("div", { class: "doc-foot" }, el("span", { class: "muted", text: `${d.checked} / ${d.pages} pages checked` }), pill(cls, label), d.open_high ? pill("high", `${d.open_high} high`) : null),
        el("div", { class: "muted small", text: d.last ? `Last activity: ${d.last.actor.split("@")[0]}, ${ago(d.last.at)}` : "No review activity yet" }));
    }) : [el("p", { class: "muted", text: "No documents match." })]));
  };
  for (const control of [search, typeSelect, statusSelect, sortSelect]) control.addEventListener("input", render);
  render();

  const titleOf = (e) => e.title || e.id || "";
  const activity = el("div", { class: "card activity" }, el("h3", { text: "Recent activity" }),
    data.activity.length ? el("ul", { class: "plain" }, data.activity.map((e) => el("li", {},
      el("span", { text: `${e.actor.split("@")[0]} ${ACTION_LABEL[e.action] || e.action.replace(/_/g, " ")}` }),
      e.page ? " " : "", e.page && e.id ? el("a", { href: `#/doc/${encodeURIComponent(e.id)}/page/${e.page}`, text: `page ${e.page}` }) : "",
      titleOf(e) ? el("span", { class: "muted", text: ` · ${titleOf(e)}` }) : "",
      el("div", { class: "muted small", text: ago(e.at) })))) : el("p", { class: "muted", text: "Nothing yet." }));

  main(el("div", { class: "home" },
    el("div", { class: "home-main" },
      el("div", { class: "home-head" }, el("h1", { text: "Review workbench" }), el("p", { class: "muted", text: `Signed in as ${app.me.name} (${app.me.roles.join(", ")})` })),
      tiles, resume, needs,
      el("div", { class: "card" },
        el("div", { class: "doc-tools" }, el("h3", { text: "Documents" }), search, typeSelect, statusSelect, sortSelect),
        cards)),
    el("aside", { class: "home-side" }, activity)));
}

// --- one document: progress, pages, approval ------------------------------------------------------------

async function actView(actId) {
  const { data } = await api(`/api/documents/${encodeURIComponent(actId)}`);
  const act = data.act;
  setCrumbs(el("a", { href: "#/", text: "Home" }), act.title);
  const ready = data.readiness;
  const grid = el("div", { class: "pages" }, data.pages.map((page) => {
    const classes = [page.status || "", page.open_high ? "high" : "", page.stale ? "stale" : ""].join(" ");
    const tip = `${page.status || "not reviewed"}${page.open_high ? `, ${page.open_high} high flags open` : ""}${page.stale ? ", reading changed" : ""}`;
    return el("a", { class: classes, href: `#/doc/${encodeURIComponent(actId)}/page/${page.page}`, title: tip, text: String(page.page) });
  }));
  const nextTodo = data.pages.find((p) => p.status !== "checked" || p.open_high || p.stale);
  const head = data.approvals[0];
  const current = data.approved; // the approval covers exactly the current text and confirmed details
  main(
    el("div", { class: "card" },
      el("h2", { text: act.title }),
      el("p", { class: "muted", text: `Primary reading: Document AI ${data.readers.primary.version.version}. Checks: ${Object.keys(data.readers.checks).join(", ")}.` }),
      el("p", {},
        pill(ready.unchecked_pages.length ? "medium" : "ok", `${ready.pages - ready.unchecked_pages.length} of ${ready.pages} pages checked`), " ",
        pill(ready.open_high_flags.length ? "high" : "ok", `${ready.open_high_flags.length} high flags open`), " ",
        ready.stale_pages.length ? pill("warn", `${ready.stale_pages.length} pages to recheck`) : "", " ",
        current ? pill("ok", "approved") : head && head.decision === "approved" ? pill("warn", "approval out of date") : ""),
      el("p", {},
        nextTodo ? el("a", { href: `#/doc/${encodeURIComponent(actId)}/page/${nextTodo.page}` }, el("button", { class: "primary", text: `Continue reviewing at page ${nextTodo.page}` })) : null, " ",
        el("a", { href: `#/doc/${encodeURIComponent(actId)}/read` }, el("button", { text: "Read the whole document" })), " ",
        previewLink(actId)),
      el("h3", { text: "Pages" }),
      el("p", { class: "muted", text: "Green: checked. Yellow: needs attention. Red border: high flags open. Dashed: the machine reading changed since review." }),
      grid,
    ),
    detailsSummary(actId, ready),
    findReplaceCard(actId),
    approvalPanel(actId, data, current),
  );
}

function findReplaceCard(actId) {
  const find = el("input", { type: "text", placeholder: "Find across the document" });
  const replace = el("input", { type: "text", placeholder: "Replace with (optional)" });
  const caseBox = el("input", { type: "checkbox" });
  const wordBox = el("input", { type: "checkbox", checked: true });
  const results = el("div");
  let last = null;
  const run = async () => {
    const result = await api(`/api/documents/${encodeURIComponent(actId)}/search?${new URLSearchParams({ q: find.value, case: caseBox.checked, word: wordBox.checked })}`);
    if (!result.ok) return toast(message(result));
    last = { ...result.data, find: find.value, case: caseBox.checked, word: wordBox.checked };
    const byPage = new Map();
    for (const hit of result.data.hits) { if (!byPage.has(hit.page)) byPage.set(hit.page, []); byPage.get(hit.page).push(hit); }
    const boxes = [];
    const list = el("div", { class: "hits" }, [...byPage.entries()].map(([page, hits]) => {
      const box = el("input", { type: "checkbox", checked: true, "data-page": page });
      boxes.push(box);
      return el("div", { class: "hit" },
        el("label", {}, box, " ", el("a", { href: `#/doc/${encodeURIComponent(actId)}/page/${page}`, text: `Page ${page}` }), ` · ${hits.length}`),
        ...hits.slice(0, 5).map((h) => el("p", { class: "snippet" }, h.before, el("mark", { text: h.match }), h.after)));
    }));
    const apply = el("button", { class: "primary", text: "Replace in ticked pages", disabled: !app.me.roles.includes("reviewer") || !byPage.size, on: { click: async () => {
      const pages = Object.fromEntries(boxes.filter((b) => b.checked).map((b) => [b.dataset.page, last.revisions[b.dataset.page]]));
      if (!Object.keys(pages).length) return toast("Tick at least one page");
      if (!confirm(`Replace “${last.find}” with “${replace.value}” on ${Object.keys(pages).length} page(s)? Each becomes a new revision in your name.`)) return;
      const done = await api(`/api/documents/${encodeURIComponent(actId)}/replace`, { method: "POST", body: { find: last.find, replace: replace.value, case: last.case, word: last.word, pages } });
      if (!done.ok) return toast(message(done));
      const changed = done.data.changed.reduce((n, c) => n + c.replacements, 0);
      toast(`Replaced ${changed} on ${done.data.changed.length} page(s)${done.data.conflicts.length ? `; pages ${done.data.conflicts.join(", ")} changed meanwhile — search again` : ""}`);
      run();
    } } });
    results.replaceChildren(
      el("p", { class: "muted", text: result.data.hits.length ? `${result.data.hits.length} match(es) on ${byPage.size} page(s)${result.data.truncated ? " (first 500 shown)" : ""}.` : "No matches." }),
      list, byPage.size ? el("p", {}, apply) : null);
  };
  find.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  return el("div", { class: "card" }, el("h3", { text: "Find and replace across the document" }),
    el("div", { class: "findbar" }, find, replace, el("button", { text: "Find", on: { click: run } })),
    el("p", { class: "muted" }, el("label", {}, caseBox, " match case"), " ", el("label", {}, wordBox, " whole words only")),
    results);
}

// --- reading a whole document continuously -------------------------------------------------------------

function renderUnits(units) {
  const box = el("div", { class: "layout" });
  for (const unit of units) {
    if (unit.kind === "table") {
      const table = el("table", {}, unit.caption ? el("caption", { text: unit.caption }) : null,
        unit.rows.map((row, r) => el("tr", {}, row.map((cell) => el(r < (unit.header_rows || 0) ? "th" : "td", { text: cell })))));
      box.append(table);
      continue;
    }
    const level = unit.level ?? depthOfText(unit.marker || unit.text);
    const body = readParagraph(unit);
    const css = [unit.kind === "heading" ? "h" : level !== null ? `d${level}` : "", unit.align ? `align-${unit.align}` : ""].join(" ").trim();
    box.append(unit.notes && unit.notes.length
      ? el("div", { class: `unit ${css}` }, body, el("div", {}, unit.notes.map((n) => el("p", { class: "note", text: n }))))
      : el("div", { class: css }, body));
  }
  return box;
}

// A section number hangs in the margin as in the editor, whether it is a chip or the first words.
function readParagraph(unit) {
  const RE = window.ReviewEditor;
  if (unit.marker) {
    return el("p", { class: RE.hangClass(unit.kind, unit.marker) },
      el("span", { class: "restored lead", text: `${unit.marker} ` }), ...formattedText(unit.text, unit.marks));
  }
  const match = RE.SECTION_LEAD.exec(unit.text || "");
  const cls = match ? RE.hangClass(unit.kind, match[0].trim()) : "";
  if (!cls) return el("p", {}, ...formattedText(unit.text, unit.marks));
  const cut = match[0].length;
  const shift = (from, to) => (unit.marks || [])
    .map((m) => ({ ...m, start: Math.max(m.start, from) - from, end: Math.min(m.end, to) - from }))
    .filter((m) => m.end > m.start);
  return el("p", { class: cls },
    el("span", { class: "lead" }, ...formattedText(unit.text.slice(0, cut), shift(0, cut))),
    ...formattedText(unit.text.slice(cut), shift(cut, unit.text.length)));
}

const MARK_TAG = { bold: "strong", italic: "em", underline: "u", superscript: "sup", subscript: "sub" };

function formattedText(text, marks) {
  if (!marks || !marks.length) return [text];
  const cuts = [...new Set([0, text.length, ...marks.flatMap((m) => [m.start, m.end])])].sort((a, b) => a - b);
  const out = [];
  for (let i = 0; i + 1 < cuts.length; i++) {
    let node = document.createTextNode(text.slice(cuts[i], cuts[i + 1]));
    for (const mark of marks.filter((m) => m.start <= cuts[i] && m.end >= cuts[i + 1])) node = el(MARK_TAG[mark.type] || "span", {}, node);
    out.push(node);
  }
  return out;
}

function depthOfText(lead) {
  const text = (lead || "").trimStart();
  if (/^\d{1,3}[A-Z]?\./.test(text)) return 0;
  if (/^\(\d{1,3}[A-Z]?\)/.test(text)) return 1;
  if (/^\([ivxl]{2,6}\)/.test(text)) return 3;
  if (/^\([a-z]{1,2}\)/.test(text)) return 2;
  return null;
}

async function readView(actId) {
  const { data } = await api(`/api/documents/${encodeURIComponent(actId)}`);
  setCrumbs(el("a", { href: "#/", text: "Home" }), el("a", { href: `#/doc/${encodeURIComponent(actId)}`, text: data.act.title }), "Read");
  const list = el("div", { class: "reading" });
  const sentinel = el("p", { class: "muted", text: "Loading…" });
  const canEdit = app.me.roles.includes("reviewer");
  let next = 1, total = Infinity, busy = false;
  const load = async () => {
    if (busy || next > total) return;
    busy = true;
    const { data: chunk } = await api(`/api/documents/${encodeURIComponent(actId)}/stream?start=${next}&count=6`);
    total = chunk.total;
    for (const page of chunk.pages) list.append(readPage(actId, page, canEdit));
    next += 6;
    busy = false;
    sentinel.textContent = next > total ? "End of the document." : "Loading…";
    if (next <= total && sentinel.getBoundingClientRect().top < window.innerHeight + 600) load();
  };
  main(el("div", { class: "card" },
    el("h2", { text: data.act.title }),
    el("p", { class: "muted", text: "The document as it now stands, page by page beside the original. Mark pages checked as you read; open a page to edit it." })),
    list, sentinel);
  new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting)) load(); }, { rootMargin: "600px" }).observe(sentinel);
  load();
}

function readPage(actId, page, canEdit) {
  const status = page.status === "checked" ? pill("ok", "checked") : page.status ? pill("warn", page.status) : pill("medium", "not reviewed");
  const flags = page.open_high ? pill("high", `${page.open_high} high flags open`) : page.open ? pill("medium", `${page.open} flags open`) : null;
  const mark = canEdit && page.status !== "checked" && !page.open_high
    ? el("button", { class: "small", text: "Mark checked", on: { click: async (event) => {
        const result = await api(`/api/documents/${encodeURIComponent(actId)}/pages/${page.page}`, { method: "POST", body: { expected_revision: page.revision, units: page.units, status: "checked", resolutions: page.resolutions || {}, comment: "Checked while reading the whole document" } });
        if (result.status === 409) return toast(`Page ${page.page} changed meanwhile; open it to check`);
        if (!result.ok) return toast(message(result));
        page.revision = result.data.revision; page.status = "checked";
        event.currentTarget.replaceWith(pill("ok", "checked"));
      } } })
    : null;
  return el("section", { class: "readpage", id: `read-${page.page}` },
    el("div", { class: "viewer" }, el("img", { loading: "lazy", src: `/api/documents/${encodeURIComponent(actId)}/pages/${page.page}/image`, alt: `Original page ${page.page}` })),
    el("div", {},
      el("div", { class: "pagebar" }, el("strong", { text: `Page ${page.page}` }), status, flags,
        el("a", { href: `#/doc/${encodeURIComponent(actId)}/page/${page.page}` }, el("button", { class: "small", text: "Edit page" })), mark),
      el("div", { class: "card" }, renderUnits(page.units))));
}

// The document as the public site will show it, in a new tab, from the latest saved pages.
function previewLink(actId, page, cls) {
  const id = encodeURIComponent(actId);
  const at = page ? `#/sources/${id}?page=${page}` : "";
  return el("a", { href: `/preview/${id}${at}`, target: "_blank", rel: "noopener", title: "How this document will look on the public site, from the latest saved pages" },
    el("button", { class: cls, text: "Preview on the web" }));
}

function approvalPanel(actId, data, current) {
  const ready = data.readiness;
  const canApprove = app.me.roles.includes("approver");
  const panel = el("div", { class: "card" }, el("h3", { text: "Approval" }));
  const blockers = el("ul", { class: "ready" });
  if (ready.unchecked_pages.length) blockers.append(el("li", { text: `Pages not yet checked: ${summarise(ready.unchecked_pages)}` }));
  if (ready.open_high_flags.length) blockers.append(el("li", {}, "High flags open on pages ", ...unique(ready.open_high_flags.map((f) => f.page)).flatMap((p, i) => [i ? ", " : "", el("a", { href: `#/doc/${encodeURIComponent(actId)}/page/${p}`, text: String(p) })])));
  if (ready.stale_pages.length) blockers.append(el("li", { text: `Pages to recheck because the reading changed: ${summarise(ready.stale_pages)}` }));
  if (ready.missing_checks.length) blockers.append(el("li", { text: `Check readings missing: ${ready.missing_checks.join(", ")} — rerun the batch` }));
  if (!ready.details.confirmed) blockers.append(el("li", {}, "Document details ", el("a", { href: `#/doc/${encodeURIComponent(actId)}/details`, text: ready.details.status ? "not yet confirmed" : "not yet reviewed" })));
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
      const result = await api(`/api/documents/${encodeURIComponent(actId)}/approval`, { method: "POST", body: { expected_revision: expected, decision, rights_basis: basis.value, materials, note: note.value } });
      if (!result.ok) return toast(message(result));
      toast(decision === "approved" ? "Approved and recorded" : "Approval revoked");
      actView(actId);
    };
    panel.append(
      el("h4", { text: "Rights basis" }), basis,
      el("h4", { text: "Verification (confirm each against the gazette)" }), el("div", { class: "materials" }, boxes.map((b) => b.row)),
      note,
      el("p", {},
        el("button", { class: "primary", text: "Approve this document", disabled: !ready.ready, on: { click: () => send("approved") } }), " ",
        current ? el("button", { text: "Revoke approval", on: { click: () => confirm("Revoke the approval of this document?") && send("revoked") } }) : null),
    );
  } else {
    panel.append(el("p", { class: "muted", text: "Approval needs the approver role." }));
  }
  if (data.approvals.length) {
    panel.append(el("h4", { text: "Approval record" }), el("ul", {}, data.approvals.map((a) => el("li", {},
      `${a.created_at.slice(0, 16).replace("T", " ")} · ${a.decision} by ${a.author} · content ${a.content_sha256.slice(0, 12)}… `,
      a.content_sha256 !== data.content_sha256 ? pill("warn", "text changed since")
        : a.details_sha256 !== data.details_sha256 ? pill("warn", a.details_sha256 ? "details changed since" : "details not part of this approval")
        : pill("ok", "matches current text and details")))));
  }
  panel.append(el("p", {},
    el("a", { href: `/api/documents/${encodeURIComponent(actId)}/export`, target: "_blank", rel: "noopener", text: "Reviewed content (JSON)" }), " · ",
    el("a", { href: `/api/documents/${encodeURIComponent(actId)}/text`, target: "_blank", rel: "noopener", text: "Plain text" }), " · ",
    el("a", { href: `/api/documents/${encodeURIComponent(actId)}/akn`, text: "Akoma Ntoso XML" }), " · ",
    el("a", { href: `#/audit/${encodeURIComponent(actId)}`, text: "Activity log" })));
  return panel;
}

// --- document details: the comprehensive record beside the text (standard DETAILS-1) -------------

const DETAIL_STATUS = { suggested: "Suggested from the document — not yet saved", draft: "Draft", confirmed: "Confirmed" };

function detailsSummary(actId, ready) {
  const d = ready.details;
  const state = !d.status ? pill("medium", "details not started")
    : d.confirmed ? pill("ok", "details confirmed")
    : pill("warn", d.missing && d.missing.length ? `details: ${d.missing.length} to give or account for` : "details in draft");
  return el("div", { class: "card" },
    el("h3", { text: "Document details" }),
    el("p", { class: "muted", text: "Title, number, the dates it passed, was assented to and commenced, its gazette, the form of this text, its status and related laws, its language. Confirmed details are approved with the text and shown to the public beside it." }),
    el("p", {}, state, " ",
      el("a", { href: `#/doc/${encodeURIComponent(actId)}/details` }, el("button", { class: d.confirmed ? "" : "primary", text: d.confirmed ? "Review the details" : "Review and confirm the details" }))));
}

async function detailsView(actId) {
  const { data } = await api(`/api/documents/${encodeURIComponent(actId)}/details`);
  const doc = await api(`/api/documents/${encodeURIComponent(actId)}`);
  const title = doc.data.act.title;
  setCrumbs(el("a", { href: "#/", text: "Home" }), el("a", { href: `#/doc/${encodeURIComponent(actId)}`, text: title }), "Details");
  const { standard, record, suggestions } = data;
  const canEdit = app.me.roles.includes("reviewer");
  const values = structuredClone(record.details);
  const absent = { ...record.absent };
  const rows = new Map();
  const applicable = standard.fields.filter((f) => !f.types || f.types.includes(data.document_type));

  const isEmpty = (v) => v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length);
  const refresh = () => {
    let gaps = 0;
    for (const field of applicable) {
      const gap = field.required && isEmpty(values[field.key]) && !absent[field.key];
      rows.get(field.key).classList.toggle("missing", gap);
      gaps += gap ? 1 : 0;
    }
    counter.textContent = gaps ? `${gaps} detail${gaps === 1 ? "" : "s"} still to give or account for` : "Every detail is given or accounted for";
    counter.className = gaps ? "pill warn" : "pill ok";
    confirmButton.disabled = !canEdit || gaps > 0;
  };
  const touched = () => { app.dirty = true; refresh(); };

  const control = (field) => {
    const set = (value) => { values[field.key] = value; if (!isEmpty(value)) { delete absent[field.key]; why.value = ""; } touched(); };
    const value = values[field.key];
    let input;
    if (field.kind === "choice") {
      input = el("select", { on: { change: () => {
        set(input.value);
        if (field.key === "identity.family") {  // the kinds follow the family; the fields follow on save
          const kind = rows.get("identity.type")?.querySelector("select");
          if (kind) {
            kind.replaceChildren(el("option", { value: "", text: "—" }), (standard.kinds[input.value] || []).map((k) => el("option", { value: k, text: k.replace(/_/g, " ") })));
            values["identity.type"] = (standard.kinds[input.value] || [])[0] || "";
            kind.value = values["identity.type"];
          }
          toast("Save the draft to see this family's fields");
        }
      } } }, el("option", { value: "", text: "—" }),
        field.choices.map((c) => el("option", { value: c.value, text: c.label })));
      input.value = value || "";
    } else if (field.kind === "long") {
      input = el("textarea", { rows: field.key === "summary" ? 4 : 2, on: { input: () => set(input.value) } });
      input.value = value || "";
    } else if (field.kind === "list") {
      input = el("input", { type: "text", placeholder: "Separate with commas", on: { input: () => set(input.value.split(",").map((x) => x.trim()).filter(Boolean)) } });
      input.value = (value || []).join(", ");
    } else if (field.kind === "relations") {
      input = relationsEditor(standard, value || [], (links) => set(links));
    } else if (field.kind === "rows") {
      input = rowsEditor(field.columns, structuredClone(value || []), (rows) => set(rows));
    } else {
      const type = field.kind === "date" ? "date" : field.kind === "url" ? "url" : "text";
      input = el("input", { type, inputmode: field.kind === "year" ? "numeric" : undefined, on: { input: () => set(input.value) } });
      input.value = value || "";
    }
    if (!canEdit) input.setAttribute("disabled", "");
    const why = el("select", { class: "absent", title: "Why this detail is not given", disabled: !canEdit,
      on: { change: () => { if (why.value) { absent[field.key] = why.value; delete values[field.key]; if (input.tagName !== "DIV") input.value = ""; } else delete absent[field.key]; touched(); } } },
      el("option", { value: "", text: "given" }), Object.entries(standard.absent).map(([k, label]) => el("option", { value: k, text: label })));
    why.value = absent[field.key] || "";
    const hint = suggestions[field.key];
    let note = null;
    if (hint) {
      const shown = hint.absent ? standard.absent[hint.absent]
        : Array.isArray(hint.value) ? hint.value.map((l) => (typeof l === "object" ? Object.values(l).join(" ") : l)).join("; ")
        : String(hint.value);
      const differs = hint.absent ? absent[field.key] !== hint.absent : JSON.stringify(hint.value) !== JSON.stringify(values[field.key]);
      note = el("div", { class: "hint" },
        hint.page ? el("a", { href: `#/doc/${encodeURIComponent(actId)}/page/${hint.page}`, text: `p. ${hint.page}` }) : el("span", { text: "From the collection" }), " · ",
        el("span", { class: "evidence", text: `“${hint.evidence}”` }),
        differs && canEdit ? el("button", { class: "small", text: `Use: ${shown.length > 60 ? shown.slice(0, 60) + "…" : shown}`, on: { click: () => {
          if (hint.absent) { absent[field.key] = hint.absent; delete values[field.key]; why.value = hint.absent; }
          else { values[field.key] = structuredClone(hint.value); delete absent[field.key]; why.value = ""; }
          rows.get(field.key).replaceWith(row(field)); touched();
        } } }) : null);
    }
    return { input, why, note };
  };
  const row = (field) => {
    const { input, why, note } = control(field);
    const line = el("div", { class: "detail-row", "data-key": field.key },
      el("label", {}, field.label, field.required ? null : el("span", { class: "muted", text: " (optional)" })),
      el("div", { class: "detail-input" }, input, field.help ? el("div", { class: "muted small", text: field.help }) : null, note),
      field.required && !field.essential ? why : el("span", { class: "muted small", text: field.essential ? "must be given" : "" }));
    rows.set(field.key, line);
    return line;
  };

  const counter = el("span");
  const comment = el("textarea", { rows: 2, placeholder: "Note for the record (optional)" });
  const save = async (status) => {
    const result = await api(`/api/documents/${encodeURIComponent(actId)}/details`, { method: "POST",
      body: { expected_revision: record.revision, status, details: values, absent, comment: comment.value } });
    if (!result.ok) {
      if (result.data && result.data.missing) result.data.missing.forEach((key) => rows.get(key)?.classList.add("missing"));
      return toast(message(result));
    }
    app.dirty = false;
    toast(status === "confirmed" ? "Details confirmed and recorded" : "Draft saved");
    detailsView(actId);
  };
  const confirmButton = el("button", { class: "primary", text: "Confirm details", on: { click: () => save("confirmed") } });
  const groups = standard.groups.map((group) => {
    const fields = applicable.filter((f) => f.group === group.key);
    return fields.length ? el("div", { class: "card details-group" }, el("h3", { text: group.label }), fields.map(row)) : null;
  });
  const history = data.history.length ? el("div", { class: "card" }, el("h3", { text: "Details record" }),
    el("ul", {}, data.history.map((h) => el("li", { text: `${h.at.slice(0, 16).replace("T", " ")} · revision ${h.revision} · ${h.status} by ${h.author}${h.comment ? ` — ${h.comment}` : ""}` })))) : null;
  main(
    el("div", { class: "card details-head" },
      el("h2", { text: `${title}: details` }),
      el("p", { class: "muted", text: "Each detail was read from the document where it is printed; the page and words are shown beside it. Check each against the page image, correct it, or say why it is not given. Confirmed details are approved with the text and shown to the public." }),
      el("p", {}, pill(record.status === "confirmed" ? "ok" : record.status === "draft" ? "warn" : "medium", DETAIL_STATUS[record.status] + (record.revision ? ` · revision ${record.revision} by ${record.author}` : "")), " ", counter),
      canEdit ? el("p", {}, el("button", { text: "Save draft", on: { click: () => save("draft") } }), " ", confirmButton, " ", previewLink(actId)) : el("p", { class: "muted", text: "Editing needs the reviewer role." }),
      canEdit ? comment : null),
    ...groups,
    history,
  );
  refresh();
}

function relationsEditor(standard, links, onChange) {
  const box = el("div", { class: "relations" });
  const draw = () => {
    box.replaceChildren(
      ...links.map((link, i) => {
        const kind = el("select", { on: { change: () => { link.kind = kind.value; onChange(links); } } }, standard.relations.map((r) => el("option", { value: r.value, text: r.label })));
        kind.value = link.kind;
        const target = el("input", { type: "text", placeholder: "Title or citation of the other law", value: link.target || "", on: { input: () => { link.target = target.value; onChange(links); } } });
        const id = el("input", { type: "text", class: "narrow", placeholder: "catalogue id (if any)", value: link.target_id || "", on: { input: () => { if (id.value) link.target_id = id.value; else delete link.target_id; onChange(links); } } });
        const note = el("input", { type: "text", class: "narrow", placeholder: "note", value: link.note || "", on: { input: () => { if (note.value) link.note = note.value; else delete link.note; onChange(links); } } });
        return el("div", { class: "relation" }, kind, target, id, note,
          el("button", { class: "small", text: "Remove", on: { click: () => { links.splice(i, 1); onChange(links); draw(); } } }));
      }),
      el("button", { class: "small", text: "+ Add a related law", on: { click: () => { links.push({ kind: "amends", target: "" }); onChange(links); draw(); } } }));
  };
  draw();
  return box;
}

// A small table of rows (language versions, parties, bench): one input per column.
function rowsEditor(columns, rows, onChange) {
  const box = el("div", { class: "rows-editor" });
  const draw = () => {
    box.replaceChildren(
      ...rows.map((row, i) => el("div", { class: "row-line" },
        ...columns.map((column) => {
          let cell;
          if (column.choices && column.choices.length) {
            cell = el("select", { title: column.label, on: { change: () => { row[column.key] = cell.value; onChange(rows); } } },
              el("option", { value: "", text: column.label }), column.choices.map((c) => el("option", { value: c.value, text: c.label })));
          } else {
            cell = el("input", { type: column.kind === "date" ? "date" : "text", placeholder: column.label, title: column.label,
              on: { input: () => { row[column.key] = cell.value; onChange(rows); } } });
          }
          cell.value = row[column.key] || "";
          return cell;
        }),
        el("button", { class: "small", text: "Remove", on: { click: () => { rows.splice(i, 1); onChange(rows); draw(); } } }))),
      el("button", { class: "small", text: "+ Add", on: { click: () => { rows.push({}); onChange(rows); draw(); } } }));
  };
  draw();
  return box;
}

function unique(values) { return [...new Set(values)]; }
function summarise(pages) { return pages.length > 20 ? `${pages.slice(0, 20).join(", ")} and ${pages.length - 20} more` : pages.join(", "); }

// --- one page: image, flags, document surface ------------------------------------------------------

const words = (value) => (String(value || "").match(/\S+/g) || []);
const fold = (value) => words(value).slice(0, 6).map((w) => w.toLowerCase().replace(/[^0-9a-z]/g, "")).filter(Boolean).join(" ");

function unitText(unit) {
  if (unit.kind === "table") return [unit.caption || "", ...unit.rows.map((r) => r.join(" "))].join(" ");
  return [unit.marker || "", unit.text || ""].join(" ").trim();
}

async function pageView(actId, pageNumber) {
  const [actResult, pageResult] = await Promise.all([
    api(`/api/documents/${encodeURIComponent(actId)}`),
    api(`/api/documents/${encodeURIComponent(actId)}/pages/${pageNumber}`),
  ]);
  if (!pageResult.ok) return main(el("p", { text: message(pageResult) }));
  const detail = actResult.data;
  const page = pageResult.data;
  const latest = page.latest;
  const view = {
    actId, pageNumber, detail, page,
    machineUnits: page.machine_units,
    units: structuredClone(latest ? latest.units : page.machine_units),
    resolutions: structuredClone(latest ? latest.resolutions : {}),
    comment: latest ? latest.comment : "",
    expected: latest ? latest.revision : 0,
    tab: "edit",
    showLow: false,
    surface: null,
  };
  app.view = view;
  app.dirty = false;
  setCrumbs(el("a", { href: "#/", text: "Home" }), el("a", { href: `#/doc/${encodeURIComponent(actId)}`, text: detail.act.title }), `Page ${pageNumber}`);
  renderPage(view);
}

function changed(view) { app.dirty = true; if (view.dirtyBadge) view.dirtyBadge.hidden = false; saveDraft(view); }

// The working units: from the surface while it is open, otherwise the last snapshot.
function currentUnits(view) {
  if (view.surface) view.units = view.surface.units();
  return view.units;
}

function renderPage(view) {
  const { detail, page, actId, pageNumber } = view;
  const numbers = detail.pages.map((p) => p.page);
  const index = numbers.indexOf(pageNumber);
  const go = (n) => { location.hash = `#/doc/${encodeURIComponent(actId)}/page/${n}`; };
  const nextTodo = detail.pages.find((p) => p.page > pageNumber && (p.status !== "checked" || p.open_high || p.stale));
  const picker = el("select", { on: { change: (e) => go(e.target.value) } }, numbers.map((n) => el("option", { value: n, text: `Page ${n}`, selected: n === pageNumber })));
  view.dirtyBadge = el("span", { class: "pill warn", text: "unsaved changes", hidden: !app.dirty });
  const latest = page.latest;
  const pagebar = el("div", { class: "pagebar" },
    el("button", { text: "← Previous", disabled: index <= 0, on: { click: () => go(numbers[index - 1]) } }),
    picker,
    el("button", { text: "Next →", disabled: index >= numbers.length - 1, on: { click: () => go(numbers[index + 1]) } }),
    nextTodo ? el("button", { text: `Next to review (${nextTodo.page})`, on: { click: () => go(nextTodo.page) } }) : null,
    latest ? pill(latest.status === "checked" ? "ok" : "warn", `revision ${latest.revision}: ${latest.status} by ${latest.author}`) : pill("medium", "not reviewed"),
    view.dirtyBadge,
    previewLink(actId, view.pageNumber, "small"),
  );
  const viewer = buildViewer(view);
  const right = el("div");
  const draft = loadDraft(view);
  if (draft && !view.draftHandled) right.append(draftBanner(view, draft));
  if (latest && latest.stale) right.append(el("div", { class: "card", text: "The machine reading of this page changed after it was reviewed (a re-read). Compare the Machine reading tab and save again to confirm." }));
  const tabs = el("div", { class: "tabs" });
  const body = el("div");
  const tabNames = [["edit", "Review and edit"], ["changes", "Changes"], ["machine", "Machine reading"], ["checks", "Check readings"], ["history", `History (${page.history.length})`]];
  const show = (name) => {
    if (view.tab === "edit" && name !== "edit") currentUnits(view);
    if (view.surface && name !== "edit") { view.surface.destroy(); view.surface = null; }
    view.tab = name;
    tabs.replaceChildren(...tabNames.map(([key, label]) => el("button", { class: key === name ? "on" : "", text: label, on: { click: () => show(key) } })));
    if (name === "edit") body.replaceChildren(...editTab(view));
    if (name === "changes") body.replaceChildren(changesView(view));
    if (name === "machine") { const box = el("div", { class: "card layout" }); box.innerHTML = page.machine_html; body.replaceChildren(box); }
    if (name === "checks") body.replaceChildren(...Object.entries(page.checks).map(([reader, text]) => el("div", {}, el("h4", { text: reader }), el("pre", { class: "check", text }))));
    if (name === "history") body.replaceChildren(page.history.length ? el("ul", {}, page.history.map((h) => el("li", { text: `Revision ${h.revision} · ${h.status} · ${h.author} · ${h.created_at.slice(0, 16).replace("T", " ")}${h.comment ? ` — ${h.comment}` : ""}` }))) : el("p", { class: "muted", text: "Not reviewed yet." }));
  };
  view.showTab = show;
  right.append(pagebar, tabs, body);
  main(el("div", { class: "review" }, viewer, right));
  show(view.tab);
}

// --- the page image: zoom, pan, follow -------------------------------------------------------------

function buildViewer(view) {
  const { actId, pageNumber } = view;
  view.image = el("img", { src: `/api/documents/${encodeURIComponent(actId)}/pages/${pageNumber}/image`, alt: `Original page ${pageNumber}`, draggable: "false" });
  view.overlay = el("div", { class: "region", hidden: true });
  view.lineOverlay = el("div", { class: "region line", hidden: true });
  const frame = el("div", { class: "frame" }, view.image, view.overlay, view.lineOverlay);
  const label = el("span", { class: "muted zoom-label" });
  const viewer = el("div", { class: "viewer" });
  const setZoom = (value, anchor) => {
    const before = { w: frame.offsetWidth, h: frame.offsetHeight };
    app.zoom = Math.max(1, Math.min(4, value));
    frame.style.width = `${app.zoom * 100}%`;
    label.textContent = `${Math.round(app.zoom * 100)}%`;
    if (anchor) { // keep the point under the pointer where it was
      const ratio = frame.offsetWidth / before.w;
      viewer.scrollLeft = (viewer.scrollLeft + anchor.x) * ratio - anchor.x;
      viewer.scrollTop = (viewer.scrollTop + anchor.y) * (frame.offsetHeight / before.h) - anchor.y;
    }
  };
  const follow = el("input", { type: "checkbox", checked: app.follow !== false, on: { change: (e) => { app.follow = e.target.checked; } } });
  viewer.append(
    el("div", { class: "viewer-tools" },
      el("button", { class: "small", title: "Zoom out", text: "−", on: { click: () => setZoom(app.zoom / 1.25) } }),
      label,
      el("button", { class: "small", title: "Zoom in (or Ctrl/Cmd + scroll)", text: "+", on: { click: () => setZoom(app.zoom * 1.25) } }),
      el("button", { class: "small", title: "Fit the page width", text: "Fit", on: { click: () => setZoom(1) } }),
      el("label", { class: "muted", title: "Keep the image on the text you are reading" }, follow, " follow text"),
      el("span", { class: "muted hint", text: "Drag to move when zoomed. Click the page to find its text." })),
    frame);
  viewer.addEventListener("wheel", (event) => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    const box = viewer.getBoundingClientRect();
    setZoom(app.zoom * (event.deltaY < 0 ? 1.12 : 1 / 1.12), { x: event.clientX - box.left, y: event.clientY - box.top });
  }, { passive: false });
  let drag = null;
  frame.addEventListener("pointerdown", (event) => { drag = { x: event.clientX, y: event.clientY, left: viewer.scrollLeft, top: viewer.scrollTop, moved: false }; });
  window.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
    if (drag.moved && app.zoom > 1) { viewer.scrollLeft = drag.left - dx; viewer.scrollTop = drag.top - dy; frame.classList.add("dragging"); }
  });
  window.addEventListener("pointerup", (event) => {
    if (drag && !drag.moved && frame.contains(event.target)) imageClicked(view, event);
    drag = null;
    frame.classList.remove("dragging");
  });
  setZoom(app.zoom || 1);
  return viewer;
}

const norm = (value) => words(value).map((w) => w.toLowerCase().replace(/[^0-9a-z]/g, "")).filter(Boolean);

function containsRun(haystack, needle) {
  if (!needle.length) return false;
  outer: for (let i = 0; i + needle.length <= haystack.length; i++) {
    for (let j = 0; j < needle.length; j++) if (haystack[i + j] !== needle[j]) continue outer;
    return true;
  }
  return false;
}

// The printed line the cursor is on, found from the words either side of it.
function showLine(view) {
  if (!view.surface || !view.page.lines || !view.page.lines.length) return;
  const { before, after } = view.surface.caretContext();
  // The whole word at the cursor (it may sit mid-word), with its neighbours either side.
  const leftWords = norm(before), rightWords = norm(after);
  const midWord = /\S$/.test(before) && /^\S/.test(after);
  const word = midWord ? (leftWords.pop() || "") + (rightWords.shift() || "") : rightWords.shift() || leftWords.pop() || "";
  const left = [leftWords[leftWords.length - 1], word].filter(Boolean);
  const right = [word, rightWords[0]].filter(Boolean);
  const lines = view.page.lines.map((line) => ({ line, words: norm(line.text) }));
  const region = view.overlay.hidden ? null : view.currentRegion;
  const inside = (line) => !region || (line.box[1] >= region.box[1] - 0.01 && line.box[3] <= region.box[3] + 0.01);
  const probes = [right, left, [word]].filter((p) => p.length && p[0]);
  for (const probe of probes) {
    const hit = lines.find((l) => inside(l.line) && containsRun(l.words, probe)) || lines.find((l) => containsRun(l.words, probe));
    if (hit) return placeBox(view.lineOverlay, hit.line.box);
  }
  view.lineOverlay.hidden = true;
}

function placeBox(box, [x0, y0, x1, y1]) {
  Object.assign(box.style, { left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%` });
  box.hidden = false;
}

let followTimer = null;
window.addEventListener("scroll", () => {
  const view = app.view;
  if (!view || app.follow === false || view.tab !== "edit" || !view.surface || !location.hash.includes("/page/")) return;
  clearTimeout(followTimer);
  followTimer = setTimeout(() => {
    const blocks = [...document.querySelectorAll(".surface .ProseMirror > *")];
    const index = blocks.findIndex((b) => b.getBoundingClientRect().bottom > 140);
    const units = view.surface.units();
    if (index >= 0 && units[index]) showRegion(view, regionFor(view, units[index]));
  }, 120);
}, { passive: true });

// --- drafts: unsaved work survives a crash or an accidental close ----------------------------------

const draftKey = (view) => `oa-draft:${view.actId}:${view.pageNumber}`;

function saveDraft(view) {
  clearTimeout(view.draftTimer);
  view.draftTimer = setTimeout(() => {
    try {
      localStorage.setItem(draftKey(view), JSON.stringify({ base: view.expected, at: new Date().toISOString(), units: currentUnits(view), resolutions: view.resolutions, comment: view.comment }));
    } catch { /* storage unavailable: the unload warning still protects the work */ }
  }, 700);
}

function loadDraft(view) {
  try { return JSON.parse(localStorage.getItem(draftKey(view)) || "null"); } catch { return null; }
}

function clearDraft(view) {
  clearTimeout(view.draftTimer);
  try { localStorage.removeItem(draftKey(view)); } catch { /* nothing to clear */ }
}

function draftBanner(view, draft) {
  const when = new Date(draft.at).toLocaleString();
  const current = draft.base === view.expected;
  const banner = el("div", { class: "card draft" },
    el("strong", { text: "Unsaved changes found. " }),
    current ? `You were editing this page (${when}) and did not save.`
      : `You were editing revision ${draft.base} (${when}); someone has since saved revision ${view.expected}. Restoring puts your version back — check the Changes tab before saving.`,
    " ",
    el("button", { class: "small primary", text: "Restore my changes", on: { click: () => {
      view.units = draft.units; view.resolutions = draft.resolutions || {}; view.comment = draft.comment || "";
      view.draftHandled = true; app.dirty = true;
      banner.remove(); view.showTab("edit"); view.dirtyBadge.hidden = false;
    } } }), " ",
    el("button", { class: "small", text: "Discard them", on: { click: () => { clearDraft(view); view.draftHandled = true; banner.remove(); } } }));
  return banner;
}

// --- keyboard flag review ---------------------------------------------------------------------------

function visibleOpenFlags(view) {
  if (!view.surface) return [];
  const known = new Map(view.page.flags.map((f) => [f.id, f]));
  return view.surface.openFlags().map((d) => known.get(d.id)).filter((f) => f && !view.resolutions[f.id] && (view.showLow || f.severity !== "low"));
}

function gotoFlag(view, step) {
  const flags = visibleOpenFlags(view);
  if (!flags.length) return toast("No open flags highlighted on this page");
  const at = flags.findIndex((f) => f.id === view.openFlagId);
  const next = flags[(at + step + flags.length) % flags.length] || flags[0];
  view.surface.reveal(next.id);
  setTimeout(() => {
    const node = view.surface.flagElement(next.id);
    if (node) { node.scrollIntoView({ block: "center" }); openFlag(view, next.id, node.getBoundingClientRect()); }
  }, 0);
}

function actOnFlag(code) {
  const pop = document.querySelector(".popover");
  if (!pop) return toast("Open a flag first (Alt+N)");
  const buttons = [...pop.querySelectorAll("button")];
  const pick = (test) => buttons.find((b) => test(b.textContent));
  const choice = {
    KeyK: () => pick((t) => t.startsWith("Keep as read")),
    KeyD: () => pick((t) => t === "Dismiss"),
    KeyE: () => pick((t) => t === "I corrected it"),
  }[code];
  const button = choice ? choice() : buttons.filter((b) => /^(Use |Number |Remove words)/.test(b.textContent))[Number(code.slice(-1)) - 1];
  if (button) { button.click(); if (app.view) setTimeout(() => gotoFlag(app.view, 0), 50); }
}

function resolveLow(view) {
  const low = view.page.flags.filter((f) => f.severity === "low" && !view.resolutions[f.id]);
  for (const flag of low) { view.resolutions[flag.id] = { decision: "keep", reader: "", note: "punctuation only" }; if (view.surface) view.surface.resolve(flag.id); }
  if (low.length) { changed(view); refreshFlags(view); }
  toast(low.length ? `Kept ${low.length} punctuation-only differences as read` : "No punctuation-only flags open");
}

document.addEventListener("keydown", (event) => {
  const view = app.view;
  if (!view || view.tab !== "edit" || !view.surface || !location.hash.includes("/page/")) return;
  if (!event.altKey || event.ctrlKey || event.metaKey) return;
  const actions = {
    KeyN: () => gotoFlag(view, 1), KeyP: () => gotoFlag(view, -1), KeyL: () => resolveLow(view),
    KeyK: () => actOnFlag("KeyK"), KeyD: () => actOnFlag("KeyD"), KeyE: () => actOnFlag("KeyE"),
    Digit1: () => actOnFlag("Digit1"), Digit2: () => actOnFlag("Digit2"), Digit3: () => actOnFlag("Digit3"),
  };
  const action = actions[event.code];
  if (!action) return;
  event.preventDefault();
  event.stopPropagation();
  action();
}, true);

// --- linking text and image ---------------------------------------------------------------------------

function regionFor(view, unit) {
  const target = fold(unitText(unit));
  if (!target) return null;
  return view.page.regions.find((r) => r.role !== "pageHeader" && r.role !== "pageFooter" && fold(r.text) && (fold(r.text).startsWith(target) || target.startsWith(fold(r.text))))
    || view.page.regions.find((r) => fold(r.text).split(" ").slice(0, 3).join(" ") === target.split(" ").slice(0, 3).join(" "));
}

function showRegion(view, region) {
  view.currentRegion = region;
  if (!region) { view.overlay.hidden = true; return; }
  placeBox(view.overlay, region.box);
  const viewer = view.overlay.closest(".viewer");
  const { offsetTop: top, offsetLeft: left, offsetWidth: width } = view.overlay;
  const outside = top < viewer.scrollTop + 30 || top > viewer.scrollTop + viewer.clientHeight - 60
    || left < viewer.scrollLeft || left + Math.min(width, viewer.clientWidth) > viewer.scrollLeft + viewer.clientWidth;
  if (outside) viewer.scrollTo({ top: top - viewer.clientHeight / 3, left: Math.max(0, left - 20), behavior: "smooth" });
}

function imageClicked(view, event) {
  const rect = view.image.getBoundingClientRect();
  const x = (event.clientX - rect.left) / rect.width;
  const y = (event.clientY - rect.top) / rect.height;
  const region = view.page.regions.find((r) => x >= r.box[0] && x <= r.box[2] && y >= r.box[1] && y <= r.box[3]);
  if (!region) return;
  showRegion(view, region);
  if (view.tab !== "edit" || !view.surface) return;
  const target = fold(region.text);
  const units = view.surface.units();
  const index = units.findIndex((u) => { const t = fold(unitText(u)); return t && (t.startsWith(target) || target.startsWith(t)); });
  if (index >= 0) view.surface.focusUnit(index);
}

// --- the editing tab --------------------------------------------------------------------------------

function markButton(surface, type, label, title) {
  const button = el("button", { class: `small mark-${type}`, title, "aria-label": title, "aria-pressed": "false", "data-mark": type, text: label });
  button.addEventListener("mousedown", (event) => event.preventDefault()); // keep the text selection
  button.addEventListener("click", () => surface.run(window.ReviewEditor.commands.mark(type)));
  return button;
}

function updateMarkButtons(view) {
  if (!view.surface) return;
  const inNote = view.surface.inNote();
  document.querySelectorAll(".toolbar .note-move").forEach((b) => { b.disabled = !inNote; });
  const active = view.surface.activeMarks();
  document.querySelectorAll(".toolbar [data-mark]").forEach((b) => {
    const on = active.has(b.dataset.mark);
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  });
}

function alignIcon(side) {
  // Four lines of text drawn in SVG (inline elements are allowed; inline styles are not).
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 14");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "14");
  svg.setAttribute("aria-hidden", "true");
  [[16, 2], [10, 6], [14, 10], [8, 13]].forEach(([width, y], i) => {
    const line = document.createElementNS(ns, "rect");
    if (side === "justify") width = 16;
    const x = side === "left" || side === "justify" ? 0 : side === "right" ? 16 - width : (16 - width) / 2;
    line.setAttribute("x", x); line.setAttribute("y", y - (i === 3 ? 0 : 1)); line.setAttribute("width", width); line.setAttribute("height", "1.6");
    line.setAttribute("fill", "currentColor");
    svg.append(line);
  });
  return svg;
}

function editTab(view) {
  const canEdit = app.me.roles.includes("reviewer");
  const mount = el("div", { class: `surface layout ${remember("restored") === "1" ? "show-restored" : ""}` });
  const surface = window.ReviewEditor.createEditor(mount, {
    units: view.units,
    machineUnits: view.machineUnits,
    flags: view.page.flags.filter((f) => !view.resolutions[f.id] && (view.showLow || f.severity !== "low")),
    editable: canEdit,
    onChange: () => changed(view),
    onFlagClick: (id, rect) => openFlag(view, id, rect),
    onUnitFocus: (index) => { const units = view.surface ? view.surface.units() : view.units; showRegion(view, units[index] ? regionFor(view, units[index]) : null); },
    onSelect: () => setTimeout(() => { showLine(view); updateMarkButtons(view); }, 0),
    onMarkerEdit: (value, apply) => { const next = prompt("Section or paragraph number (empty to remove):", value); if (next !== null) apply(next.trim()); },
  });
  view.surface = surface;
  const C = window.ReviewEditor.commands;
  const run = (command) => () => surface.run(command);
  const toolbar = el("div", { class: "toolbar" },
    el("button", { class: "small", title: "Heading (Ctrl/Cmd+Alt+1)", text: "Heading", on: { click: run(C.kind("heading")) } }),
    el("button", { class: "small", title: "Paragraph (Ctrl/Cmd+Alt+2)", text: "Paragraph", on: { click: run(C.kind("paragraph")) } }),
    el("button", { class: "small", title: "List item (Ctrl/Cmd+Alt+3)", text: "List item", on: { click: run(C.kind("list_item")) } }),
    el("span", { class: "sep" }),
    markButton(surface, "strong", "B", "Bold (Ctrl/Cmd+B)"),
    markButton(surface, "em", "I", "Italic (Ctrl/Cmd+I)"),
    markButton(surface, "underline", "U", "Underline (Ctrl/Cmd+U)"),
    markButton(surface, "sup", "x²", "Superscript (Ctrl/Cmd+.)"),
    markButton(surface, "sub", "x₂", "Subscript (Ctrl/Cmd+,)"),
    el("span", { class: "sep" }),
    el("button", { class: "small icon", title: "Align left (Ctrl/Cmd+Alt+L)", "aria-label": "Align left", on: { click: run(C.align("left")) } }, alignIcon("left")),
    el("button", { class: "small icon", title: "Centre (Ctrl/Cmd+Alt+E)", "aria-label": "Centre", on: { click: run(C.align("center")) } }, alignIcon("center")),
    el("button", { class: "small icon", title: "Align right (Ctrl/Cmd+Alt+R)", "aria-label": "Align right", on: { click: run(C.align("right")) } }, alignIcon("right")),
    el("button", { class: "small icon", title: "Justify (Ctrl/Cmd+Alt+J). Select several paragraphs, or Ctrl/Cmd+A, to change them all.", "aria-label": "Justify", on: { click: run(C.align("justify")) } }, alignIcon("justify")),
    el("span", { class: "sep" }),
    el("button", { class: "small", title: "Outdent (Shift+Tab)", text: "⇤", on: { click: run(C.level(-1)) } }),
    el("button", { class: "small", title: "Indent (Tab)", text: "⇥", on: { click: run(C.level(1)) } }),
    el("button", { class: "small", title: "Add a margin note (Ctrl/Cmd+Alt+N)", text: "+ Margin note", on: { click: run(C.addNote) } }),
    el("button", { class: "small note-move", title: "Move this margin note to the previous paragraph (Alt+Shift+↑)", text: "Note ↑", disabled: true, on: { click: run(C.moveNote(-1)) } }),
    el("button", { class: "small note-move", title: "Move this margin note to the next paragraph (Alt+Shift+↓)", text: "Note ↓", disabled: true, on: { click: run(C.moveNote(1)) } }),
    el("button", { class: "small", title: "Insert a table after this unit", text: "+ Table", on: { click: run(C.insertTable) } }),
    el("button", { class: "small", title: "Add a row below (in a table)", text: "+ Row", on: { click: run(C.addRow) } }),
    el("button", { class: "small", title: "Add a column (in a table)", text: "+ Column", on: { click: run(C.addColumn) } }),
    el("span", { class: "sep" }),
    el("button", { class: "small", title: "Undo (Ctrl/Cmd+Z)", text: "↶", on: { click: run(C.undo) } }),
    el("button", { class: "small", title: "Redo (Ctrl/Cmd+Shift+Z)", text: "↷", on: { click: run(C.redo) } }),
    el("label", { class: "muted small", title: "Numbers the main reading missed and the check readings supplied" },
      el("input", { type: "checkbox", checked: remember("restored") === "1", on: { change: (e) => { remember("restored", e.target.checked ? "1" : "0"); mount.classList.toggle("show-restored", e.target.checked); } } }),
      " mark restored numbers"),
    el("details", { class: "keys" }, el("summary", { text: "Keys" }),
      el("div", { class: "muted" },
        el("p", { text: "Type to correct words. Enter splits a paragraph; Backspace at its start joins it to the one above. Tab / Shift+Tab change the level. Ctrl/Cmd+Alt+L, E, R, J align left, centre, right or justify; with several paragraphs selected (or Ctrl/Cmd+A) they all change. Double-click a section number to change it." }),
        el("p", { text: "Margin notes: click into a note, then Note ↑ / Note ↓ (Alt+Shift+↑ / ↓) moves it to the paragraph it belongs beside." }),
        el("p", { text: "Flags: Alt+N next, Alt+P previous; then Alt+1 / Alt+2 use that reading, Alt+K keep as read, Alt+E I corrected it, Alt+D dismiss. Alt+L keeps all punctuation-only differences." }),
        el("p", { text: "Ctrl/Cmd+S save as checked; Ctrl/Cmd+Enter save as checked and go to the next page. Ctrl/Cmd + scroll zooms the page image." }))));
  const list = flagList(view);
  return [list, toolbar, mount, saveBar(view)];
}

// A slim bar above the text: flags are worked where they are highlighted, details in a pop-up.
function flagList(view) {
  const all = view.page.flags;
  const shown = all.filter((f) => view.showLow || f.severity !== "low");
  const open = shown.filter((f) => !view.resolutions[f.id]);
  const high = open.filter((f) => f.severity === "high").length;
  const openLow = all.filter((f) => f.severity === "low" && !view.resolutions[f.id]).length;
  const offText = open.filter((f) => !(view.surface && view.surface.highlighted(f.id)));
  const bar = el("div", { class: "flag-bar" });
  if (!open.length) {
    bar.append(el("span", { class: "muted", text: all.length ? "All flags on this page are resolved." : "No flags on this page — compare the text with the image." }));
  } else {
    bar.append(
      el("strong", { text: `${open.length} open flag${open.length === 1 ? "" : "s"}` }),
      high ? pill("high", `${high} high`) : null,
      el("button", { class: "small", title: "Previous flag (Alt+P)", text: "◀", on: { click: () => gotoFlag(view, -1) } }),
      el("button", { class: "small primary", title: "Next flag (Alt+N)", text: "Next flag ▶", on: { click: () => gotoFlag(view, 1) } }));
  }
  for (const flag of offText) {
    bar.append(el("button", { class: `small chip ${flag.severity}`, title: "Not highlighted in the text: open it here",
      text: `${flag.severity}: ${(flag.primary || Object.values(flag.checks).find(Boolean) || "missing words").slice(0, 24)}`,
      on: { click: (event) => openFlag(view, flag.id, event.currentTarget.getBoundingClientRect()) } }));
  }
  bar.append(el("span", { class: "spacer" }));
  if (openLow) bar.append(el("button", { class: "small", text: `Keep ${openLow} punctuation-only (Alt+L)`, on: { click: () => resolveLow(view) } }));
  if (all.some((f) => f.severity === "low")) {
    bar.append(el("label", { class: "muted small" }, el("input", { type: "checkbox", checked: view.showLow, on: { change: (e) => { view.showLow = e.target.checked; currentUnits(view); view.showTab("edit"); } } }), " show punctuation-only"));
  }
  return bar;
}

function flagCard(view, flag, inList) {
  const resolution = view.resolutions[flag.id];
  const highlighted = view.surface && view.surface.highlighted(flag.id);
  const close = () => { document.querySelectorAll(".popover").forEach((p) => p.remove()); };
  const resolve = (decision, reader = "") => {
    view.resolutions[flag.id] = { decision, reader, note: "" };
    if (view.surface && decision !== "use_check") view.surface.resolve(flag.id);
    changed(view); close(); refreshFlags(view);
  };
  const reads = el("div", { class: "reads" }, el("span", { class: "muted", text: "Main reading" }), el("q", { text: flag.primary || "—" }), el("span"));
  for (const [reader, text] of Object.entries(flag.checks)) {
    const usable = !resolution && highlighted && view.surface.applicable(flag.id)
      && !(flag.kind === "missing_from_primary" && !text) && !(flag.kind === "marker_conflict" && !/^\S{1,12}$/.test(text || ""));
    const label = flag.kind === "marker_conflict" ? `Number ${text}` : text ? `Use ${reader}` : "Remove words";
    reads.append(el("span", { class: "muted", text: reader }), el("q", { text: text || "—" }),
      usable ? el("button", { class: "small", text: label, on: { click: () => { if (view.surface.applyReading(flag.id, text)) resolve("use_check", reader); } } }) : el("span"));
  }
  const actions = resolution
    ? el("div", { class: "actions" }, pill("ok", DECISION_LABEL[resolution.decision] + (resolution.reader ? ` (${resolution.reader})` : "")),
        el("button", { class: "small", text: "Reopen", on: { click: () => { delete view.resolutions[flag.id]; changed(view); currentUnits(view); view.showTab("edit"); } } }))
    : el("div", { class: "actions" },
        el("button", { class: "small", text: "Keep as read (printed so)", on: { click: () => resolve("keep") } }),
        el("button", { class: "small", text: "I corrected it", on: { click: () => resolve("edited") } }),
        el("button", { class: "small", text: "Dismiss", on: { click: () => resolve("dismissed") } }));
  return el("div", { class: `flag ${flag.severity} ${resolution ? "resolved" : ""}`, "data-card": flag.id },
    el("div", {}, pill(flag.severity, flag.severity), " ", KIND_LABEL[flag.kind] || flag.kind,
      !highlighted && !resolution ? el("span", { class: "muted", text: ` · ${whyNotHighlighted(view, flag)}` }) : null),
    reads, actions);
}

function whyNotHighlighted(view, flag) {
  if (flag.kind === "marker_conflict") return "the unit it belongs to has been edited: set the number by hand";
  const units = view.surface ? view.surface.units() : view.units;
  const edited = units.length !== view.machineUnits.length || units.some((u, i) => JSON.stringify(u) !== JSON.stringify(view.machineUnits[i]));
  return edited ? "not highlighted: the text around it has been edited" : "not highlighted: it covers a number or crosses into the next unit";
}

function refreshFlags(view) {
  const bar = document.querySelector(".flag-bar");
  if (bar) bar.replaceWith(flagList(view));
}

function openFlag(view, id, rect) {
  document.querySelectorAll(".popover").forEach((p) => p.remove());
  const flag = view.page.flags.find((f) => f.id === id);
  if (!flag) return;
  view.openFlagId = id;
  const pop = el("div", { class: "popover" }, flagCard(view, flag, false),
    el("button", { class: "small close", text: "Close", on: { click: () => pop.remove() } }));
  document.body.append(pop);
  const top = window.scrollY + rect.bottom + 6;
  const left = Math.min(window.scrollX + rect.left, window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 12);
  Object.assign(pop.style, { top: `${top}px`, left: `${Math.max(8, left)}px` });
}

document.addEventListener("keydown", (event) => { if (event.key === "Escape") document.querySelectorAll(".popover").forEach((p) => p.remove()); });

// --- changes against the machine reading --------------------------------------------------------------

function diffWords(before, after) {
  const a = words(before), b = words(after);
  const n = a.length, m = b.length;
  const table = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push(["same", a[i]]); i++; j++; }
    else if (table[i + 1][j] >= table[i][j + 1]) out.push(["del", a[i++]]);
    else out.push(["ins", b[j++]]);
  }
  while (i < n) out.push(["del", a[i++]]);
  while (j < m) out.push(["ins", b[j++]]);
  return out;
}

function changesView(view) {
  const machine = view.machineUnits.map(unitText).join("\n");
  const current = currentUnits(view).map(unitText).join("\n");
  const parts = diffWords(machine, current);
  const changes = parts.filter(([kind]) => kind !== "same").length;
  const box = el("div", { class: "card changes" },
    el("p", { class: "muted", text: changes ? `${changes} word changes from the machine reading (struck out: removed; underlined: added). Structure changes are not shown here.` : "No word changes from the machine reading." }));
  const text = el("p", { class: "diff" });
  for (const [kind, word] of parts) {
    text.append(kind === "same" ? document.createTextNode(`${word} `) : el(kind === "ins" ? "ins" : "del", { text: word }), kind === "same" ? "" : " ");
  }
  box.append(text);
  return box;
}

// --- saving -------------------------------------------------------------------------------------------

function saveBar(view) {
  const comment = el("textarea", { rows: 1, value: view.comment, placeholder: "Comment for this revision (optional)", on: { input: (e) => { view.comment = e.target.value; changed(view); } } });
  const save = async (status, thenNext = false) => {
    const high = view.page.flags.filter((f) => f.severity === "high" && !view.resolutions[f.id]);
    if (status === "checked" && high.length && !confirm(`${high.length} high flag(s) are not resolved. Save as checked anyway? (The document cannot be approved until they are.)`)) return;
    const result = await api(`/api/documents/${encodeURIComponent(view.actId)}/pages/${view.pageNumber}`, {
      method: "POST",
      body: { expected_revision: view.expected, units: currentUnits(view), status, resolutions: view.resolutions, comment: view.comment },
    });
    if (result.status === 409) { toast("Someone saved this page first. Reloading their version."); app.dirty = false; return pageView(view.actId, view.pageNumber); }
    if (!result.ok) return toast(message(result));
    app.dirty = false;
    clearDraft(view);
    toast(`Saved revision ${result.data.revision}`);
    const numbers = view.detail.pages.map((p) => p.page);
    const next = numbers[numbers.indexOf(view.pageNumber) + 1];
    if (thenNext && next) location.hash = `#/doc/${encodeURIComponent(view.actId)}/page/${next}`;
    else pageView(view.actId, view.pageNumber);
  };
  const restore = () => {
    if (!confirm("Replace the text with the machine reading? (Nothing is saved until you save.)")) return;
    view.units = structuredClone(view.machineUnits);
    view.resolutions = {};
    changed(view); view.showTab("edit");
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
  setCrumbs(el("a", { href: "#/", text: "Home" }), el("a", { href: `#/doc/${encodeURIComponent(actId)}`, text: actId }), "Activity");
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
    if (parts[0] === "doc" && parts[2] === "page") return await pageView(parts[1], Number(parts[3]));
    if (parts[0] === "doc" && parts[2] === "read") return await readView(parts[1]);
    if (parts[0] === "doc" && parts[2] === "details") return await detailsView(parts[1]);
    if (parts[0] === "doc" && parts[1]) return await actView(parts[1]);
    if (parts[0] === "audit" && parts[1]) return await auditView(parts[1]);
    return await dashboard();
  } catch (error) {
    if (error.message !== "signed out") toast(error.message);
  }
}

window.addEventListener("beforeunload", (event) => { if (app.dirty) { event.preventDefault(); event.returnValue = ""; } });
document.addEventListener("keydown", (event) => {
  if (!(event.metaKey || event.ctrlKey) || !app.view || !location.hash.includes("/page/")) return;
  if (event.key === "s") { event.preventDefault(); document.querySelector(".savebar .primary")?.click(); }
  if (event.key === "Enter") { event.preventDefault(); [...document.querySelectorAll(".savebar button")].find((b) => b.textContent.startsWith("Checked →"))?.click(); }
}, true);

async function start() {
  const signedOut = await loadMe();
  if (signedOut) return showSignIn(signedOut);
  if (!start.listening) { window.addEventListener("hashchange", route); start.listening = true; }
  route();
}

start();
