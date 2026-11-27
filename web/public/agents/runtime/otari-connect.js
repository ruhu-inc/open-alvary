// otari-connect.js — the "Connect with Otari" sign-in.
// Added by agentic-web. See README.md in this folder.
//
// Otari's app sign-in (OAuth with PKCE) gives this page a key for the visitor's own
// otari.ai workspace, so the visitor does not copy and paste one. Sites need no app
// registration: a site's client ID is its own origin. The key is saved like a pasted key
// (byok.js).

import { loadByokStore, saveTabByokSetup, upsertByokProfile } from "./byok.js";

/**
 * Runs the "Connect with Otari" sign-in and saves the key it returns.
 *
 * start() sends the visitor to otari.ai with a PKCE challenge. When otari.ai sends them
 * back, finish() puts back the page address without the one-time code, checks the state
 * and the issuer, swaps the code for a key and saves it as this tab's setup.
 *
 * Only a tab that started a sign-in finishes one, so a page's own `code` parameter is
 * left alone.
 */
export class OtariConnect {
  /**
   * The provider's name, used for the saved setup and the panel's provider button.
   */
  static NAME = "Otari";

  /**
   * The name of the setup for a key that belongs to otari.ai's EU region.
   */
  static EU_NAME = "Otari EU";

  /**
   * Where the visitor signs in. otari.ai's reply names it as `iss`, which finish() checks.
   */
  static ISSUER = "https://otari.ai";

  /**
   * The page that starts the sign-in. It sends the visitor on to otari.ai's consent screen.
   */
  static AUTHORIZE_URL = "https://otari.ai/api/v1/oauth/apps/authorize";

  /**
   * The endpoint that swaps the one-time code for a key.
   */
  static TOKEN_URL = "https://otari.ai/api/v1/oauth/apps/token";

  /**
   * otari.ai's OpenAI-compatible API, where a key from its US region is used.
   */
  static ROUTER = "https://api.otari.ai/api/v1";

  /**
   * The API of otari.ai's EU region. The US API refuses an EU key with HTTP 421.
   */
  static EU_ROUTER = "https://eu.api.otari.ai/api/v1";

  /**
   * Every otari.ai key starts with this.
   */
  static KEY_PREFIX = "otk_";

  /**
   * Every key from otari.ai's EU region starts with this.
   */
  static EU_KEY_PREFIX = "otk_v1_eu_";

  /**
   * The model a connected setup uses. The visitor can pick another one in the panel.
   */
  static MODEL = "alibaba/qwen3.8-flash";

  /**
   * The spending limit, in US dollars, the consent screen suggests for the key. The
   * visitor can change it or remove it there.
   */
  static SUGGESTED_LIMIT = "5";

  /**
   * How long the consent screen suggests the key lives. The visitor can change it there.
   */
  static SUGGESTED_EXPIRATION = "30-days";

  /**
   * The key's page on otari.ai, where the visitor sees what it spends or revokes it.
   */
  static KEYS_PAGE_URL = "https://otari.ai/dashboard#/keys";

  /**
   * The session storage item that holds a sign-in this tab started.
   */
  static PENDING_ITEM = "nsweb-otari-pending";

  /**
   * How long a sign-in may take. The visitor may have to create an account first. The
   * code itself expires 2 minutes after the visitor approves.
   */
  static SIGN_IN_LIFETIME_MS = 30 * 60 * 1000;

  /**
   * How long the key exchange may take. boot() waits for it before it shows the chat.
   */
  static EXCHANGE_TIMEOUT_MS = 15000;

  /**
   * Say whether this page can run the sign-in.
   *
   * The PKCE challenge needs the browser's crypto functions, which work only in a secure
   * context: an https page or this computer's own address. A page inside a frame cannot
   * show otari.ai's sign-in page.
   *
   * :return: True when the Connect button can work on this page.
   */
  static isAvailable() {
    const secure = Boolean(globalThis.isSecureContext && globalThis.crypto?.subtle);
    return secure && globalThis.top === globalThis.self;
  }

  /**
   * Return the API that a key belongs to: the EU region's for an EU key, the US region's
   * otherwise.
   *
   * :param key: The otari.ai key.
   * :return: The router URL.
   */
  static routerFor(key) {
    return String(key || "").trim().startsWith(OtariConnect.EU_KEY_PREFIX) ? OtariConnect.EU_ROUTER : OtariConnect.ROUTER;
  }

  /**
   * Handle a click on the Connect button: start the sign-in and report problems.
   *
   * :param report: Shows a message in the settings panel. It takes the text and an
   *   optional style, "warn" for problems.
   * :return: A promise that settles once the browser starts leaving the page, or once a
   *   problem is reported.
   */
  static async connect(report) {
    if (!OtariConnect.isAvailable()) {
      report("Connect with Otari needs this page to be open in its own tab over https.", "warn");
      return;
    }
    try {
      report("Opening Otari...");
      await OtariConnect.start();
    } catch (error) {
      report(`Could not start the Otari sign-in: ${error.message}`, "warn");
    }
  }

  /**
   * Send the visitor to otari.ai to approve a key for this site.
   *
   * Keeps the PKCE verifier, a random state and this page's address in this tab's session
   * storage, so finish() can check the reply and put the address back. The verifier never
   * goes in a URL. The site's client ID is its origin, and otari.ai sends the visitor back
   * to this page's path.
   *
   * :return: A promise that resolves once the browser starts leaving the page.
   * :raises Error: When this browser blocks session storage.
   */
  static async start() {
    const verifier = OtariConnect.randomText(32);
    const state = OtariConnect.randomText(16);
    const challenge = await OtariConnect.challengeFor(verifier);
    const pending = {
      verifier,
      state,
      path: location.pathname,
      search: location.search,
      hash: location.hash,
      startedAt: Date.now(),
    };
    try {
      sessionStorage.setItem(OtariConnect.PENDING_ITEM, JSON.stringify(pending));
    } catch {
      throw new Error("this browser blocks session storage for this site.");
    }
    const url = new URL(OtariConnect.AUTHORIZE_URL);
    url.searchParams.set("client_id", location.origin);
    url.searchParams.set("redirect_uri", location.origin + location.pathname);
    url.searchParams.set("response_type", "code");
    url.searchParams.set("state", state);
    url.searchParams.set("code_challenge", challenge);
    url.searchParams.set("code_challenge_method", "S256");
    url.searchParams.set("limit", OtariConnect.SUGGESTED_LIMIT);
    url.searchParams.set("expiration", OtariConnect.SUGGESTED_EXPIRATION);
    location.assign(url.toString());
  }

  /**
   * Finish a sign-in when otari.ai has sent the visitor back to this page.
   *
   * Does nothing unless this tab started a sign-in. Otherwise it removes the reply from
   * the address before anything else, then checks the state and the issuer, swaps the
   * code for a key and saves the key as this tab's setup.
   *
   * :return: null when no sign-in was waiting. Otherwise an object with `status`
   *   ("connected", "cancelled" or "failed") and a `message` for the visitor.
   */
  static async finish() {
    const pending = OtariConnect.takePending();
    if (!pending) return null;
    const params = new URLSearchParams(location.search);
    const code = params.get("code") || "";
    const state = params.get("state") || "";
    const issuer = params.get("iss") || "";
    const error = params.get("error") || "";
    OtariConnect.restoreAddress(pending, params);
    const expired = Date.now() - pending.startedAt > OtariConnect.SIGN_IN_LIFETIME_MS;
    if (!code) {
      // An old, unfinished sign-in is dropped without a message.
      if (expired) return null;
      return {
        status: "cancelled",
        message: error === "access_denied"
          ? "The Otari sign-in was cancelled. To try again, select Connect with Otari in ⚙ AI model settings."
          : "The Otari sign-in was not finished. To try again, select Connect with Otari in ⚙ AI model settings.",
      };
    }
    if (state !== pending.state) {
      return OtariConnect.failure("the reply did not match the sign-in this tab started");
    }
    if (issuer !== OtariConnect.ISSUER) {
      return OtariConnect.failure("the reply did not come from otari.ai");
    }
    if (expired) return OtariConnect.failure("it took more than 30 minutes");
    let key = "";
    try {
      key = await OtariConnect.exchange(code, pending);
    } catch (failure) {
      return OtariConnect.failure(failure.message);
    }
    OtariConnect.saveKey(key);
    return {
      status: "connected",
      message: "Connected to Otari. Your new key is saved in this browser for this site. "
        + `It uses the model ${OtariConnect.MODEL} and spends within the limit you approved. `
        + "To see what it spends or revoke it, use Manage this key on Otari in ⚙ AI model settings.",
    };
  }

  /**
   * Build the result finish() returns for a sign-in that failed.
   *
   * :param reason: Why it failed, in a few plain words.
   * :return: An object with status "failed" and a message for the visitor.
   */
  static failure(reason) {
    return {
      status: "failed",
      message: `The Otari sign-in failed: ${reason}. To try again, select Connect with Otari in ⚙ AI model settings.`,
    };
  }

  /**
   * Read and remove the sign-in this tab started, so it is used at most once.
   *
   * :return: The pending sign-in, or null when there is none or it cannot be read.
   */
  static takePending() {
    let raw = null;
    try {
      raw = sessionStorage.getItem(OtariConnect.PENDING_ITEM);
      sessionStorage.removeItem(OtariConnect.PENDING_ITEM);
    } catch {
      return null;
    }
    if (!raw) return null;
    let pending = null;
    try {
      pending = JSON.parse(raw);
    } catch {
      return null;
    }
    const fields = ["verifier", "state", "path", "search", "hash"];
    for (const field of fields) {
      if (typeof pending?.[field] !== "string") return null;
    }
    if (typeof pending.startedAt !== "number") return null;
    return pending;
  }

  /**
   * Remove the sign-in reply from the address bar.
   *
   * On the page the sign-in started from, the address from before the sign-in comes
   * back, which drops anything otari.ai added. On another page, only the reply's own
   * parameters are removed.
   *
   * :param pending: The sign-in this tab started.
   * :param params: The current page's query parameters.
   */
  static restoreAddress(pending, params) {
    let address = "";
    if (location.pathname === pending.path) {
      address = pending.path + pending.search + pending.hash;
    } else {
      for (const name of ["code", "state", "iss", "error", "error_description"]) params.delete(name);
      const query = params.toString();
      address = location.pathname + (query ? `?${query}` : "") + location.hash;
    }
    try {
      history.replaceState(null, "", address);
    } catch {
      // The code is useless without the verifier, which never left this tab.
    }
  }

  /**
   * Swap the one-time code for a key in the visitor's otari.ai workspace.
   *
   * :param code: The code otari.ai added to the address.
   * :param pending: The sign-in start() kept, with the verifier and the page's path.
   * :return: The new API key.
   * :raises Error: When otari.ai cannot be reached, refuses the code or sends no key.
   */
  static async exchange(code, pending) {
    const body = new URLSearchParams({
      grant_type: "authorization_code",
      client_id: location.origin,
      redirect_uri: location.origin + pending.path,
      code,
      code_verifier: pending.verifier,
    });
    let response = null;
    try {
      response = await fetch(OtariConnect.TOKEN_URL, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
        cache: "no-store",
        signal: AbortSignal.timeout(OtariConnect.EXCHANGE_TIMEOUT_MS),
      });
    } catch {
      throw new Error("could not reach otari.ai");
    }
    let reply = null;
    try {
      reply = await response.json();
    } catch {
      reply = null;
    }
    if (!response.ok) {
      const reason = reply?.error_description || `otari.ai answered ${response.status}`;
      throw new Error(String(reason).replace(/\.$/, ""));
    }
    const key = typeof reply?.access_token === "string" ? reply.access_token.trim() : "";
    if (!key) throw new Error("otari.ai sent no key");
    return key;
  }

  /**
   * Save the key as the selected setup and apply it to this tab, as Save and start does.
   *
   * The setup uses the API of the key's region. A setup that an earlier sign-in or a
   * pasted key for the same region made is updated, so connecting again does not add
   * another one.
   *
   * :param key: The new otari.ai key.
   * :return: The saved setup, or null when it could not be saved.
   */
  static saveKey(key) {
    const router = OtariConnect.routerFor(key);
    const name = router === OtariConnect.EU_ROUTER ? OtariConnect.EU_NAME : OtariConnect.NAME;
    let id = "";
    for (const profile of loadByokStore().profiles) {
      if (profile.name === name && profile.router === router) {
        id = profile.id;
        break;
      }
    }
    const draft = {
      id,
      name,
      key,
      model: OtariConnect.MODEL,
      router,
      runner: false,
    };
    const saved = upsertByokProfile(draft, { select: true }).profile;
    saveTabByokSetup(saved);
    return saved;
  }

  /**
   * Show the "Manage this key on Otari" link when the key field holds an otari.ai key, and
   * hide it otherwise.
   *
   * :param keyEl: The panel's API key field.
   * :param boxEl: The element that holds the link.
   */
  static refreshKeyLink(keyEl, boxEl) {
    if (!keyEl || !boxEl) return;
    boxEl.hidden = !keyEl.value.trim().startsWith(OtariConnect.KEY_PREFIX);
  }

  /**
   * Make a random string for a PKCE verifier or a state.
   *
   * :param byteCount: How many random bytes to use. 32 bytes make a 43-character verifier.
   * :return: The bytes in base64url, without padding.
   */
  static randomText(byteCount) {
    const bytes = crypto.getRandomValues(new Uint8Array(byteCount));
    return OtariConnect.base64Url(bytes);
  }

  /**
   * Compute the S256 PKCE challenge for a verifier.
   *
   * :param verifier: The PKCE verifier.
   * :return: The base64url SHA-256 of the verifier, without padding.
   */
  static async challengeFor(verifier) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
    return OtariConnect.base64Url(new Uint8Array(digest));
  }

  /**
   * Encode bytes as base64url without padding, as PKCE requires.
   *
   * :param bytes: The bytes, as a Uint8Array.
   * :return: The encoded text.
   */
  static base64Url(bytes) {
    let binary = "";
    for (const byte of bytes) {
      binary += String.fromCharCode(byte);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
}
