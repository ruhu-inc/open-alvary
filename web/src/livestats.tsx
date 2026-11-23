import { useEffect, useState, type ReactNode } from "react";
import { Lock, ShieldCheck } from "lucide-react";
import { Link, typeLabel } from "./site";

// The home page's live totals (/site/v1/stats, api.public_stats). The server works them out at most
// every 30 seconds; the page asks again every minute while it is in view and as soon as the reader comes
// back to the tab. Numbers are shown as they are: nothing counts up, nothing moves on its own.
export type LiveStats = {
  as_of: string;
  window_days: number;
  catalogue: {
    verified_texts: number;
    official_sources: number;
    jurisdictions: { id: string; name: string; count: number }[];
    kinds: { value: string; count: number }[];
    rights_basis_share: number | null;
    changes: number | null;
    last_change_at: string | null;
    changes_complete: boolean;
  };
  // null where the totals are not kept (a pilot database); a total under 10 comes as null.
  usage: {
    days: number;
    texts_read: number | null;
    searches: number | null;
    searches_answered: number | null;
    api_requests: number | null;
    organisations_week: number | null;
  } | null;
};

export const REFRESH_MS = 60_000;

// Exact up to 9,999, then 12.4K and 1.2M; a withheld total (under 10) is "<10".
export function figure(n: number | null | undefined, withheld = "<10"): string {
  if (n === null || n === undefined) return withheld;
  if (n < 10_000) return n.toLocaleString("en-GB");
  const [size, unit] = n < 1_000_000 ? [1_000, "K"] : [1_000_000, "M"];
  const short = Math.floor((n / size) * 10) / 10; // never rounds up past the real number
  return (Number.isInteger(short) ? String(short) : short.toFixed(1)) + unit;
}

function plural(count: number, one: string): string {
  const name = one.toLowerCase();
  return count === 1 || name.endsWith("s") ? name : name + "s";
}

export function lastChange(at: string | null, now: Date): string {
  if (!at) return "No changes in the last 30 days";
  const when = new Date(at);
  const day = (d: Date) => d.toISOString().slice(0, 10);
  if (day(when) === day(now))
    return `Last change today, ${when.toISOString().slice(11, 16)} UTC`;
  return (
    "Last change " +
    when.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })
  );
}

export function updated(asOf: string, now: number): string {
  const seconds = Math.max(0, Math.round((now - Date.parse(asOf)) / 1000));
  if (seconds < 10) return "updated just now";
  if (seconds < 90) return `updated ${seconds} seconds ago`;
  const minutes = Math.round(seconds / 60);
  return `updated ${minutes} minute${minutes === 1 ? "" : "s"} ago`;
}

export function useLiveStats() {
  const [stats, setStats] = useState<LiveStats>();
  const [failed, setFailed] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    let controller: AbortController | undefined;
    const read = () => {
      controller?.abort();
      const asking = (controller = new AbortController());
      fetch("/site/v1/stats", { signal: asking.signal })
        .then((r) => {
          if (!r.ok) throw new Error(`Request failed (${r.status})`);
          return r.json();
        })
        .then((data: LiveStats) => {
          if (asking.signal.aborted) return;
          setStats(data);
          setFailed(false);
          setNow(Date.now());
        })
        .catch((e) => {
          // The last totals stay on screen; the line under them says they are not fresh.
          if (!asking.signal.aborted && e.name !== "AbortError") setFailed(true);
        });
    };
    const visible = () => document.visibilityState === "visible";
    read();
    const every = window.setInterval(() => {
      if (visible()) read();
    }, REFRESH_MS);
    // The "seconds ago" line keeps time between reads without asking the server.
    const clock = window.setInterval(() => {
      if (visible()) setNow(Date.now());
    }, 10_000);
    const back = () => {
      if (visible()) read();
    };
    document.addEventListener("visibilitychange", back);
    return () => {
      controller?.abort();
      window.clearInterval(every);
      window.clearInterval(clock);
      document.removeEventListener("visibilitychange", back);
    };
  }, []);
  return { stats, failed, now };
}

function Cell({
  label,
  value,
  to,
  unit,
  children,
}: {
  label: string;
  value: string;
  to?: string;
  unit?: string;
  children: ReactNode;
}) {
  const number = (
    <>
      {value}
      {unit && <small>{unit}</small>}
    </>
  );
  return (
    <div className="live-cell">
      <span>{label}</span>
      {to ? (
        <Link to={to} className="live-number">
          {number}
        </Link>
      ) : (
        <strong className="live-number">{number}</strong>
      )}
      <small>{children}</small>
    </div>
  );
}

export function LiveStatsBlock() {
  const { stats, failed, now } = useLiveStats();
  const c = stats?.catalogue;
  const u = stats?.usage;
  const days = `${stats?.window_days ?? 30} days`;
  const known = (n: number | undefined) => (n === undefined ? "—" : figure(n));
  const usage = (n: number | null | undefined) => (u ? figure(n) : "—");
  const kinds = (c?.kinds ?? []).slice(0, 3).map((k) => plural(k.count, typeLabel(k.value)));
  const countries = (c?.jurisdictions ?? []).map((j) => j.name);
  const share =
    u && u.searches && u.searches_answered !== null
      ? Math.floor((u.searches_answered / u.searches) * 100)
      : null;
  return (
    <section className="live-stats" aria-label="Open Alvary in numbers">
      <div className="live-row live-catalogue">
        <h2>In the catalogue</h2>
        <Cell label="Verified texts" value={known(c?.verified_texts)} to="/browse?access=open">
          Checked page by page against the official original · <Link to="/governance">how</Link>
        </Cell>
        <Cell label="Official sources" value={known(c?.official_sources)} to="/browse">
          {[countries.join(", "), kinds.join(", ")].filter(Boolean).join(" · ")}
          {countries.length || kinds.length ? " · " : ""}
          <Link to="/coverage">coverage</Link>
        </Cell>
        <Cell
          label="Rights basis"
          value={c?.rights_basis_share != null ? `${Math.round(c.rights_basis_share * 100)}%` : "—"}
          to="/governance"
        >
          {c?.rights_basis_share != null
            ? "of published texts have a recorded rights basis"
            : "Shown once texts are published: the share with a recorded rights basis"}
        </Cell>
        <Cell
          label="Changes"
          value={c && c.changes !== null ? figure(c.changes) : "—"}
          unit={days}
          to="/changes"
        >
          {c && c.changes !== null && <>{lastChange(c.last_change_at, new Date(now))} · </>}
          <Link to="/changes">change log</Link>
        </Cell>
      </div>
      <div className="live-row live-usage">
        <h2>How it’s used · last {days}</h2>
        <Cell label="Texts read" value={usage(u?.texts_read)} unit={days} to="/privacy#reading-the-website">
          People only · crawlers and bots excluded
        </Cell>
        <Cell
          label="Searches answered"
          value={usage(u?.searches_answered)}
          unit={days}
          to="/privacy#reading-the-website"
        >
          {share !== null ? `${share}% of searches found a law` : "Searches that found a law"}
        </Cell>
        <Cell label="API & MCP requests" value={usage(u?.api_requests)} unit={days} to="/developers">
          {u?.organisations_week != null
            ? `${figure(u.organisations_week)} organisations active this week`
            : u
              ? "Fewer than 10 organisations active this week"
              : "From keys made in the developer console"}
        </Cell>
        <div className="live-cell live-privacy">
          <Lock size={18} strokeWidth={1.6} aria-hidden="true" />
          <p>
            <b>Searched words are never stored.</b> We count that a search happened, never what it was.{" "}
            <Link to="/privacy#reading-the-website">Privacy</Link>
          </p>
        </div>
      </div>
      <div className="live-foot">
        <span>
          <ShieldCheck size={16} strokeWidth={1.6} aria-hidden="true" />
          Publicly accessible doesn’t automatically mean redistributable.
        </span>
        <span>
          <span className={failed || !stats ? "live-dot stale" : "live-dot"} aria-hidden="true" />
          {stats
            ? `${failed ? "Last totals" : "Live totals"} · ${updated(stats.as_of, now)} · people only, never what they searched`
            : failed
              ? "Totals unavailable just now"
              : "Loading the totals…"}
        </span>
      </div>
    </section>
  );
}
