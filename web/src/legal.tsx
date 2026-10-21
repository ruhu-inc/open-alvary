// The documents people accept when they create a developer account (D25): terms of service, the
// acceptable use policy and the privacy notice. Their version is the date in accounts/policies.py;
// a new version is a new date, which people accept at their next sign-in.

import React from "react";
import { Link, PageTitle } from "./site";

export const LEGAL_VERSION = "2026-10-21";
// Approved by the owner on 2026-10-16 (terms, acceptable use, privacy, and the security page's
// disclosure terms); version 2026-10-18 (data a day in the terms, no request logs for the API and MCP
// in the privacy notice) approved on 2026-10-18; version 2026-10-21 (no query strings in website request
// logs and the Referrer-Policy in the privacy notice; keeping copies in the acceptable use policy)
// approved on 2026-10-21. A new version starts as a draft again: set this to false until it is approved.
const APPROVED = true;
const TO_CONFIRM: string[] = [];
const OPERATOR = "Ruhu, Inc.";
const ADDRESS = "1111B South Governors Ave, STE 52865, Dover, DE 19904, United States";

export function Status() {
  return (
    <div className="legal-status" role="note">
      <p>
        <b>Version {LEGAL_VERSION}.</b>{" "}
        {APPROVED
          ? "In effect for developer accounts."
          : "Draft for review: not yet in effect. Developer accounts open once it is final."}
      </p>
      {!APPROVED && TO_CONFIRM.length ? <p>Still to confirm: {TO_CONFIRM.join("; ")}.</p> : null}
    </div>
  );
}

// Ruhu, Inc.'s own addresses, as on its legal pages: legal matters and privacy.
export const LEGAL_EMAIL = "hello@ruhu.ai";
const PRIVACY_EMAIL = "privacy@ruhu.ai";

export function Email({ address }: { address: string }) {
  return <a href={`mailto:${address}`}>{address}</a>;
}

export function Terms() {
  return (
    <div className="page legal">
      <PageTitle label="TERMS OF SERVICE" title="Terms of service.">
        For developer accounts, the console, the API and MCP. Reading the website needs no
        account and is not covered by these terms.
      </PageTitle>
      <Status />
      <section>
        <h2>1. Who we are</h2>
        <p>
          Open Alvary is operated by {OPERATOR}, a Delaware company whose registered address is {ADDRESS}
          ("we", "us"). These terms are an agreement between us and you,
          the person creating or using a developer account, and, where you act for an organisation, that
          organisation too.
        </p>
      </section>
      <section>
        <h2>2. What these terms cover</h2>
        <p>
          The developer console at open.alvary.ai/console, the REST API and MCP server at api.open.alvary.ai
          (together, "the services"). Searching and reading
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
          The API and MCP are for organisations with access. We grant access, including any
          free or reduced access for public-interest work, at our discretion, for the jurisdictions and
          period we state. Each plan has limits on requests and on how much data you receive each day; we
          publish them and may change them with notice, or at once to protect the service.
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
          appeal by writing to <Email address={LEGAL_EMAIL} />. You may close your account at any time in the console.
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
        <h2>12. Law and disputes</h2>
        <p>
          These terms are governed by the laws of the State of Delaware, United States. If a dispute arises, we
          will first try to settle it with you in good faith for at least 30 days, and then through mediation,
          before either of us goes to court. The courts of the State of Delaware have exclusive jurisdiction.
          Nothing in these terms takes away rights you have under the consumer law of the country where you
          live.
        </p>
      </section>
      <section>
        <h2>Contact</h2>
        <p>
          Questions and legal notices about these terms: <Email address={LEGAL_EMAIL} />, or by post to{" "}
          {OPERATOR}, {ADDRESS}.
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
        <h2>Keeping copies</h2>
        <p>
          You may keep copies of the records and texts your organisation is entitled to, and index them for
          your own search, within each record's rights status and conditions. Keep your copies current: apply
          the withdrawals and corrections the change feed announces within 7 days of their publication, and
          stop showing a withdrawn text from then on.
        </p>
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
          about misuse at <Email address={LEGAL_EMAIL} />.
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
          {OPERATOR} operates Open Alvary and decides how personal information is used for it. Write to us
          about privacy at <Email address={PRIVACY_EMAIL} />, or by post to {OPERATOR}, {ADDRESS}.
        </p>
      </section>
      <section>
        <h2>Reading the website</h2>
        <p>
          Your browser requests pages, fonts and data from this site; search terms go to our server to find
          matching records, only to answer that search. We set no cookies on the website and use no advertising
          or analytics. Our hosting processes connection information such as IP addresses to deliver pages,
          limit abuse and keep operational logs for up to 30 days. Those logs never hold what you search for:
          a request whose address carries a search, or anything else after a question mark, is left out of
          them. Our pages also ask your browser to tell other sites at most that you came from Open Alvary,
          never which page or search. Please don't put personal or confidential information in searches.
        </p>
      </section>
      <section>
        <h2>Developer accounts</h2>
        <p>If you create an account for the API or MCP, we process:</p>
        <ul>
          <li>your email address, name and how you sign in (Google or an email code), to identify you and keep your account secure;</li>
          <li>your organisations, roles and invitations, and the access requests you make, to run access;</li>
          <li>the versions of these documents you accepted, and when;</li>
          <li>the names and dates of your keys and connected apps (never a key's secret, which we store only as a keyed hash);</li>
          <li>daily totals of requests and data per key or app, with refusals and their reasons (never the content of your queries), to apply limits and show you your usage;</li>
          <li>
            what you send the API and MCP (search words, citations, the records you ask for), only to answer
            that request. We don't keep it: the API and MCP keep no request logs, and an error never records it;
          </li>
          <li>an audit log of access changes, with a truncated network address and browser family, to protect your organisation;</li>
          <li>a session cookie for the console only, to keep you signed in;</li>
          <li>
            if you create monitors: their names and what they follow (countries, records, kinds of change,
            keywords), their matches and which you have read, and your digest settings (time zone, time, day),
            to send you what you asked for. What you follow can show what you are working on: we keep it out of
            logs, analytics and exports, never use it to train anything, and only you (or your organisation,
            for a monitor you share) can see it.
          </li>
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
          <li>
            Resend sends our emails, including sign-in codes, from Ireland, and keeps its records of those emails
            in the United States.
          </li>
        </ul>
        <p>
          They act on our instructions under contracts that protect your information, including where it is held
          outside your country.
        </p>
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
          <li>
            Monitors and their filters: while the monitor exists, removed at once when you delete it. Matches: 400
            days. Records of digests sent: 30 days.
          </li>
          <li>Backups: 35 days.</li>
        </ul>
      </section>
      <section>
        <h2>Your choices and rights</h2>
        <p>
          You can see and change your details in the console, export what we hold about you, and delete your
          account there. Every digest email has a link that stops it in one click. You can also ask us to access, correct or delete your information, or object to how we
          use it, by writing to <Email address={PRIVACY_EMAIL} />; you may complain to your data protection
          authority. Depending on where you live, these rights come from laws such as the EU and UK General Data
          Protection Regulation, Nigeria's Data Protection Act 2023, Kenya's Data Protection Act 2019 and South
          Africa's Protection of Personal Information Act.
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
