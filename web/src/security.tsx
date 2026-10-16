// The public security page (MAC-13): how accounts and data are protected, our providers'
// certifications, and the vulnerability disclosure policy that /.well-known/security.txt points to.
// It says only what the service does today; the disclosure terms follow the legal pages' approval.

import React from "react";
import { Email, LEGAL_EMAIL, Status } from "./legal";
import { Link, PageTitle } from "./site";

function External({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer">
      {children}
    </a>
  );
}

export function Security() {
  return (
    <div className="page legal">
      <PageTitle label="SECURITY" title="Security.">
        How we protect developer accounts and the data behind them, and how to tell us about a vulnerability.
      </PageTitle>
      <Status />
      <section>
        <h2>Report a vulnerability</h2>
        <p>
          Email <Email address={LEGAL_EMAIL} /> with "Security" in the subject: what you found, where, how to
          reproduce it and what an attacker could do with it. We acknowledge every report within 3 working days,
          keep you informed until it is fixed, and agree with you when it can be published. Please read
          the <a href="#disclosure">disclosure policy</a> below first.
        </p>
      </section>
      <section>
        <h2>Signing in</h2>
        <ul>
          <li>
            Sign-in runs on Auth0, by Okta. You sign in with Google or with a one-time code sent to your email; we
            never hold a password.
          </li>
          <li>
            Owners and admins of an organisation with access confirm with a second factor. Sensitive actions, such
            as creating or rotating a key, changing roles, transferring ownership, adding a webhook or exporting
            data, need a sign-in from the last 10 minutes.
          </li>
          <li>
            The console keeps you signed in with a cookie that page scripts cannot read and that travels only over
            HTTPS. Sessions end after 7 days without use and after 30 days in all, and you can sign out everywhere
            at once. Every change needs a token tied to your session.
          </li>
        </ul>
      </section>
      <section>
        <h2>Keys and data</h2>
        <ul>
          <li>
            An API key is shown once, when it is created. We keep only a keyed hash of it, so even a copy of our
            database would not reveal it. You can revoke a key or a connected app at any time, and it stops working
            at once.
          </li>
          <li>
            Webhook signing secrets are kept encrypted, with the encryption keys held separately in Google Cloud's
            Secret Manager. Every delivery is signed, so you can check it came from us.
          </li>
          <li>
            Everything travels over HTTPS. Google Cloud encrypts the database and its backups at rest; backups are
            kept for 35 days.
          </li>
          <li>
            Every request checks a record's approval as it is served, so a withdrawal takes effect on the next
            request.
          </li>
          <li>
            What you follow with monitors is visible only to you, or to your organisation for a monitor you share,
            and is kept out of our logs.
          </li>
        </ul>
      </section>
      <section>
        <h2>Audit log</h2>
        <p>
          Changes to access are recorded: sign-ins and sign-in methods, organisations, invitations, members and
          roles, ownership transfers, keys, connected apps, webhooks and access decisions. Entries name people by
          an opaque reference, never by a secret. The service can add entries but not change or delete them;
          they are removed only after two years, under our <Link to="/privacy">retention schedule</Link>.
          Each day's entries are sealed with a hash that also covers the day before, and the seal is kept
          outside the database too, so a change to a past entry shows in our daily check. Owners and admins
          see and export their organisation's entries in the console.
        </p>
      </section>
      <section>
        <h2>Who we rely on</h2>
        <ul>
          <li>
            Auth0, by Okta, runs sign-in and second factors. Its SOC 2 and ISO/IEC 27001 reports are listed in{" "}
            <External href="https://security.okta.com/">Okta's Security Trust Center</External>, and Auth0 is
            an <External href="https://openid.net/certification/certified-openid-providers-profiles/">OpenID
            Certified</External> provider. We review its current reports before developer accounts open and each
            year.
          </li>
          <li>
            Google Cloud hosts the services and their database in Belgium:{" "}
            <External href="https://cloud.google.com/compliance">its certifications and audit reports</External>.
          </li>
          <li>Resend sends our email.</li>
        </ul>
      </section>
      <section>
        <h2>Independent testing</h2>
        <p>
          Before developer accounts open, a qualified independent firm tests the website, the console, the API
          and MCP, sign-in and downloads. Critical and high findings are fixed and retested before launch. The
          test is repeated every year and after major changes, and a summary letter is available to customers on
          request. Between tests, every change to our code, and every week, is checked for dependencies with known
          vulnerabilities and for secrets committed by mistake.
        </p>
      </section>
      <section>
        <h2>If something goes wrong</h2>
        <p>
          If a security incident affects your information, we tell you, and the authorities the law names, within
          the time it sets. If you think your account has been misused, sign out everywhere and revoke your keys
          and connected apps in the console, then write to <Email address={LEGAL_EMAIL} />.
        </p>
      </section>
      <section id="disclosure">
        <h2>Disclosure policy</h2>
        <h3>In scope</h3>
        <ul>
          <li>open.alvary.ai: the website, its data and downloads, and the developer console;</li>
          <li>api.open.alvary.ai: the REST API and the MCP server;</li>
          <li>signing in to the console and connecting apps.</li>
        </ul>
        <h3>Out of scope</h3>
        <ul>
          <li>our providers' own services (Auth0, Google Cloud, Resend): please report those to them;</li>
          <li>denial of service, load testing, and automated scanning at a volume that slows the service;</li>
          <li>social engineering, phishing, or physical attempts against our people or providers;</li>
          <li>findings without a demonstrated security impact, such as version banners;</li>
          <li>
            errors in legal records, which are not security issues: see <Link to="/governance">Governance</Link>{" "}
            for corrections.
          </li>
        </ul>
        <h3>Acting in good faith</h3>
        <ul>
          <li>Test only with accounts and organisations that are yours, or that you have permission to use.</li>
          <li>If you reach anyone else's data, stop, don't keep or share it, and tell us at once.</li>
          <li>Don't change or delete data that isn't yours, and don't degrade the service for others.</li>
          <li>Give us reasonable time to fix the issue before you publish, and agree the date with us.</li>
        </ul>
        <h3>Safe harbour</h3>
        <p>
          If you research in good faith and follow this policy, we consider your research authorised. We won't
          bring or support legal action against you for it, and if anyone else does, we will make clear that you
          acted under this policy. If you are unsure whether something is allowed, ask us first.
        </p>
        <p>
          We don't run a paid bug bounty. Contact details for tools are in{" "}
          <a href="/.well-known/security.txt">security.txt</a>.
        </p>
      </section>
    </div>
  );
}
