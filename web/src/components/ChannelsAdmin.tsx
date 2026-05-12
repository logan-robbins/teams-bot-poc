import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Moon, Plus, Trash2, Save, RefreshCw, Phone, ExternalLink } from "lucide-react";
import {
  bot,
  type ChannelAttachment,
  type ConsumerConfig,
} from "../lib/bot";

/**
 * Per-channel consumer config admin page.
 *
 * The bot's EventFanoutDispatcher fans every event for a channel out
 * to every URL in that channel's consumer list. This page is the
 * operator UI for that list — pure CRUD over /api/channels/.../consumers.
 *
 * Internal/VPN deployment, no auth.
 */
export function ChannelsAdmin() {
  const [channels, setChannels] = useState<ChannelAttachment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true);
    try {
      const body = await bot.listChannels();
      setChannels(body.attachments ?? []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load channels");
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
            Channel Consumers
          </span>
          <span className="font-mono text-[10px] uppercase tracking-widest text-ink-400">
            alfred-events-v1 · per-channel routing
          </span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={() => void refresh()}
            className="flex items-center gap-1 rounded-md border border-ink-700 bg-ink-900 px-3 py-1.5 text-xs text-ink-200 hover:bg-ink-800"
          >
            <RefreshCw size={12} />
            Refresh
          </button>
        </div>
      </header>

      <main className="flex-1 overflow-auto px-6 py-8">
        <div className="mx-auto max-w-5xl">
          <p className="text-sm text-ink-300">
            Each row is one Teams channel Alfred is attached to. The bot
            POSTs every event for that channel to every consumer URL
            below. See{" "}
            <a
              href="https://github.com/logan-robbins/alfred-teams-bot/blob/main/docs/event-contract.md"
              className="text-gold-400 underline"
              target="_blank"
              rel="noreferrer"
            >
              docs/event-contract.md
            </a>{" "}
            for the envelope shape.
          </p>

          {error ? (
            <div className="mt-6 rounded-md border border-crimson-500/40 bg-crimson-500/10 px-4 py-3 text-sm text-crimson-300">
              Could not load channels: {error}
            </div>
          ) : null}

          <ul className="mt-6 space-y-6">
            {loading && channels.length === 0 ? (
              <li className="text-sm italic text-ink-300">Loading…</li>
            ) : null}

            {!loading && channels.length === 0 && !error ? (
              <li className="rounded-md border border-ink-800 bg-ink-900/40 px-4 py-3 text-sm italic text-ink-300">
                No channels attached yet. Use{" "}
                <code className="font-mono">POST /api/channels/attach</code>{" "}
                or install the app at the team level.
              </li>
            ) : null}

            {channels.map((c) => (
              <ChannelRow
                key={`${c.team_id}|${c.channel_id}`}
                channel={c}
                onChange={() => void refresh()}
              />
            ))}
          </ul>
        </div>
      </main>
    </div>
  );
}

function ChannelRow({
  channel,
  onChange,
}: {
  channel: ChannelAttachment;
  onChange: () => void;
}) {
  const [consumers, setConsumers] = useState<ConsumerConfig[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [joining, setJoining] = useState(false);
  const [joinMessage, setJoinMessage] = useState<string | null>(null);
  const [autoJoin, setAutoJoin] = useState<boolean>(channel.auto_join_enabled !== false);

  async function load() {
    try {
      const body = await bot.listConsumers(channel.team_id, channel.channel_id);
      setConsumers(body.consumers ?? []);
      setError(null);
      setLoaded(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load consumers");
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channel.team_id, channel.channel_id]);

  useEffect(() => {
    setAutoJoin(channel.auto_join_enabled !== false);
  }, [channel.auto_join_enabled]);

  async function joinNow() {
    setJoining(true);
    setJoinMessage(null);
    setError(null);
    try {
      const r = await bot.joinNow(channel.team_id, channel.channel_id);
      setJoinMessage(
        r.deferred
          ? `Deferred (${r.join_mode}): ${r.message ?? "Waiting for Teams to invite Alfred."}`
          : r.call_id
            ? `Alfred is joining. callId=${r.call_id}`
            : (r.message ?? "OK"),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Join failed");
    } finally {
      setJoining(false);
    }
  }

  async function toggleAutoJoin(next: boolean) {
    const prev = autoJoin;
    setAutoJoin(next);
    setError(null);
    try {
      await bot.setAutoJoin(channel.team_id, channel.channel_id, next);
    } catch (e) {
      setAutoJoin(prev);
      setError(e instanceof Error ? e.message : "Toggle failed");
    }
  }

  function patchConsumer(idx: number, patch: Partial<ConsumerConfig>) {
    setConsumers((prev) => prev.map((c, i) => (i === idx ? { ...c, ...patch } : c)));
  }

  function addBlank() {
    setConsumers((prev) => [
      ...prev,
      { name: "", url: "", event_kinds: ["*"], enabled: true },
    ]);
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      // Strip blank rows; trim names + urls.
      const cleaned = consumers
        .map((c) => ({ ...c, name: c.name.trim(), url: c.url.trim() }))
        .filter((c) => c.name.length > 0 && c.url.length > 0);
      const body = await bot.replaceConsumers(
        channel.team_id,
        channel.channel_id,
        cleaned,
      );
      setConsumers(body.consumers);
      onChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function removeOne(name: string) {
    setError(null);
    try {
      await bot.removeConsumer(channel.team_id, channel.channel_id, name);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed");
    }
  }

  return (
    <li className="rounded-md border border-ink-800 bg-ink-900/40 px-4 py-3">
      <div className="flex items-baseline gap-3">
        <h3 className="font-serif text-base text-ink-100">
          {channel.team_display_name ?? <span className="italic text-ink-400">Team (name unknown)</span>}
          <span className="text-ink-500"> / </span>
          {channel.channel_display_name ?? <span className="italic text-ink-400">Channel (name unknown)</span>}
        </h3>
        <span className="ml-auto font-mono text-[10px] text-ink-500">
          {channel.source ?? "unknown source"} ·
          attached {fmtTs(channel.attached_at_utc)}
        </span>
      </div>
      <details className="mt-1 cursor-pointer text-[10px] text-ink-500">
        <summary className="font-mono opacity-60 hover:opacity-100">show ids</summary>
        <div className="mt-1 font-mono">
          team_id: {channel.team_id}<br />
          channel_id: {channel.channel_id}
        </div>
      </details>

      <div className="mt-2">
        <StatusPills channel={channel} />
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-3 border-b border-ink-800 pb-3">
        <Link
          to={`/channels/inspect/${encodeURIComponent(channel.team_id)}/${encodeURIComponent(channel.channel_id)}`}
          className="flex items-center gap-1 rounded-md bg-gold-500/20 px-3 py-1.5 text-xs text-gold-200 ring-1 ring-gold-500/40 hover:bg-gold-500/30"
          title="Open per-channel command center"
        >
          <ExternalLink size={12} />
          Open
        </Link>
        <button
          type="button"
          onClick={() => void joinNow()}
          disabled={joining}
          className="flex items-center gap-1 rounded-md bg-emerald-500/20 px-3 py-1.5 text-xs text-emerald-200 ring-1 ring-emerald-500/40 hover:bg-emerald-500/30 disabled:opacity-50"
          title="Manually trigger Alfred to join the active channel meeting"
        >
          <Phone size={12} />
          {joining ? "Joining…" : "Join now"}
        </button>
        <label className="flex items-center gap-2 text-xs text-ink-200">
          <input
            type="checkbox"
            checked={autoJoin}
            onChange={(e) => void toggleAutoJoin(e.target.checked)}
          />
          Auto-join meetings (fires on Teams "Meeting started" event)
        </label>
        {joinMessage ? (
          <span className="font-mono text-[10px] text-emerald-300">{joinMessage}</span>
        ) : null}
      </div>

      {error ? (
        <div className="mt-3 rounded-md border border-crimson-500/40 bg-crimson-500/10 px-3 py-2 text-xs text-crimson-300">
          {error}
        </div>
      ) : null}

      {!loaded ? (
        <div className="mt-3 text-xs italic text-ink-400">Loading consumers…</div>
      ) : (
        <div className="mt-3 space-y-2">
          {consumers.length === 0 ? (
            <div className="text-xs italic text-ink-400">
              No consumers — events for this channel are dropped after raw audit.
            </div>
          ) : null}
          <table className="w-full table-fixed text-xs">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-wider text-ink-500">
                <th className="w-40 pb-1 pr-2">Name</th>
                <th className="pb-1 pr-2">URL</th>
                <th className="w-44 pb-1 pr-2">Event kinds</th>
                <th className="w-16 pb-1 pr-2">Enabled</th>
                <th className="w-8 pb-1" />
              </tr>
            </thead>
            <tbody>
              {consumers.map((c, idx) => (
                <tr key={`${idx}-${c.name}`} className="border-t border-ink-800">
                  <td className="py-2 pr-2 align-top">
                    <input
                      value={c.name}
                      onChange={(e) => patchConsumer(idx, { name: e.target.value })}
                      placeholder="team-a"
                      className="w-full rounded border border-ink-700 bg-ink-950 px-2 py-1 font-mono text-xs text-ink-100"
                    />
                  </td>
                  <td className="py-2 pr-2 align-top">
                    <input
                      value={c.url}
                      onChange={(e) => patchConsumer(idx, { url: e.target.value })}
                      placeholder="https://team-a.internal/sink"
                      className="w-full rounded border border-ink-700 bg-ink-950 px-2 py-1 font-mono text-xs text-ink-100"
                    />
                  </td>
                  <td className="py-2 pr-2 align-top">
                    <input
                      value={(c.event_kinds ?? ["*"]).join(",")}
                      onChange={(e) =>
                        patchConsumer(idx, {
                          event_kinds: e.target.value
                            .split(",")
                            .map((s) => s.trim())
                            .filter((s) => s.length > 0),
                        })
                      }
                      placeholder="*"
                      className="w-full rounded border border-ink-700 bg-ink-950 px-2 py-1 font-mono text-xs text-ink-100"
                    />
                  </td>
                  <td className="py-2 pr-2 align-top text-center">
                    <input
                      type="checkbox"
                      checked={c.enabled !== false}
                      onChange={(e) => patchConsumer(idx, { enabled: e.target.checked })}
                    />
                  </td>
                  <td className="py-2 align-top">
                    <button
                      type="button"
                      onClick={() => {
                        if (c.name && consumers.find((x, i) => i !== idx && x.name === c.name)) {
                          // duplicate name — strip locally
                          setConsumers((prev) => prev.filter((_, i) => i !== idx));
                          return;
                        }
                        if (c.name) {
                          void removeOne(c.name);
                        } else {
                          setConsumers((prev) => prev.filter((_, i) => i !== idx));
                        }
                      }}
                      className="rounded p-1 text-ink-400 hover:bg-ink-800 hover:text-crimson-300"
                      aria-label="Remove consumer"
                    >
                      <Trash2 size={12} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="flex items-center gap-2 pt-2">
            <button
              type="button"
              onClick={addBlank}
              className="flex items-center gap-1 rounded-md border border-ink-700 bg-ink-950 px-3 py-1.5 text-xs text-ink-200 hover:bg-ink-800"
            >
              <Plus size={12} />
              Add consumer
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => void save()}
              className="flex items-center gap-1 rounded-md bg-gold-500/20 px-3 py-1.5 text-xs text-gold-200 ring-1 ring-gold-500/40 hover:bg-gold-500/30 disabled:opacity-50"
            >
              <Save size={12} />
              {saving ? "Saving…" : "Save list"}
            </button>
          </div>
        </div>
      )}
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

function StatusPills({ channel }: { channel: ChannelAttachment }) {
  const sub = channel.subscription_id ? "ok" : "missing";
  const autoJoinOn = channel.auto_join_enabled !== false;
  const last = channel.last_auto_join_attempt;

  const lastPill = last ? (
    <span
      className={
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] ring-1 " +
        (last.status === "success"
          ? "bg-emerald-500/15 text-emerald-200 ring-emerald-500/30"
          : last.status === "failure"
            ? "bg-crimson-500/15 text-crimson-200 ring-crimson-500/30"
            : "bg-gold-500/15 text-gold-200 ring-gold-500/30")
      }
      title={
        last.error_message
          ? `${last.status} via ${last.trigger} · ${last.error_code ?? ""} ${last.error_message}`
          : `${last.status} via ${last.trigger}`
      }
    >
      last join: {last.status} ({fmtRelative(last.ts)})
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 rounded bg-ink-800/40 px-1.5 py-0.5 text-[10px] text-ink-400 ring-1 ring-ink-700">
      no join attempts yet
    </span>
  );

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span
        className={
          "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] ring-1 " +
          (sub === "ok"
            ? "bg-emerald-500/15 text-emerald-200 ring-emerald-500/30"
            : "bg-gold-500/15 text-gold-200 ring-gold-500/30")
        }
      >
        sub: {sub}
      </span>
      <span
        className={
          "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] ring-1 " +
          (autoJoinOn
            ? "bg-emerald-500/15 text-emerald-200 ring-emerald-500/30"
            : "bg-ink-800/40 text-ink-300 ring-ink-700")
        }
      >
        auto-join: {autoJoinOn ? "on" : "off"}
      </span>
      {lastPill}
    </div>
  );
}

function fmtRelative(ts?: string): string {
  if (!ts) return "?";
  const t = new Date(ts).getTime();
  if (Number.isNaN(t)) return ts;
  const diff = Date.now() - t;
  if (diff < 60_000) return `${Math.round(diff / 1000)}s ago`;
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)}h ago`;
  return `${Math.round(diff / 86_400_000)}d ago`;
}
