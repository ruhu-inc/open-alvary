"use strict";

// Operators: who may use the workbench, for administrators. Adding a person, changing their roles,
// deactivating them and issuing a passkey code each need the administrator's passkey (passkeys.js).
// Uses app.js's helpers (el, api, main, toast, setCrumbs, pill). No inline handlers (strict CSP).

const ROLE_LABELS = {
  reviewer: ["Reviewer", "checks and corrects pages"],
  approver: ["Approver", "approves documents for publication"],
  admin: ["Administrator", "manages operators"],
  access_admin: ["Access administrator", "the Access section: developer accounts and API access"],
};

// A group of role checkboxes that reads back as a list (form() calls read()).
function roleChoice(roles, chosen = []) {
  const boxes = roles.map((role) => {
    const box = el("input", { type: "checkbox", value: role, checked: chosen.includes(role) });
    const [label, hint] = ROLE_LABELS[role] || [role, ""];
    return el("label", { class: "role-choice" }, box, el("span", {}, el("strong", { text: label }), hint ? el("span", { class: "muted", text: ` · ${hint}` }) : null));
  });
  const group = el("div", { class: "role-choices" }, boxes);
  group.read = () => boxes.map((label) => label.querySelector("input")).filter((box) => box.checked).map((box) => box.value);
  return group;
}

async function operatorsView() {
  setCrumbs("Operators");
  const [bar, listed] = await Promise.all([passkeyBar(), api("/api/operators")]);
  if (!listed.ok) return main(el("p", { text: message(listed) }));
  const { operators, roles, iap_group: group } = listed.data;
  const again = () => route();
  const codeBox = el("div", { class: "code-box", hidden: true });
  const showCode = (email) => (data) => {
    codeBox.replaceChildren(
      el("p", {}, "One-time code for ", el("strong", { text: email }), " (once, within 24 hours):"),
      el("code", { class: "enrolment-code", text: data.code }),
      el("p", { class: "muted", text: "Shown only now. Give it to them yourself; they enter it on the Access or Operators page to register their passkey." }));
    codeBox.hidden = false;
    codeBox.scrollIntoView({ block: "center" });
  };
  const rows = operators.map((o) => {
    const path = `/api/operators/${encodeURIComponent(o.email)}`;
    const actions = el("details", {}, el("summary", { text: "Change" }),
      form("Name and roles", [["name", "Name", el("input", { type: "text", value: o.name, maxlength: "200" })], ["roles", "Roles", roleChoice(roles, o.roles)]], "Save",
        (v) => change("/api/operators", { email: o.email, name: v.name, roles: v.roles }, again), { evidence: false, reason: false }),
      o.active && o.roles.some((role) => role === "admin" || role === "access_admin")
        ? form("", [], o.passkeys ? "Replace a lost passkey: new code" : "Issue a passkey code",
          () => change(`${path}/enrolment`, { replace: o.passkeys > 0 }, showCode(o.email)), { evidence: false, reason: false })
        : null,
      form("", [], o.active ? "Deactivate" : "Reactivate", (v) => change(`${path}/active`, { active: !o.active }, again), { evidence: false, reason: false, danger: o.active }));
    return el("tr", { class: o.active ? "" : "inactive" },
      el("td", {}, el("strong", { text: o.name }), el("div", { class: "muted small", text: o.email })),
      el("td", {}, ...o.roles.map((role) => pill("role", (ROLE_LABELS[role] || [role])[0]))),
      el("td", {}, pill(o.active ? "ok" : "warn", o.active ? "active" : "inactive")),
      el("td", { class: "muted", text: o.last_sign_in ? `${ago(o.last_sign_in)}` : "never" }),
      el("td", { text: o.passkeys ? `${o.passkeys}` : "—" }),
      el("td", {}, actions));
  });
  const email = el("input", { type: "email", placeholder: "their.name@gmail.com or @ruhu.ai", maxlength: "320" });
  const name = el("input", { type: "text", placeholder: "Full name", maxlength: "200" });
  main(
    bar,
    codeBox,
    el("div", { class: "card" },
      el("h3", { text: `Operators (${operators.filter((o) => o.active).length} active)` }),
      el("table", { class: "list operators" },
        el("thead", {}, el("tr", {}, ["Person", "Roles", "Status", "Last sign-in", "Passkeys", ""].map((h) => el("th", { text: h })))),
        el("tbody", {}, rows))),
    el("div", { class: "card" },
      el("h3", { text: "Add a person" }),
      el("p", { class: "muted", text: "Use the Google account they sign in with: a Workspace address or a personal one such as Gmail." }),
      form("", [["email", "Google account", email], ["name", "Name", name], ["roles", "Roles", roleChoice(roles, ["reviewer"])]], "Add",
        (v) => change("/api/operators", { email: v.email, name: v.name, roles: v.roles }, again), { evidence: false, reason: false })),
    el("div", { class: "card muted-card" },
      el("h3", { text: "Reaching the workbench" }),
      el("p", {}, "People also pass Google's sign-in gate (IAP) before they see this page. ",
        group
          ? el("span", {}, "Add their Google account to ", el("strong", { text: group }), " in Google Workspace too, and remove it when they leave.")
          : el("span", { text: "Ask the project's owner to let them through (deployment/gcp/workbench.sh operator)." }))),
  );
}
