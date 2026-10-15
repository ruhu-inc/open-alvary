// Shared pieces of the console: the mark, status pills, refusals with a way forward, dialogs and
// copy fields.

import React, { useEffect, useId, useRef, useState } from "react";
import { ApiError, STEP_UP, stepUpUrl } from "./api";

export function Mark({ size = 28 }: { size?: number }) {
  // The site's outlined mark (web/brand/mark.py); the heavier line below 36px.
  const heavy = size < 36;
  return (
    <svg className="mark" width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" focusable="false">
      <g fill="none" stroke="currentColor" strokeWidth={heavy ? 3.2 : 2.2}>
        {(heavy
          ? [
              "32,7.77 20.23,32.85 32,40.42 43.77,32.85",
              "17.78,38.09 27.86,44.31 9.48,55.78",
              "46.22,38.09 54.52,55.78 36.14,44.31",
            ]
          : [
              "32,6.59 19.59,33.04 32,41.01 44.41,33.04",
              "17.56,37.37 28.81,44.31 8.31,57.1",
              "46.44,37.37 55.69,57.1 35.19,44.31",
            ]
        ).map((points) => (
          <polygon key={points} points={points} />
        ))}
      </g>
    </svg>
  );
}

export function Pill({ tone, children }: { tone?: "good" | "warn" | "bad"; children: React.ReactNode }) {
  return <span className={"pill" + (tone ? " " + tone : "")}>{children}</span>;
}

export function accessLabel(state: string): [string, "good" | "warn" | "bad" | undefined] {
  switch (state) {
    case "active":
      return ["Access active", "good"];
    case "lapsed":
    case "ended":
      return ["Access ended", "bad"];
    case "revoked":
      return ["Access revoked", "bad"];
    default:
      return ["No access yet", undefined];
  }
}

export const ROLE_NAMES: Record<string, string> = {
  owner: "Owner",
  admin: "Admin",
  developer: "Developer",
  viewer: "Viewer",
};

export const ROLE_HELP: Record<string, string> = {
  owner: "Manages access and can delete the organisation",
  admin: "Manages members and shared keys",
  developer: "Creates own keys and connects apps",
  viewer: "Sees access and usage only",
};

// A refusal, said plainly, with the step that fixes it when there is one.
export function Problem({ error, onDismiss }: { error: unknown; onDismiss?: () => void }) {
  if (!error) return null;
  const e = error instanceof ApiError ? error : new ApiError(0, "unknown", "Something went wrong. Try again.");
  if (STEP_UP.has(e.code)) {
    const second = e.code === "second_factor_required";
    return (
      <div className="callout" role="alert">
        <p>
          {second
            ? "Owners and admins confirm sensitive changes with a second factor. Set one up or use it now; you'll come straight back here."
            : "For your security, sign in again to confirm it's you. You'll come straight back here."}
        </p>
        <a className="btn" href={stepUpUrl(e.code)}>
          {second ? "Continue with second factor" : "Sign in again"}
        </a>
      </div>
    );
  }
  return (
    <div className="callout bad" role="alert">
      <p>
        {e.message}
        {e.requestId ? <span className="fine"> Reference {e.requestId}.</span> : null}
      </p>
      {onDismiss ? (
        <button className="link" type="button" onClick={onDismiss}>
          Dismiss
        </button>
      ) : null}
    </div>
  );
}

export function Dialog({
  title,
  onClose,
  children,
  closable = true,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  closable?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("input, select, textarea, button")?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape" && closable) onClose();
    };
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      opener?.focus();
    };
  }, [onClose, closable]);
  return (
    <div className="scrim">
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby={id} ref={ref}>
        <h2 id={id}>{title}</h2>
        {children}
      </div>
    </div>
  );
}

export function CopyField({ value, label, onCopied }: { value: string; label: string; onCopied?: () => void }) {
  const [state, setState] = useState<"idle" | "copied" | "select">("idle");
  const ref = useRef<HTMLElement>(null);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setState("copied");
      onCopied?.();
    } catch {
      // No clipboard access: select the text so the person can copy it themselves.
      const range = document.createRange();
      if (ref.current) {
        range.selectNodeContents(ref.current);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
      }
      setState("select");
    }
  }
  return (
    <div className="copyline">
      <code className="code" ref={ref} aria-label={label} dir="ltr">
        {value}
      </code>
      <button className="btn secondary" type="button" onClick={copy}>
        {state === "copied" ? "Copied" : "Copy"}
      </button>
      <span className="sr-status" aria-live="polite">
        {state === "copied" ? "Copied to the clipboard." : state === "select" ? "Selected. Press Ctrl+C or ⌘C to copy." : ""}
      </span>
    </div>
  );
}

export function when(value: string | null | undefined, withTime = false): string {
  if (!value) return "—";
  const date = new Date(value);
  return date.toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit", timeZone: "UTC", timeZoneName: "short" } : {}),
  });
}

export function ago(value: string | null | undefined): string {
  if (!value) return "Never";
  const seconds = (Date.now() - new Date(value).getTime()) / 1000;
  if (seconds < 90) return "Just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} minutes ago`;
  if (seconds < 86400 * 2) return `${Math.round(seconds / 3600)} hours ago`;
  return when(value);
}

export function number(value: number): string {
  return value.toLocaleString("en-GB");
}

export function useAsync<T>(load: () => Promise<T>, deps: unknown[]) {
  const [state, setState] = useState<{ data?: T; error?: unknown; loading: boolean }>({ loading: true });
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let live = true;
    setState((s) => ({ ...s, loading: true }));
    load().then(
      (data) => live && setState({ data, loading: false }),
      (error) => live && setState({ error, loading: false }),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, version]);
  return { ...state, reload: () => setVersion((v) => v + 1) };
}

export function Loading() {
  return (
    <p className="fine" role="status">
      Loading…
    </p>
  );
}
