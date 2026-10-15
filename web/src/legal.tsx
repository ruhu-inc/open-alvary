// The documents people accept when they create a developer account (D25): terms of service, the
// acceptable use policy and the privacy notice. Their version is the date in accounts/policies.py;
// a new version is a new date, which people accept at their next sign-in.

import React, { useContext } from "react";
import { Link, PageTitle, ProjectContext } from "./site";

export const LEGAL_VERSION = "2026-10-15";
// Until the owner approves them, the pages say they are a draft and not in effect.
const APPROVED = false;
const TO_CONFIRM = [
  "Ruhu Inc.'s registered address",
  "the law and courts that govern these terms",
];

function Status() {
  return (
    <div className="legal-status" role="note">
      <p>
        <b>Version {LEGAL_VERSION}.</b>{" "}
        {APPROVED
          ? "In effect for developer accounts."
          : "Draft for review: not yet in effect. Developer accounts open once it is final."}
      </p>
      {!APPROVED ? <p>Still to confirm: {TO_CONFIRM.join("; ")}.</p> : null}
    </div>
  );
}

function Contact() {
  const project = useContext(ProjectContext);
  return project.contact_email ? (
    <a href={`mailto:${project.contact_email}`}>{project.contact_email}</a>
  ) : (
    <span>the contact address on the About page</span>
  );
}

export function Terms() {
  return (
    <div className="page legal">
      <PageTitle label="TERMS OF SERVICE" title="Terms of service.">
        For developer accounts, the console, the API, MCP and bulk downloads. Reading the website needs no
        account and is not covered by these terms.
      </PageTitle>
      <Status />
      <section>
        <h2>1. Who we are</h2>
        <p>
          Open Alvary is operated by Ruhu Inc. ("we", "us"). These terms are an agreement between us and you,
          the person creating or using a developer account, and, where you act for an organisation, that
          organisation too.
        </p>
      </section>
      <section>
        <h2>2. What these terms cover</h2>
        <p>
          The developer console at open.alvary.ai/console, the REST API and MCP server at api.open.alvary.ai,
          and bulk downloads for organisations with access (together, "the services"). Searching and reading
          the website stay open to everyone without an account.
        </p>
      </section>
      <section>
        <h2>3. Your account and organisations</h2>
        <p>
          Sign in with a method you control and keep it secure. Your account is for you alone. Access,
          keys and usage belong to organisations: whoever owns an organisation decides who joins it and
          which roles they hold, and is responsible for its members' use of the services.
        </p>
        <p>
          Tell us at once if you think someone else has used your account or a key, and revoke the key in the
          console.
        </p>
      </section>
      <section>
        <h2>4. Access, plans and limits</h2>
        <p>
          The API, MCP and bulk downloads are for organisations with access. We grant access, including any
          free or reduced access for public-interest work, at our discretion, for the jurisdictions and
          period we state. Each plan has limits on requests; we publish them and may change them with notice,
          or at once to protect the service.
        </p>
        <p>
          There are no charges while Open Alvary is in its pilot. Before any paid plan applies to you, we will
          tell you its price and ask you to agree to it.
        </p>
      </section>
      <section>
        <h2>5. Keys and connected apps</h2>
        <p>
          API keys are secrets. Keep them in a password manager or your server's secret store, never in
          browser code, public repositories or shared documents. Don't share keys outside your organisation.
          Apps you connect, such as Claude or ChatGPT, act for you and your organisation within the access you
          approve; you can disconnect them at any time.
        </p>
      </section>
      <section>
        <h2>6. The corpus and its rights</h2>
        <p>
          Access controls how you reach the corpus, not the rights in it. Each record says what may be
          reused and on what conditions; follow them, including attribution. Metadata is published under the
          licence stated on the Releases page. Nothing in these terms grants rights in documents published by
          others.
        </p>
        <p>
          Open Alvary is a source of legal information, not legal advice. Coverage is incomplete, and a record
          may not reflect the latest amendment. The official source prevails; check it before relying on any
          text.
        </p>
      </section>
      <section>
        <h2>7. Acceptable use</h2>
        <p>
          Use the services within the <Link to="/acceptable-use">acceptable use policy</Link>, which is part of
          these terms.
        </p>
      </section>
      <section>
        <h2>8. Suspension and closing</h2>
        <p>
          We may suspend or end an account, an organisation, a key or a connection that breaks these terms or
          endangers the service or others, telling you why unless the law or security prevents it. You can
          appeal by writing to <Contact />. You may close your account at any time in the console.
        </p>
      </section>
      <section>
        <h2>9. No warranty</h2>
        <p>
          We provide the services as they are and as available. We work to keep them accurate and running but
          do not promise that they will be error-free, complete or uninterrupted.
        </p>
      </section>
      <section>
        <h2>10. Liability</h2>
        <p>
          To the extent the law allows, we are not liable for indirect or consequential loss, or for loss
          arising from reliance on the corpus without checking the official source. Nothing in these terms
          limits liability that cannot be limited by law.
        </p>
      </section>
      <section>
        <h2>11. Changes</h2>
        <p>
          When we change these terms, we publish the new version with its date and ask you to accept it at your
          next sign-in. If you don't accept it, you can close your account.
        </p>
      </section>
      <section>
        <h2>12. Law</h2>
        <p>These terms are governed by the law stated here once confirmed (see the note above).</p>
      </section>
      <section>
        <h2>Contact</h2>
        <p>
          Questions about these terms: <Contact />.
        </p>
      </section>
    </div>
  );
}

export function AcceptableUse() {
  return (
    <div className="page legal">
      <PageTitle label="ACCEPTABLE USE" title="Acceptable use policy.">
        What you may not do with Open Alvary's developer services. It is part of the terms of service.
      </PageTitle>
      <Status />
      <section>
        <h2>Use the services as they are offered</h2>
        <ul>
          <li>Stay within your organisation's limits. Don't spread requests across accounts, organisations or keys to get around them.</li>
          <li>Don't share keys outside your organisation, sell access, or let others use your account.</li>
          <li>Don't probe, scan or test the services' security without our written agreement. Report what you find to us instead.</li>
          <li>Don't disrupt the services, overload them, or interfere with other people's use.</li>
          <li>Don't scrape the website to get around the API's access and limits.</li>
        </ul>
      </section>
      <section>
        <h2>Respect the corpus and the people in it</h2>
        <ul>
          <li>Follow each record's rights status and conditions, including attribution.</li>
          <li>Don't present Open Alvary's records as official publications, or remove notices that say where a text comes from.</li>
          <li>Don't use the services to identify, profile or target individuals named in legal texts.</li>
          <li>Don't send us confidential client or case information in requests, access requests or support messages.</li>
        </ul>
      </section>
      <section>
        <h2>Lawful use</h2>
        <p>
          Use the services lawfully. This policy doesn't limit lawful reuse of data already published under an
          open licence: its licence governs that reuse.
        </p>
      </section>
      <section>
        <h2>If this policy is broken</h2>
        <p>
          We may limit, suspend or end access, keys or connections, as the terms of service describe. Tell us
          about misuse at <Contact />.
        </p>
      </section>
    </div>
  );
}

export function Privacy() {
  return (
    <div className="page legal">
      <PageTitle label="PRIVACY" title="Privacy notice.">
        What Open Alvary collects, why, for how long, and your choices. Reading the website needs no account and
        sets no tracking.
      </PageTitle>
      <Status />
      <section>
        <h2>Who is responsible</h2>
        <p>
          Ruhu Inc. operates Open Alvary and decides how personal information is used for it. Contact us about
          privacy at <Contact />.
        </p>
      </section>
      <section>
        <h2>Reading the website</h2>
        <p>
          Your browser requests pages, fonts and data from this site; search terms go to our server to find
          matching records. We set no cookies on the website and use no advertising or analytics. Our hosting
          processes connection information such as IP addresses to deliver pages, limit abuse and keep
          operational logs, kept for up to 30 days. Please don't put personal or confidential information in
          searches.
        </p>
      </section>
      <section>
        <h2>Developer accounts</h2>
        <p>If you create an account for the API, MCP or bulk downloads, we process:</p>
        <ul>
          <li>your email address, name and how you sign in (Google or an email code), to identify you and keep your account secure;</li>
          <li>your organisations, roles and invitations, and the access requests you make, to run access;</li>
          <li>the versions of these documents you accepted, and when;</li>
          <li>the names and dates of your keys and connected apps (never a key's secret, which we store only as a keyed hash);</li>
          <li>daily totals of requests and data per key or app, with refusals and their reasons (never the content of your queries), to apply limits and show you your usage;</li>
          <li>an audit log of access changes, with a truncated network address and browser family, to protect your organisation;</li>
          <li>a session cookie for the console only, to keep you signed in.</li>
        </ul>
        <p>
          We use this to provide the services, keep them secure, apply limits and send you necessary notices,
          such as access decisions, changes to these documents and security alerts. We don't sell it or use it
          for advertising.
        </p>
      </section>
      <section>
        <h2>Who processes it for us</h2>
        <ul>
          <li>Auth0, by Okta, runs sign-in and second factors, in its European Union region.</li>
          <li>Google Cloud hosts the services and their database in Belgium (europe-west1).</li>
        </ul>
        <p>They act on our instructions under contracts that protect your information.</p>
      </section>
      <section>
        <h2>How long we keep it</h2>
        <ul>
          <li>Your profile and sign-in methods: while your account exists, removed 30 days after you delete it.</li>
          <li>Console sessions and sign-in attempts: 30 days.</li>
          <li>Organisations, memberships and invitations: while the organisation exists.</li>
          <li>Access requests and decisions: while the organisation exists and two years after.</li>
          <li>Key and app records: one year after they are revoked.</li>
          <li>Usage totals and the audit log: two years.</li>
          <li>Backups: 35 days.</li>
        </ul>
      </section>
      <section>
        <h2>Your choices and rights</h2>
        <p>
          You can see and change your details in the console, export what we hold about you, and delete your
          account there. You can also ask us to access, correct or delete your information, or object to how we
          use it, by writing to <Contact />; you may complain to your data protection authority.
        </p>
      </section>
      <section>
        <h2>Changes</h2>
        <p>
          When this notice changes, we publish the new version with its date and ask account holders to review it
          at their next sign-in.
        </p>
      </section>
    </div>
  );
}
