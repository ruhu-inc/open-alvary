"use strict";

// The Traffic section (TRF-04): how the public website and the API are being used, as daily totals, for
// operators with the admin role. el, api, main, setCrumbs and message are in app.js. No inline handlers.

const TRAFFIC_RANGES = [7, 30, 90, 365];
let trafficDays = 30;

const trafficNumber = (value) => value.toLocaleString("en-GB");
const trafficDay = (day) =>
  new Date(`${day}T00:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });

function trafficCountry(code) {
  if (code === "unknown") return "Not known";
  if (code === "all") return "All countries";
  try {
    return new Intl.DisplayNames(["en"], { type: "region" }).of(code.toUpperCase()) || code;
  } catch {
    return code;
  }
}

function trafficSvg(tag, attrs = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.append(...children);
  return node;
}

// One measure a chart, each on its own scale: a line per day, the crosshair and readout on hover. Every
// value is also in the day-by-day table below the charts.
function trafficLine(title, daily, key) {
  const width = 640, height = 190, left = 44, right = 44, top = 14, bottom = 26;
  const peak = Math.max(1, ...daily.map((row) => row[key]));
  const x = (i) => left + (i * (width - left - right)) / Math.max(1, daily.length - 1);
  const y = (value) => top + (height - top - bottom) * (1 - value / peak);
  const points = daily.map((row, i) => `${x(i).toFixed(1)},${y(row[key]).toFixed(1)}`).join(" ");
  const text = (value, attrs) => {
    const node = trafficSvg("text", { class: "traffic-axis", ...attrs });
    node.textContent = value;
    return node;
  };
  const last = daily[daily.length - 1];
  const crosshair = trafficSvg("line", { class: "traffic-crosshair", y1: top, y2: height - bottom, visibility: "hidden" });
  const dot = trafficSvg("circle", { class: "traffic-dot", r: 4, visibility: "hidden" });
  const plot = trafficSvg(
    "svg",
    { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${title}: ${trafficNumber(last[key])} on the last day, at most ${trafficNumber(peak)}` },
    trafficSvg("line", { class: "traffic-grid", x1: left, x2: width - right, y1: y(peak), y2: y(peak) }),
    trafficSvg("line", { class: "traffic-grid", x1: left, x2: width - right, y1: y(0), y2: y(0) }),
    text(trafficNumber(peak), { x: left - 6, y: y(peak) + 4, "text-anchor": "end" }),
    text("0", { x: left - 6, y: y(0) + 4, "text-anchor": "end" }),
    text(trafficDay(daily[0].day), { x: left, y: height - 8 }),
    text(trafficDay(last.day), { x: width - right, y: height - 8, "text-anchor": "end" }),
    trafficSvg("polyline", { class: "traffic-line", points }),
    text(trafficNumber(last[key]), { x: width - right + 6, y: y(last[key]) + 4 }),
    crosshair,
    dot,
  );
  const readout = el("div", { class: "traffic-readout", hidden: true });
  const hide = () => {
    readout.hidden = true;
    crosshair.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
  };
  plot.addEventListener("pointermove", (event) => {
    const box = plot.getBoundingClientRect();
    const position = ((event.clientX - box.left) / box.width) * width;
    const i = Math.max(0, Math.min(daily.length - 1, Math.round(((position - left) / (width - left - right)) * (daily.length - 1))));
    const row = daily[i];
    for (const [node, attrs] of [[crosshair, { x1: x(i), x2: x(i) }], [dot, { cx: x(i), cy: y(row[key]) }]]) {
      for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
      node.setAttribute("visibility", "visible");
    }
    readout.replaceChildren(el("strong", { text: trafficNumber(row[key]) }), ` ${trafficDay(row.day)}`);
    readout.hidden = false;
    readout.style.left = `${Math.min(box.width - 90, Math.max(0, (x(i) / width) * box.width - 40))}px`;
  });
  plot.addEventListener("pointerleave", hide);
  return el("div", { class: "card traffic-chart" }, el("h3", { text: title }), el("div", { class: "traffic-plot" }, plot, readout));
}

function trafficTile(label, value, note) {
  return el("div", { class: "traffic-tile" },
    el("div", { class: "muted", text: label }),
    el("div", { class: "traffic-value", text: value }),
    note ? el("div", { class: "muted traffic-note", text: note }) : "");
}

function trafficBar(share) {
  const bar = el("div", { class: "traffic-bar" });
  bar.style.width = `${Math.max(1, share * 100)}%`; // set here: the workbench's policy refuses style attributes
  return bar;
}

// Who or what, how many: the bar shows the size, the number stays in plain ink.
function trafficList(title, rows, name = (label) => label, note) {
  const peak = Math.max(1, ...rows.map((row) => row.count));
  return el("div", { class: "card traffic-list" }, el("h3", { text: title }),
    note ? el("p", { class: "muted traffic-note", text: note }) : "",
    rows.length
      ? el("table", { class: "list" }, el("tbody", {}, rows.map((row) => el("tr", {},
          el("td", { class: "traffic-label", text: name(row.label) }),
          el("td", { class: "traffic-size" }, trafficBar(row.count / peak)),
          el("td", { class: "traffic-count", text: trafficNumber(row.count) })))))
      : el("p", { class: "muted", text: "Nothing yet." }));
}

async function trafficView() {
  const turn = ++app.turn;
  setCrumbs("Traffic");
  const result = await api(`/api/traffic?days=${trafficDays}`);
  if (turn !== app.turn) return;
  if (!result.ok) return main(el("div", { class: "card" }, el("p", { text: message(result) })));
  const { daily, totals, lists, top_records: top, developers } = result.data;
  const anything = daily.some((row) => row.arrivals || row.searches || row.records || row.bots);
  const ranges = el("div", { class: "traffic-ranges", role: "group", "aria-label": "Period" },
    TRAFFIC_RANGES.map((days) => el("button", {
      class: days === trafficDays ? "primary" : "",
      "aria-pressed": days === trafficDays ? "true" : "false",
      text: `${days} days`,
      on: { click: () => { trafficDays = days; trafficView(); } },
    })));
  const share = totals.searches ? `${Math.round((totals.none_found / totals.searches) * 100)}%` : "–";
  const used = developers.keys_new ? ` · ${trafficNumber(developers.keys_new_used)} used so far` : "";
  const lost = lists.lost.reduce((sum, row) => sum + row.count, 0);
  const visitorDays = daily.filter((row) => row.visitors).length;
  main(
    ranges,
    anything ? "" : el("div", { class: "card" }, el("p", { class: "muted", text: "No counts in this period. Counting starts when ALVARY_TRAFFIC_COUNTS is switched on, and the numbers appear within a few minutes of the first visits." })),
    el("div", { class: "traffic-tiles" },
      trafficTile("Page arrivals", trafficNumber(totals.arrivals), "full page loads by people"),
      visitorDays ? trafficTile("Visitors a day", trafficNumber(Math.round(totals.visitors / visitorDays)), "estimate, on average; the same person counts again on another day") : "",
      trafficTile("Searches", trafficNumber(totals.searches)),
      trafficTile("Searches finding nothing", share, `${trafficNumber(totals.none_found)} searches`),
      trafficTile("Records opened", trafficNumber(totals.records)),
      trafficTile("Texts read", trafficNumber(totals.texts)),
      trafficTile("Release downloads", trafficNumber(totals.downloads)),
      trafficTile("Automated requests", trafficNumber(totals.bots), "not counted above")),
    el("div", { class: "traffic-grid-cards" },
      trafficLine("Page arrivals per day", daily, "arrivals"),
      trafficLine("Searches per day", daily, "searches"),
      trafficLine("Records opened per day", daily, "records")),
    el("div", { class: "traffic-grid-cards" },
      trafficList("Where people come from", lists.country, trafficCountry),
      trafficList("Sites that send people", lists.referrer, (label) => (label === "direct" ? "Direct (no site)" : label === "other" ? "Others" : label)),
      trafficList("First page they open", lists.landing, (label) => label[0].toUpperCase() + label.slice(1)),
      trafficList("Devices", lists.device, (label) => label[0].toUpperCase() + label.slice(1)),
      trafficList("Records opened, by the record's country", lists.read_country, trafficCountry),
      trafficList("Searches, by the country chosen", lists.searched_country, trafficCountry),
      trafficList("Search result opened, by position", lists.opened_position, (label) => `Result ${label}`),
      trafficList("Release downloads, by country and kind", lists.downloads),
      trafficList("Automated clients", lists.bots, (label) => label.replace(/^ai:/, "AI: ").replace(/^search:/, "Search: ").replace(/^monitor:/, "Monitor: ").replace(/^other$/, "Other programs"))),
    el("div", { class: "card" },
      el("h3", { text: `Most opened records, last ${top.window === "7d" ? "7" : "30"} days` }),
      el("p", { class: "muted traffic-note", text: top.computed_at ? `Worked out once a day, last on ${day(top.computed_at)}.` : "Worked out once a day." }),
      top.items.length
        ? el("table", { class: "list" }, el("tbody", {}, top.items.map((row) => el("tr", {},
            el("td", { text: row.source_id }),
            el("td", { class: "traffic-count", text: trafficNumber(row.views) })))))
        : el("p", { class: "muted", text: "Nothing yet." })),
    el("div", { class: "card" }, el("h3", { text: "Developers" }),
      el("div", { class: "traffic-tiles" },
        trafficTile("People with accounts", trafficNumber(developers.people), `${trafficNumber(developers.people_new)} new in this period`),
        trafficTile("Organisations", trafficNumber(developers.organisations), `${trafficNumber(developers.organisations_week)} active in the last 7 days`),
        trafficTile("Keys created", trafficNumber(developers.keys_new), `in this period${used}`),
        trafficTile("Apps connected", trafficNumber(developers.apps_new), "in this period"),
        trafficTile("Keys and apps active", trafficNumber(developers.credentials_week), "in the last 7 days"),
        trafficTile("API requests", trafficNumber(developers.requests_rest), "in this period"),
        trafficTile("MCP requests", trafficNumber(developers.requests_mcp), "in this period"),
        trafficTile("Bulk requests", trafficNumber(developers.requests_bulk), "in this period"))),
    el("details", { class: "card" }, el("summary", { text: "Day by day" }),
      el("div", { class: "traffic-wide" }, el("table", { class: "list" },
        el("thead", {}, el("tr", {}, ["Day", "Arrivals", ...(totals.visitors ? ["Visitors"] : []), "Searches", "Found nothing", "Records", "Texts", "Downloads", "Automated"].map((name) => el("th", { text: name })))),
        el("tbody", {}, [...daily].reverse().map((row) => el("tr", {},
          el("td", { text: trafficDay(row.day) }),
          ["arrivals", ...(totals.visitors ? ["visitors"] : []), "searches", "none_found", "records", "texts", "downloads", "bots"].map((key) => el("td", { text: trafficNumber(row[key]) })))))))),
    el("p", { class: "muted traffic-note", text: `Totals only: no visit, address or searched word is kept, and today's numbers are still growing.${lost ? ` ${trafficNumber(lost)} counts could not be kept in this period.` : ""}` }),
  );
}
