// The record page's text, on the local site (scripts/record_page_local.py): the built website and the
// website's real data path over two published texts, an invented Rwandan law in three languages with
// its structure and a text with none. No key, no secret and no outside call.
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const SITE = `http://127.0.0.1:${Number(process.env.RECORD_PAGE_PORT || 8774)}`;
const LAW = "/sources/rw-law-000-2099";
const PLAIN = "/sources/ng-foi-act-2011";

test.use({ baseURL: SITE });

// Nothing may leave this computer. Every request to another address is stopped and written down.
test.beforeEach(async ({ page }) => {
  page.outside = [];
  await page.route("**/*", (route) => {
    const url = route.request().url();
    if (url.startsWith(SITE) || url.startsWith("data:") || url.startsWith("blob:")) return route.continue();
    page.outside.push(url);
    return route.abort();
  });
});
test.afterEach(async ({ page }) => {
  expect(page.outside).toEqual([]);
});

const text = (page) => page.locator(".pub-text");
const languages = (page) => page.getByRole("navigation", { name: "Language versions" });
const contents = (page) => page.getByRole("navigation", { name: "Contents" });

/** The record's texts as the website's data path serves them, by language. */
async function served(page, source) {
  const answer = await page.request.get(`/site/v1${source}/versions`);
  expect(answer.ok()).toBe(true);
  const body = await answer.json();
  return Object.fromEntries(body.items.map((version) => [version.language, version]));
}

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
    await page.goto(LAW + "?lang=en");
    await expect(contents(page).getByRole("link")).toHaveCount(7);
    const results = await new AxeBuilder({ page }).include(".pub-reader").include(".pub-contents").analyze();
    const serious = results.violations
      .filter((violation) => ["serious", "critical"].includes(violation.impact))
      .map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.target.join(" ")).join(" | ")}`);
    expect(serious).toEqual([]);
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
