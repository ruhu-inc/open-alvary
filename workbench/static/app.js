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
  not_aligned: "articles differ between the language versions",
  heading_form: "article heading printed in an unusual form",
  heading_long: "check where this heading ends",
  borrowed_ocr: "the page is a picture; its text came from someone else's OCR: check it word by word",
  one_reading: "the page is a picture read by our OCR; no second reading has checked its words",
  title_from_listing: "no title in the text was recognised: the title is the source's file name; check it against the page",
  unsure_words_dropped: "words the OCR was unsure of and the second reading did not see (often the other side of the sheet showing through) were left out",
  number_differs: "the number is printed differently in two places",
  reading_differs: "scanned page: Document AI read words differently from the OCR layer; Document AI's are shown",
  accents_restored: "scanned page: accents restored by Document AI where the OCR layer dropped them",
  text_layer_differs: "the PDF's own text reads differently from the page image (Google's OCR); the PDF's words are kept: check them against the image",
  accents_missing: "the page prints accents the PDF's own text lacks: check them against the image",
  check_unclear: "the image is too unclear here for Google's OCR to check the PDF's words: check them against the image",
};
const DECISION_LABEL = { keep: "kept as read", use_check: "used check reading", edited: "edited by hand", dismissed: "dismissed" };
// What a page still needs (/api/documents/{id} pages[].todo); audit pages are "review" pages too.
const TODO_LABEL = { review: "to review against the image", flags: "flags to resolve" };

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
  // The tab names the screen, most specific first: "Page 2 · Electricity Act, 2023 · Open Alvary Review".
  const names = parts.map((part) => (typeof part === "string" ? part : part.textContent || "")).filter(Boolean);
  document.title = [...names.reverse().slice(0, 2), "Open Alvary Review"].join(" · ");
}

function main(...children) {
  document.getElementById("main").replaceChildren(...children);
  window.scrollTo(0, 0);
}

const pill = (cls, text) => el("span", { class: `pill ${cls}`, text });
// An operator by display name where the workbench knows it (app.names, from /api/overview).
const operatorName = (email) => (app.names || {})[email] || (email || "").split("@")[0];

// --- sign-in -------------------------------------------------------------------------------------

async function loadMe() {
  const result = await api("/api/me");
  if (!result.ok) return result.data || {};
  app.me = result.data;
  app.csrf = result.data.csrf;
  const who = document.getElementById("who");
  who.replaceChildren(
    el("a", { class: "nav-link", href: "#/", text: "Documents" }),
    el("a", { class: "nav-link", href: "#/queue", text: "Work queue" }),
    result.data.access ? el("a", { class: "nav-link", href: "#/access", text: "Access" }) : "",
    result.data.operators ? el("a", { class: "nav-link", href: "#/operators", text: "Operators" }) : "",
    el("span", { class: "who-name", text: result.data.name, title: `${result.data.email} · ${result.data.roles.join(", ")}` }),
    el("button", { class: "small", text: "Sign out", on: { click: signOut } }),
  );
  return null;
}

async function signOut() {
  if (app.dirty && !confirm("Discard unsaved changes and sign out?")) return;
  await api("/auth/logout", { method: "POST" });
  app.dirty = false;
  location.replace("/?signed_out=1");
}

// Why a sign-in ended back here (/auth/callback sends a code, never free text: a link cannot put
// words of its choosing on this page).
const SIGNIN_ERRORS = {
  other_browser: "Sign-in has to finish in the browser that started it. Start again here.",
  expired: "That sign-in took too long or was already used. Start again.",
  not_operator: "This Google account is not on the operator list. Ask an administrator to add you.",
  unverified: "Google has not verified this account's email address.",
  failed: "Sign-in did not complete. Start again; if it keeps failing, tell an administrator.",
};

function brandMark(size = 36) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [k, v] of Object.entries({ class: "brand-mark", width: size, height: size, viewBox: "0 0 64 64", "aria-hidden": "true", focusable: "false" })) svg.setAttribute(k, v);
  const g = document.createElementNS(ns, "g");
  // The site's mark (web/src/site.tsx, MARK_LINES.regular).
  g.setAttribute("fill", "none"); g.setAttribute("stroke", "currentColor"); g.setAttribute("stroke-width", "2.2");
  for (const points of ["32,6.59 19.59,33.04 32,41.01 44.41,33.04", "17.56,37.37 28.81,44.31 8.31,57.1", "46.44,37.37 55.69,57.1 35.19,44.31"]) {
    const polygon = document.createElementNS(ns, "polygon"); polygon.setAttribute("points", points); g.append(polygon);
  }
  svg.append(g);
  return svg;
}

async function showSignIn(info) {
  info = info || (await api("/api/me")).data || {};
  document.getElementById("who").replaceChildren();
  setCrumbs("");
  document.title = "Sign in · Open Alvary Review";
  const query = new URLSearchParams(location.search);
  const problem = SIGNIN_ERRORS[query.get("signin")];
  const panel = el("div", { class: "signin-panel" },
    el("div", { class: "signin-brand" }, brandMark(36), el("span", { class: "brand-name", text: "Open Alvary" })),
    el("h1", { text: "Review workbench" }),
    el("p", { class: "muted", text: "Check machine readings of legal documents against the original, correct them, and approve them for publication." }),
    problem ? el("p", { class: "signin-error", role: "alert", text: problem }) : null,
    !problem && query.has("signed_out") ? el("p", { class: "signin-notice", role: "status", text: "You are signed out." }) : null);
  if (query.has("signin") || query.has("signed_out")) history.replaceState(null, "", "/" + location.hash);
  if (info.oidc) {
    panel.append(el("a", { class: "signin-button", href: "/auth/login" }, googleMark(), el("span", { text: "Sign in with Google" })));
  }
  if (info.dev_login) {
    const email = el("input", { type: "email", placeholder: "you@organisation.org", required: true, autocomplete: "username", "aria-label": "Operator email" });
    const error = el("p", { class: "signin-error", role: "alert", hidden: true });
    panel.append(
      info.oidc ? el("div", { class: "signin-or", text: "or, on this computer only" }) : el("p", { class: "signin-note", text: "Local trial sign-in: this computer only, no password." }),
      error,
      el("form", { class: "signin-form", on: { submit: async (event) => {
        event.preventDefault();
        const result = await api("/auth/dev", { method: "POST", body: { email: email.value } });
        if (!result.ok) { error.textContent = result.status === 403 ? "That email is not on the operator list." : message(result); error.hidden = false; return; }
        await start();
      } } }, email, el("button", { class: "primary", text: "Continue" })));
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

const TYPE_LABEL = {
  act: "Act", bill: "Bill", regulation: "Regulation", order: "Order", judgment: "Judgment", gazette: "Gazette", treaty: "Treaty", other: "Document",
  constitution: "Constitution", law: "Law", organic_law: "Organic law", decree_law: "Decree-law", ordinance: "Ordinance",
  decree: "Decree", royal_decree: "Royal decree", presidential_order: "Presidential Order", prime_ministers_order: "Prime Minister's Order",
  ministerial_order: "Ministerial Order", regulations: "Regulations", instructions: "Instructions", agreement: "Agreement", convention: "Convention",
};
const ACTION_LABEL = { page_saved: "saved", details_saved: "saved the details of", act_approved: "approved", act_revoked: "revoked approval of", signed_in: "signed in", signed_out: "signed out", operator_saved: "updated operator", sign_in_refused: "was refused sign-in" };

const STATUS_CLASS = { "Approved": "ok", "Approval out of date": "warn", "Ready for approval": "ok", "Not started": "medium", "In review": "medium" };

function ago(iso) {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  const steps = [[60, "minute"], [3600, "hour"], [86400, "day"], [604800, "week"]];
  let unit = ["minute", 60];
  for (const [size, name] of steps) if (seconds >= size) unit = [name, size];
  const n = Math.floor(seconds / unit[1]);
  return `${n} ${unit[0]}${n === 1 ? "" : "s"} ago`;
}

// Page numbers as runs: "1, 3–6".
function pageRuns(pages) {
  const sorted = [...pages].sort((a, b) => a - b);
  const runs = [];
  for (const page of sorted) {
    const run = runs[runs.length - 1];
    if (run && page === run[1] + 1) run[1] = page; else runs.push([page, page]);
  }
  return runs.map(([a, b]) => (a === b ? String(a) : `${a}–${b}`)).join(", ");
}

function remember(key, value) {
  try { if (value === undefined) return localStorage.getItem(`oa-home:${key}`); localStorage.setItem(`oa-home:${key}`, value); } catch { return null; }
  return null;
}


// How this document's text was read, in plain words: its own text layer, or for scanned pages
// Google's OCR, checked by the Layout Parser and against the other language versions.
function readingDescription(data) {
  const primary = data.readers.primary;
  const checks = data.readers.checks || {};
  if (primary.reader !== "text-layer") {
    return `Primary reading: Document AI ${primary.version.version}. Checks: ${Object.keys(checks).join(", ")}.`;
  }
  const pages = new Set(data.pages.map((p) => p.page));
  const ocr = (checks.scans_read_by_ocr || []).filter((n) => pages.has(n)).length;
  const layout = (checks.scans_read_by_docai || []).filter((n) => pages.has(n)).length;
  let text = !ocr ? "Read from the PDF's own text layer"
    : ocr === pages.size ? "Scanned pages, read by Google's OCR"
    : `Read from the PDF's own text layer; its ${ocr} scanned page${ocr === 1 ? "" : "s"} read by Google's OCR`;
  const checked = [];
  if (layout) checked.push("word by word against a second reading (Document AI Layout Parser)");
  if (checks.alignment) checked.push("against the other language versions");
  return `${text}${checked.length ? `, checked ${checked.join(" and ")}` : ""}.`;
}

function meter(share) {
  const bar = el("span");
  bar.style.width = `${share}%`; // CSSOM, allowed by the strict content policy
  return el("div", { class: "bar-meter", title: `${share}% checked` }, bar);
}

async function dashboard() {
  setCrumbs("");
  document.title = "Documents · Open Alvary Review";
  const { data } = await api("/api/overview");
  const t = data.totals;
  const share = t.pages ? Math.round((100 * t.checked) / t.pages) : 0;
  const tiles = el("div", { class: "tiles" },
    el("div", { class: "tile" }, el("span", { class: "big", text: t.documents.toLocaleString() }), el("span", { text: "documents" })),
    el("div", { class: "tile" }, el("span", { class: "big", text: `${share}%` }), el("span", { text: `${t.checked.toLocaleString()} of ${t.pages.toLocaleString()} pages checked` }), meter(share)),
    el("div", { class: `tile ${t.open_high ? "alert" : ""}` }, el("span", { class: "big", text: t.open_high.toLocaleString() }), el("span", { text: "high flags open" })),
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
  const needs = data.attention.length
    ? el("div", { class: "card" }, el("h3", { text: "Needs attention" }),
        el("ul", { class: "plain" }, data.attention.map((d) => el("li", {},
          el("a", { href: `#/doc/${encodeURIComponent(d.id)}`, text: d.title }), " ", ...reasons(d).flatMap((r) => [r, " "])))))
    : null;

  // The list is searched, filtered and paged on the server: a corpus holds tens of thousands.
  const search = el("input", { type: "search", placeholder: "Search titles and ids", value: remember("q") || "" });
  const typeSelect = el("select", {}, el("option", { value: "", text: "All types" }), data.types.map((type) => el("option", { value: type, text: TYPE_LABEL[type] || type.replace(/_/g, " ") })));
  const languageSelect = el("select", {}, el("option", { value: "", text: "All languages" }), data.languages.map((code) => el("option", { value: code, text: LANGUAGE_NAMES[code] || code })));
  const statusSelect = el("select", {}, ["All statuses", "Not started", "In review", "Ready for approval", "Approved", "Approval out of date"].map((label, i) => el("option", { value: i ? label : "", text: label })));
  const sortSelect = el("select", {}, [["recent", "Recently active"], ["title", "Title"], ["flags", "Most high flags"], ["progress", "Least checked"]].map(([value, label]) => el("option", { value, text: label })));
  typeSelect.value = remember("type") || ""; languageSelect.value = remember("language") || ""; statusSelect.value = remember("status") || ""; sortSelect.value = remember("sort") || "recent";
  const cards = el("div", { class: "doc-cards" });
  const pager = el("div", { class: "pager" });
  const PAGE = 48;
  let offset = 0, asked = 0;
  const load = async () => {
    remember("q", search.value); remember("type", typeSelect.value); remember("language", languageSelect.value); remember("status", statusSelect.value); remember("sort", sortSelect.value);
    const params = new URLSearchParams({ q: search.value.trim(), type: typeSelect.value, language: languageSelect.value, status: statusSelect.value, sort: sortSelect.value, offset: String(offset), limit: String(PAGE) });
    const ticket = ++asked;
    const { data: page } = await api(`/api/documents?${params}`);
    if (ticket !== asked) return; // a later search has been asked since
    cards.replaceChildren(...(page.documents.length ? page.documents.map((d) => {
      const pct = d.pages ? Math.round((100 * d.checked) / d.pages) : 0;
      return el("a", { class: "doc-card", href: `#/doc/${encodeURIComponent(d.id)}` },
        el("div", { class: "doc-meta" }, el("span", { class: "type", text: TYPE_LABEL[d.type] || d.type.replace(/_/g, " ") }), d.year ? el("span", { text: String(d.year) }) : null, d.language ? el("span", { text: d.language.toUpperCase() }) : null, d.jurisdiction ? el("span", { text: d.jurisdiction.toUpperCase() }) : null),
        el("strong", { class: "doc-title", text: d.title }),
        meter(pct),
        el("div", { class: "doc-foot" }, el("span", { class: "muted", text: `${d.checked} / ${d.pages} pages checked` }), pill(STATUS_CLASS[d.status] || "medium", d.status), d.open_high ? pill("high", `${d.open_high} high`) : null),
        el("div", { class: "muted small", text: d.last ? `Last activity: ${operatorName(d.last.actor)}, ${ago(d.last.at)}` : "No review activity yet" }));
    }) : [el("p", { class: "muted", text: "No documents match." })]));
    const last = Math.min(page.offset + page.documents.length, page.total);
    pager.replaceChildren(
      el("span", { class: "muted", text: page.total ? `${(page.offset + 1).toLocaleString()}–${last.toLocaleString()} of ${page.total.toLocaleString()}` : "" }),
      el("button", { class: "small", text: "← Previous", disabled: page.offset === 0, on: { click: () => { offset = Math.max(0, offset - PAGE); load(); } } }),
      el("button", { class: "small", text: "Next →", disabled: last >= page.total, on: { click: () => { offset += PAGE; load(); } } }));
  };
  let typing;
  search.addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(() => { offset = 0; load(); }, 250); });
  for (const control of [typeSelect, languageSelect, statusSelect, sortSelect]) control.addEventListener("input", () => { offset = 0; load(); });
  load();

  app.names = data.names || {};
  const titleOf = (e) => e.title || e.id || "";
  // Consecutive saves of one document by one person read as one line: "saved pages 1, 3–6".
  const groups = [];
  for (const e of data.activity) {
    const last = groups[groups.length - 1];
    if (last && last.actor === e.actor && last.action === e.action && last.id === e.id) {
      if (e.page) last.pages.add(e.page);
    } else {
      groups.push({ ...e, pages: new Set(e.page ? [e.page] : []) });
    }
  }
  const pageList = pageRuns;
  const activity = el("div", { class: "card activity" }, el("h3", { text: "Recent activity" }),
    groups.length ? el("ul", { class: "plain" }, groups.slice(0, 10).map((g) => el("li", {},
      el("div", {},
        el("strong", { text: operatorName(g.actor) }), ` ${ACTION_LABEL[g.action] || g.action.replace(/_/g, " ")} `,
        g.pages.size && g.id
          ? el("a", { href: `#/doc/${encodeURIComponent(g.id)}/page/${Math.max(...g.pages)}`, text: `${g.pages.size > 1 ? "pages" : "page"} ${pageList(g.pages)}` })
          : ""),
      titleOf(g) ? el("div", { class: "activity-title", text: titleOf(g) }) : "",
      el("div", { class: "muted small", text: ago(g.at) })))) : el("p", { class: "muted", text: "Nothing yet." }));

  main(el("div", { class: "home" },
    el("div", { class: "home-main" },
      el("div", { class: "home-head" }, el("h1", { text: "Review workbench" })),
      tiles, resume, needs,
      el("div", { class: "card" },
        el("div", { class: "doc-tools" }, el("h3", { text: "Documents" }), search, typeSelect, languageSelect, statusSelect, sortSelect),
        cards, pager)),
    el("aside", { class: "home-side" }, activity)));
}

// --- one document: progress, pages, approval ------------------------------------------------------------

async function actView(actId) {
  const { data } = await api(`/api/documents/${encodeURIComponent(actId)}`);
  const act = data.act;
  setCrumbs(el("a", { href: "#/", text: "Documents" }), act.title);
  const ready = data.readiness;
  // Under a routing decision each page says what it still needs: the whole page, or its flags. An audit
  // page is a page to review like any other (CPR-01: the reviewer is not told it was sampled).
  const routed = (page) => (!ready.routed ? "" : page.todo ? `todo-${page.todo}` : page.status === "checked" ? "" : "settled");
  const grid = el("div", { class: "pages" }, data.pages.map((page) => {
    const classes = [page.status || "", page.open_high ? "high" : "", page.stale ? "stale" : "", routed(page)].join(" ");
    const need = ready.routed ? (TODO_LABEL[page.todo] || (page.status === "checked" ? "" : "nothing to review")) : "";
    const tip = `${page.status || "not reviewed"}${need ? `, ${need}` : ""}${page.open_high ? `, ${page.open_high} high flags open` : ""}${page.stale ? ", reading changed" : ""}`;
    return el("a", { class: classes, href: `#/doc/${encodeURIComponent(actId)}/page/${page.page}`, title: tip, text: String(page.page) });
  }));
  const nextTodo = data.pages.find((p) => p.todo);
  const head = data.approvals[0];
  const current = data.approved; // the approval covers exactly the current text and confirmed details
  main(
    el("div", { class: "card" },
      el("h2", { text: act.title }),
      el("p", { class: "muted", text: readingDescription(data) }),
      standingPanel(data.standing),
      approvalChecklist(actId, data, current, head),
      el("p", {},
        nextTodo ? el("a", { href: `#/doc/${encodeURIComponent(actId)}/page/${nextTodo.page}` }, el("button", { class: "primary", text: `Continue reviewing at page ${nextTodo.page}` })) : null, " ",
        el("a", { href: `#/doc/${encodeURIComponent(actId)}/read` }, el("button", { text: "Read the whole document" })), " ",
        act.versions ? el("a", { href: `#/doc/${encodeURIComponent(actId)}/languages` }, el("button", { text: "Compare the language versions" })) : null, " ",
        previewLink(actId)),
      el("h3", { text: "Pages" }),
      el("p", { class: "muted", text: "Green: checked. Yellow: needs attention. Red border: high flags open. Dashed: the machine reading changed since review." }),
      ready.routed ? el("p", { class: "muted", text: "Blue border: to review against the image. Dotted blue border: only its flags to resolve. Faded: nothing to review (the readings agree, or every flag is resolved)." }) : null,
      grid,
    ),
    detailsSummary(actId, ready),
    editorialSummary(actId),
    act.type === "judgment" || act.type === "ruling" ? citationsSummary(actId) : null,
    textCheckCard(actId, data.text_check),
    findReplaceCard(actId),
    approvalPanel(actId, data, current),
  );
}

// Whether the document is current, as the collection's texts and RLRC's listing tell: repealed (and by
// what, and the law in force now), in force, or not yet known; what amends it; a law it was made under
// that is since repealed; the instruments made under it. Each other document opens from its name.
function standingPanel(standing) {
  if (!standing) return null;
  const doc = (item) => (item.id ? el("a", { href: `#/doc/${encodeURIComponent(item.id)}`, text: item.title }) : el("span", { text: item.title }));
  const list = (items) => items.flatMap((item, i) => [i ? "; " : "", doc(item)]);
  const lines = [];
  if (standing.status === "repealed" && standing.repealed_by) {
    lines.push(el("p", {}, el("strong", { text: "Repealed" }), " by ", ...list(standing.repealed_by), "."));
    if (standing.latest) lines.push(el("p", {}, "That law was repealed in turn. The law in force now: ", doc(standing.latest), "."));
    if (standing.disagrees) lines.push(el("p", { class: "warn", text: `But ${standing.disagrees}: check which is right before confirming the status.` }));
  } else if (standing.status === "repealed") {
    lines.push(el("p", {}, el("strong", { text: "Repealed" }), ", as RLRC's listing files it. No later text in the collection names it as repealed."));
  } else if (standing.status === "in_force") {
    lines.push(el("p", {}, el("strong", { text: "In force" }), ", as RLRC's listing files it; no later text in the collection repeals it."));
  } else {
    lines.push(el("p", {}, el("strong", { text: "Not yet known" }), " whether it is in force: no later text in the collection repeals it, and RLRC's listing does not file it."));
  }
  if (standing.amended_by) lines.push(el("p", {}, "Amended by ", ...list(standing.amended_by), ". The text here is as published, before those amendments."));
  for (const parent of standing.parents_repealed || []) {
    lines.push(el("p", { class: "warn" }, "Made under ", doc(parent), ", which ", ...list(parent.repealed_by), " has since repealed: check it is still in force."));
  }
  if (standing.enables) {
    const more = standing.enables.total - standing.enables.shown.length;
    lines.push(el("details", { class: "enables" },
      el("summary", { text: `${standing.enables.total} instrument${standing.enables.total === 1 ? " is" : "s are"} made under it` }),
      el("ul", {}, standing.enables.shown.map((item) => el("li", {}, doc(item)))),
      more > 0 ? el("p", { class: "muted", text: `and ${more} more` }) : null));
  }
  return el("div", { class: "standing" }, el("h3", { text: "Where it stands" }), ...lines);
}

// The steps to approval, each done or with what is left and a link to it.
function approvalChecklist(actId, data, current, head) {
  const ready = data.readiness;
  const page = (n) => el("a", { href: `#/doc/${encodeURIComponent(actId)}/page/${n}`, text: `page ${n}` });
  const step = (done, label, action, tone = "") =>
    el("li", { class: done ? "done" : tone || "todo" },
      el("span", { class: "tick", "aria-hidden": "true", text: done ? "✓" : tone === "warn" ? "!" : "" }),
      el("span", {}, label, action ? el("span", { class: "muted" }, " · ", action) : null));
  const checked = ready.pages - ready.unchecked_pages.length;
  const highPages = unique(ready.open_high_flags.map((f) => f.page));
  const toApproval = el("button", { class: "small", text: "Go to approval", on: { click: () => document.getElementById("approval")?.scrollIntoView({ block: "start" }) } });
  const text = data.text_check;
  const toText = (label) => el("button", { class: "small", text: label, on: { click: () => document.getElementById("text-check")?.scrollIntoView({ block: "start" }) } });
  const toReview = ready.unchecked_pages, toResolve = ready.unresolved_flag_pages;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const steps = [
    ready.routed
      ? step(!toReview.length, toReview.length ? `${plural(toReview.length, "page", "pages")} to review against the image` : "Every page to review is checked",
        toReview.length ? el("span", {}, "next: ", page(toReview[0])) : null)
      : step(!ready.unchecked_pages.length, `${checked} of ${ready.pages} pages checked`,
        ready.unchecked_pages.length ? el("span", {}, "next: ", page(ready.unchecked_pages[0])) : null),
    ready.routed
      ? step(!toResolve.length, toResolve.length ? `Flags to resolve on ${plural(toResolve.length, "page", "pages")}` : "Every flag on the flagged pages resolved",
        toResolve.length ? el("span", {}, "next: ", page(toResolve[0])) : null)
      : null,
    step(!highPages.length, highPages.length ? `High flags open on ${highPages.length} page${highPages.length === 1 ? "" : "s"}` : "No high flags open",
      highPages.length ? el("span", {}, "first: ", page(highPages[0])) : null),
    step(!ready.stale_pages.length, ready.stale_pages.length ? `${ready.stale_pages.length} page${ready.stale_pages.length === 1 ? "" : "s"} to recheck: the reading changed` : "No page needs rechecking",
      ready.stale_pages.length ? el("span", {}, "first: ", page(ready.stale_pages[0])) : null, ready.stale_pages.length ? "warn" : ""),
    ready.missing_checks.length ? step(false, `Check readings missing: ${ready.missing_checks.join(", ")}`, "rerun the batch", "warn") : null,
    text ? step(!text.errors.length,
      !text.rules ? "No structure rules for this kind of document: it cannot be approved until they are written"
        : text.errors.length ? `${text.errors.length} problem${text.errors.length === 1 ? "" : "s"} in the text's structure to fix` : `Text and structure: ${describeCounts(text.counts)}`,
      text.errors.length ? toText("See them") : text.warnings.length ? toText(`${text.warnings.length} to look at`) : null) : null,
    step(ready.details.confirmed, ready.details.confirmed ? "Details confirmed" : ready.details.status ? "Details not yet confirmed" : "Details not yet reviewed",
      ready.details.confirmed ? null : el("a", { href: `#/doc/${encodeURIComponent(actId)}/details`, text: "review the details" })),
    current ? step(true, "Approved", null)
      : head && head.decision === "approved" ? step(false, "Approval out of date: the text or details changed after it was approved", toApproval, "warn")
      : step(false, ready.ready ? "Ready for approval" : "Approval", ready.ready ? toApproval : el("span", { text: "after the steps above" })),
  ];
  return el("ol", { class: "checklist", "aria-label": "Steps to approval" }, steps);
}

// What publishing will write: the parts, sections and schedules found in the text as it now stands, and
// what to fix (errors block approval) or look at (warnings) first, each with its page.
const ISSUES = { // most consequential first
  no_structure_rules: "Layout not supported yet",
  join: "Page-break joins refused",
  note_without_words: "Margin notes beside no words",
  two_numbers: "Two numbers in a row",
  contents_unmatched: "Contents entries with no provision",
  repeated_locator: "Numbers that repeat",
  section_order: "Sections out of order",
  doubled_word: "Words doubled in editing",
  covered_cell_text: "Words in a merged cell's covered position",
  outside_section: "Numbered outside any section",
  outside_schedule: "Text before the first schedule",
  contents_unclosed: "Contents without enacting words after them",
  running_head: "Running heads left in the text",
  empty_table: "Tables with no words",
  empty_unit: "Empty units left out",
};
const ISSUE_ORDER = Object.keys(ISSUES);

function describeCounts(counts) {
  const parts = Object.entries(counts || {}).map(([kind, n]) => `${n} ${kind}${n === 1 ? "" : "s"}`);
  return parts.length ? parts.join(", ") : "no structure found";
}

function textCheckCard(actId, text) {
  if (!text) return null;
  const page = (n) => (n ? el("a", { href: `#/doc/${encodeURIComponent(actId)}/page/${n}`, text: `page ${n}` }) : null);
  const grouped = (issues, tone) => {
    const byCode = new Map();
    for (const issue of issues) byCode.set(issue.code, [...(byCode.get(issue.code) || []), issue]);
    const rank = (code) => (ISSUE_ORDER.includes(code) ? ISSUE_ORDER.indexOf(code) : ISSUE_ORDER.length);
    const ordered = [...byCode].sort(([a], [b]) => rank(a) - rank(b));
    return el("ul", { class: "issues" }, ordered.map(([code, list]) => el("li", { class: tone },
      el("details", { open: tone === "high" || undefined },
        el("summary", { text: `${ISSUES[code] || code} (${list.length})` }),
        el("ul", {}, list.slice(0, 25).map((issue) => el("li", {}, page(issue.page), page(issue.page) ? " · " : "", issue.message))),
        list.length > 25 ? el("p", { class: "muted", text: `and ${list.length - 25} more` }) : null))));
  };
  return el("div", { class: "card", id: "text-check" },
    el("h3", { text: "Text and structure" }),
    el("p", { class: "muted", text: "What publishing will write from the pages as they now stand. Errors must be fixed before approval; warnings are worth a look, because nobody reads the whole text." }),
    el("p", {}, `${describeCounts(text.counts)} · ${Math.max(1, Math.round(text.bytes / 1024))} KB of text · ${text.rules ? `rules ${text.rules}` : "no structure rules for this kind of document"}`),
    text.errors.length ? el("div", {}, el("h4", { text: "To fix before approval" }), grouped(text.errors, "high")) : null,
    text.warnings.length ? el("div", {}, el("h4", { text: "To look at" }), grouped(text.warnings, "warn")) : el("p", {}, pill("ok", "Nothing to look at")),
    el("p", {}, el("a", { href: `/api/documents/${encodeURIComponent(actId)}/canonical`, target: "_blank", rel: "noopener", text: "The text and structure as they would be published (JSON)" })));
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

// --- a law's language versions side by side ------------------------------------------------------

const LANGUAGE_NAMES = { rw: "Ikinyarwanda", en: "English", fr: "Français", sw: "Kiswahili" };

async function languagesView(actId) {
  const { ok, data } = await api(`/api/documents/${encodeURIComponent(actId)}/languages`);
  if (!ok) return main(el("p", { text: message({ ok, data }) }));
  const own = data.instrument[Object.keys(data.instrument).find((l) => data.instrument[l].id === actId)] || {};
  setCrumbs(el("a", { href: "#/", text: "Documents" }), el("a", { href: `#/doc/${encodeURIComponent(actId)}`, text: own.title || actId }), "Language versions");
  const versionLink = (language, page) => `#/doc/${encodeURIComponent(data.instrument[language].id)}/page/${page}`;
  const instrument = el("table", { class: "instrument" },
    el("thead", {}, el("tr", {}, el("th", { text: "Version" }), el("th", { text: "Title as printed" }), el("th", { text: "Pages" }))),
    el("tbody", {}, data.languages.map((language) => {
      const v = data.instrument[language];
      return el("tr", {}, el("td", { text: LANGUAGE_NAMES[language] || language }),
        el("td", { lang: language }, el("a", { href: `#/doc/${encodeURIComponent(v.id)}`, text: v.title })),
        el("td", { text: v.pages ? `${v.pages[0]}–${v.pages[1]}` : "" }));
    })));
  const table = el("table", { class: "side-by-side" },
    el("thead", {}, el("tr", {}, el("th", { text: "Article" }), data.languages.map((language) => el("th", { lang: language, text: LANGUAGE_NAMES[language] || language })))),
    el("tbody", {}, data.rows.map((row) => el("tr", { class: row.complete ? "" : "differs", id: `article-${row.article}` },
      el("td", { class: "num", text: row.article }),
      data.languages.map((language) => {
        const article = row.versions[language];
        if (!article) return el("td", { class: "absent", text: "No such article found in this version" });
        return el("td", { lang: language },
          el("a", { href: versionLink(language, article.page), class: "art-head", title: `Edit page ${article.page}` },
            el("strong", { text: `${article.marker} ${article.title}`.trim() })),
          el("p", { text: article.text }));
      })))));
  const summary = data.differences.length
    ? `${data.rows.length} articles. Not in every version: ${data.differences.join(", ")}. Open the version that lacks one and correct it, or record that it is printed so.`
    : `${data.rows.length} articles, the same in every version.`;
  main(el("div", { class: "card languages" },
    el("h2", { text: "The language versions, article by article" }),
    el("p", { class: "muted", text: "Each version is reviewed on its own pages; this view sets them side by side as they now read, so a missing, extra or misnumbered article shows at once." }),
    instrument,
    el("p", {}, data.differences.length ? pill("high", summary) : pill("ok", summary)),
    table));
}

async function readView(actId) {
  const { data } = await api(`/api/documents/${encodeURIComponent(actId)}`);
  setCrumbs(el("a", { href: "#/", text: "Documents" }), el("a", { href: `#/doc/${encodeURIComponent(actId)}`, text: data.act.title }), "Read");
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
  const panel = el("div", { class: "card", id: "approval" }, el("h3", { text: "Approval" }));
  const blockers = el("ul", { class: "ready" });
  if (ready.unchecked_pages.length) blockers.append(el("li", { text: `${ready.routed ? "Pages to review" : "Pages not yet checked"}: ${summarise(ready.unchecked_pages)}` }));
  if (ready.unresolved_flag_pages.length) blockers.append(el("li", { text: `Pages with flags to resolve: ${summarise(ready.unresolved_flag_pages)}` }));
  if (ready.open_high_flags.length) blockers.append(el("li", {}, "High flags open on pages ", ...unique(ready.open_high_flags.map((f) => f.page)).flatMap((p, i) => [i ? ", " : "", el("a", { href: `#/doc/${encodeURIComponent(actId)}/page/${p}`, text: String(p) })])));
  if (ready.stale_pages.length) blockers.append(el("li", { text: `Pages to recheck because the reading changed: ${summarise(ready.stale_pages)}` }));
  if (ready.missing_checks.length) blockers.append(el("li", { text: `Check readings missing: ${ready.missing_checks.join(", ")} — rerun the batch` }));
  if (!ready.details.confirmed) blockers.append(el("li", {}, "Document details ", el("a", { href: `#/doc/${encodeURIComponent(actId)}/details`, text: ready.details.status ? "not yet confirmed" : "not yet reviewed" })));
  if (ready.text && ready.text.errors) blockers.append(el("li", { text: `${ready.text.errors} problem${ready.text.errors === 1 ? "" : "s"} in the text's structure: see Text and structure above` }));
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
  setCrumbs(el("a", { href: "#/", text: "Documents" }), el("a", { href: `#/doc/${encodeURIComponent(actId)}`, text: title }), "Details");
  const { standard, record, suggestions, related = {} } = data;
  const firstEvidence = Object.values(suggestions).map((h) => h.page).filter(Boolean).sort((a, b) => a - b)[0];
  const original = sourcePanel(actId, doc.data.pages.map((p) => p.page), doc.data.source || {}, firstEvidence);
  const canEdit = app.me.roles.includes("reviewer");
  const values = structuredClone(record.details);
  const absent = { ...record.absent };
  const rows = new Map();
  const applicable = standard.fields.filter((f) => !f.types || f.types.includes(data.document_type));

  const isEmpty = (v) => v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length);
  const pageLink = (page, text = `p. ${page}`) => el("a", { href: `#/doc/${encodeURIComponent(actId)}/page/${page}`, text, title: "Show this page beside the details",
    on: { click: (event) => { event.preventDefault(); original.show(page); } } });
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
      input = relationsEditor(standard, value || [], (links) => set(links), related);
    } else if (field.kind === "rows") {  // its suggestions are by row: a version's authority, by its language
      input = rowsEditor(field.columns, structuredClone(value || []), (rows) => set(rows), { suggested: suggestions[field.key] || {}, pageLink, canEdit });
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
    const hint = field.kind === "rows" ? null : suggestions[field.key];
    let note = null;
    if (hint) {
      const label = (value) => field.kind === "choice" ? (field.choices.find((c) => c.value === value) || { label: String(value) }).label : String(value);
      const shown = hint.absent ? standard.absent[hint.absent]
        : Array.isArray(hint.value) ? hint.value.map((l) => (typeof l === "object" ? Object.values(l).join(" ") : l)).join("; ")
        : label(hint.value);
      const differs = hint.absent ? absent[field.key] !== hint.absent : JSON.stringify(hint.value) !== JSON.stringify(values[field.key]);
      // Words printed in another law (the provision that repeals this one): that law and its page.
      const other = hint.source && related[hint.source.work];
      const where = hint.source ? `${hint.source.title}${hint.source.page ? `, p. ${hint.source.page}` : ""}` : "";
      note = el("div", { class: "hint" },
        hint.page ? pageLink(hint.page)
          : other && hint.source.page ? el("a", { href: `#/doc/${encodeURIComponent(other)}/page/${hint.source.page}`, text: where, title: "Open that page of the other law" })
          : el("span", { text: where || "From the collection" }), " · ",
        el("span", { class: "evidence", text: `“${hint.evidence}”` }),
        differs && canEdit ? el("button", { class: "small", text: `Use: ${shown.length > 60 ? shown.slice(0, 60) + "…" : shown}`, on: { click: () => {
          if (hint.absent) { absent[field.key] = hint.absent; delete values[field.key]; why.value = hint.absent; }
          else { values[field.key] = structuredClone(hint.value); delete absent[field.key]; why.value = ""; }
          rows.get(field.key).replaceWith(row(field)); touched();
        } } }) : null,
        hint.secondary ? el("div", { class: "muted small", text: `Secondary source: ${label(hint.secondary.value)} — “${hint.secondary.evidence}”` }) : null);
    }
    return { input, why, note };
  };
  const row = (field) => {
    const { input, why, note } = control(field);
    const line = el("div", { class: "detail-row", "data-key": field.key },
      el("label", {}, field.label, field.required ? null : el("span", { class: "muted", text: " (optional)" })),
      el("div", { class: "detail-input" }, input, field.help ? el("div", { class: "muted small", text: field.help }) : null, note),
      (field.required || field.accountable) && !field.essential ? why : el("span", { class: "muted small", text: field.essential ? "must be given" : "" }));
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
    el("div", { class: "details-layout" },
      el("div", { class: "details-form" },
        el("div", { class: "card details-head" },
          el("h2", { text: `${title}: details` }),
          el("p", { class: "muted", text: "Each detail was read from the document where it is printed; the page and words are shown beside it. Check each against the original beside the form (a page number opens that page there), correct it, or say why it is not given. Legal facts (what each language version is worth as law, when it commences, whether it is current, the laws it affects) are only suggested: none is filled in until you use the suggestion or give it yourself, and an authority left empty is published as unknown. Confirmed details are approved with the text and shown to the public." }),
          el("p", {}, pill(record.status === "confirmed" ? "ok" : record.status === "draft" ? "warn" : "medium", DETAIL_STATUS[record.status] + (record.revision ? ` · revision ${record.revision} by ${record.author}` : "")), " ", counter),
          canEdit ? el("p", {}, el("button", { text: "Save draft", on: { click: () => save("draft") } }), " ", confirmButton, " ", previewLink(actId)) : el("p", { class: "muted", text: "Editing needs the reviewer role." }),
          canEdit ? comment : null),
        ...groups,
        history),
      original.panel),
  );
  refresh();
}

// The original beside the details: its pages, turned by the evidence links, with the official source
// and the original file one click away, so every detail is checked against where it is printed. Pages
// go by their numbers in the reading: a law printed in a gazette issue keeps the issue's (45 to 52).
function sourcePanel(actId, numbers, source, first) {
  const id = encodeURIComponent(actId);
  const pages = numbers.length ? numbers : [1];
  const closest = (n) => pages.reduce((best, p, i) => (Math.abs(p - n) < Math.abs(pages[best] - n) ? i : best), 0);
  let at = closest(first || pages[0]);
  let page = pages[at];
  let zoom = 1;
  const image = el("img", { alt: "", draggable: "false" });
  const frame = el("div", { class: "frame" }, image);
  const pick = el("select", { title: "Page", on: { change: () => show(Number(pick.value)) } },
    pages.map((n) => el("option", { value: String(n), text: pages.at(-1) === pages.length ? `Page ${n} of ${pages.length}` : `Page ${n}` })));
  const editor = el("a", { title: "Open this page in the editor" });
  const label = el("span", { class: "muted zoom-label" });
  const setZoom = (value) => {
    zoom = Math.max(1, Math.min(4, value));
    frame.style.width = `${zoom * 100}%`;
    label.textContent = `${Math.round(zoom * 100)}%`;
  };
  const show = (n) => {
    at = closest(n);
    page = pages[at];
    pick.value = String(page);
    image.src = `/api/documents/${id}/pages/${page}/image`;
    image.alt = `Original page ${page}`;
    editor.href = `#/doc/${id}/page/${page}`;
    editor.textContent = `Edit page ${page}`;
  };
  const links = el("p", { class: "source-links" },
    source.official_url
      ? el("a", { href: source.official_url, target: "_blank", rel: "noopener noreferrer", title: source.official_url, text: "Official source ↗" })
      : el("span", { class: "muted", text: "No official address recorded" }),
    source.original_pdf ? [" · ", el("a", { href: `/api/documents/${id}/original.pdf`, target: "_blank", rel: "noopener", text: "Original PDF ↗" })] : null,
    " · ", editor);
  const viewer = el("div", { class: "viewer" },
    el("div", { class: "viewer-tools" },
      el("button", { class: "small", title: "Previous page", text: "←", on: { click: () => show(pages[Math.max(at - 1, 0)]) } }),
      pick,
      el("button", { class: "small", title: "Next page", text: "→", on: { click: () => show(pages[Math.min(at + 1, pages.length - 1)]) } }),
      el("button", { class: "small", title: "Zoom out", text: "−", on: { click: () => setZoom(zoom / 1.25) } }),
      label,
      el("button", { class: "small", title: "Zoom in", text: "+", on: { click: () => setZoom(zoom * 1.25) } }),
      el("button", { class: "small", title: "Fit the page width", text: "Fit", on: { click: () => setZoom(1) } })),
    frame);
  setZoom(1);
  show(page);
  return { panel: el("aside", { class: "details-source", "aria-label": "The original document" }, el("h3", { text: "The original" }), links, viewer), show };
}

function relationsEditor(standard, links, onChange, related = {}) {
  const box = el("div", { class: "relations" });
  const draw = () => {
    box.replaceChildren(
      ...links.map((link, i) => {
        const kind = el("select", { on: { change: () => { link.kind = kind.value; onChange(links); } } }, standard.relations.map((r) => el("option", { value: r.value, text: r.label })));
        kind.value = link.kind;
        const target = el("input", { type: "text", placeholder: "Title or citation of the other law", value: link.target || "", on: { input: () => { link.target = target.value; onChange(links); } } });
        const id = el("input", { type: "text", class: "narrow", placeholder: "catalogue id (if any)", value: link.target_id || "", on: { input: () => { if (id.value) link.target_id = id.value; else delete link.target_id; onChange(links); } } });
        const note = el("input", { type: "text", class: "narrow", placeholder: "note", value: link.note || "", on: { input: () => { if (note.value) link.note = note.value; else delete link.note; onChange(links); } } });
        const other = related[link.target_id];  // the other law, if the workbench holds it
        return el("div", { class: "relation" }, kind, target, id, note,
          el("span", { class: "relation-actions" },
            other ? el("a", { href: `#/doc/${encodeURIComponent(other)}/details`, text: "Open", title: "Open the other law's details" }) : null,
            el("button", { class: "small", text: "Remove", on: { click: () => { links.splice(i, 1); onChange(links); draw(); } } })));
      }),
      el("button", { class: "small", text: "+ Add a related law", on: { click: () => { links.push({ kind: "amends", target: "" }); onChange(links); draw(); } } }));
  };
  draw();
  return box;
}

// A small table of rows (language versions, parties, bench): one input per column. A column may have
// suggestions by row (a version's authority, by its language): each is shown under its row with the
// words that state it and their page, and is applied only when the reviewer presses "Use suggestion".
function rowsEditor(columns, rows, onChange, { suggested = {}, pageLink = null, canEdit = false } = {}) {
  const box = el("div", { class: "rows-editor" });
  const use = (row, key, hint) => {
    row[key] = hint.value;
    if (key === "authority") {  // with where it is stated: its page and words in this document
      row.authority_page = hint.page ? String(hint.page) : "";
      row.authority_words = hint.evidence || "";
      delete row.authority_url;
    }
  };
  const suggestion = (row) => columns.filter((column) => suggested[column.key] && suggested[column.key][row.language]).map((column) => {
    const hint = suggested[column.key][row.language];
    const label = ((column.choices || []).find((c) => c.value === hint.value) || { label: String(hint.value) }).label;
    return el("div", { class: "hint row-suggestion", "data-column": column.key, "data-language": row.language },
      el("span", { text: `Suggested: ${label.toLowerCase()} —` }),
      el("span", { class: "evidence", text: `“${hint.evidence}”` }),
      hint.page && pageLink ? pageLink(hint.page, `(p. ${hint.page})`) : null,
      canEdit && row[column.key] !== hint.value
        ? el("button", { class: "small", text: "Use suggestion", on: { click: () => { use(row, column.key, hint); onChange(rows); draw(); } } })
        : null);
  });
  const draw = () => {
    box.replaceChildren(
      ...rows.flatMap((row, i) => [el("div", { class: "row-line" },
        ...columns.map((column) => {
          let cell;
          if (column.choices && column.choices.length) {
            cell = el("select", { title: column.label, on: { change: () => { row[column.key] = cell.value; onChange(rows); if (suggested[column.key]) draw(); } } },
              el("option", { value: "", text: column.label }), column.choices.map((c) => el("option", { value: c.value, text: c.label })));
          } else {
            cell = el("input", { type: column.kind === "date" ? "date" : "text", placeholder: column.label, title: column.label,
              on: { input: () => { row[column.key] = cell.value; onChange(rows); } } });
          }
          cell.value = row[column.key] || "";
          return cell;
        }),
        el("button", { class: "small", text: "Remove", on: { click: () => { rows.splice(i, 1); onChange(rows); draw(); } } })), ...suggestion(row)]),
      el("button", { class: "small", text: "+ Add", on: { click: () => { rows.push({}); onChange(rows); draw(); } } }));
  };
  draw();
  return box;
}

// --- the work queue (CPR-06) ------------------------------------------------------------------------

const TASK_PRIORITY = { high: ["high", "high flags"], full: ["medium", "review"], flags: ["warn", "flags"], proposal: ["role", "proposal"] };
const TASK_KINDS = [["", "All work"], ["review", "Pages to review"], ["flags", "Flags to resolve"], ["events", "Legal events"], ["instructions", "Amendment instructions"], ["citations", "Citations"]];

// One task in words, with a link to where it is done. Pages to review are listed alike whatever the
// reason (an audit page is one of them: CPR-01).
function taskLine(t) {
  const id = encodeURIComponent(t.id);
  const s = t.count === 1 ? "" : "s";
  if (!t.started && !t.pages) return el("a", { href: `#/doc/${id}`, text: `Not started: ${t.count} page${s}` });
  if (t.kind === "review" || t.kind === "flags") {
    const what = t.kind === "review" ? `${t.count} page${s} to review against the image` : `Flags to resolve on ${t.count} page${s}`;
    return el("span", {}, el("a", { href: `#/doc/${id}/page/${t.pages[0]}`, text: what }), el("span", { class: "muted", text: ` · pages ${pageRuns(t.pages.slice(0, 60))}${t.pages.length > 60 ? "…" : ""}` }));
  }
  if (t.kind === "events") return el("a", { href: `#/doc/${id}/events`, text: `${t.count} legal event${s} to review` });
  if (t.kind === "citations") return el("a", { href: `#/doc/${id}/citations`, text: `${t.count} citation${s} to confirm or reject` });
  return el("a", { href: `#/doc/${id}/instructions`, text: t.step === "second_review" ? `${t.count} instruction${s} awaiting a second editor` : `${t.count} instruction${s} to review` });
}

async function queueView() {
  setCrumbs("Work queue");
  const kind = el("select", {}, TASK_KINDS.map(([value, label]) => el("option", { value, text: label })));
  kind.value = remember("queue-kind") || "";
  const rows = el("tbody");
  const pager = el("div", { class: "pager" });
  const summary = el("p", { class: "muted", text: "Work in documents already started first, the pages with high flags at the top, then pages to review, flags to resolve, and proposals to review; within each, what has waited longest." });
  const PAGE = 100;
  let offset = 0;
  const load = async () => {
    remember("queue-kind", kind.value);
    const { data } = await api(`/api/queue?${new URLSearchParams({ kind: kind.value, offset: String(offset), limit: String(PAGE) })}`);
    const c = data.counts;
    for (const option of kind.options) option.textContent = `${TASK_KINDS.find(([v]) => v === option.value)[1]}${option.value ? ` (${c[option.value]})` : ""}`;
    rows.replaceChildren(...(data.tasks.length ? data.tasks.map((t) => el("tr", {},
      el("td", {}, pill(...TASK_PRIORITY[t.priority])),
      el("td", {}, taskLine(t)),
      el("td", {}, el("a", { href: `#/doc/${encodeURIComponent(t.id)}`, text: t.title })),
      el("td", { class: "muted", text: t.since ? ago(t.since) : "not started", title: t.since ? new Date(t.since).toLocaleString() : "" })))
      : [el("tr", {}, el("td", { colspan: 4, class: "muted", text: "Nothing waiting." }))]));
    const last = Math.min(data.offset + data.tasks.length, data.total);
    pager.replaceChildren(
      el("span", { class: "muted", text: data.total ? `${data.offset + 1}–${last} of ${data.total}` : "" }),
      el("button", { class: "small", text: "← Previous", disabled: data.offset === 0, on: { click: () => { offset = Math.max(0, offset - PAGE); load(); } } }),
      el("button", { class: "small", text: "Next →", disabled: last >= data.total, on: { click: () => { offset += PAGE; load(); } } }));
  };
  kind.addEventListener("input", () => { offset = 0; load(); });
  main(el("div", { class: "card" },
    el("div", { class: "doc-tools" }, el("h3", { text: "Work queue" }), kind),
    summary,
    el("table", { class: "list queue" }, el("thead", {}, el("tr", {}, ["Priority", "Task", "Document", "Waiting since"].map((h) => el("th", { text: h })))), rows),
    pager));
  await load();
}

// --- legal events and amendment instructions (CPR-02, CPR-03) ----------------------------------------

const EVENT_STATUS = { proposed: ["medium", "proposed"], reviewed: ["ok", "reviewed"], rejected: ["warn", "rejected"] };
const INSTRUCTION_STATUS = { proposed: ["medium", "proposed"], reviewed: ["warn", "reviewed: awaiting a second editor"], second_reviewed: ["ok", "reviewed by two editors"], rejected: ["warn", "rejected"] };
const OPERATION_LABEL = { substitute: "Substitute the provision", insert: "Insert new provisions", repeal: "Repeal the provision", renumber: "Renumber the provision", amend_words: "Amend words in it" };
const spaced = (value) => String(value || "").replace(/_/g, " ");

// On the document's page: what its work's events and instructions are waiting for, and where to edit them.
function editorialSummary(actId) {
  const id = encodeURIComponent(actId);
  const line = el("p", { class: "muted", text: "Loading…" });
  const tally = (items, labels) => {
    const counts = {};
    for (const item of items) counts[item.status] = (counts[item.status] || 0) + 1;
    return Object.entries(counts).map(([status, n]) => `${n} ${labels[status][1]}`).join(", ") || "none yet";
  };
  Promise.all([api(`/api/documents/${id}/events`), api(`/api/documents/${id}/instructions`)]).then(([events, instructions]) => {
    if (!events.ok || !instructions.ok) { line.textContent = ""; return; }
    line.textContent = `Legal events: ${tally(events.data.events, EVENT_STATUS)}. Amendment instructions: ${tally(instructions.data.instructions, INSTRUCTION_STATUS)}.`;
  });
  return el("div", { class: "card" },
    el("h3", { text: "Legal events and amendments" }),
    el("p", { class: "muted", text: "When this law and its provisions came into force, were repealed or suspended (proposed from its text, checked by you), and, for an amending law, the instructions that consolidate the law it amends." }),
    line,
    el("p", {},
      el("a", { href: `#/doc/${id}/events` }, el("button", { text: "Legal events" })), " ",
      el("a", { href: `#/doc/${id}/instructions` }, el("button", { text: "Amendment instructions" }))));
}

const field = (label, control, hint) => el("label", { class: "editor-field" }, el("span", { class: "editor-label", text: label }), control, hint ? el("span", { class: "muted small", text: hint }) : null);
const optional = (value) => (value === "" ? null : value);

function historyList(item) {
  return el("details", { class: "editor-history" }, el("summary", { text: `History (${item.history.length})` }),
    el("ul", {}, item.history.map((h) => el("li", { text: `${h.at.slice(0, 16).replace("T", " ")} · revision ${h.revision} · ${spaced(h.status)} by ${operatorName(h.author)}${h.comment ? ` — ${h.comment}` : ""}` }))));
}

// The provisions of this law, to choose some (the affected-provision picker), with a filter.
function provisionPicker(provisions, chosen, onChange) {
  const picked = new Set(chosen);
  const filter = el("input", { type: "search", placeholder: "Filter provisions" });
  const list = el("div", { class: "picker" });
  const draw = () => {
    const words = filter.value.toLowerCase();
    list.replaceChildren(...provisions.filter((p) => !words || `${p.locator} ${p.label} ${p.heading}`.toLowerCase().includes(words)).slice(0, 400).map((p) => {
      const box = el("input", { type: "checkbox", checked: picked.has(p.locator), on: { change: () => { if (box.checked) picked.add(p.locator); else picked.delete(p.locator); onChange([...picked]); } } });
      return el("label", { class: "picker-row" }, box, el("code", { text: p.locator }), el("span", { class: "muted", text: ` ${p.label} ${p.heading}`.trimEnd() }));
    }));
  };
  filter.addEventListener("input", draw);
  draw();
  return el("div", {}, filter, list);
}

function provisionSelect(provisions, value) {
  const select = el("select", {}, el("option", { value: "", text: "—" }),
    provisions.map((p) => el("option", { value: p.locator, text: `${p.locator}${p.label || p.heading ? ` · ${[p.label, p.heading].filter(Boolean).join(" ")}` : ""}`.slice(0, 90) })));
  select.value = value || "";
  return select;
}

async function eventsView(actId) {
  const id = encodeURIComponent(actId);
  const [listing, doc] = await Promise.all([api(`/api/documents/${id}/events`), api(`/api/documents/${id}`)]);
  if (!listing.ok) return main(el("p", { text: message(listing) }));
  const data = listing.data;
  app.names = { ...(app.names || {}), ...data.names };
  setCrumbs(el("a", { href: "#/", text: "Documents" }), el("a", { href: `#/doc/${id}`, text: doc.data.act.title }), "Legal events");
  const original = sourcePanel(actId, doc.data.pages.map((p) => p.page), doc.data.source || {}, 1);
  const canPropose = app.me.roles.includes("reviewer"), canReview = app.me.roles.includes("approver");
  const pageOf = Object.fromEntries(data.provisions.map((p) => [p.locator, p.page]));
  const cards = el("div");
  const card = (item) => {
    const e = item ? item.event : { type: "commenced", affected_work: data.work, scope: "whole", locators: [], precision: "day", evidence: { url: data.source_url } };
    let locators = [...(e.locators || [])];
    const touched = () => { app.dirty = true; };
    const type = el("select", { on: { change: touched } }, data.types.map((t) => el("option", { value: t, text: spaced(t) })));
    type.value = e.type;
    const affected = el("input", { type: "text", value: e.affected_work || "", placeholder: "the affected law's record id", on: { input: () => { touched(); drawScope(); } } });
    const scope = el("select", { on: { change: () => { touched(); drawScope(); } } }, el("option", { value: "whole", text: "the whole law" }), el("option", { value: "explicit_nodes", text: "chosen provisions" }));
    scope.value = e.scope;
    const scopeBox = el("div");
    const typed = el("input", { type: "text", placeholder: "locators, separated by commas (sec_5, sec_6__subsec_2)", on: { input: () => { touched(); locators = typed.value.split(",").map((x) => x.trim()).filter(Boolean); } } });
    const drawScope = () => {
      if (scope.value !== "explicit_nodes") return scopeBox.replaceChildren();
      if (affected.value.trim() === data.work) return scopeBox.replaceChildren(provisionPicker(data.provisions, locators, (chosen) => { locators = chosen; touched(); }));
      typed.value = locators.join(", ");
      scopeBox.replaceChildren(field("Provisions of the affected law", typed, "Checked against its published text when the events are published"));
    };
    const from = el("input", { type: "date", value: e.date_from || "", on: { input: touched } });
    const to = el("input", { type: "date", value: e.date_to || "", on: { input: touched } });
    const precision = el("select", { on: { change: touched } }, data.precisions.map((p) => el("option", { value: p, text: p })));
    precision.value = e.precision || "unknown";
    const conditions = el("textarea", { rows: 2, value: e.conditions || "", placeholder: "A condition, when no date is known (“on a date the Minister appoints”)", on: { input: touched } });
    const evidence = e.evidence || {};
    const anchor = provisionSelect(data.provisions, evidence.locator);
    const showAnchor = el("button", { class: "small", text: "Show its page", on: { click: () => { if (pageOf[anchor.value]) original.show(pageOf[anchor.value]); } } });
    anchor.addEventListener("change", touched);
    const quote = el("textarea", { rows: 3, value: evidence.quote || "", placeholder: "The words that say so", on: { input: touched } });
    const url = el("input", { type: "url", value: evidence.url || "", on: { input: touched } });
    const supersedes = el("input", { type: "text", value: e.supersedes || "", placeholder: "evt_… (optional)", on: { input: touched } });
    const comment = el("textarea", { rows: 1, placeholder: "Note for the record (optional)" });
    const read = () => ({
      type: type.value, affected_work: affected.value.trim() || null, scope: scope.value, locators: scope.value === "explicit_nodes" ? locators : [],
      date_from: optional(from.value), date_to: optional(to.value), precision: precision.value, conditions: optional(conditions.value.trim()),
      evidence: { url: optional(url.value.trim()), locator: optional(anchor.value), quote: optional(quote.value.trim()) }, supersedes: optional(supersedes.value.trim()),
    });
    const send = async (status) => {
      if (status === "rejected" && !confirm("Reject this event? It stays in the record and is never exported.")) return;
      const body = { expected_revision: item ? item.revision : 0, status, event: read(), comment: comment.value };
      if (item) body.event_id = item.event_id;
      const result = await api(`/api/documents/${id}/events`, { method: "POST", body });
      if (!result.ok) return toast(message(result));
      app.dirty = false;
      toast(status === "reviewed" ? "Reviewed and recorded in your name" : status === "rejected" ? "Rejected" : "Saved");
      eventsView(actId);
    };
    drawScope();
    const [tone, label] = item ? EVENT_STATUS[item.status] : ["medium", "new"];
    return el("div", { class: `card editor-card status-${item ? item.status : "new"}` },
      el("div", { class: "editor-head" }, pill(tone, label),
        el("span", { class: "muted", text: item ? `revision ${item.revision} by ${operatorName(item.author)}, ${ago(item.at)}${item.comment ? ` — ${item.comment}` : ""}` : "not saved yet" })),
      el("div", { class: "editor-grid" },
        field("Event", type),
        field("Affected law", affected, affected.value === data.work ? "this law" : "a record id, as published"),
        field("Scope", scope),
        field("From", from), field("Until (optional)", to), field("Precision of the date", precision)),
      scopeBox,
      field("Condition", conditions),
      el("div", { class: "editor-grid" },
        el("div", { class: "editor-field" }, el("span", { class: "editor-label", text: "Evidence: the provision that says so" }), anchor, showAnchor),
        field("Address of the text", url),
        field("Corrects a published event", supersedes)),
      field("Its words", quote),
      comment,
      el("p", { class: "editor-actions" },
        canPropose ? el("button", { text: item ? "Save as proposed" : "Save the proposal", on: { click: () => send("proposed") } }) : null,
        canReview ? el("button", { class: "primary", text: "Mark reviewed", title: "Records you as its reviewer, now", on: { click: () => send("reviewed") } }) : null,
        canReview && item && item.status !== "rejected" ? el("button", { class: "danger", text: "Reject", on: { click: () => send("rejected") } }) : null,
        !canReview ? el("span", { class: "muted", text: "Marking an event reviewed needs the approver role." }) : null),
      item ? historyList(item) : null);
  };
  cards.replaceChildren(...data.events.map(card));
  const propose = async () => {
    const result = await api(`/api/documents/${id}/events/proposals`, { method: "POST" });
    if (!result.ok) return toast(message(result));
    toast(result.data.added.length ? `${result.data.added.length} proposal${result.data.added.length === 1 ? "" : "s"} recorded from the text` : "No new proposals in the text");
    eventsView(actId);
  };
  const reviewed = data.events.filter((e) => e.status === "reviewed").length;
  main(el("div", { class: "details-layout" },
    el("div", {},
      el("div", { class: "card" },
        el("h2", { text: "Legal events" }),
        el("p", { class: "muted", text: "When this law, or provisions of it, came into force, were repealed, suspended or revived. Proposals are read from the text's commencement and repeal provisions; check each against the original beside it, complete it, and mark it reviewed, or reject it. Only reviewed events are exported for publishing." }),
        el("p", {},
          canPropose ? el("button", { class: "primary", text: "Propose from the text", on: { click: propose } }) : null, " ",
          canPropose ? el("button", { text: "+ Add an event", on: { click: () => cards.prepend(card(null)) } }) : null, " ",
          el("a", { href: `/api/documents/${id}/events/export`, text: `Export the ${reviewed} reviewed event${reviewed === 1 ? "" : "s"} (JSON, for operations.events publish)` }))),
      cards,
      data.events.length ? null : el("p", { class: "muted", text: "No events recorded for this law yet." })),
    original.panel));
}

async function instructionsView(actId) {
  const id = encodeURIComponent(actId);
  const [listing, doc] = await Promise.all([api(`/api/documents/${id}/instructions`), api(`/api/documents/${id}`)]);
  if (!listing.ok) return main(el("p", { text: message(listing) }));
  const data = listing.data;
  app.names = { ...(app.names || {}), ...data.names };
  setCrumbs(el("a", { href: "#/", text: "Documents" }), el("a", { href: `#/doc/${id}`, text: doc.data.act.title }), "Amendment instructions");
  const original = sourcePanel(actId, doc.data.pages.map((p) => p.page), doc.data.source || {}, 1);
  const isReviewer = app.me.roles.includes("reviewer"), isApprover = app.me.roles.includes("approver");
  const pageOf = Object.fromEntries(data.provisions.map((p) => [p.locator, p.page]));
  const cards = el("div");
  const card = (item) => {
    const i = item ? item.instruction : { operation: "amend_words", occurrences: 1, amending: {} };
    const touched = () => { app.dirty = true; };
    const target = el("input", { type: "text", value: item ? item.target_work : "", placeholder: "the amended law's record id", on: { input: touched } });
    const operation = el("select", { on: { change: () => { touched(); show(); } } }, Object.entries(OPERATION_LABEL).map(([value, text]) => el("option", { value, text })));
    operation.value = i.operation;
    const locator = el("input", { type: "text", value: i.target || "", placeholder: "sec_5, sec_5__subsec_2", on: { input: touched } });
    const effective = el("input", { type: "date", value: i.effective_date || "", on: { input: touched } });
    const position = el("select", { on: { change: touched } }, el("option", { value: "after", text: "after it" }), el("option", { value: "before", text: "before it" }));
    position.value = i.position || "after";
    // New text: one paragraph per line; units the editor has not touched are kept as they were.
    const units = i.units || [];
    const initial = units.map((u) => [u.marker, u.text].filter(Boolean).join(" ")).join("\n");
    const firstNote = (units[0] && units[0].notes && units[0].notes[0]) || "";
    const text = el("textarea", { rows: 4, value: initial, placeholder: "The new text, one paragraph per line (“5A. A loan may be deferred.”)", on: { input: touched } });
    const note = el("input", { type: "text", value: firstNote, placeholder: "Margin note of the first paragraph (optional)", on: { input: touched } });
    const remove = el("input", { type: "text", value: i.words_delete || "", on: { input: touched } });
    const add = el("input", { type: "text", value: i.words_insert || "", on: { input: touched } });
    const times = el("input", { type: "number", min: "1", max: "100", value: String(i.occurrences || 1), on: { input: touched } });
    const labelFrom = el("input", { type: "text", value: i.label_from || "", placeholder: "5.", on: { input: touched } });
    const labelTo = el("input", { type: "text", value: i.label_to || "", placeholder: "6.", on: { input: touched } });
    const amending = i.amending || {};
    const anchor = provisionSelect(data.provisions, amending.locator);
    anchor.addEventListener("change", touched);
    const citation = el("input", { type: "text", value: amending.citation || "", placeholder: "Example (Amendment) Act, 2024, s. 1", on: { input: touched } });
    const comment = el("textarea", { rows: 1, placeholder: "Note for the record (optional)" });
    const newText = el("div", { class: "editor-grid wide" }, field("New text", text), field("Margin note", note));
    const words = el("div", { class: "editor-grid" }, field("Words to delete", remove), field("Words to insert (empty: delete only)", add), field("Times they occur", times));
    const numbers = el("div", { class: "editor-grid" }, field("Number from", labelFrom), field("Number to", labelTo));
    const where = field("Insert", position);
    const show = () => {
      const op = operation.value;
      newText.hidden = !["substitute", "insert"].includes(op); where.hidden = op !== "insert";
      words.hidden = op !== "amend_words"; numbers.hidden = op !== "renumber";
    };
    const read = () => {
      const op = operation.value;
      let built = [];
      if (["substitute", "insert"].includes(op)) {
        built = text.value === initial && note.value === firstNote ? units
          : text.value.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => ({ kind: "paragraph", text: line }));
        if (built !== units && note.value.trim() && built[0]) built[0].notes = [note.value.trim()];
      }
      return {
        operation: op, target: locator.value.trim(), effective_date: optional(effective.value), units: built,
        position: op === "insert" ? position.value : null,
        words_delete: op === "amend_words" ? optional(remove.value) : null, words_insert: op === "amend_words" ? add.value : "",
        occurrences: op === "amend_words" ? Number(times.value || 1) : 1,
        label_from: op === "renumber" ? optional(labelFrom.value.trim()) : null, label_to: op === "renumber" ? optional(labelTo.value.trim()) : null,
        amending: { locator: anchor.value, citation: citation.value.trim() },
      };
    };
    const send = async (status) => {
      if (status === "rejected" && !confirm("Reject this instruction? It stays in the record and is never exported.")) return;
      const body = { expected_revision: item ? item.revision : 0, status, target_work: target.value.trim(), instruction: read(), comment: comment.value };
      if (item) body.instruction_id = item.instruction_id;
      const result = await api(`/api/documents/${id}/instructions`, { method: "POST", body });
      if (!result.ok) return toast(message(result));
      app.dirty = false;
      toast(status === "second_reviewed" ? "Second review recorded: it now exports" : status === "reviewed" ? "Reviewed: another editor confirms it next" : status === "rejected" ? "Rejected" : "Saved");
      instructionsView(actId);
    };
    show();
    // The second review confirms the instruction exactly as the first editor saved it (EDT-04).
    const second = item && item.status === "reviewed" && isApprover;
    const mine = item && item.author === app.me.email;
    const [tone, label] = item ? INSTRUCTION_STATUS[item.status] : ["medium", "new"];
    const held = item && data.targets[item.target_work];
    return el("div", { class: `card editor-card status-${item ? item.status : "new"}` },
      el("div", { class: "editor-head" }, pill(tone, label),
        el("span", { class: "muted", text: item ? `revision ${item.revision} by ${operatorName(item.author)}, ${ago(item.at)}${item.comment ? ` — ${item.comment}` : ""}` : "not saved yet" }),
        held ? el("a", { href: `#/doc/${encodeURIComponent(held)}`, text: "Open the amended law" }) : null),
      el("div", { class: "editor-grid" },
        field("Amended law", target, "its record id"),
        field("Operation", operation),
        field("Target provision", locator, "its locator in the amended law, as it stands when this applies"),
        field("In force from", effective)),
      where, newText, words, numbers,
      el("div", { class: "editor-grid" },
        el("div", { class: "editor-field" }, el("span", { class: "editor-label", text: "Amending provision (in this law)" }), anchor,
          el("button", { class: "small", text: "Show its page", on: { click: () => { if (pageOf[anchor.value]) original.show(pageOf[anchor.value]); } } })),
        field("Its citation", citation)),
      comment,
      el("p", { class: "editor-actions" },
        isReviewer ? el("button", { text: item ? "Save as proposed" : "Save the proposal", on: { click: () => send("proposed") } }) : null,
        isReviewer ? el("button", { class: second ? "" : "primary", text: "Save as reviewed", title: "Your review; a second editor confirms it", on: { click: () => send("reviewed") } }) : null,
        second ? el("button", { class: "primary", text: "Confirm: second review", disabled: mine, title: mine ? "You reviewed it: another editor gives the second review" : "Confirms it exactly as reviewed", on: { click: () => send("second_reviewed") } }) : null,
        isApprover && item && item.status !== "rejected" ? el("button", { class: "danger", text: "Reject", on: { click: () => send("rejected") } }) : null),
      item ? historyList(item) : null);
  };
  cards.replaceChildren(...data.instructions.map(card));
  const exports = Object.keys(data.targets).map((work) => el("li", {},
    el("a", { href: `/api/instructions/export?${new URLSearchParams({ target: work })}`, text: `Instructions amending ${work} (JSON)` })));
  main(el("div", { class: "details-layout" },
    el("div", {},
      el("div", { class: "card" },
        el("h2", { text: "Amendment instructions" }),
        el("p", { class: "muted", text: "For an amending law: what each of its provisions does to the law it amends, read from its text. An editor reviews each instruction; a second editor confirms it unchanged. Only then is it exported to build an unofficial consolidation of the amended law (with every law that amends it)." }),
        isReviewer ? el("p", {}, el("button", { class: "primary", text: "+ Add an instruction", on: { click: () => cards.prepend(card(null)) } })) : null,
        exports.length ? el("ul", {}, exports) : null),
      cards,
      data.instructions.length ? null : el("p", { class: "muted", text: "No instructions recorded from this law yet." })),
    original.panel));
}

// --- a judgment's citations and their treatment (EDT-01, EDT-02) -----------------------------------

const CITATION_STATUS = { proposed: ["medium", "proposed"], confirmed: ["ok", "confirmed"], rejected: ["warn", "rejected"], withdrawn: ["warn", "withdrawn"] };

// On a judgment's page: how many of its citations wait for review, and where to review them.
function citationsSummary(actId) {
  const id = encodeURIComponent(actId);
  const line = el("p", { class: "muted", text: "Loading…" });
  api(`/api/documents/${id}/citations`).then((listing) => {
    if (!listing.ok) { line.textContent = ""; return; }
    const counts = {};
    for (const c of listing.data.citations) counts[c.status] = (counts[c.status] || 0) + 1;
    line.textContent = `Citations: ${Object.entries(counts).map(([status, n]) => `${n} ${CITATION_STATUS[status][1]}`).join(", ") || "none yet"}.`;
  });
  return el("div", { class: "card" },
    el("h3", { text: "Citations and treatment" }),
    el("p", { class: "muted", text: "The provisions and judgments this judgment's paragraphs cite (proposed from its published text, confirmed by you), and how it treats the judgments it cites, recorded only by a legally qualified reviewer." }),
    line,
    el("p", {}, el("a", { href: `#/doc/${id}/citations` }, el("button", { text: "Citations" }))));
}

async function citationsView(actId) {
  const id = encodeURIComponent(actId);
  const [listing, doc] = await Promise.all([api(`/api/documents/${id}/citations`), api(`/api/documents/${id}`)]);
  if (!listing.ok) return main(el("p", { text: message(listing) }));
  const data = listing.data;
  app.names = { ...(app.names || {}), ...data.names };
  setCrumbs(el("a", { href: "#/", text: "Documents" }), el("a", { href: `#/doc/${id}`, text: doc.data.act.title }), "Citations");
  const original = sourcePanel(actId, doc.data.pages.map((p) => p.page), doc.data.source || {}, 1);
  const canImport = app.me.roles.includes("reviewer"), canReview = app.me.roles.includes("approver");
  const paragraphOf = Object.fromEntries(data.paragraphs.map((p) => [p.locator, p]));
  const card = (item) => {
    const c = item.citation;
    const paragraph = paragraphOf[c.citing_locator] || {};
    const role = el("input", { type: "text", placeholder: "such as owner or Nigerian advocate" });
    const comment = el("textarea", { rows: 1, placeholder: "Note for the record (optional)" });
    const decide = async (status) => {
      if (status === "rejected" && !confirm("Reject this citation? It stays in the record and is never exported.")) return;
      if (status === "withdrawn" && !confirm("Withdraw this citation, and its treatment? Publishing then takes them out of public view.")) return;
      const body = { citation_id: item.citation_id, expected_revision: item.revision, status, reviewer_role: role.value, comment: comment.value };
      const result = await api(`/api/documents/${id}/citations`, { method: "POST", body });
      if (!result.ok) return toast(message(result));
      toast(status === "confirmed" ? "Confirmed and recorded in your name" : status === "rejected" ? "Rejected" : "Withdrawn");
      citationsView(actId);
    };
    const treatments = c.treatments || [];
    const live = treatments.find((t) => t.review_state === "reviewed");
    const withdrawn = treatments.filter((t) => t.review_state === "withdrawn");
    const past = withdrawn.length ? el("p", { class: "muted small", text: `Withdrawn: ${withdrawn.map((t) => `${spaced(t.kind)}, recorded by ${operatorName(t.reviewed_by)}`).join("; ")}` }) : null;
    // How the paragraph treats the judgment it cites: a doctrinal review, never inferred from the citation.
    const treatment = () => {
      if (c.cited_document_type !== "judgment") return null;
      const send = async (body) => {
        const result = await api(`/api/documents/${id}/citations/${encodeURIComponent(item.citation_id)}/treatment`, { method: "POST", body: { expected_revision: item.revision, ...body } });
        if (!result.ok) return toast(message(result));
        toast(body.action === "record" ? "Treatment recorded in your name" : "Treatment withdrawn");
        citationsView(actId);
      };
      if (live) {
        return el("div", { class: "editor-field" },
          el("p", {}, "Treatment: ", el("strong", { text: spaced(live.kind) }), ` (${live.opinion} opinion), recorded by ${operatorName(live.reviewed_by)}, ${live.reviewer_role}: ${live.reviewer_qualification}`),
          el("blockquote", { class: "muted", text: live.quote }),
          past,
          canReview ? el("button", { class: "danger small", text: "Withdraw the treatment", on: { click: () => { if (confirm("Withdraw this treatment? Publishing then corrects its announcements.")) send({ action: "withdraw" }); } } }) : null);
      }
      if (!canReview) return el("div", {}, el("p", { class: "muted", text: "No treatment recorded, so none is published. Recording one needs the approver role and a legal qualification." }), past);
      const kind = el("select", {}, el("option", { value: "", text: "—" }), data.treatments.map((t) => el("option", { value: t, text: spaced(t) })));
      const opinion = el("select", {}, data.opinions.map((o) => el("option", { value: o, text: o })));
      opinion.value = "unknown";
      const passage = el("textarea", { rows: 3, value: c.quote });
      const by = el("input", { type: "text", placeholder: "such as Nigerian advocate" });
      const qualification = el("input", { type: "text", placeholder: `Your legal qualification for ${data.jurisdiction || "this jurisdiction"}` });
      return el("details", { class: "editor-history" },
        el("summary", { text: "Record how this paragraph treats the judgment it cites (a doctrinal review)" }),
        el("p", { class: "muted", text: "Only what the paragraph itself says, in the opinion it is in. No treatment recorded means none is published: never infer one from the citation." }),
        el("div", { class: "editor-grid" },
          field("Treatment", kind),
          field("Opinion the paragraph is in", opinion, paragraph.opinion ? `The text calls it the ${paragraph.opinion} opinion` : "")),
        field("The passage that does it, in the paragraph's own words", passage),
        el("div", { class: "editor-grid" }, field("Your role", by), field("Your legal qualification", qualification, "Required: a treatment is refused without it")),
        past,
        el("p", { class: "editor-actions" }, el("button", { class: "primary", text: "Record the treatment", on: { click: () => send({ action: "record", review_level: "doctrinal", kind: kind.value, opinion: opinion.value, quote: passage.value, reviewer_role: by.value, reviewer_qualification: qualification.value }) } })));
    };
    const cited = c.cited_locator
      ? `${c.cited_pinpoint || c.cited_locator} of ${c.cited_title || c.cited_work}`
      : `${c.cited_title || c.cited_work}, as a whole${c.unresolved_pinpoint ? ` (its pinpoint names no part: ${spaced(c.unresolved_pinpoint)})` : ""}`;
    const [tone, label] = CITATION_STATUS[item.status];
    return el("div", { class: `card editor-card status-${item.status}` },
      el("div", { class: "editor-head" }, pill(tone, label),
        el("span", { class: "muted", text: `revision ${item.revision} by ${operatorName(item.author)}, ${ago(item.at)}${item.comment ? ` — ${item.comment}` : ""}` })),
      el("p", {}, el("strong", { text: c.citing_pinpoint || paragraph.label || c.citing_locator }),
        el("span", { class: "muted", text: ` ${c.citing_locator}${paragraph.opinion ? ` · ${paragraph.opinion} opinion` : ""} ` }),
        paragraph.page ? el("button", { class: "small", text: "Show its page", on: { click: () => original.show(paragraph.page) } }) : null),
      el("p", {}, "Cites ", el("strong", { text: cited }), el("span", { class: "muted", text: ` (${c.cited_work}${c.cited_citation ? `, ${c.cited_citation}` : ""})` })),
      el("p", {}, "As written: ", el("q", { text: c.printed })),
      el("blockquote", { class: "muted", text: c.quote }),
      c.review ? el("p", { class: "muted", text: `Confirmed by ${operatorName(c.review.reviewed_by)} (${c.review.reviewer_role}), ${ago(c.review.reviewed_at)}` }) : null,
      item.status === "proposed" && canReview ? el("div", {},
        field("Your role (a structural review: whether the paragraph cites this; no qualification is needed)", role),
        comment,
        el("p", { class: "editor-actions" },
          el("button", { class: "primary", text: "Confirm: the paragraph cites this", on: { click: () => decide("confirmed") } }), " ",
          el("button", { class: "danger", text: "Reject", on: { click: () => decide("rejected") } }))) : null,
      item.status === "proposed" && !canReview ? el("p", { class: "muted", text: "Confirming a citation needs the approver role." }) : null,
      item.status === "confirmed" ? treatment() : null,
      item.status === "withdrawn" ? past : null,
      item.status === "confirmed" && canReview ? el("p", { class: "editor-actions" }, el("button", { class: "danger small", text: "Withdraw the citation", on: { click: () => decide("withdrawn") } })) : null,
      historyList(item));
  };
  const file = el("input", { type: "file", accept: "application/json,.json" });
  const importProposals = async () => {
    const chosen = file.files[0];
    if (!chosen) return toast("Choose the proposals file that operations.citations propose printed");
    let body;
    try { body = JSON.parse(await chosen.text()); } catch { return toast("That file is not JSON"); }
    const result = await api(`/api/documents/${id}/citations/proposals`, { method: "POST", body });
    if (!result.ok) return toast(message(result));
    const { added, skipped } = result.data;
    toast(`${added.length} proposal${added.length === 1 ? "" : "s"} recorded${skipped.length ? `; ${skipped.length} left out (${unique(skipped.map((s) => s.reason)).join("; ")})` : ""}`);
    citationsView(actId);
  };
  const exported = data.citations.filter((c) => c.status === "confirmed" || c.status === "withdrawn").length;
  main(el("div", { class: "details-layout" },
    el("div", {},
      el("div", { class: "card" },
        el("h2", { text: "Citations and treatment" }),
        el("p", { class: "muted", text: "The provisions and judgments each paragraph of this judgment cites. Proposals are read from its published text with the catalogue's citation resolver (python -m operations.citations propose --work …); import the file, check each against the paragraph beside it, and confirm or reject it. A confirmed citation of another judgment may also have its treatment recorded, by a reviewer who states their legal qualification. Only confirmed and withdrawn citations are exported for publishing." }),
        data.judgment ? null : el("p", { class: "warn", text: "This document is not read as a judgment's paragraphs, so it has no citations to review here." }),
        canImport && data.judgment ? el("p", {}, file, " ", el("button", { class: "primary", text: "Import proposals", on: { click: importProposals } })) : null,
        el("p", {}, el("a", { href: `/api/documents/${id}/citations/export`, text: `Export the ${exported} confirmed or withdrawn citation${exported === 1 ? "" : "s"} (JSON, for operations.citations publish)` }))),
      ...data.citations.map(card),
      data.citations.length ? null : el("p", { class: "muted", text: "No citations recorded for this judgment yet." })),
    original.panel));
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
    join: joinOf(latest ? latest.units : page.machine_units),
    tab: "edit",
    showLow: false,
    surface: null,
  };
  app.view = view;
  app.dirty = false;
  setCrumbs(el("a", { href: "#/", text: "Documents" }), el("a", { href: `#/doc/${encodeURIComponent(actId)}`, text: detail.act.title }), `Page ${pageNumber}`);
  renderPage(view);
}

function changed(view) { app.dirty = true; if (view.dirtyBadge) view.dirtyBadge.hidden = false; saveDraft(view); }

// Under a routing decision: what this page needs, in a sentence (nothing for a page to review whole,
// which is reviewed as before, whether it was routed there or sampled for the audit).
function routedNeed(view) {
  if (!view.detail.readiness.routed) return "";
  const here = view.detail.pages.find((p) => p.page === view.pageNumber) || {};
  if (here.todo === "flags") return "Only the flags on this page need you: resolve each one against the image, then save. The rest of the page does not need proofreading.";
  if (!here.todo && here.status !== "checked") return view.page.flags.length ? "Every flag on this page is resolved: nothing more to review here." : "Nothing to review on this page: the readings agree on every word.";
  return "";
}

// The working units: from the surface while it is open, otherwise the last snapshot.
function currentUnits(view) {
  if (view.surface) view.units = view.surface.units();
  view.units = withJoin(view.units, view.join);
  return view.units;
}

// A paragraph running over from the previous page is joined in the published text only where the
// reviewer says so (TXP-02 rule 8). The page view keeps the mark; the page's first paragraph carries it.
const firstWords = (units) => (units || []).findIndex((u) => u.kind !== "table" && ((u.text || "").trim() || u.marker));

function joinOf(units) {
  const first = (units || [])[firstWords(units)];
  return first && first.continues_previous ? (first.dehyphenate ? "dehyphenate" : "space") : null;
}

function withJoin(units, join) {
  const at = firstWords(units);
  return (units || []).map((unit, i) => {
    const { continues_previous: _joined, dehyphenate: _hyphen, ...rest } = unit;
    if (i !== at || !join) return rest;
    return { ...rest, continues_previous: true, ...(join === "dehyphenate" ? { dehyphenate: true } : {}) };
  });
}

function refreshJoin(view) {
  if (!view.joinSlot) return;
  const before = view.page.previous_end;
  const first = (view.units || [])[firstWords(view.units)];
  const set = (join) => { view.join = join; currentUnits(view); changed(view); refreshJoin(view); };
  if (!before || !first) return view.joinSlot.replaceChildren();
  const tail = `“…${before.text.slice(-60)}”`;
  if (view.join) {
    return view.joinSlot.replaceChildren(el("div", { class: "joinbar on" },
      el("span", { text: `Joined to page ${before.page}'s last paragraph ${view.join === "dehyphenate" ? "(its hyphen removed)" : "(with a space)"}: ${tail}` }),
      el("button", { class: "small", text: "Keep separate", on: { click: () => set(null) } })));
  }
  const lower = !first.marker && /^[a-z]/.test((first.text || "").trim());
  if (!lower || before.ends_sentence || view.joinDismissed) return view.joinSlot.replaceChildren();
  view.joinSlot.replaceChildren(el("div", { class: "joinbar" },
    el("span", { text: `Page ${before.page} ends mid-sentence (${tail}) and this page starts in lower case. The same paragraph?` }),
    el("button", { class: "small primary", text: "Join with a space", on: { click: () => set("space") } }),
    before.hyphen ? el("button", { class: "small", text: "Join, removing the hyphen", on: { click: () => set("dehyphenate") } }) : null,
    el("button", { class: "small", text: "Keep separate", on: { click: () => { view.joinDismissed = true; refreshJoin(view); } } })));
}

function renderPage(view) {
  const { detail, page, actId, pageNumber } = view;
  const numbers = detail.pages.map((p) => p.page);
  const index = numbers.indexOf(pageNumber);
  const go = (n) => { location.hash = `#/doc/${encodeURIComponent(actId)}/page/${n}`; };
  const nextTodo = detail.pages.find((p) => p.page > pageNumber && p.todo);
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
  const need = routedNeed(view);
  if (need) right.append(el("div", { class: "card routed-note", text: need }));
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
  view.joinSlot = el("div");
  right.append(pagebar, view.joinSlot, tabs, body);
  refreshJoin(view);
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
      view.join = joinOf(draft.units);
      view.draftHandled = true; app.dirty = true;
      banner.remove(); view.showTab("edit"); view.dirtyBadge.hidden = false; refreshJoin(view);
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
    // A flag with no words of its own (an article missing from this version) has no place in the
    // text: its card opens under the flag bar instead.
    const node = view.surface.flagElement(next.id) || document.querySelector(".flag-bar");
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

// A toolbar button for an editing command. It keeps the text selection when clicked; `on` and
// `can` read the editor's status (updateToolbar), so it shows where it applies and whether it is on.
function tool(view, { label, name, title, command, on, can, cls = "" }) {
  const button = el("button", { type: "button", class: `small ${cls}`.trim(), title, "aria-label": name, "aria-pressed": on ? "false" : null }, label);
  button.addEventListener("mousedown", (event) => event.preventDefault());
  button.addEventListener("click", () => { view.surface.run(command); updateToolbar(view); });
  view.tools.push({ button, on, can });
  return button;
}

function updateToolbar(view) {
  if (!view.surface || !view.toolbar) return;
  const status = view.surface.status();
  const editable = app.me.roles.includes("reviewer");
  for (const { button, on, can } of view.tools) {
    button.disabled = !editable || (can ? !can(status) : false);
    if (on) {
      const pressed = Boolean(on(status));
      button.classList.toggle("on", pressed);
      button.setAttribute("aria-pressed", String(pressed));
    }
  }
  view.tableTools.classList.toggle("on", status.inTable);
  rove(view.toolbar);
  if (view.findBar && !view.findBar.hidden) showFindStatus(view, status.find);
}

// The toolbar is one stop for Tab; the arrow keys move along it (WAI-ARIA toolbar pattern), Escape
// goes back to the text. In the text, Escape or Alt+F10 comes to the toolbar.
const shown = (item) => {
  if (item.closest("[hidden]")) return false;
  const menu = item.closest("details");
  return !menu || menu.open || item.localName === "summary";
};
const toolbarItems = (toolbar) => [...toolbar.querySelectorAll("button, input[type=checkbox], summary")].filter((item) => !item.disabled && shown(item));

function rove(toolbar, focus) {
  const items = toolbarItems(toolbar);
  const current = focus || items.find((item) => item.tabIndex === 0) || items[0];
  for (const item of toolbar.querySelectorAll("button, input[type=checkbox], summary")) item.tabIndex = item === current ? 0 : -1;
  if (focus) focus.focus();
}

function toolbarKeys(view, event) {
  if (event.target.matches("input[type=text], input[type=search]")) return;
  const items = toolbarItems(view.toolbar);
  const at = items.indexOf(document.activeElement);
  const move = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: items.length - 1 }[event.key];
  if (move !== undefined) {
    event.preventDefault();
    rove(view.toolbar, items[(move + items.length) % items.length]);
  } else if (event.key === "Escape") {
    event.preventDefault();
    view.surface.view.focus();
  }
}

function focusToolbar(view) {
  const items = toolbarItems(view.toolbar);
  rove(view.toolbar, items.find((item) => item.tabIndex === 0) || items[0]);
}

// Find and replace on this page (Ctrl/Cmd+F). The document page finds and replaces across every page.
function findBar(view) {
  const canEdit = app.me.roles.includes("reviewer");
  const query = el("input", { type: "search", placeholder: "Find on this page", "aria-label": "Find on this page" });
  const replacement = el("input", { type: "text", placeholder: "Replace with", "aria-label": "Replace with", disabled: !canEdit });
  const matchCase = el("input", { type: "checkbox", "aria-label": "Match case" });
  const wholeWord = el("input", { type: "checkbox", "aria-label": "Whole words" });
  const count = el("span", { class: "muted find-count", "aria-live": "polite" });
  const S = () => view.surface, C = window.ReviewEditor.commands;
  const search = () => showFindStatus(view, S().find(query.value, { matchCase: matchCase.checked, wholeWord: wholeWord.checked }));
  const step = (direction) => { S().run(C.findNext(direction), { focus: false }); showFindStatus(view, S().findStatus()); };
  const replaceAll = () => {
    const { count: total } = S().findStatus();
    if (total && S().run(C.replaceAll(replacement.value), { focus: false })) toast(`Replaced ${total} on this page (one undo takes it back)`);
    search();
  };
  query.addEventListener("input", search);
  query.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); step(event.shiftKey ? -1 : 1); } });
  replacement.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); S().run(C.replaceOne(replacement.value), { focus: false }); search(); } });
  [matchCase, wholeWord].forEach((box) => box.addEventListener("change", search));
  const bar = el("div", { class: "findbar-page", role: "search", hidden: true },
    query, el("button", { type: "button", class: "small", title: "Previous match (Shift+Enter)", "aria-label": "Previous match", text: "↑", on: { click: () => step(-1) } }),
    el("button", { type: "button", class: "small", title: "Next match (Enter, or Ctrl/Cmd+G in the text)", "aria-label": "Next match", text: "↓", on: { click: () => step(1) } }),
    count, replacement,
    el("button", { type: "button", class: "small", text: "Replace", disabled: !canEdit, on: { click: () => { S().run(C.replaceOne(replacement.value), { focus: false }); search(); } } }),
    el("button", { type: "button", class: "small", text: "Replace all", disabled: !canEdit, on: { click: replaceAll } }),
    el("label", { class: "muted small" }, matchCase, " Match case"),
    el("label", { class: "muted small" }, wholeWord, " Whole words"),
    el("button", { type: "button", class: "small", title: "Close (Escape)", "aria-label": "Close find", text: "✕", on: { click: () => closeFind(view) } }));
  bar.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeFind(view); } });
  bar.query = query;
  bar.count = count;
  bar.search = search;
  return bar;
}

function showFindStatus(view, { count, current }) {
  const bar = view.findBar;
  bar.count.textContent = !bar.query.value ? "" : !count ? "No matches" : current ? `${current} of ${count}` : `${count} match${count === 1 ? "" : "es"}`;
}

function openFind(view) {
  const bar = view.findBar;
  bar.hidden = false;
  // The words selected (on one line) are what to find, as in a word processor.
  const { from, to } = view.surface.view.state.selection;
  const selected = view.surface.view.state.doc.textBetween(from, to, "\n");
  if (selected && selected.length <= 200 && !selected.includes("\n")) bar.query.value = selected;
  bar.query.focus();
  bar.query.select();
  bar.search();
}

function closeFind(view) {
  view.findBar.hidden = true;
  view.surface.find("");
  view.surface.view.focus();
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
  // The version's language, so the browser checks spelling and hyphenates in Kinyarwanda or French.
  const mount = el("div", { class: `surface layout ${remember("restored") === "1" ? "show-restored" : ""}`, lang: view.detail.act.language || undefined });
  const surface = window.ReviewEditor.createEditor(mount, {
    units: view.units,
    machineUnits: view.machineUnits,
    flags: view.page.flags.filter((f) => !view.resolutions[f.id] && (view.showLow || f.severity !== "low")),
    editable: canEdit,
    label: `Text of page ${view.pageNumber}`,
    onChange: () => changed(view),
    onFlagClick: (id, rect) => openFlag(view, id, rect),
    onUnitFocus: (index) => { const units = view.surface ? view.surface.units() : view.units; showRegion(view, units[index] ? regionFor(view, units[index]) : null); },
    onSelect: () => setTimeout(() => { showLine(view); updateToolbar(view); }, 0),
    onMarkerEdit: (value, apply) => { const next = prompt("Section or paragraph number (empty to remove):", value); if (next !== null) apply(next.trim()); },
    onFind: () => openFind(view),
    onEscape: () => focusToolbar(view),
    onNotice: (message) => toast(message),
  });
  view.surface = surface;
  view.tools = [];
  const C = window.ReviewEditor.commands;
  const t = (spec) => tool(view, spec);
  const format = (type, label, name, keys) => t({ label, name, title: `${name} (${keys})`, cls: `mark-${type}`, command: C.mark(type), on: (s) => s.formats[type].on,
    can: (s) => s.formats[type].can });
  const group = (label, ...buttons) => el("div", { class: "tool-group", role: "group", "aria-label": label, title: label }, buttons);
  // The table commands are a menu, so the toolbar keeps its size when the cursor enters a table.
  const tableMenu = (...buttons) => {
    const menu = el("details", { class: "tool-menu" }, el("summary", { class: "small", title: "Table commands: put the cursor in a table, or select cells by dragging across them", text: "Table ▾" }),
      el("div", { role: "group", "aria-label": "Table" }, buttons));
    menu.addEventListener("click", (event) => { if (event.target.closest("button")) menu.open = false; });
    menu.addEventListener("keydown", (event) => { if (event.key === "Escape" && menu.open) { event.stopPropagation(); menu.open = false; menu.querySelector("summary").focus(); } });
    return menu;
  };
  view.tableTools = tableMenu(
    t({ label: "Insert row above", title: "Insert a row above", command: C.addRowBefore, can: (s) => s.can.addRowBefore }),
    t({ label: "Insert row below", title: "Insert a row below (Tab in the last cell)", command: C.addRowAfter, can: (s) => s.can.addRowAfter }),
    t({ label: "Insert column left", title: "Insert a column to the left", command: C.addColumnBefore, can: (s) => s.can.addColumnBefore }),
    t({ label: "Insert column right", title: "Insert a column to the right", command: C.addColumnAfter, can: (s) => s.can.addColumnAfter }),
    t({ label: "Merge cells", title: "Merge the selected cells (drag across cells, or Shift+arrow keys, to select them); their words are kept", command: C.mergeCells, can: (s) => s.can.mergeCells }),
    t({ label: "Split cell", title: "Split a merged cell back into its rows and columns", command: C.splitCell, can: (s) => s.can.splitCell }),
    t({ label: "Header row", title: "Make this row (and those above it) header rows, or this row (and those below it) body rows", command: C.toggleHeaderRow, on: (s) => s.headerRow, can: (s) => s.can.toggleHeaderRow }),
    t({ label: "Delete row", title: "Delete the selected rows", command: C.deleteRow, cls: "danger-text", can: (s) => s.can.deleteRow }),
    t({ label: "Delete column", title: "Delete the selected columns", command: C.deleteColumn, cls: "danger-text", can: (s) => s.can.deleteColumn }),
    t({ label: "Delete table", title: "Delete this table", command: C.deleteTable, cls: "danger-text", can: (s) => s.can.deleteTable }));
  view.findBar = findBar(view);
  const toolbar = el("div", { class: "toolbar", role: "toolbar", "aria-label": "Editing" },
    group("Structure",
      t({ label: "Heading", title: "Heading (Ctrl/Cmd+Alt+1)", command: C.kind("heading"), on: (s) => s.kind === "heading" }),
      t({ label: "Paragraph", title: "Paragraph (Ctrl/Cmd+Alt+2)", command: C.kind("paragraph"), on: (s) => s.kind === "paragraph" }),
      t({ label: "List item", title: "List item (Ctrl/Cmd+Alt+3)", command: C.kind("list_item"), on: (s) => s.kind === "list_item" }),
      t({ label: "⇤", name: "Outdent", title: "Outdent (Shift+Tab)", command: C.level(-1), can: (s) => Boolean(s.kind) }),
      t({ label: "⇥", name: "Indent", title: "Indent (Tab)", command: C.level(1), can: (s) => Boolean(s.kind) })),
    group("Text",
      format("strong", "B", "Bold", "Ctrl/Cmd+B"),
      format("em", "I", "Italic", "Ctrl/Cmd+I"),
      format("underline", "U", "Underline", "Ctrl/Cmd+U"),
      format("sup", "x²", "Superscript", "Ctrl/Cmd+."),
      format("sub", "x₂", "Subscript", "Ctrl/Cmd+,")),
    group("Alignment",
      t({ label: alignIcon("left"), name: "Align left", title: "Align left (Ctrl/Cmd+Alt+L)", cls: "icon", command: C.align("left"), on: (s) => s.align === "left", can: (s) => Boolean(s.kind) }),
      t({ label: alignIcon("center"), name: "Centre", title: "Centre (Ctrl/Cmd+Alt+E)", cls: "icon", command: C.align("center"), on: (s) => s.align === "center", can: (s) => Boolean(s.kind) }),
      t({ label: alignIcon("right"), name: "Align right", title: "Align right (Ctrl/Cmd+Alt+R)", cls: "icon", command: C.align("right"), on: (s) => s.align === "right", can: (s) => Boolean(s.kind) }),
      t({ label: alignIcon("justify"), name: "Justify", title: "Justify (Ctrl/Cmd+Alt+J). Select several paragraphs, or Ctrl/Cmd+A, to change them all.", cls: "icon", command: C.align("justify"), on: (s) => s.align === "justify", can: (s) => Boolean(s.kind) })),
    group("Insert",
      t({ label: "+ Margin note", title: "Add a margin note (Ctrl/Cmd+Alt+N)", command: C.addNote, can: (s) => s.can.addNote }),
      t({ label: "Note ↑", name: "Move note up", title: "Move this margin note to the previous paragraph (Alt+Shift+↑)", command: C.moveNote(-1), can: (s) => s.inNote }),
      t({ label: "Note ↓", name: "Move note down", title: "Move this margin note to the next paragraph (Alt+Shift+↓)", command: C.moveNote(1), can: (s) => s.inNote }),
      t({ label: "+ Table", title: "Insert a table after this unit", command: C.insertTable, can: (s) => s.can.insertTable }),
      view.tableTools),
    group("History",
      t({ label: "↶", name: "Undo", title: "Undo (Ctrl/Cmd+Z)", command: C.undo }),
      t({ label: "↷", name: "Redo", title: "Redo (Ctrl/Cmd+Shift+Z)", command: C.redo })),
    el("button", { type: "button", class: "small", title: "Find and replace on this page (Ctrl/Cmd+F)", text: "Find", on: { click: () => openFind(view) } }),
    el("label", { class: "muted small", title: "Numbers the main reading missed and the check readings supplied" },
      el("input", { type: "checkbox", checked: remember("restored") === "1", on: { change: (e) => { remember("restored", e.target.checked ? "1" : "0"); mount.classList.toggle("show-restored", e.target.checked); } } }),
      " mark restored numbers"),
    el("details", { class: "keys" }, el("summary", { text: "Keys" }),
      el("div", { class: "muted" },
        el("p", { text: "Type to correct words. Enter splits a paragraph (at its very start, it opens an empty one above); Backspace at its start joins it to the one above, Delete at its end joins the one below. Tab / Shift+Tab change the level. Ctrl/Cmd+Alt+L, E, R, J align left, centre, right or justify; with several paragraphs selected (or Ctrl/Cmd+A) they all change. Double-click a section number to change it." }),
        el("p", { text: "Bold, italic and the rest work as in a word processor: on a selection that is only partly bold, B makes all of it bold. They apply to a paragraph's words, not to section numbers, margin notes, captions or table cells (those hold plain text)." }),
        el("p", { text: "Tables: Tab / Shift+Tab move between cells (Tab in the last cell adds a row). Drag across cells, or Shift+arrow keys, to select several; then Table ▾ › Merge cells, or type to replace their words. The Table menu also adds and deletes rows and columns, splits merged cells and marks header rows." }),
        el("p", { text: "Paste: text from Word, a web page or a PDF becomes paragraphs, headings, list items and tables; bold, italic, underline, superscript and subscript are kept, fonts and colours are not." }),
        el("p", { text: "Margin notes: click into a note, then Note ↑ / Note ↓ (Alt+Shift+↑ / ↓) moves it to the paragraph it belongs beside." }),
        el("p", { text: "Flags: Alt+N next, Alt+P previous; then Alt+1 / Alt+2 use that reading, Alt+K keep as read, Alt+E I corrected it, Alt+D dismiss. Alt+L keeps all punctuation-only differences." }),
        el("p", { text: "Ctrl/Cmd+F find and replace on this page (Ctrl/Cmd+G next match). Escape or Alt+F10 goes to the toolbar; there the arrow keys move along it and Escape returns to the text. Ctrl/Cmd+S save as checked; Ctrl/Cmd+Enter save as checked and go to the next page. Ctrl/Cmd + scroll zooms the page image." }))),
    // Far from the save buttons: it discards this page's corrections (after asking; nothing is saved).
    canEdit ? el("button", { type: "button", class: "small danger restore", title: "Replace the text with the machine reading. Nothing is saved until you save.", text: "Restore machine reading…", on: { click: () => restoreMachineReading(view) } }) : null,
    view.findBar);
  toolbar.addEventListener("keydown", (event) => toolbarKeys(view, event));
  view.toolbar = toolbar;
  updateToolbar(view);
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
    // (DOM append writes a null as the word "null": only the parts present are added.)
    bar.append(...[
      el("strong", { text: `${open.length} open flag${open.length === 1 ? "" : "s"}` }),
      high ? pill("high", `${high} high`) : null,
      el("button", { class: "small", title: "Previous flag (Alt+P)", text: "◀", on: { click: () => gotoFlag(view, -1) } }),
      el("button", { class: "small primary", title: "Next flag (Alt+N)", text: "Next flag ▶", on: { click: () => gotoFlag(view, 1) } }),
    ].filter(Boolean));
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
  const reads = el("div", { class: "reads" }, el("span", { class: "muted", text: flag.compare ? "This version" : "Main reading" }),
    el("q", { text: flag.primary || (flag.missing ? `no article ${flag.missing} found` : "—") }), el("span"));
  for (const [reader, text] of Object.entries(flag.checks)) {
    // Another language's text is shown for comparison only, never offered as a replacement.
    const usable = !flag.compare && !resolution && highlighted && view.surface.applicable(flag.id)
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

function restoreMachineReading(view) {
  if (!confirm("Replace this page's text with the machine reading? Your unsaved corrections and flag decisions on this page are discarded; nothing is saved until you save.")) return;
  view.units = structuredClone(view.machineUnits);
  view.resolutions = {};
  changed(view); view.showTab("edit");
}

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
  const canEdit = app.me.roles.includes("reviewer");
  // Under a routing decision a flagged page is done once every flag is resolved (CPR-01): saved without
  // marking the whole page checked. It is the main action there (Ctrl/Cmd+S).
  const here = view.detail.pages.find((p) => p.page === view.pageNumber) || {};
  const spans = view.detail.readiness.routed && here.todo !== "review" && view.page.flags.length > 0;
  const flagsDone = () => {
    const open = view.page.flags.filter((f) => !view.resolutions[f.id]).length;
    if (open) return toast(`Resolve every flag first: ${open} still open${view.showLow ? "" : " (tick “show punctuation-only” to see them all)"}`);
    save("edited");
  };
  return el("div", { class: "savebar" },
    comment,
    spans ? el("button", { class: "primary", text: "Save: flags resolved", title: "Records your decision on each flag; the rest of the page needs no proofreading", disabled: !canEdit, on: { click: flagsDone } }) : null,
    el("button", { class: spans ? "" : "primary", text: "Save as checked", disabled: !canEdit, on: { click: () => save("checked") } }),
    el("button", { text: "Checked → next page", disabled: !canEdit, on: { click: () => save("checked", true) } }),
    el("button", { text: "Save: needs attention", disabled: !canEdit, on: { click: () => save("attention") } }),
    canEdit ? null : el("span", { class: "muted", text: "Editing needs the reviewer role." }));
}

// --- activity log ---------------------------------------------------------------------------------

async function auditView(actId) {
  const { data } = await api(`/api/audit?act=${encodeURIComponent(actId)}`);
  setCrumbs(el("a", { href: "#/", text: "Documents" }), el("a", { href: `#/doc/${encodeURIComponent(actId)}`, text: actId }), "Activity");
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
  if (!location.hash.includes("/page/") || !document.querySelector(".review")) main(el("div", { class: "loading" }, el("span", { class: "spinner" }), "Loading…"));
  const parts = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  for (const link of document.querySelectorAll("#who .nav-link")) {
    const section = ["access", "operators", "queue"].includes(parts[0]) ? `#/${parts[0]}` : "#/";
    link.classList.toggle("on", link.hash === section);
  }
  try {
    if (parts[0] === "doc" && parts[2] === "page") return await pageView(parts[1], Number(parts[3]));
    if (parts[0] === "doc" && parts[2] === "read") return await readView(parts[1]);
    if (parts[0] === "doc" && parts[2] === "details") return await detailsView(parts[1]);
    if (parts[0] === "doc" && parts[2] === "languages") return await languagesView(parts[1]);
    if (parts[0] === "doc" && parts[2] === "events") return await eventsView(parts[1]);
    if (parts[0] === "doc" && parts[2] === "instructions") return await instructionsView(parts[1]);
    if (parts[0] === "doc" && parts[2] === "citations") return await citationsView(parts[1]);
    if (parts[0] === "queue") return await queueView();
    if (parts[0] === "doc" && parts[1]) return await actView(parts[1]);
    if (parts[0] === "audit" && parts[1]) return await auditView(parts[1]);
    if (parts[0] === "access" && app.me.access) return await accessView(parts.slice(1));
    if (parts[0] === "operators" && app.me.operators) return await operatorsView();
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
