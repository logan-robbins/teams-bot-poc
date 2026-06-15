import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Moon, Plus, Trash2, Save, CalendarClock } from "lucide-react";
import { TopNav } from "./TopNav";
import { bot, type ClientRoute, type MeetingRoute } from "../lib/bot";

/**
 * Email-based client routing admin page.
 *
 * One row per client: email → sink URL (+ optional client-owned
 * storage container). When that person adds Alfred to a meeting,
 * organizes one, or speaks first in its chat, the bot binds the
 * meeting to their route and POSTs every event there — and mirrors
 * envelope blobs into their container when one is registered. Pure
 * CRUD over /api/client-routes.
 *
 * Internal/VPN deployment, no auth.
 */
export function ClientsAdmin() {
  const [routes, setRoutes] = useState<ClientRoute[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true);
    try {
      const body = await bot.listClientRoutes();
      setRoutes(body.routes ?? []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load client routes");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  return (
    <div className="flex h-screen flex-col bg-ink-950 text-ink-50">
      <header className="flex items-center gap-3 border-b border-ink-800 bg-ink-950/80 px-6 py-3 backdrop-blur">
        <Link
          to="/"
          className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-gold-500/20 to-gold-500/5 ring-1 ring-gold-500/30"
          aria-label="Back to meetings"
        >
          <Moon size={18} className="text-gold-400" />
        </Link>
        <div className="flex flex-col leading-tight">
          <span className="font-serif text-lg font-medium text-ink-50">
            Meeting Config
          </span>
          <span className="font-mono text-[10px] uppercase tracking-widest text-ink-400">
            alfred-events-v2 · email-based routing
          </span>
        </div>
        <TopNav onRefresh={() => void refresh()} />
      </header>

      <main className="flex-1 overflow-auto px-6 py-8">
        <div className="mx-auto max-w-5xl">
          <p className="text-sm text-ink-300">
            Register a client by email once. When that person adds Alfred to
            a meeting (or organizes one), every event for that meeting is
            POSTed to their sink URL — and mirrored into their storage
            container if one is set. They never need a meeting, chat, team,
            or channel id. Sink URLs are used exactly as written — include
            the full path (e.g. <code className="font-mono">/v2/events</code>).
            For channel events, register on the{" "}
            <Link to="/channels" className="text-gold-400 underline">Channel Config</Link>{" "}
            page instead.
          </p>

          {error ? (
            <div className="mt-6 rounded-md border border-crimson-500/40 bg-crimson-500/10 px-4 py-3 text-sm text-crimson-300">
              Could not load client routes: {error}
            </div>
          ) : null}

          <AddRoutePanel onSaved={() => void refresh()} />

          <ul className="mt-6 space-y-6">
            {loading && routes.length === 0 ? (
              <li className="text-sm italic text-ink-300">Loading…</li>
            ) : null}

            {!loading && routes.length === 0 && !error ? (
              <li className="rounded-md border border-ink-800 bg-ink-900/40 px-4 py-3 text-sm italic text-ink-300">
                No client routes registered yet. Add one above.
              </li>
            ) : null}

            {routes.map((r) => (
              <ClientRouteRow key={r.email} route={r} onChange={() => void refresh()} />
            ))}
          </ul>
        </div>
      </main>
    </div>
  );
}

function AddRoutePanel({ onSaved }: { onSaved: () => void }) {
  const [email, setEmail] = useState("");
  const [sinkUrl, setSinkUrl] = useState("");
  const [storageUrl, setStorageUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await bot.upsertClientRoute({
        email: email.trim(),
        sink_url: sinkUrl.trim(),
        storage_container_url: storageUrl.trim() || undefined,
        event_kinds: ["*"],
        enabled: true,
      });
      setEmail("");
      setSinkUrl("");
      setStorageUrl("");
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mt-6 rounded-md border border-ink-800 bg-ink-900/40 px-4 py-3">
      <h2 className="font-serif text-base text-ink-100">Register a client</h2>
      <div className="mt-3 grid gap-2 sm:grid-cols-[minmax(14rem,1fr)_minmax(18rem,2fr)_minmax(18rem,2fr)_auto]">
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="michael.barron@disney.com"
          className="rounded border border-ink-700 bg-ink-950 px-3 py-1.5 font-mono text-xs text-ink-100"
          disabled={busy}
        />
        <input
          type="url"
          value={sinkUrl}
          onChange={(e) => setSinkUrl(e.target.value)}
          placeholder="https://michael-agent.example.com/v2/events"
          className="rounded border border-ink-700 bg-ink-950 px-3 py-1.5 font-mono text-xs text-ink-100"
          disabled={busy}
        />
        <input
          type="url"
          value={storageUrl}
          onChange={(e) => setStorageUrl(e.target.value)}
          placeholder="https://account.blob.core.windows.net/container?sv=…  (optional)"
          className="rounded border border-ink-700 bg-ink-950 px-3 py-1.5 font-mono text-xs text-ink-100"
          disabled={busy}
        />
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy || email.trim().length === 0 || sinkUrl.trim().length === 0}
          className="flex items-center gap-1 rounded-md bg-gold-500/20 px-3 py-1.5 text-xs text-gold-200 ring-1 ring-gold-500/40 hover:bg-gold-500/30 disabled:opacity-50"
        >
          <Plus size={12} />
          {busy ? "Saving…" : "Add"}
        </button>
      </div>
      <p className="mt-2 text-[11px] text-ink-400">
        Storage container is a full Azure Blob container URL with a SAS
        granting create/write. Envelopes land at the same canonical paths
        as the central archive (<code className="font-mono">meetings/…</code>).
      </p>
      {error ? (
        <p className="mt-2 font-mono text-[10px] text-crimson-300">{error}</p>
      ) : null}
    </section>
  );
}

function ClientRouteRow({
  route,
  onChange,
}: {
  route: ClientRoute;
  onChange: () => void;
}) {
  const [draft, setDraft] = useState<ClientRoute>(route);
  const [meetings, setMeetings] = useState<MeetingRoute[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDraft(route);
  }, [route]);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await bot.upsertClientRoute({
        ...draft,
        sink_url: draft.sink_url.trim(),
        storage_container_url: draft.storage_container_url?.trim() || undefined,
        event_kinds:
          draft.event_kinds && draft.event_kinds.length > 0
            ? draft.event_kinds
            : ["*"],
      });
      onChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    setError(null);
    try {
      await bot.removeClientRoute(route.email);
      onChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed");
    }
  }

  async function loadMeetings() {
    try {
      const body = await bot.listClientRouteMeetings(route.email);
      setMeetings(body.meetings ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load meetings");
    }
  }

  return (
    <li className="rounded-md border border-ink-800 bg-ink-900/40 px-4 py-3">
      <div className="flex items-baseline gap-3">
        <h3 className="font-mono text-sm text-ink-100">{route.email}</h3>
        <span
          className={
            "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] ring-1 " +
            (route.enabled !== false
              ? "bg-emerald-500/15 text-emerald-200 ring-emerald-500/30"
              : "bg-ink-800/40 text-ink-300 ring-ink-700")
          }
        >
          {route.enabled !== false ? "enabled" : "disabled"}
        </span>
        {route.storage_container_url ? (
          <span className="inline-flex items-center gap-1 rounded bg-gold-500/15 px-1.5 py-0.5 text-[10px] text-gold-200 ring-1 ring-gold-500/30">
            storage mirror
          </span>
        ) : null}
        <span className="ml-auto font-mono text-[10px] text-ink-500">
          updated {fmtTs(route.updated_at_utc)}
        </span>
      </div>

      {error ? (
        <div className="mt-3 rounded-md border border-crimson-500/40 bg-crimson-500/10 px-3 py-2 text-xs text-crimson-300">
          {error}
        </div>
      ) : null}

      <div className="mt-3 grid gap-2">
        <label className="grid grid-cols-[8rem_1fr] items-center gap-2 text-xs text-ink-300">
          Sink URL
          <input
            value={draft.sink_url}
            onChange={(e) => setDraft({ ...draft, sink_url: e.target.value })}
            className="rounded border border-ink-700 bg-ink-950 px-2 py-1 font-mono text-xs text-ink-100"
          />
        </label>
        <label className="grid grid-cols-[8rem_1fr] items-center gap-2 text-xs text-ink-300">
          Storage container
          <input
            value={draft.storage_container_url ?? ""}
            onChange={(e) =>
              setDraft({ ...draft, storage_container_url: e.target.value })
            }
            placeholder="(none — central archive only)"
            className="rounded border border-ink-700 bg-ink-950 px-2 py-1 font-mono text-xs text-ink-100"
          />
        </label>
        <label className="grid grid-cols-[8rem_1fr] items-center gap-2 text-xs text-ink-300">
          Event kinds
          <input
            value={(draft.event_kinds ?? ["*"]).join(",")}
            onChange={(e) =>
              setDraft({
                ...draft,
                event_kinds: e.target.value
                  .split(",")
                  .map((s) => s.trim())
                  .filter((s) => s.length > 0),
              })
            }
            placeholder="*"
            className="rounded border border-ink-700 bg-ink-950 px-2 py-1 font-mono text-xs text-ink-100"
          />
        </label>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-xs text-ink-200">
          <input
            type="checkbox"
            checked={draft.enabled !== false}
            onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
          />
          Enabled
        </label>
        <button
          type="button"
          disabled={saving}
          onClick={() => void save()}
          className="flex items-center gap-1 rounded-md bg-gold-500/20 px-3 py-1.5 text-xs text-gold-200 ring-1 ring-gold-500/40 hover:bg-gold-500/30 disabled:opacity-50"
        >
          <Save size={12} />
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          onClick={() => void loadMeetings()}
          className="flex items-center gap-1 rounded-md border border-ink-700 bg-ink-950 px-3 py-1.5 text-xs text-ink-200 hover:bg-ink-800"
        >
          <CalendarClock size={12} />
          Bound meetings
        </button>
        <button
          type="button"
          onClick={() => void remove()}
          className="ml-auto flex items-center gap-1 rounded-md px-3 py-1.5 text-xs text-ink-400 hover:bg-ink-800 hover:text-crimson-300"
        >
          <Trash2 size={12} />
          Delete route
        </button>
      </div>

      {meetings !== null ? (
        <div className="mt-3 border-t border-ink-800 pt-3">
          {meetings.length === 0 ? (
            <p className="text-xs italic text-ink-400">
              No meetings bound yet. The next meeting this person installs,
              organizes, or speaks in binds automatically.
            </p>
          ) : (
            <ul className="space-y-1">
              {meetings.map((m) => (
                <li
                  key={m.meeting_chat_thread_id}
                  className="flex items-baseline gap-2 font-mono text-[11px] text-ink-300"
                >
                  <span className="truncate">{m.meeting_chat_thread_id}</span>
                  <span className="text-ink-500">via {m.source ?? "?"}</span>
                  <span className="ml-auto shrink-0 text-ink-500">
                    {fmtTs(m.updated_at_utc)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </li>
  );
}

function fmtTs(ts?: string): string {
  if (!ts) return "?";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}
