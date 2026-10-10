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

function changed(view) { app.dirty = true; if (view.dirtyBadge) view.dirtyBadge.hidden = false; }

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
  view.image = el("img", { src: `/api/acts/${encodeURIComponent(actId)}/pages/${pageNumber}/image`, alt: `Gazette page ${pageNumber}` });
  view.overlay = el("div", { class: "region", hidden: true });
  const frame = el("div", { class: "frame" }, view.image, view.overlay);
  frame.addEventListener("click", (event) => imageClicked(view, event));
  const viewer = el("div", { class: "viewer" },
    el("div", { class: "viewer-tools" },
      el("button", { class: "small", text: "Zoom", on: { click: (e) => { e.stopPropagation(); e.currentTarget.closest(".viewer").classList.toggle("zoom"); } } }),
      el("span", { class: "muted", text: "Click text to find it on the page; click the page to find its text." })),
    frame);
  const right = el("div");
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

// --- linking text and image ---------------------------------------------------------------------------

function regionFor(view, unit) {
  const target = fold(unitText(unit));
  if (!target) return null;
  return view.page.regions.find((r) => r.role !== "pageHeader" && r.role !== "pageFooter" && fold(r.text) && (fold(r.text).startsWith(target) || target.startsWith(fold(r.text))))
    || view.page.regions.find((r) => fold(r.text).split(" ").slice(0, 3).join(" ") === target.split(" ").slice(0, 3).join(" "));
}

function showRegion(view, region) {
  if (!region) { view.overlay.hidden = true; return; }
  const [x0, y0, x1, y1] = region.box;
  Object.assign(view.overlay.style, { left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%` });
  view.overlay.hidden = false;
  const viewer = view.overlay.closest(".viewer");
  const top = view.overlay.offsetTop - viewer.clientHeight / 3;
  if (view.overlay.offsetTop < viewer.scrollTop || view.overlay.offsetTop > viewer.scrollTop + viewer.clientHeight) viewer.scrollTo({ top, behavior: "smooth" });
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
      el("p", { class: "muted", text: "Type to correct words. Enter splits a paragraph; Backspace at its start joins it to the one above. Tab / Shift+Tab change the level (section, subsection, paragraph, sub-paragraph). Double-click a blue number to change it. Ctrl/Cmd+S saves." })));
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
  if (lowCount) box.append(el("label", { class: "muted" }, el("input", { type: "checkbox", checked: view.showLow, on: { change: (e) => { view.showLow = e.target.checked; currentUnits(view); view.showTab("edit"); } } }), ` include ${lowCount} punctuation-only`));
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
    const usable = !resolution && highlighted && !(flag.kind === "missing_from_primary" && !text);
    reads.append(el("span", { class: "muted", text: reader }), el("q", { text: text || "—" }),
      usable ? el("button", { class: "small", text: text ? `Use ${reader}` : "Remove words", on: { click: () => { view.surface.applyReading(flag.id, text); resolve("use_check", reader); } } }) : el("span"));
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
  if (flag.kind === "marker_conflict") return "about a number: double-click the number in the text to set it";
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
