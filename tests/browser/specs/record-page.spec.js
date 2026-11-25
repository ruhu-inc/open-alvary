// The record page's text, on the local site (scripts/record_page_local.py): the built website and the
// website's real data path over three published texts: an invented Rwandan law in three languages with
// its structure, a longer one with a table, and a text with no structure. The page is sent one text at a
// time as reader-1 (/site/v1/sources/{id}/text). No key, no secret and no outside call.
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const SITE = `http://127.0.0.1:${Number(process.env.RECORD_PAGE_PORT || 8774)}`;
const LAW = "/sources/rw-law-000-2099";
const LONG = "/sources/rw-law-001-2099";
const PLAIN = "/sources/ng-foi-act-2011";

test.use({ baseURL: SITE });

// Nothing may leave this computer. Every request to another address is stopped and written down.
const guard = (page) =>
  page.route("**/*", (route) => {
    const url = route.request().url();
    if (url.startsWith(SITE) || url.startsWith("data:") || url.startsWith("blob:")) return route.continue();
    page.outside.push(url);
    return route.abort();
  });
test.beforeEach(async ({ page }) => {
  page.outside = [];
  await guard(page);
});
test.afterEach(async ({ page }) => {
  expect(page.outside).toEqual([]);
});

const text = (page) => page.locator(".pub-text");
const languages = (page) => page.getByRole("navigation", { name: "Language versions" });
const contents = (page) => page.getByRole("navigation", { name: "Contents" });

/** The record's texts as the data path serves them one at a time, by language: what the page is compared with. */
async function served(page, source) {
  const first = await (await page.request.get(`/site/v1${source}/text`)).json();
  const texts = {};
  for (const listed of first.texts) {
    const answer = await page.request.get(`/site/v1${source}/text?version=${listed.id}`);
    expect(answer.ok()).toBe(true);
    const body = await answer.json();
    texts[listed.language] = { ...body.text, canonical_text: body.canonical_text };
  }
  return texts;
}

/** Answers the page's next requests for a text as the data path would refuse them. */
const refuse = (page, status, code, headers = {}) =>
  page.route(/\/site\/v1\/sources\/[^/]+\/text/, (route) =>
    route.fulfill({
      status,
      contentType: "application/problem+json",
      headers,
      body: JSON.stringify({ status, code, title: "Request refused", detail: "x", retryable: status !== 413 }),
    }),
  );
const problem = (page) => page.locator(".pub-problem");

/** How far below the top of the window an element starts. */
const top = (locator) => locator.evaluate((element) => Math.round(element.getBoundingClientRect().top));

test.describe("a published law in three languages", () => {
  test("opens in the record's own language, and the switch changes the text, its hash and the contents @smoke", async ({ page }) => {
    const versions = await served(page, LAW);
    await page.goto(LAW);

    // By language name, not by version identifier; the record's own language, not the first served.
    await expect(languages(page).getByRole("link")).toHaveText(["English", "Français", "Ikinyarwanda"]);
    await expect(languages(page).locator("[aria-current]")).toHaveText("Ikinyarwanda");
    await expect(text(page)).toHaveAttribute("lang", "rw");
    await expect(page.locator(".pub-reader")).not.toContainText("ver-rw-law");
    await expect(page.locator(".pub-reader .hash")).toHaveText("SHA-256 " + versions.rw.content_hash);
    await expect(page.locator(".pub-reader .badge")).toHaveText("Open full text");
    await expect(page.locator(".pub-reader .copy-note")).toContainText("official copy");

    // Every character of the published text is on the page once, in order.
    expect(await text(page).evaluate((element) => element.textContent)).toBe(versions.rw.canonical_text);

    await languages(page).getByRole("link", { name: "Français" }).click();
    await expect(languages(page).locator("[aria-current]")).toHaveText("Français");
    await expect(text(page)).toHaveAttribute("lang", "fr");
    await expect(page).toHaveURL(/\?lang=fr$/);
    await expect(page.locator(".pub-reader .hash")).toHaveText("SHA-256 " + versions.fr.content_hash);
    expect(await text(page).evaluate((element) => element.textContent)).toBe(versions.fr.canonical_text);
    await expect(contents(page).getByRole("link").first()).toHaveText("CHAPITRE PREMIER: DISPOSITIONS GÉNÉRALES");
  });

  test("is fetched one text at a time: the switch fetches the next, once @smoke", async ({ page }) => {
    const versions = await served(page, LAW);
    const fetched = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (!/\/site\/v1\/sources\/[^/]+\/(text|versions)$/.test(url.pathname)) return;
      fetched.push(url.pathname + url.search);
      // The page sends nothing extra with it.
      expect(request.headers()["x-text-switch"]).toBeUndefined();
    });
    const data = "/site/v1" + LAW + "/text";
    await page.goto(LAW);
    await expect(text(page)).toHaveAttribute("lang", "rw");
    // The server chose the text; the page never asks for all of them.
    expect(fetched).toEqual([data]);
    const [first] = await Promise.all([
      page.waitForResponse((answer) => answer.url().includes("/text?version=")),
      languages(page).getByRole("link", { name: "Français" }).click(),
    ]);
    const answer = await first.json();
    expect([answer.schema, answer.text.language, answer.matched, answer.missed]).toEqual(["reader-1", "fr", "version", []]);
    expect(answer.texts.map((listed) => listed.language)).toEqual(["en", "fr", "rw"]);
    // Nodes in document order, with no parents and no hashes: only what the page lays the text out from.
    expect(answer.nodes.length).toBeGreaterThan(5);
    expect(answer.nodes.every((node) => !("parent_id" in node) && !("span_hash" in node) && !("version_id" in node))).toBe(true);
    await expect(text(page)).toHaveAttribute("lang", "fr");
    expect(fetched).toEqual([data, `${data}?version=${versions.fr.id}`]);

    // A text already read is not fetched again.
    await languages(page).getByRole("link", { name: "Ikinyarwanda" }).click();
    await expect(text(page)).toHaveAttribute("lang", "rw");
    await expect(page).toHaveURL(/\?lang=rw$/);
    expect(await text(page).evaluate((element) => element.textContent)).toBe(versions.rw.canonical_text);
    expect(fetched).toHaveLength(2);

    // A link that names a language asks for that one alone.
    await page.goto(LAW + "?lang=fr");
    await expect(text(page)).toHaveAttribute("lang", "fr");
    expect(fetched.slice(2)).toEqual([data + "?language=fr"]);
  });

  test("a link to a text the record does not have opens another, says so, and corrects its address @smoke", async ({ page }) => {
    const versions = await served(page, LAW);
    const line = page.locator(".pub-missed");
    // A language the site has a name for; one it has not; and something that is no language at all.
    for (const [asked, said] of [
      ["?lang=pt#article-2", "This record has no Português text. Showing the Ikinyarwanda text."],
      ["?lang=zz", "This record has no text in that language. Showing the Ikinyarwanda text."],
      ["?lang=a%20b", "This record has no text in that language. Showing the Ikinyarwanda text."],
      ["?version=ver-gone&lang=fr#article-2", "The text this link names is not here. Showing the Français text."],
      ["?version=ver-gone&lang=pt", "The text this link names is not here. Showing the Ikinyarwanda text."],
    ]) {
      await page.goto(LAW + asked);
      await expect(line).toHaveText(said);
      const shown = said.includes("Français") ? "fr" : "rw";
      await expect(text(page)).toHaveAttribute("lang", shown);
      // The address names the text shown, and the anchor, which belonged to another text, is gone.
      await expect(page).toHaveURL(new RegExp(`${LAW}\\?lang=${shown}$`));
      expect(await text(page).evaluate((element) => element.textContent)).toBe(versions[shown].canonical_text);
    }
    // The line belongs to the link: it goes with the first choice, and does not come back.
    await languages(page).getByRole("link", { name: "English" }).click();
    await expect(text(page)).toHaveAttribute("lang", "en");
    await expect(line).toHaveCount(0);
    await languages(page).getByRole("link", { name: "Ikinyarwanda" }).click();
    await expect(text(page)).toHaveAttribute("lang", "rw");
    await expect(line).toHaveCount(0);

    // A version that is found settles it. The address named another language: corrected without a
    // word, and the anchor, which belongs to the text found, is kept.
    await page.setViewportSize({ width: 1280, height: 420 });
    await page.goto(`${LAW}?lang=fr&version=${versions.en.id}#article-3`);
    await expect(text(page)).toHaveAttribute("lang", "en");
    await expect(line).toHaveCount(0);
    await expect(page).toHaveURL(new RegExp(`${LAW}\\?lang=en#article-3$`));
    await expect(page.locator("#article-3")).toBeInViewport();
  });

  test("says why there is no text, by the refusal's code, and lets the reader try again", async ({ page }) => {
    // Too large for one answer: the official copy, and nothing promised.
    await refuse(page, 413, "result_too_large");
    await page.goto(LAW);
    await expect(problem(page)).toHaveText("This text is too large to show on this page. Read the official copy.");
    await expect(problem(page).getByRole("link", { name: "official copy" })).toHaveAttribute("href", /^https?:/);
    await expect(page.locator(".pub-text")).toHaveCount(0);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible(); // the record itself is shown

    // Busy, by any of its codes: a line and a control, and the control fetches the text.
    for (const [status, code] of [[429, "rate_limited"], [503, "capacity_exhausted"], [503, "temporarily_unavailable"]]) {
      await page.unrouteAll({ behavior: "wait" });
      await guard(page);
      await refuse(page, status, code, { "Retry-After": "7" });
      await page.goto(LAW);
      await expect(problem(page)).toContainText("The text cannot be sent right now. Please try again shortly.");
      await expect(problem(page)).not.toContainText(String(status));
      await page.unrouteAll({ behavior: "wait" });
      await guard(page);
      await problem(page).getByRole("button", { name: "Try again" }).click();
      await expect(text(page)).toHaveAttribute("lang", "rw");
      await expect(problem(page)).toHaveCount(0);
    }

    // Anything else on first load, or no answer at all.
    await page.route(/\/site\/v1\/sources\/[^/]+\/text/, (route) => route.abort());
    await page.goto(LAW);
    await expect(problem(page)).toContainText("This text could not be loaded. Please try again.");
    await page.unrouteAll({ behavior: "wait" });
    await guard(page);
    await problem(page).getByRole("button", { name: "Try again" }).click();
    await expect(text(page)).toHaveAttribute("lang", "rw");
  });

  test("a page older than the data path says so, and a record that goes takes its text with it", async ({ page }) => {
    // The data path speaks a contract this page does not know: no guess at the text.
    await page.route(/\/site\/v1\/sources\/[^/]+\/text/, async (route) => {
      const answer = await route.fetch();
      await route.fulfill({ response: answer, json: { ...(await answer.json()), schema: "reader-2" } });
    });
    await page.goto(LAW);
    await expect(problem(page)).toContainText("This page is out of date.");
    await expect(problem(page).getByRole("button", { name: "Reload" })).toBeVisible();
    await expect(page.locator(".pub-text")).toHaveCount(0);
    await page.unrouteAll({ behavior: "wait" });
    await guard(page);

    // The record is withdrawn between opening one language and asking for another.
    await page.goto(LAW);
    await expect(text(page)).toHaveAttribute("lang", "rw");
    await page.route(/\/text\?version=/, (route) =>
      route.fulfill({
        status: 404,
        contentType: "application/problem+json",
        body: JSON.stringify({ status: 404, code: "unavailable", title: "Not available", detail: "x" }),
      }),
    );
    await languages(page).getByRole("link", { name: "Français" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("There is no record at this address.");
    await expect(page.locator(".pub-text")).toHaveCount(0);
  });

  test("a text that cannot be fetched leaves the one being read, and says so", async ({ page }) => {
    const versions = await served(page, LAW);
    await page.goto(LAW);
    await expect(text(page)).toHaveAttribute("lang", "rw");
    let refuse = true;
    await page.route(/\/text\?version=/, (route) => (refuse ? route.abort() : route.continue()));
    const french = languages(page).getByRole("link", { name: "Français" });
    await french.click();
    await expect(languages(page).getByRole("alert")).toHaveText("That text could not be loaded. Please try again.");
    await expect(languages(page).locator("[aria-current]")).toHaveText("Ikinyarwanda");
    expect(await text(page).evaluate((element) => element.textContent)).toBe(versions.rw.canonical_text);
    await expect(page).toHaveURL(new RegExp(LAW + "$"));

    refuse = false;
    await french.click();
    await expect(text(page)).toHaveAttribute("lang", "fr");
    await expect(languages(page).getByRole("alert")).toHaveCount(0);
    await expect(page).toHaveURL(/\?lang=fr$/);
  });

  test("sets chapters, divisions and articles as headings and the rest as paragraphs", async ({ page }) => {
    await page.goto(LAW + "?lang=en");
    await expect(text(page).getByRole("heading")).toHaveText([
      "TABLE OF CONTENTS",
      "CHAPTER ONE: GENERAL PROVISIONS",
      "Article One: Purpose of this Law",
      "Article 2: Definition of terms",
      "CHAPTER II: THE REGISTER",
      "Section One: Keeping the register",
      "Article 3: Entries in the register",
      "Article 4: Commencement",
    ]);
    await expect(page.locator("#article-2")).toHaveText("Article 2: Definition of terms");
    await expect(page.locator("#article-2 + p")).toHaveText(
      "In this Law, the following terms have the following meanings:",
    );
    // The text is read in its own size and colour, not the site's small grey paragraphs.
    const paragraph = page.locator("#article-2 + p");
    expect(await paragraph.evaluate((element) => getComputedStyle(element).fontSize)).toBe("17px");
    expect(await paragraph.evaluate((element) => getComputedStyle(element).color)).toBe("rgb(26, 26, 26)");
  });

  test("lists the parts and articles once, and each link goes to its article and moves the focus there @smoke", async ({ page }) => {
    await page.goto(LAW + "?lang=en");
    await expect(contents(page).getByRole("link")).toHaveText([
      "CHAPTER ONE: GENERAL PROVISIONS",
      "Article One: Purpose of this Law",
      "Article 2: Definition of terms",
      "CHAPTER II: THE REGISTER",
      "Section One: Keeping the register",
      "Article 3: Entries in the register",
      "Article 4: Commencement",
    ]);
    // The law's own printed table of contents stays in the text, set apart, with no anchors of its own.
    const printed = page.getByRole("group", { name: "Table of contents as printed" });
    await expect(printed).toContainText("Article 3: Entries in the register");
    await expect(printed.locator("[id]")).toHaveCount(0);

    // A short window, so that going to an article has to move the page.
    await page.setViewportSize({ width: 1280, height: 420 });
    await contents(page).getByRole("link", { name: "Article 2: Definition of terms" }).click();
    const article = page.locator("#article-2");
    await expect(article).toBeFocused();
    await expect(article).toHaveText("Article 2: Definition of terms");
    await expect(article).toBeInViewport();
    await expect(page).toHaveURL(/\?lang=en#article-2$/);

    // From the keyboard too, and the place it lands on shows where it is.
    const last = contents(page).getByRole("link", { name: "Article 4: Commencement" });
    await last.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#article-4")).toBeFocused();
    await expect(page.locator("#article-4")).toHaveText("Article 4: Commencement");
    await expect(page.locator("#article-4")).toBeInViewport();
    expect(await page.locator("#article-4").evaluate((element) => getComputedStyle(element).outlineStyle)).not.toBe("none");
  });

  test("a link to an article opens at it, in the language it names", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 420 });
    await page.goto(LAW + "?lang=fr#article-3");
    await expect(text(page)).toHaveAttribute("lang", "fr");
    await expect(page.locator("#article-3")).toHaveText("Article 3: Inscriptions au registre");
    await expect(page.locator("#article-3")).toBeInViewport();
  });

  test("on a phone the Contents comes before the text, closed, and nothing scrolls sideways @smoke", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 740 });
    await page.goto(LAW + "?lang=en");
    const toggle = contents(page).getByRole("button", { name: "Contents" });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(contents(page).getByRole("link")).toHaveCount(0);
    expect(await top(contents(page))).toBeLessThan(await top(page.locator(".pub-reader")));

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(contents(page).getByRole("link")).toHaveCount(7);
    await contents(page).getByRole("link", { name: "Article 3: Entries in the register" }).click();
    await expect(page.locator("#article-3")).toBeFocused();
    await expect(page.locator("#article-3")).toBeInViewport();

    for (const language of ["English", "Français", "Ikinyarwanda"]) {
      await languages(page).getByRole("link", { name: language }).click();
      await expect(languages(page).locator("[aria-current]")).toHaveText(language);
      const sideways = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(sideways, language).toBeLessThanOrEqual(0);
    }
  });

  test("the text and its Contents have no serious accessibility problem", async ({ page }) => {
    // The law in three languages, and the longer one with its folded table of contents and its table.
    for (const [address, entries] of [[LAW + "?lang=en", 7], [LONG, 14]]) {
      await page.goto(address);
      await expect(contents(page).getByRole("link")).toHaveCount(entries);
      const results = await new AxeBuilder({ page }).include(".pub-reader").include(".pub-contents").analyze();
      const serious = results.violations
        .filter((violation) => ["serious", "critical"].includes(violation.impact))
        .map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.target.join(" ")).join(" | ")}`);
      expect(serious, address).toEqual([]);
    }
  });
});

test.describe("a longer law in one language", () => {
  test("folds its long printed table of contents under its own heading, and opens it when asked @smoke", async ({ page }) => {
    const versions = await served(page, LONG);
    await page.goto(LONG);
    // One text: nothing to switch between.
    await expect(languages(page)).toHaveCount(0);
    await expect(contents(page).getByRole("link")).toHaveCount(14);

    const printed = page.locator("details.pub-printed-contents");
    const summary = printed.locator("summary");
    await expect(summary).toHaveText("TABLE OF CONTENTS");
    await expect(summary).toHaveAttribute("data-note", "as printed, 14 lines");
    const line = printed.getByText("Article 9: Matter 9", { exact: true });
    await expect(line).toBeHidden();
    // The first article is close under it: the folded list does not push the text away.
    expect((await top(page.locator("#article-1"))) - (await top(summary))).toBeLessThan(140);
    // Folded or not, the text on the page is the published text.
    const whole = versions.en.canonical_text.replace(/[\t\n]/g, "");
    expect((await text(page).evaluate((element) => element.textContent)).replace(/\n/g, "")).toBe(whole);

    await summary.focus();
    await page.keyboard.press("Enter");
    await expect(line).toBeVisible();
    await expect(printed.locator("[id]")).toHaveCount(0);
  });

  test("shows a table inside an article as a table", async ({ page }) => {
    await page.goto(LONG);
    const table = text(page).getByRole("table");
    await expect(table).toHaveCount(1);
    await expect(table.getByRole("row")).toHaveCount(3);
    await expect(table.getByRole("row").nth(1).getByRole("cell")).toHaveText(["310", "Filing number"]);
    // It sits in article 2, between that article's words and article 3.
    await expect(page.locator("#article-2 + p")).toHaveText("Matter 2 is governed by this invented article.");
    expect(await top(table)).toBeLessThan(await top(page.locator("#article-3")));
    expect(await top(table)).toBeGreaterThan(await top(page.locator("#article-2")));
  });
});

test.describe("a published text with no structure", () => {
  test("is shown as plain text, with its hash, no Contents and no switch", async ({ page }) => {
    const versions = await served(page, PLAIN);
    await page.goto(PLAIN);
    const plain = page.locator(".pub-reader pre.legal-text");
    await expect(plain).toBeVisible();
    expect(await plain.evaluate((element) => element.textContent)).toBe(versions.en.canonical_text);
    await expect(page.locator(".pub-reader .hash")).toHaveText("SHA-256 " + versions.en.content_hash);
    await expect(page.locator(".pub-reader .badge")).toHaveText("Open full text");
    await expect(contents(page)).toHaveCount(0);
    await expect(languages(page)).toHaveCount(0);
    await expect(page.locator(".pub-text")).toHaveCount(0);
  });
});

test.describe("a record with nothing to read", () => {
  test("says the status of a record with no public text, and that there is no record at an address that has none", async ({ page }) => {
    const listed = await (await page.request.get("/site/v1/sources?access=metadata")).json();
    const answer = await (await page.request.get(`/site/v1/sources/${listed.items[0].id}/text`)).json();
    expect([answer.schema, answer.text, answer.missed]).toEqual(["reader-1", null, null]);
    // Asked for a language too: there is no text to show another of, so no line.
    await page.goto(`/sources/${listed.items[0].id}?lang=fr`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.locator(".pub-reader, .pub-problem, .pub-missed")).toHaveCount(0);
    await expect(page.locator(".record-main")).toContainText(/text/i);

    await page.goto("/sources/no-such-record");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("There is no record at this address.");
    await expect(page.locator(".pub-reader, .pub-problem")).toHaveCount(0);
  });
});
