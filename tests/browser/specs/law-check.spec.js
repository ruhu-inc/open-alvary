import fs from "node:fs";

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const CITATION = "Freedom of Information Act 2011, s. 1(1)";
const TRUE_QUOTE =
  "Notwithstanding anything contained in any other Act, law or regulation, the right of any person to " +
  "access or request information, whether or not contained in any written form, which is in the custody " +
  "or possession of any public official, agency or institution howsoever described, is established.";
const ALTERED_QUOTE = TRUE_QUOTE.replace("any person", "any citizen");

// Nothing may leave this computer. Every request to another address is stopped and written down.
test.beforeEach(async ({ page, baseURL }) => {
  const origin = new URL(baseURL).origin;
  page.outside = [];
  await page.route("**/*", (route) => {
    const url = route.request().url();
    if (url.startsWith(origin) || url.startsWith("data:") || url.startsWith("blob:")) return route.continue();
    page.outside.push(url);
    return route.abort();
  });
});

const bands = (page) => page.locator("oa-receipt .band-text");
const cards = (page) => page.locator("oa-receipt .card");
const messageBox = (page) => page.locator("#composer-input");
const send = (page) => page.locator("#composer-send");

async function checkQuote(page, quote, citation = CITATION) {
  await messageBox(page).click();
  await page.locator("#citation-input").fill(citation);
  await messageBox(page).fill(quote);
  await send(page).click();
}

/** Save a key for the scripted stand-in through the runtime's own key store, then open Ask AI. */
async function useScriptedProvider(page) {
  await page.goto("/agents/");
  await page.evaluate(async () => {
    const byok = await import("/agents/runtime/byok.js");
    const setup = { router: `${location.origin}/test-llm/v1`, model: "scripted", key: "local-test" };
    byok.upsertByokProfile({ name: "Scripted", ...setup }, { select: true });
    byok.saveTabByokSetup(setup);
  });
  // Changing only the "#ask" part does not load the page again, so it is loaded again here.
  await page.goto("/agents/#ask");
  await page.reload();
  await expect(page.locator("#provider-pill-label")).toHaveText("Scripted · scripted");
}

async function ask(page, question) {
  await messageBox(page).fill(question);
  await messageBox(page).press("Enter");
}

test.describe("checking a quote, with no AI and no key", () => {
  test("a true quote is found and one with a word changed is not @smoke", async ({ page }) => {
    await page.goto("/agents/");
    await expect(page.locator("#welcome")).toBeVisible();
    await page.getByRole("button", { name: "Try a true quote" }).click();

    await expect(bands(page).first()).toContainText("Quoted words found · status not confirmed");
    await expect(page.locator("oa-receipt .band").first()).toHaveAttribute("data-look", "amber");
    await expect(page.locator(".lc-msg-caption").first()).toHaveText("Checked by Open Alvary. No AI was used.");
    await expect(page.locator("oa-receipt .published-block mark").first()).toContainText("the right of any person");
    await expect(page.locator("#welcome")).toBeHidden();

    await checkQuote(page, ALTERED_QUOTE);
    await expect(bands(page)).toHaveCount(2);
    await expect(bands(page).nth(1)).toContainText("Quoted words not found");
    await expect(page.locator("oa-receipt .band").nth(1)).toHaveAttribute("data-look", "red");
    // The older card closes to its band, which still says its result in words.
    await expect(cards(page).first()).toHaveAttribute("data-collapsed", "true");
    await expect(bands(page).first()).toContainText("Quoted words found");
    expect(page.outside).toEqual([]);
  });

  test("a message that is plainly a citation and a quote is checked without AI", async ({ page }) => {
    await page.goto("/agents/#ask");
    await ask(page, `${CITATION}: "${TRUE_QUOTE}"`);
    await expect(bands(page).first()).toContainText("Quoted words found");
    await expect(page.locator(".lc-msg-caption").first()).toHaveText("Checked by Open Alvary. No AI was used.");
  });

  test("a law that is not covered gets a grey card, never a guess", async ({ page }) => {
    await page.goto("/agents/");
    await checkQuote(page, TRUE_QUOTE, "An Act Nobody Passed 1999, s. 4");
    await expect(page.locator("oa-receipt .band").first()).toHaveAttribute("data-look", "grey");
    await expect(page.locator("oa-receipt .published-block")).toBeHidden();
  });

  test("an empty send says what is missing and moves the focus there", async ({ page }) => {
    await page.goto("/agents/");
    await send(page).click();
    await expect(page.locator("#citation-error")).toContainText("Enter the law and section");
    await expect(page.locator("#citation-input")).toBeFocused();
    await expect(page.locator("oa-receipt")).toHaveCount(0);
  });
});

test.describe("the result card", () => {
  test("Hide result works from the keyboard and keeps the focus @smoke", async ({ page }) => {
    await page.goto("/agents/");
    await page.getByRole("button", { name: "Try a true quote" }).click();
    const toggle = page.locator("oa-receipt .collapse-toggle").first();
    await expect(toggle).toHaveText("Hide result");

    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(cards(page).first()).toHaveAttribute("data-collapsed", "true");
    await expect(toggle).toHaveText("Show result");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(toggle).toBeFocused();

    await page.keyboard.press("Enter");
    await expect(cards(page).first()).toHaveAttribute("data-collapsed", "false");
    await expect(toggle).toBeFocused();
  });

  test("Hide result works with the mouse", async ({ page }) => {
    await page.goto("/agents/");
    await page.getByRole("button", { name: "Try a true quote" }).click();
    await page.locator("oa-receipt .collapse-toggle").first().click();
    await expect(cards(page).first()).toHaveAttribute("data-collapsed", "true");
  });
});

test.describe("the composer", () => {
  test("the slash menu is run from the keyboard and moves nothing", async ({ page }) => {
    await page.goto("/agents/");
    const form = page.locator("#composer-form");
    await messageBox(page).click();
    const before = await form.boundingBox();

    await page.keyboard.type("/");
    await expect(page.locator("#slash-menu")).toBeVisible();
    const during = await form.boundingBox();
    expect(Math.round(during.y)).toBe(Math.round(before.y));

    await expect(page.locator("#slash-opt-quote")).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("ArrowDown");
    await expect(page.locator("#slash-opt-ask")).toHaveAttribute("aria-selected", "true");
    await expect(messageBox(page)).toHaveAttribute("aria-activedescendant", "slash-opt-ask");

    await page.keyboard.press("Escape");
    await expect(page.locator("#slash-menu")).toBeHidden();
    await expect(messageBox(page)).toHaveValue("");
    await expect(messageBox(page)).toHaveAttribute("aria-invalid", "false");

    await page.keyboard.type("/a");
    await page.keyboard.press("Enter");
    await expect(page.locator("#mode-ask")).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("#citation-field")).toBeHidden();
  });

  test("with a thread on screen only the thread scrolls, and the composer folds @smoke", async ({ page }) => {
    await page.goto("/agents/");
    await page.getByRole("button", { name: "Try a true quote" }).click();
    await expect(bands(page).first()).toBeVisible();
    await checkQuote(page, ALTERED_QUOTE);
    await expect(bands(page)).toHaveCount(2);

    const measure = () =>
      page.evaluate(() => {
        const rect = (id) => document.getElementById(id).getBoundingClientRect();
        const scroller = document.getElementById("thread-scroll");
        return {
          documentHeight: document.documentElement.scrollHeight,
          windowHeight: window.innerHeight,
          windowScroll: Math.round(window.scrollY),
          headerTop: Math.round(document.querySelector(".site-header").getBoundingClientRect().top),
          dockBottom: Math.round(rect("composer-dock").bottom),
          dockHeight: Math.round(rect("composer-dock").height),
          threadScrolls: scroller.scrollHeight > scroller.clientHeight,
          threadHeight: scroller.clientHeight,
        };
      });

    const after = await measure();
    expect(after.documentHeight).toBe(after.windowHeight);
    expect(after.windowScroll).toBe(0);
    expect(after.headerTop).toBe(0);
    expect(after.dockBottom).toBe(after.windowHeight);
    expect(after.threadScrolls).toBe(true);
    // Sending folds the composer to its message line and, under it, the buttons, so the thread
    // has most of the window.
    expect(after.dockHeight).toBeLessThan(120);
    expect(after.threadHeight).toBeGreaterThan(after.windowHeight * 0.6);

    await page.locator("#thread-scroll").evaluate((el) => el.scrollTo(0, 0));
    const scrolled = await measure();
    expect(scrolled.windowScroll).toBe(0);
    expect(scrolled.headerTop).toBe(0);

    // A key in the box opens the composer again, with the law and section still there.
    await messageBox(page).click();
    await expect(page.locator("#citation-input")).toBeVisible();
    await expect(page.locator("#citation-input")).toHaveValue(CITATION);
  });

  test("at 320px nothing scrolls sideways and the folded composer leaves the thread the screen", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 640 });
    await page.goto("/agents/");
    await page.getByRole("button", { name: "Try a true quote" }).click();
    await expect(bands(page).first()).toBeVisible();
    await page.locator("#thread-scroll").click({ position: { x: 5, y: 5 } });
    const sizes = await page.evaluate(() => ({
      sideways: document.documentElement.scrollWidth - window.innerWidth,
      dock: Math.round(document.getElementById("composer-dock").getBoundingClientRect().height),
    }));
    expect(sizes.sideways).toBeLessThanOrEqual(0);
    expect(sizes.dock).toBeLessThan(120);
  });
});

test.describe("the receipt panel", () => {
  test("a wide window shows the chosen check in full beside the thread @smoke", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/agents/");
    await page.getByRole("button", { name: "Try a true quote" }).click();
    await expect(bands(page).first()).toBeVisible();
    const panel = page.locator("#receipt-panel");
    await expect(panel).toBeVisible();
    await expect(panel.locator(".rp-kicker")).toContainText("Quoted words found");
    await expect(panel.locator(".rp-passage mark")).toContainText("the right of any person");
    // Beside the panel the card is the verdict: its passage is in the panel.
    await expect(page.locator("oa-receipt .published-block").first()).toBeHidden();

    await checkQuote(page, ALTERED_QUOTE);
    await expect(bands(page)).toHaveCount(2);
    await expect(panel.locator(".rp-kicker")).toContainText("not found");
    // The card says which word differs.
    await expect(page.locator("oa-receipt .diff-text del").last()).toContainText("citizen");
    await expect(page.locator("oa-receipt .diff-text ins").last()).toContainText("person");

    // An earlier check can be chosen again; the panel follows, and the tabs work from the keyboard.
    await page.getByRole("button", { name: "Show the receipt" }).first().click();
    await expect(panel.locator(".rp-kicker")).toContainText("Quoted words found");
    await expect(page.getByRole("tab", { name: "Receipt" })).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "Proof JSON" })).toHaveAttribute("aria-selected", "true");
    await expect(panel.locator(".rp-json")).toContainText('"receipt_id"');
  });

  test("a narrow window keeps each card whole and shows no panel", async ({ page }) => {
    await page.setViewportSize({ width: 900, height: 800 });
    await page.goto("/agents/");
    await page.getByRole("button", { name: "Try a true quote" }).click();
    await expect(bands(page).first()).toBeVisible();
    await expect(page.locator("#receipt-panel")).toBeHidden();
    await expect(page.locator("oa-receipt .published-block mark").first()).toContainText("the right of any person");
  });
});

test.describe("saved proofs", () => {
  test("a saved proof verifies and is checked again; a changed one is refused", async ({ page }, testInfo) => {
    await page.goto("/agents/");
    await page.getByRole("button", { name: "Try a true quote" }).click();
    await expect(bands(page).first()).toBeVisible();

    const downloading = page.waitForEvent("download");
    await page.locator("oa-receipt").first().getByRole("button", { name: "Save proof" }).click();
    const download = await downloading;
    const saved = testInfo.outputPath("proof.json");
    await download.saveAs(saved);
    const proof = JSON.parse(fs.readFileSync(saved, "utf8"));
    expect(proof.receipt.schema_version).toBe("oa-receipt-1");

    await page.locator("#proof-file").setInputFiles(saved);
    await expect(page.locator(".lc-msg-caption").last()).toHaveText("Saved proof, checked by Open Alvary");
    await expect(page.locator(".lc-msg-note").last()).toContainText("The saved proof is genuine");
    await expect(page.locator(".lc-msg-note").last()).toContainText("Same words, same record");
    await expect(bands(page)).toHaveCount(2);

    proof.receipt.status = "matches";
    await page.locator("#proof-file").setInputFiles({
      name: "changed.json",
      mimeType: "application/json",
      buffer: Buffer.from(JSON.stringify(proof)),
    });
    await expect(page.locator(".lc-msg-note").last()).toContainText("The signature does not match");
    await expect(bands(page)).toHaveCount(2);
  });
});

test.describe("the thread", () => {
  test("a reload brings the thread back, verified again, at its latest message", async ({ page }) => {
    await page.goto("/agents/");
    await page.getByRole("button", { name: "Try a true quote" }).click();
    await expect(bands(page).first()).toBeVisible();
    await checkQuote(page, ALTERED_QUOTE);
    await expect(bands(page)).toHaveCount(2);

    await page.reload();
    await expect(bands(page)).toHaveCount(2);
    await expect(page.locator(".lc-msg-user")).toHaveCount(2);
    await expect(cards(page).first()).toHaveAttribute("data-collapsed", "true");
    await expect(cards(page).nth(1)).toHaveAttribute("data-collapsed", "false");
    const atEnd = await page
      .locator("#thread-scroll")
      .evaluate((el) => Math.abs(el.scrollTop + el.clientHeight - el.scrollHeight) < 4);
    expect(atEnd).toBe(true);
    expect(await page.evaluate(() => Math.round(window.scrollY))).toBe(0);

    await page.getByRole("button", { name: "New chat" }).click();
    await expect(page.locator(".lc-divider").last()).toHaveText("New chat");
    await expect(bands(page)).toHaveCount(2);
  });
});

test.describe("the AI key", () => {
  test("a question with no key keeps the question and offers the key dialog", async ({ page }) => {
    await page.goto("/agents/#ask");
    await ask(page, "What does section 1 say?");
    await expect(page.locator(".lc-msg-note").last()).toContainText("needs an AI provider and your own key");
    await expect(messageBox(page)).toHaveValue("What does section 1 say?");
    await expect(page.locator(".lc-msg-user")).toHaveCount(0);
    // Nothing of the runtime's agent code is loaded without a key.
    const loaded = await page.evaluate(() =>
      performance.getEntriesByType("resource").some((entry) => entry.name.endsWith("/runtime/index.js")),
    );
    expect(loaded).toBe(false);
  });

  test("the dialog saves, never shows the saved key again, and removes it", async ({ page }) => {
    await page.goto("/agents/");
    const pill = page.locator("#open-provider-modal");
    const dialog = page.locator("#provider-dialog");
    const key = page.locator("#provider-key-input");

    await pill.click();
    await expect(dialog).toBeVisible();
    await expect(key).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(pill).toBeFocused();

    await pill.click();
    await page.locator("#provider-save-btn").click();
    await expect(page.locator("#provider-key-error")).toContainText("Paste an API key");
    await expect(key).toBeFocused();

    await dialog.locator('[data-preset="anthropic"]').click();
    await dialog.locator('[data-preset="custom"]').click();
    await expect(page.locator("#provider-url-input")).toBeVisible();
    await expect(page.locator("#provider-url-input")).toHaveValue("");

    await dialog.locator('[data-preset="openai"]').click();
    await key.fill("test-key-not-real-1234");
    await page.locator("#toggle-key-visibility").click();
    await expect(key).toHaveAttribute("type", "text");
    await page.locator("#provider-save-btn").click();
    await expect(dialog).toBeHidden();
    await expect(page.locator("#provider-pill-label")).toContainText("OpenAI");
    await expect(page.locator("#mode-ask")).toHaveAttribute("aria-pressed", "true");

    await pill.click();
    await expect(key).toHaveValue("");
    await expect(key).toHaveAttribute("type", "password");
    await expect(page.locator("#provider-key-saved")).toContainText("A key ending 1234 is saved");
    await page.locator("#provider-remove-btn").click();
    await expect(page.locator("#provider-pill-label")).toHaveText("Add AI key (optional)");
    const left = await page.evaluate(() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage) + document.cookie);
    expect(left).not.toContain("test-key-not-real-1234");
    // The only address outside this computer the page tried is the provider's own, and it was stopped.
    for (const url of page.outside) expect(new URL(url).hostname).toBe("api.openai.com");
  });
});

test.describe("asking the AI, on the challenge runtime with a scripted model", () => {
  test("the agents check the quote, the card comes first, then the answer and the count @smoke", async ({ page }) => {
    await useScriptedProvider(page);
    await ask(page, "What does section 1(1) of the Freedom of Information Act 2011 say?");

    const answer = page.locator(".lc-msg-ai");
    await expect(answer).toHaveCount(1);
    await expect(bands(page)).toHaveCount(1);
    await expect(bands(page).first()).toContainText("Quoted words found");
    await expect(page.locator(".lc-msg-caption").first()).toHaveText("Checked by Open Alvary, on a section the AI named");
    await expect(page.locator("oa-receipt .ai-suggested").first()).toBeVisible();
    await expect(page.locator("oa-receipt [data-ref='quote-label']").first()).toHaveText("Quote sent (the AI's)");

    // The thread's order: the question, the card the agents earned, then the AI's words.
    const order = await page
      .locator("#thread-list > li")
      .evaluateAll((items) => items.map((li) => li.className.replace("lc-msg ", "")));
    expect(order).toEqual(["lc-msg-user", "lc-msg-result", "lc-msg-ai"]);

    await expect(answer.locator(".lc-ai-label")).toHaveText("AI's answer. Only quotes with a result card were checked.");
    await expect(answer.locator(".lc-ai-text")).toContainText("Section 1(1) of the Freedom of Information Act 2011 says");
    // What a model writes is text: its HTML is shown as written and its card block is not drawn.
    await expect(answer.locator(".lc-ai-text")).toContainText("<b>This tag must show as text.</b>");
    await expect(answer.locator(".lc-ai-text b")).toHaveCount(0);
    // (The runtime's own trace, inside "Show the agents at work", may quote the raw reply as text.)
    await expect(page.locator("#thread-list").getByText("Checked: found")).toHaveCount(0);
    await expect(page.locator(".ns-presentation")).toHaveCount(0);
    await expect(answer.locator(".lc-ai-summary")).toHaveText(
      "This answer: 1 quote checked, 1 found, 0 not found. Sentences without a result card were not checked.",
    );
    await expect(page.locator("#agents-disclosure")).toBeVisible();
    expect(page.outside).toEqual([]);

    await page.reload();
    await expect(page.locator(".lc-msg-ai")).toHaveCount(1);
    await expect(bands(page)).toHaveCount(1);
    await expect(page.locator(".lc-divider").last()).toHaveText("Earlier in this tab. The AI does not remember it.");
  });

  test("a quote the AI got wrong is shown as not found, and counted so", async ({ page }) => {
    await useScriptedProvider(page);
    await ask(page, "What does section 1(1) say? [altered]");
    await expect(page.locator(".lc-msg-ai")).toHaveCount(1);
    await expect(bands(page).first()).toContainText("Quoted words not found");
    await expect(page.locator(".lc-ai-summary")).toHaveText(
      "This answer: 1 quote checked, 0 found, 1 not found. Sentences without a result card were not checked.",
    );
  });

  test("an answer with no check says so in red", async ({ page }) => {
    await useScriptedProvider(page);
    await ask(page, "What does section 1(1) say? [plain]");
    const summary = page.locator(".lc-ai-summary");
    await expect(summary).toHaveText("Nothing in this answer was checked.");
    await expect(summary).toHaveAttribute("data-tone", "red");
    await expect(page.locator("oa-receipt")).toHaveCount(0);
  });
});

test.describe("accessibility rules", () => {
  const serious = (results) =>
    results.violations
      .filter((violation) => ["serious", "critical"].includes(violation.impact))
      .map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.target.join(" ")).join(" | ")}`);

  test("the first screen has no serious problem @smoke", async ({ page }) => {
    await page.goto("/agents/");
    await expect(page.locator("#welcome")).toBeVisible();
    expect(serious(await new AxeBuilder({ page }).analyze())).toEqual([]);
  });

  test("a thread with a found card, a not-found card and the open composer has none", async ({ page, browserName }) => {
    // In Playwright's Firefox the page crashes when the scanner runs on this screen inside this
    // suite ("Page crashed"), though the same steps in a file of their own scan cleanly and every
    // other Firefox test of this screen passes. Not understood yet. The scan runs in Chromium and
    // WebKit, and Firefox still runs the scans of the first screen and of the key dialog.
    test.fixme(browserName === "firefox", "The accessibility scanner crashes Playwright's Firefox on this screen.");
    await page.goto("/agents/");
    await page.getByRole("button", { name: "Try a true quote" }).click();
    await expect(bands(page).first()).toBeVisible();
    await checkQuote(page, ALTERED_QUOTE);
    await expect(bands(page)).toHaveCount(2);
    await messageBox(page).click();
    expect(serious(await new AxeBuilder({ page }).analyze())).toEqual([]);
  });

  test("the key dialog has none", async ({ page }) => {
    await page.goto("/agents/");
    await page.locator("#open-provider-modal").click();
    await expect(page.locator("#provider-dialog")).toBeVisible();
    expect(serious(await new AxeBuilder({ page }).include("#provider-dialog").analyze())).toEqual([]);
  });
});
