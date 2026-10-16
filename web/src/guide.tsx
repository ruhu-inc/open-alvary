// The console, step by step: an illustrated guide to every page of the developer console. The
// pictures (web/public/guide) show a made-up organisation; the numbers in them match the numbered
// steps beside each one.

import React from "react";
import { Link, PageTitle } from "./site";

const API = "https://api.open.alvary.ai";

// Each picture's size in CSS pixels (they are saved at twice that), so the page doesn't jump as they load.
const SIZES: Record<string, [number, number]> = {
  "sign-in": [488, 517],
  welcome: [588, 616],
  dashboard: [1280, 800],
  plans: [1280, 800],
  "plan-request": [608, 756],
  "plan-requested": [1280, 800],
  "keys-empty": [1280, 800],
  "key-create": [608, 521],
  "key-created": [608, 436],
  "keys-list": [1280, 800],
  apps: [1280, 800],
  "connect-claude": [1280, 800],
  "connect-code": [1024, 239],
  consent: [588, 579],
  "apps-list": [1280, 800],
  "monitor-empty": [1280, 800],
  "monitor-new": [608, 826],
  "monitor-preview": [558, 231],
  "monitor-inbox": [1280, 800],
  "record-monitor": [417, 397],
  "monitor-digest": [1024, 355],
  "webhooks-empty": [1280, 800],
  "webhooks-add": [608, 457],
  "webhooks-secret": [608, 367],
  "webhooks-list": [1280, 800],
  usage: [1280, 800],
  members: [1280, 800],
  security: [1280, 800],
  "your-data": [1024, 207],
};

const SECTIONS: [string, string][] = [
  ["sign-in", "Sign in"],
  ["plan", "Choose a plan"],
  ["keys", "Create an API key"],
  ["apps", "Connect an AI tool"],
  ["monitor", "Monitor changes"],
  ["webhooks", "Webhooks"],
  ["usage", "Usage"],
  ["members", "Members"],
  ["security", "Security and your data"],
];

// A number as it appears in the pictures.
function N({ n }: { n: number }) {
  return (
    <span className="guide-n" aria-label={`(${n})`}>
      {n}
    </span>
  );
}

function Shot({ name, alt, children }: { name: string; alt: string; children?: React.ReactNode }) {
  const [width, height] = SIZES[name];
  const src = `/guide/${name}.webp`;
  return (
    <figure className={"guide-shot" + (width < 1280 ? " part" : "")}>
      <a href={src} target="_blank" rel="noreferrer" title="Open the full-size picture">
        <img src={src} alt={alt} width={width} height={height} loading="lazy" decoding="async" />
      </a>
      {children ? <figcaption>{children}</figcaption> : null}
    </figure>
  );
}

function Steps({ children }: { children: React.ReactNode }) {
  return <ol className="guide-steps">{children}</ol>;
}

function Step({ n, children }: { n?: number; children: React.ReactNode }) {
  return (
    <li>
      {n ? <N n={n} /> : <span className="guide-n quiet" aria-hidden="true" />}
      <span>{children}</span>
    </li>
  );
}

function Code({ children }: { children: string }) {
  return (
    <pre className="guide-code" dir="ltr">
      {children}
    </pre>
  );
}

export function ConsoleGuide() {
  return (
    <div className="page guide">
      <PageTitle label="DEVELOPERS" title="The console, step by step.">
        The console at <a href="/console">open.alvary.ai/console</a> is where you get access, create API keys, connect AI
        tools such as Claude, and follow changes with monitors. This guide walks through each page. The pictures show a
        made-up organisation, Kigali Legal Lab; the numbers in them match the steps beside each picture. Select a
        picture to see it full size.
      </PageTitle>

      {/* Not a <nav>: the site styles those as its header menu. */}
      <div className="guide-contents" role="navigation" aria-label="On this page">
        <ol>
          {SECTIONS.map(([id, title], i) => (
            <li key={id}>
              <a href={`#${id}`}>
                <span>{i + 1}</span> {title}
              </a>
            </li>
          ))}
        </ol>
      </div>

      <section id="sign-in">
        <h2>1. Sign in</h2>
        <p>
          Open <a href="/console">the console</a>. There is no separate sign-up: the first time you sign in, your
          account is made.
        </p>
        <Steps>
          <Step n={1}>
            <b>Continue with Google</b> if you have a Google account, or
          </Step>
          <Step n={2}>
            <b>Continue with email</b>: we send a code to your address and you type it in.
          </Step>
        </Steps>
        <Shot name="sign-in" alt="The console's sign-in page, with Continue with Google and Continue with email." />
        <p>The first time, the console asks two things before you start.</p>
        <Steps>
          <Step n={1}>Your full name, as colleagues will see it.</Step>
          <Step n={2}>
            A name for your organisation. Access, keys and usage belong to an organisation; this one is yours, and you
            can rename it or invite colleagues later.
          </Step>
          <Step n={3}>
            Agree to the <Link to="/terms">terms of service</Link> and <Link to="/acceptable-use">acceptable use
            policy</Link>, and confirm you have read the <Link to="/privacy">privacy notice</Link>.
          </Step>
          <Step n={4}>
            <b>Continue to the console</b>.
          </Step>
        </Steps>
        <Shot name="welcome" alt="The welcome form: full name, organisation name, two agreement boxes and Continue to the console." />
        <p>The dashboard is the starting point. It shows your plan, today's requests and what is in use.</p>
        <Steps>
          <Step n={1}>
            <b>Create an API key</b> to call the API from your own code (<a href="#keys">step 3</a>).
          </Step>
          <Step n={2}>
            <b>Connect Claude or Codex</b> to ask questions over the corpus from an AI tool (<a href="#apps">step 4</a>).
          </Step>
          <Step n={3}>
            <b>Read the docs</b>: endpoints, limits and error codes on the <Link to="/developers">developer page</Link>.
          </Step>
        </Steps>
        <Shot name="dashboard" alt="The dashboard: three cards to create an API key, connect Claude or Codex, and read the docs." />
      </section>

      <section id="plan">
        <h2>2. Choose a plan</h2>
        <p>
          Every new organisation starts on <b>Starter</b>: free, with no card, and ready at once. It reads every
          country through the REST API and MCP, at 60 requests a minute. You can create a key and start straight away;
          the other plans raise the limits.
        </p>
        <Steps>
          <Step n={1}>Your current plan and its limits.</Step>
          <Step n={2}>
            <b>Request upgrade</b> on Developer or Team. For research, government or legal aid, choose <b>Apply</b>{" "}
            under Public interest; for Enterprise, <b>Contact us</b>.
          </Step>
        </Steps>
        <Shot name="plans" alt="Plan and limits: the current plan, Starter, and cards for Developer, Team and Enterprise." />
        <p>The request form asks how you will use the corpus.</p>
        <Steps>
          <Step n={1}>What you are building. Don't include confidential client or case details.</Step>
          <Step n={2}>
            The countries you need most. Developer includes 2 of your choice and Team 5; other countries still answer,
            at the Starter rate.
          </Step>
          <Step n={3}>Roughly how many requests a day you expect.</Step>
          <Step n={4}>
            <b>Send request</b>.
          </Step>
        </Steps>
        <Shot name="plan-request" alt="The upgrade request form: how you will use it, countries, expected requests a day and Send request." />
        <p>
          Your request shows as <b>Being reviewed</b>, and we reply by email. Prices for paid plans are coming; until
          then, we confirm each upgrade by email. You can withdraw the request while it is open.
        </p>
        <Shot name="plan-requested" alt="The plan page with the request for Developer marked Being reviewed." />
      </section>

      <section id="keys">
        <h2>3. Create an API key</h2>
        <p>
          A key lets your code call the REST API and MCP. Claude, ChatGPT and Codex don't need one: they sign in
          themselves (<a href="#apps">step 4</a>).
        </p>
        <div className="guide-note">
          On a paid or approved plan, owners and admins confirm a second factor before creating keys or connecting
          apps. Set one up first under <a href="#security">Security and profile</a>; on Starter it's recommended.
        </div>
        <Steps>
          <Step n={1}>
            Open <b>API keys</b> and choose <b>Create key</b>.
          </Step>
        </Steps>
        <Shot name="keys-empty" alt="The API keys page with no keys yet and the Create key button." />
        <Steps>
          <Step n={1}>A name, so you can tell keys apart later.</Step>
          <Step n={2}>
            <b>Personal</b> for your own scripts: it stops working if you leave the organisation. <b>Shared, for
            servers</b> belongs to the organisation and survives people leaving (owners and admins).
          </Step>
          <Step n={3}>When it expires. A key can read the published corpus within the organisation's access; nothing else.</Step>
          <Step n={4}>
            <b>Create key</b>.
          </Step>
        </Steps>
        <Shot name="key-create" alt="The Create an API key form: name, type, expiry and Create key." />
        <Steps>
          <Step n={1}>
            <b>Copy</b> the key. This is the only time it is shown: store it in a password manager or your server's
            secret store. (It is blurred in this picture.)
          </Step>
          <Step n={2}>
            <b>I've stored it</b> closes the window.
          </Step>
        </Steps>
        <Shot name="key-created" alt="The new key, blurred, with a Copy button, a sample request and I've stored it." />
        <p>Then make your first request, with the key in the Authorization header:</p>
        <Code>{`export OPEN_ALVARY_KEY=oa_live_...
curl "${API}/api/v1/search?q=land" \\
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"`}</Code>
        <p>
          The list shows each key's type, who made it, when it expires and when it was last used. <b>Rotate</b> gives a
          new key in its place; <b>Revoke</b> stops it at once.
        </p>
        <Shot name="keys-list" alt="The API keys list with one personal key, Rotate and Revoke." />
      </section>

      <section id="apps">
        <h2>4. Connect an AI tool</h2>
        <p>
          AI tools that speak MCP sign in with your Open Alvary account and read the corpus for one organisation. You
          approve each one, and can disconnect it any time. Open <b>Connected apps</b> and choose <b>Connect an app</b>.
        </p>
        <Shot name="apps" alt="The Connected apps page with no apps yet and the Connect an app button." />
        <h3>Claude</h3>
        <Steps>
          <Step n={1}>Choose the tab for your tool.</Step>
          <Step n={2}>
            Copy the address, <code>{API}/mcp</code>.
          </Step>
          <Step>
            In Claude, open <b>Settings → Connectors</b>, choose <b>Add custom connector</b>, paste the address and add
            it. Then choose <b>Connect</b>: Claude sends you to Open Alvary to sign in and approve.
          </Step>
        </Steps>
        <Shot name="connect-claude" alt="Connect an app, Claude tab: the steps and the MCP address with a Copy button." />
        <h3>Claude Code</h3>
        <p>Add the server, then run /mcp in Claude Code, choose open-alvary and Authenticate. Your browser opens to approve.</p>
        <Code>{`claude mcp add --transport http open-alvary ${API}/mcp`}</Code>
        <Shot name="connect-code" alt="The Claude Code tab: the command to add the server and how to authenticate." />
        <h3>Codex</h3>
        <p>Add the server and sign in; approve in the browser window that opens.</p>
        <Code>{`codex mcp add open-alvary --url ${API}/mcp
codex mcp login open-alvary`}</Code>
        <h3>Other tools</h3>
        <p>
          Other MCP clients that sign in with OAuth, such as ChatGPT, use the same address. Clients that take a header
          instead can send an API key as <code>Authorization: Bearer</code>.
        </p>
        <h3>Approve</h3>
        <p>
          The tool sends you to this page. The badge says who published it: <b>Verified publisher</b> means we have
          checked who makes the app; be careful with an <b>Unverified app</b> you didn't start yourself.
        </p>
        <Steps>
          <Step n={1}>Choose the organisation it acts for.</Step>
          <Step n={2}>
            <b>Approve</b>. You go back to the tool, and it can now search and read the corpus.
          </Step>
        </Steps>
        <Shot name="consent" alt="The approval page: Claude wants to connect, a Verified publisher badge, the organisation and Approve." />
        <p>
          Connected apps lists every app with who connected it, when, and when it was last used. <b>Disconnect</b> stops
          it at its next request; remove it in the app too, so it stops asking you to sign in.
        </p>
        <Shot name="apps-list" alt="Connected apps listing Claude, a verified publisher, with Disconnect." />
      </section>

      <section id="monitor">
        <h2>5. Monitor changes</h2>
        <p>
          A monitor watches for changes to the records you care about: laws added, catalogue records corrected, texts
          published, records withdrawn. Matches arrive in an inbox in the console, and by email if you choose. Open{" "}
          <b>Monitor</b> and choose <b>New monitor</b>.
        </p>
        <Shot name="monitor-empty" alt="The Monitor page before any monitor exists, with New monitor." />
        <Steps>
          <Step n={1}>A name, such as "Nigeria: new and corrected laws".</Step>
          <Step n={2}>
            Countries to follow. Choosing none means every country; a country includes its states and local law.
          </Step>
          <Step n={3}>
            Kinds of change: <b>Added</b>, <b>Corrected</b>, <b>Text published</b>, <b>Withdrawn</b> and{" "}
            <b>Coverage</b>. None means every kind.
          </Step>
          <Step n={4}>Optionally, a keyword from the title or citation.</Step>
          <Step n={5}>
            Email: <b>No email, inbox only</b>, a <b>Weekly digest</b>, or a <b>Daily digest</b> on paid plans. On Team
            and above you can also share a monitor with your organisation.
          </Step>
          <Step n={6}>
            <b>Create monitor</b>.
          </Step>
        </Steps>
        <Shot name="monitor-new" alt="The New monitor form: name, countries, kinds of change, keyword, email and Create monitor." />
        <p>
          A monitor tells you about changes after it starts. To check your choices first, <b>Preview recent changes</b>{" "}
          shows what it would have matched recently.
        </p>
        <Shot name="monitor-preview" alt="Preview recent changes listing two corrected catalogue records." />
        <p>
          Matches arrive in the <b>Inbox</b>, newest first, with the kind of change, the date and the monitor that
          matched. Each links to the record. <b>Mark all read</b> clears the count; <b>Your monitors</b> lists your
          monitors to edit or delete. The page also shows how many monitors your plan allows.
        </p>
        <Shot name="monitor-inbox" alt="The Monitor inbox with one new match: a corrected record." />
        <p>
          You can also start from any record on this website: under <b>Changes to this record</b>, choose{" "}
          <b>Monitor this record</b>. The console opens a new monitor for that record.
        </p>
        <Shot name="record-monitor" alt="A record page's Changes to this record panel with Monitor this record." />
        <p>
          Choose when digests arrive in <b>Email digests</b>, under <b>Security and profile</b> (your name, at the
          bottom left).
        </p>
        <Steps>
          <Step n={1}>Turn digest emails on or off. Matches always stay in your inbox.</Step>
          <Step n={2}>Your time zone, the time of day, and the day for weekly digests.</Step>
          <Step n={3}>
            <b>Save</b>.
          </Step>
        </Steps>
        <Shot name="monitor-digest" alt="Email digests settings: send digest emails, time zone, time, weekly day and Save." />
        <p>Every digest email has a link to stop it, without signing in.</p>
      </section>

      <section id="webhooks">
        <h2>6. Webhooks</h2>
        <p>
          On paid plans, webhooks send your monitors' matches to your own HTTPS endpoint as they happen, signed so you
          can check each one came from Open Alvary. Create a monitor first, then open <b>Webhooks</b> and choose{" "}
          <b>Add endpoint</b>.
        </p>
        <Shot name="webhooks-empty" alt="The Webhooks page with no endpoints and Add endpoint." />
        <Steps>
          <Step n={1}>
            Your endpoint's address: HTTPS on port 443, reachable from the internet. Add a description if you like, and
            choose the monitors it receives.
          </Step>
          <Step n={2}>
            <b>Add endpoint</b>.
          </Step>
        </Steps>
        <Shot name="webhooks-add" alt="The Add a webhook endpoint form: endpoint URL, description, monitors and Add endpoint." />
        <p>
          Copy the signing secret: it is shown only once (blurred here). We then send your endpoint a challenge. To
          answer it, reply to that first POST, of type <code>endpoint.verification</code>, with a 2xx status and the
          body <code>{'{"challenge": "<the challenge>"}'}</code>. If your endpoint wasn't ready, choose <b>Verify</b>{" "}
          later to try again.
        </p>
        <Shot name="webhooks-secret" alt="The endpoint's signing secret, blurred, and how to verify the endpoint." />
        <p>
          Each endpoint has <b>Verify</b>, <b>Send test</b>, <b>Deliveries</b> (what was sent and how it answered),{" "}
          <b>Rotate secret</b> and <b>Delete</b>. <b>How to receive deliveries</b> on the page has sample code.
        </p>
        <Shot name="webhooks-list" alt="An endpoint not yet verified, with Verify, Send test, Deliveries, Rotate secret and Delete." />
        <p>
          Each delivery is a POST with a JSON body and the headers <code>Open-Alvary-Timestamp</code> and{" "}
          <code>Open-Alvary-Signature</code>: an HMAC-SHA256, with your secret, of the timestamp, a dot and the body.
          Answer 2xx within 10 seconds. We retry for 24 hours; answering 410 turns the endpoint off.
        </p>
      </section>

      <section id="usage">
        <h2>7. Usage</h2>
        <p>
          <b>Usage</b> counts the requests of all your organisation's keys and apps, per day (UTC): requests and data
          sent today, requests refused in the last 30 days, a daily chart, and today's requests by key or
          app. Limits are shared by every key and app in the organisation.
        </p>
        <Shot name="usage" alt="The Usage page: requests today, data sent, refused requests, a daily chart and requests by key." />
      </section>

      <section id="members">
        <h2>8. Members</h2>
        <p>Invite colleagues to your organisation from <b>Members</b>.</p>
        <Steps>
          <Step n={1}>Their email address.</Step>
          <Step n={2}>
            Their role. <b>Admin</b> manages members and shared keys; <b>Developer</b> creates their own keys and
            connects apps; <b>Viewer</b> sees access and usage only. The <b>Owner</b> also manages access and can delete
            the organisation.
          </Step>
          <Step n={3}>
            <b>Send invitation</b>. They accept by signing in with that address within 7 days.
          </Step>
        </Steps>
        <Shot name="members" alt="The Members page: the owner, and the Invite someone form with email, role and Send invitation." />
      </section>

      <section id="security">
        <h2>9. Security and your data</h2>
        <p>
          Choose your name at the bottom left of the console, then <b>Security and profile</b>.
        </p>
        <Steps>
          <Step n={1}>
            <b>Set up a second factor</b>: Touch ID, a security key or an authenticator app. You set it up on the
            sign-in page and come back. Keep the recovery code it shows.
          </Step>
        </Steps>
        <Shot name="security" alt="The Security page: Second factor with Set up a second factor, and sign-in methods." />
        <p>
          Below that you can add a sign-in method (Google or an email code), change your name, and see and end the
          places you are signed in. At the bottom, under Your data:
        </p>
        <Steps>
          <Step n={1}>
            <b>Download my data</b>: everything Open Alvary holds about you.
          </Step>
          <Step n={2}>
            <b>Delete my account</b>. The console first tells you what follows: your keys and connected apps stop,
            and an organisation where you are the only member is deleted with it.
          </Step>
        </Steps>
        <Shot name="your-data" alt="Your data: Download my data and Delete my account." />
        <p>
          See the <Link to="/privacy">privacy notice</Link> for how long things are kept, and the{" "}
          <Link to="/security">security page</Link> for how accounts and keys are protected.
        </p>
      </section>

      <div className="notice">
        Something not working? Each refusal from the API has a code explained on the <Link to="/docs/errors">errors
        page</Link>. For anything else, write to <a href="mailto:hello@ruhu.ai">hello@ruhu.ai</a>.
      </div>
    </div>
  );
}
