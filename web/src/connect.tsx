// Public onboarding page for connecting AI tools to Open Alvary (/connect).
// Readable without signing in; host options and commands come from web/src/ai-hosts.ts.

import React, { useEffect, useId, useRef, useState } from "react";
import { ArrowRight, ArrowUpRight, Check, Copy } from "lucide-react";
import { PLUGIN_REPO_URL, formatCommand, mcpUrl, verifiedHosts, type AiHost } from "./ai-hosts";
import { Link, PageTitle } from "./site";

let lastCopyAnnouncement = "";

async function writeClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const previousFocus = document.activeElement as HTMLElement | null;
    const field = Object.assign(document.createElement("textarea"), { value: text });
    field.setAttribute("readonly", "");
    field.style.cssText = "position:fixed;inset-block-start:0;opacity:0";
    document.body.append(field);
    field.select();
    try {
      return document.execCommand("copy");
    } finally {
      field.remove();
      previousFocus?.focus({ preventScroll: true });
    }
  }
}

function CopyCommand({ value, label }: { value: string; label: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const [announcement, setAnnouncement] = useState("");
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  async function onCopy() {
    window.clearTimeout(timer.current);
    const ok = await writeClipboard(value);
    let message = ok ? `Copied ${label}.` : `Could not copy ${label}.`;
    if (message === lastCopyAnnouncement) message += "\u00A0";
    lastCopyAnnouncement = message;
    setState(ok ? "copied" : "failed");
    setAnnouncement(message);
    timer.current = window.setTimeout(() => {
      setState("idle");
      setAnnouncement("");
    }, 2000);
  }

  return (
    <div className="connect-cmd">
      <code dir="ltr">{value}</code>
      <div className="connect-copy-wrap">
        <button
          type="button"
          className="connect-copy-btn"
          aria-label={`Copy ${label}`}
          data-copied={state === "copied" ? "" : undefined}
          data-failed={state === "failed" ? "" : undefined}
          onClick={onCopy}
        >
          {state === "copied" ? (
            <Check size={15} aria-hidden="true" />
          ) : (
            <Copy size={15} aria-hidden="true" />
          )}
          <span>{state === "copied" ? "Copied" : state === "failed" ? "Retry copy" : "Copy"}</span>
        </button>
        <span className="visually-hidden" role="status" aria-live="polite">
          {announcement}
        </span>
      </div>
    </div>
  );
}

function HostPicker({ hosts }: { hosts: AiHost[] }) {
  const [selectedId, setSelectedId] = useState<string>(hosts[0]?.id ?? "claude");
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const idPrefix = useId();
  const active = hosts.find((h) => h.id === selectedId) ?? hosts[0];
  const endpoint = mcpUrl();

  function selectAt(index: number) {
    const next = hosts[index];
    if (!next) return;
    setSelectedId(next.id);
    const el = tabRefs.current[index];
    el?.focus();
    el?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function onTabKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key === "ArrowRight") {
      event.preventDefault();
      selectAt((index + 1) % hosts.length);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      selectAt((index - 1 + hosts.length) % hosts.length);
    } else if (event.key === "Home") {
      event.preventDefault();
      selectAt(0);
    } else if (event.key === "End") {
      event.preventDefault();
      selectAt(hosts.length - 1);
    }
  }

  if (!active) return null;

  return (
    <section className="connect-picker" aria-labelledby="choose-host">
      <div className="connect-picker-head">
        <h2 id="choose-host">Choose your AI tool</h2>
        <p>
          Pick a supported tool below to copy the Open Alvary MCP address or install command. No
          API key is needed: the tool opens Open Alvary in your browser so you can approve access
          for your organisation.
        </p>
      </div>

      <div className="connect-tabs" role="tablist" aria-label="Supported AI tools">
        {hosts.map((host, index) => {
          const isSelected = host.id === active.id;
          return (
            <button
              key={host.id}
              ref={(el) => {
                tabRefs.current[index] = el;
              }}
              id={`${idPrefix}-tab-${host.id}`}
              type="button"
              role="tab"
              className="connect-tab"
              aria-selected={isSelected}
              aria-controls={`${idPrefix}-panel-${host.id}`}
              tabIndex={isSelected ? 0 : -1}
              onClick={() => selectAt(index)}
              onKeyDown={(event) => onTabKeyDown(event, index)}
            >
              {host.name}
            </button>
          );
        })}
      </div>

      <div
        id={`${idPrefix}-panel-${active.id}`}
        className="connect-panel"
        role="tabpanel"
        aria-labelledby={`${idPrefix}-tab-${active.id}`}
      >
        <p className="connect-panel-summary">{active.summary}</p>
        <ol className="connect-steps">
          {active.installSteps.map((step, i) => {
            const command = step.command ? formatCommand(step.command) : undefined;
            const copyLabel =
              command === endpoint ? `${active.name} MCP address` : `${active.name} install command`;
            return (
              <li key={i}>
                <span>{step.label}</span>
                {command ? (
                  step.copyable ? (
                    <CopyCommand value={command} label={copyLabel} />
                  ) : (
                    <code className="connect-inline-cmd" dir="ltr">
                      {command}
                    </code>
                  )
                ) : null}
              </li>
            );
          })}
        </ol>
        <p className="connect-consent-note">{active.consentNote}</p>
        {active.pluginRepoUrl ? (
          <p className="connect-plugin-link">
            <a href={active.pluginRepoUrl} target="_blank" rel="noreferrer" className="text-link">
              View the Open Alvary plugin and agent skills on GitHub{" "}
              <ArrowUpRight size={15} aria-hidden="true" />
            </a>
          </p>
        ) : null}
      </div>
    </section>
  );
}

export function ConnectPage() {
  const hosts = verifiedHosts();
  return (
    <div className="page connect-page">
      <PageTitle
        label="AI INTEGRATIONS"
        title="Connect Open Alvary to your AI tools."
      >
        Ask questions about African legislation, treaties, regulations and court judgments inside
        your AI assistant, backed by Open Alvary's published catalogue and cleared full texts.
        Read-only access; no API key required for supported tools.
      </PageTitle>

      <section className="start-steps" aria-labelledby="connect-start">
        <h2 id="connect-start">Start in two steps</h2>
        <ol>
          <li>
            <b>Create a free Starter account first.</b> Sign in to the{" "}
            <a href="/console" className="connect-inline-link">
              console
            </a>{" "}
            so your organisation has active Starter access (free, no payment card needed) and
            terms accepted before connecting an AI tool.
          </li>
          <li>
            <b>Connect your AI tool below.</b> Add Open Alvary, sign in when your browser opens,
            choose your organisation, and approve the read-only connection.
          </li>
        </ol>
        <div className="start-actions">
          <a className="button primary" href="/console">
            Open the console <ArrowRight size={16} aria-hidden="true" />
          </a>
          <Link className="text-link" to="/docs/ai-tools">
            Technical MCP guide <ArrowRight size={16} aria-hidden="true" />
          </Link>
        </div>
      </section>

      <div className="info-panel connect-scope">
        <h2>Starter plan jurisdiction scope</h2>
        <p>
          The free Starter plan reads the published law of one jurisdiction — Nigeria by default,
          together with applicable regional African bodies. An organisation owner can change that
          jurisdiction once a month under{" "}
          <a href="/console/access" className="connect-inline-link">
            Plan and limits
          </a>
          .
        </p>
      </div>

      <HostPicker hosts={hosts} />

      <div className="two-columns connect-boundaries">
        <section>
          <h2>What Open Alvary can answer</h2>
          <ul className="connect-list">
            <li>
              Search catalogued legislation, regulations, treaties and court judgments across
              covered jurisdictions.
            </li>
            <li>
              Read and quote exact sections from cleared full texts, with citations, page numbers
              and links to official sources.
            </li>
            <li>
              Check recorded legal status, repeals, amendments, language editions and recent
              catalogue changes.
            </li>
          </ul>
          <p>
            <Link className="text-link" to="/coverage">
              Check current corpus coverage <ArrowRight size={16} aria-hidden="true" />
            </Link>
          </p>
        </section>

        <section>
          <h2>What it cannot answer</h2>
          <ul className="connect-list">
            <li>
              Records without a published text only provide catalogue metadata, rights status and
              links to the official source — not searchable full text.
            </li>
            <li>
              Zero results means Open Alvary does not hold or publish the text within your
              plan&rsquo;s active jurisdictions, never that a law does not exist.
            </li>
            <li>
              AI tools write their own summaries and do not give legal advice. The official
              published copy always prevails.
            </li>
          </ul>
          <p>
            <a
              className="text-link"
              href={PLUGIN_REPO_URL}
              target="_blank"
              rel="noreferrer"
            >
              Open Alvary agent skills on GitHub <ArrowUpRight size={16} aria-hidden="true" />
            </a>
          </p>
        </section>
      </div>

      <section className="info-panel connect-manage">
        <h2>Review or disconnect at any time</h2>
        <p>
          Every connected AI tool has read-only access for the single organisation you approve and
          cannot view your API keys or change settings. You can inspect every active connection or
          revoke access immediately under{" "}
          <a href="/console/apps" className="connect-inline-link">
            Connected apps
          </a>
          . Disconnecting stops the tool on its next request; remove the connector inside your AI
          tool as well.
        </p>
        <p>
          Building a script or custom client that cannot sign in through a browser? Use an API key
          from{" "}
          <a href="/console/keys" className="connect-inline-link">
            API keys
          </a>{" "}
          and see the{" "}
          <Link to="/docs/ai-tools" className="connect-inline-link">
            MCP and AI tools documentation
          </Link>
          .
        </p>
      </section>
    </div>
  );
}
