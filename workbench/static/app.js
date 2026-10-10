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
      el("p", {},
        nextTodo ? el("a", { href: `#/act/${encodeURIComponent(actId)}/page/${nextTodo.page}` }, el("button", { class: "primary", text: `Continue reviewing at page ${nextTodo.page}` })) : null, " ",
        el("a", { href: `#/act/${encodeURIComponent(actId)}/read` }, el("button", { text: "Read the whole Act" }))),
      el("h3", { text: "Pages" }),
      el("p", { class: "muted", text: "Green: checked. Yellow: needs attention. Red border: high flags open. Dashed: the machine reading changed since review." }),
      grid,
    ),
    findReplaceCard(actId),
    approvalPanel(actId, data, current),
  );
}

function findReplaceCard(actId) {
  const find = el("input", { type: "text", placeholder: "Find across the Act" });
  const replace = el("input", { type: "text", placeholder: "Replace with (optional)" });
  const caseBox = el("input", { type: "checkbox" });
  const wordBox = el("input", { type: "checkbox", checked: true });
  const results = el("div");
  let last = null;
  const run = async () => {
    const result = await api(`/api/acts/${encodeURIComponent(actId)}/search?${new URLSearchParams({ q: find.value, case: caseBox.checked, word: wordBox.checked })}`);
    if (!result.ok) return toast(message(result));
    last = { ...result.data, find: find.value, case: caseBox.checked, word: wordBox.checked };
    const byPage = new Map();
    for (const hit of result.data.hits) { if (!byPage.has(hit.page)) byPage.set(hit.page, []); byPage.get(hit.page).push(hit); }
    const boxes = [];
    const list = el("div", { class: "hits" }, [...byPage.entries()].map(([page, hits]) => {
      const box = el("input", { type: "checkbox", checked: true, "data-page": page });
      boxes.push(box);
      return el("div", { class: "hit" },
        el("label", {}, box, " ", el("a", { href: `#/act/${encodeURIComponent(actId)}/page/${page}`, text: `Page ${page}` }), ` · ${hits.length}`),
        ...hits.slice(0, 5).map((h) => el("p", { class: "snippet" }, h.before, el("mark", { text: h.match }), h.after)));
    }));
    const apply = el("button", { class: "primary", text: "Replace in ticked pages", disabled: !app.me.roles.includes("reviewer") || !byPage.size, on: { click: async () => {
      const pages = Object.fromEntries(boxes.filter((b) => b.checked).map((b) => [b.dataset.page, last.revisions[b.dataset.page]]));
      if (!Object.keys(pages).length) return toast("Tick at least one page");
      if (!confirm(`Replace “${last.find}” with “${replace.value}” on ${Object.keys(pages).length} page(s)? Each becomes a new revision in your name.`)) return;
      const done = await api(`/api/acts/${encodeURIComponent(actId)}/replace`, { method: "POST", body: { find: last.find, replace: replace.value, case: last.case, word: last.word, pages } });
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
  return el("div", { class: "card" }, el("h3", { text: "Find and replace across the Act" }),
    el("div", { class: "findbar" }, find, replace, el("button", { text: "Find", on: { click: run } })),
    el("p", { class: "muted" }, el("label", {}, caseBox, " match case"), " ", el("label", {}, wordBox, " whole words only")),
    results);
}

// --- reading the whole Act continuously -------------------------------------------------------------

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
    const body = el("p", {}, unit.marker ? el("span", { class: "restored", text: `${unit.marker} ` }) : null, unit.text);
    const css = unit.kind === "heading" ? "h" : level !== null ? `d${level}` : "";
    box.append(unit.notes && unit.notes.length
      ? el("div", { class: `unit ${css}` }, body, el("div", {}, unit.notes.map((n) => el("p", { class: "note", text: n }))))
      : el("div", { class: css }, body));
  }
  return box;
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
  const { data } = await api(`/api/acts/${encodeURIComponent(actId)}`);
  setCrumbs(el("a", { href: "#/", text: "Acts" }), el("a", { href: `#/act/${encodeURIComponent(actId)}`, text: data.act.title }), "Read");
  const list = el("div", { class: "reading" });
  const sentinel = el("p", { class: "muted", text: "Loading…" });
  const canEdit = app.me.roles.includes("reviewer");
  let next = 1, total = Infinity, busy = false;
  const load = async () => {
    if (busy || next > total) return;
    busy = true;
    const { data: chunk } = await api(`/api/acts/${encodeURIComponent(actId)}/stream?start=${next}&count=6`);
    total = chunk.total;
    for (const page of chunk.pages) list.append(readPage(actId, page, canEdit));
    next += 6;
    busy = false;
    sentinel.textContent = next > total ? "End of the Act." : "Loading…";
    if (next <= total && sentinel.getBoundingClientRect().top < window.innerHeight + 600) load();
  };
  main(el("div", { class: "card" },
    el("h2", { text: data.act.title }),
    el("p", { class: "muted", text: "The Act as it now stands, page by page beside the gazette. Mark pages checked as you read; open a page to edit it." })),
    list, sentinel);
  new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting)) load(); }, { rootMargin: "600px" }).observe(sentinel);
  load();
}

function readPage(actId, page, canEdit) {
  const status = page.status === "checked" ? pill("ok", "checked") : page.status ? pill("warn", page.status) : pill("medium", "not reviewed");
  const flags = page.open_high ? pill("high", `${page.open_high} high flags open`) : page.open ? pill("medium", `${page.open} flags open`) : null;
  const mark = canEdit && page.status !== "checked" && !page.open_high
    ? el("button", { class: "small", text: "Mark checked", on: { click: async (event) => {
        const result = await api(`/api/acts/${encodeURIComponent(actId)}/pages/${page.page}`, { method: "POST", body: { expected_revision: page.revision, units: page.units, status: "checked", resolutions: page.resolutions || {}, comment: "Checked while reading the whole Act" } });
        if (result.status === 409) return toast(`Page ${page.page} changed meanwhile; open it to check`);
        if (!result.ok) return toast(message(result));
        page.revision = result.data.revision; page.status = "checked";
        event.currentTarget.replaceWith(pill("ok", "checked"));
      } } })
    : null;
  return el("section", { class: "readpage", id: `read-${page.page}` },
    el("div", { class: "viewer" }, el("img", { loading: "lazy", src: `/api/acts/${encodeURIComponent(actId)}/pages/${page.page}/image`, alt: `Gazette page ${page.page}` })),
    el("div", {},
      el("div", { class: "pagebar" }, el("strong", { text: `Page ${page.page}` }), status, flags,
        el("a", { href: `#/act/${encodeURIComponent(actId)}/page/${page.page}` }, el("button", { class: "small", text: "Edit page" })), mark),
      el("div", { class: "card" }, renderUnits(page.units))));
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
    el("a", { href: `/api/acts/${encodeURIComponent(actId)}/akn`, text: "Akoma Ntoso XML" }), " · ",
    el("a", { href: `#/audit/${encodeURIComponent(actId)}`, text: "Activity log" })));
  return panel;
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
  setCrumbs(el("a", { href: "#/", text: "Acts" }), el("a", { href: `#/act/${encodeURIComponent(actId)}`, text: detail.act.title }), `Page ${pageNumber}`);
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
  const go = (n) => { location.hash = `#/act/${encodeURIComponent(actId)}/page/${n}`; };
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
  view.image = el("img", { src: `/api/acts/${encodeURIComponent(actId)}/pages/${pageNumber}/image`, alt: `Gazette page ${pageNumber}`, draggable: "false" });
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

function editTab(view) {
  const canEdit = app.me.roles.includes("reviewer");
  const mount = el("div", { class: "surface layout" });
  const surface = window.ReviewEditor.createEditor(mount, {
    units: view.units,
    machineUnits: view.machineUnits,
    flags: view.page.flags.filter((f) => !view.resolutions[f.id] && (view.showLow || f.severity !== "low")),
    editable: canEdit,
    onChange: () => changed(view),
    onFlagClick: (id, rect) => openFlag(view, id, rect),
    onUnitFocus: (index) => { const units = view.surface ? view.surface.units() : view.units; showRegion(view, units[index] ? regionFor(view, units[index]) : null); },
    onSelect: () => setTimeout(() => showLine(view), 0),
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
    el("button", { class: "small", title: "Outdent (Shift+Tab)", text: "⇤", on: { click: run(C.level(-1)) } }),
    el("button", { class: "small", title: "Indent (Tab)", text: "⇥", on: { click: run(C.level(1)) } }),
    el("button", { class: "small", title: "Add a margin note (Ctrl/Cmd+Alt+N)", text: "+ Margin note", on: { click: run(C.addNote) } }),
    el("button", { class: "small", title: "Insert a table after this unit", text: "+ Table", on: { click: run(C.insertTable) } }),
    el("button", { class: "small", title: "Add a row below (in a table)", text: "+ Row", on: { click: run(C.addRow) } }),
    el("button", { class: "small", title: "Add a column (in a table)", text: "+ Column", on: { click: run(C.addColumn) } }),
    el("span", { class: "sep" }),
    el("button", { class: "small", title: "Undo (Ctrl/Cmd+Z)", text: "↶", on: { click: run(C.undo) } }),
    el("button", { class: "small", title: "Redo (Ctrl/Cmd+Shift+Z)", text: "↷", on: { click: run(C.redo) } }),
    el("details", { class: "keys" }, el("summary", { text: "Keys" }),
      el("div", { class: "muted" },
        el("p", { text: "Type to correct words. Enter splits a paragraph; Backspace at its start joins it to the one above. Tab / Shift+Tab change the level. Double-click a blue number to change it." }),
        el("p", { text: "Flags: Alt+N next, Alt+P previous; then Alt+1 / Alt+2 use that reading, Alt+K keep as read, Alt+E I corrected it, Alt+D dismiss. Alt+L keeps all punctuation-only differences." }),
        el("p", { text: "Ctrl/Cmd+S save as checked; Ctrl/Cmd+Enter save as checked and go to the next page. Ctrl/Cmd + scroll zooms the page image." }))));
  const list = flagList(view);
  return [list, toolbar, mount, saveBar(view)];
}

function flagList(view) {
  const all = view.page.flags;
  const shown = all.filter((f) => view.showLow || f.severity !== "low");
  const open = shown.filter((f) => !view.resolutions[f.id]);
  const lowCount = all.filter((f) => f.severity === "low").length;
  // Highlighted flags are worked in the text; the list opens itself only for high flags that cannot be.
  const hidden = open.some((f) => f.severity === "high" && !(view.surface && view.surface.highlighted(f.id)));
  const box = el("details", { class: "flag-list", open: hidden || null },
    el("summary", {},
      `${open.length} open flag${open.length === 1 ? "" : "s"}`,
      ` (${open.filter((f) => f.severity === "high").length} high) — highlighted in the text; click one to resolve it.`),
  );
  if (lowCount) {
    const openLow = all.filter((f) => f.severity === "low" && !view.resolutions[f.id]).length;
    box.append(el("p", { class: "muted" },
      el("label", {}, el("input", { type: "checkbox", checked: view.showLow, on: { change: (e) => { view.showLow = e.target.checked; currentUnits(view); view.showTab("edit"); } } }), ` include ${lowCount} punctuation-only`), " ",
      openLow ? el("button", { class: "small", text: `Keep all ${openLow} punctuation-only as read (Alt+L)`, on: { click: () => resolveLow(view) } }) : null));
  }
  for (const flag of shown) box.append(flagCard(view, flag, true));
  if (!shown.length) box.append(el("p", { class: "muted", text: "No flags on this page. Compare the text with the image." }));
  return box;
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
        inList && highlighted ? el("button", { class: "small", text: "Show in text", on: { click: () => view.surface.reveal(flag.id) } }) : null,
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
  const list = document.querySelector(".flag-list");
  if (list) list.replaceWith(flagList(view));
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
    if (status === "checked" && high.length && !confirm(`${high.length} high flag(s) are not resolved. Save as checked anyway? (The Act cannot be approved until they are.)`)) return;
    const result = await api(`/api/acts/${encodeURIComponent(view.actId)}/pages/${view.pageNumber}`, {
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
    if (thenNext && next) location.hash = `#/act/${encodeURIComponent(view.actId)}/page/${next}`;
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
    if (parts[0] === "act" && parts[2] === "read") return await readView(parts[1]);
    if (parts[0] === "act" && parts[1]) return await actView(parts[1]);
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
