import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Moon, Trash2, Save } from "lucide-react";
import { TopNav } from "./TopNav";
import {
  bot,
  type ChannelAttachment,
  type ConsumerConfig,
} from "../lib/bot";

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
    <div className="flex h-screen flex-col bg-gray-50 text-gray-900">
      <header className="flex items-center gap-3 border-b border-blue-800 bg-blue-900 px-6 py-3">
        <Link
          to="/"
          className="flex h-9 w-9 items-center justify-center rounded-lg bg-white/10 ring-1 ring-white/20"
          aria-label="Back to meetings"
        >
          <Moon size={18} className="text-blue-200" />
        </Link>
        <div className="flex flex-col leading-tight">
          <span className="text-lg font-semibold text-white tracking-tight">Channel Config</span>
          <span className="text-[10px] uppercase tracking-widest text-blue-300">
            alfred-events-v2 · per-channel routing
          </span>
        </div>
        <TopNav onRefresh={() => void refresh()} />
      </header>

      <main className="flex-1 overflow-auto px-6 py-8">
        <div className="mx-auto max-w-5xl">
          <p className="text-sm text-gray-500">
            Each row is one Teams channel Alfred is attached to. Paste a sink
            URL and hit Save — the bot POSTs every matching event for that
            channel to that URL. An empty list falls back to the default sink;
            a single disabled row silences push entirely. For meeting-scoped
            routing by person, use the{" "}
            <Link to="/clients" className="text-blue-600 underline">
              Meeting Config
            </Link>{" "}
            page instead.
          </p>

          {error ? (
            <div className="mt-6 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              Could not load channels: {error}
            </div>
          ) : null}

          <ul className="mt-6 space-y-4">
            {loading && channels.length === 0 ? (
              <li className="text-sm italic text-gray-400">Loading…</li>
            ) : null}

            {!loading && channels.length === 0 && !error ? (
              <li className="rounded-lg border border-gray-200 bg-white px-4 py-3 text-sm italic text-gray-400">
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

  function patchConsumer(idx: number, patch: Partial<ConsumerConfig>) {
    setConsumers((prev) => prev.map((c, i) => (i === idx ? { ...c, ...patch } : c)));
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
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
    <li className="rounded-lg border border-gray-200 bg-white shadow-sm px-5 py-4">
      <div className="flex items-baseline gap-3">
        <h3 className="text-sm font-semibold text-gray-800">
          {channel.team_display_name ?? <span className="italic text-gray-400">Team (name unknown)</span>}
          <span className="text-gray-400"> / </span>
          {channel.channel_display_name ?? <span className="italic text-gray-400">Channel (name unknown)</span>}
        </h3>
        <span className="ml-auto font-mono text-[10px] text-gray-400">
          {channel.source ?? "unknown source"} · attached {fmtTs(channel.attached_at_utc)}
        </span>
      </div>
      <details className="mt-1 cursor-pointer text-[10px] text-gray-400">
        <summary className="font-mono opacity-60 hover:opacity-100">show ids</summary>
        <div className="mt-1 font-mono text-gray-500">
          team_id: {channel.team_id}<br />
          channel_id: {channel.channel_id}
        </div>
      </details>

      {error ? (
        <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </div>
      ) : null}

      {!loaded ? (
        <div className="mt-3 text-xs italic text-gray-400">Loading consumers…</div>
      ) : (
        <div className="mt-3 space-y-2">
          {consumers.length === 0 ? (
            <div className="text-xs italic text-gray-400">
              No consumers — events fall back to the default sink.
            </div>
          ) : null}
          <table className="w-full table-fixed text-xs">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-wider text-gray-400">
                <th className="w-40 pb-1 pr-2">Name</th>
                <th className="pb-1 pr-2">URL</th>
                <th className="w-44 pb-1 pr-2">Event kinds</th>
                <th className="w-16 pb-1 pr-2">Enabled</th>
                <th className="w-8 pb-1" />
              </tr>
            </thead>
            <tbody>
              {consumers.map((c, idx) => (
                <tr key={`${idx}-${c.name}`} className="border-t border-gray-100">
                  <td className="py-2 pr-2 align-top">
                    <input
                      value={c.name}
                      onChange={(e) => patchConsumer(idx, { name: e.target.value })}
                      placeholder="team-a"
                      className="w-full rounded border border-gray-300 bg-white px-2 py-1 font-mono text-xs text-gray-800"
                    />
                  </td>
                  <td className="py-2 pr-2 align-top">
                    <input
                      value={c.url}
                      onChange={(e) => patchConsumer(idx, { url: e.target.value })}
                      placeholder="https://your-host/v2/events"
                      className="w-full rounded border border-gray-300 bg-white px-2 py-1 font-mono text-xs text-gray-800"
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
                      className="w-full rounded border border-gray-300 bg-white px-2 py-1 font-mono text-xs text-gray-800"
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
                          setConsumers((prev) => prev.filter((_, i) => i !== idx));
                          return;
                        }
                        if (c.name) {
                          void removeOne(c.name);
                        } else {
                          setConsumers((prev) => prev.filter((_, i) => i !== idx));
                        }
                      }}
                      className="rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-500"
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
              disabled={saving}
              onClick={() => void save()}
              className="flex items-center gap-1 rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
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
