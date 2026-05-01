import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Moon } from "lucide-react";
import { sink, type MeetingListEntry } from "../lib/sink";

/**
 * Meeting picker shown at the root path.
 *
 * The dossier UI requires a chat_thread_id in the URL — there is no
 * "current meeting" fallback. This page polls ``/m`` every 2 seconds and
 * lists active meetings the operator can click into.
 */
export function MeetingList() {
  const [meetings, setMeetings] = useState<MeetingListEntry[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function tick() {
      try {
        const body = await sink.listMeetings();
        if (!cancelled) {
          setMeetings(body.meetings ?? []);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load meetings");
        }
      } finally {
        if (!cancelled) {
          timer = setTimeout(tick, 2000);
        }
      }
    }
    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  return (
    <div className="flex h-screen flex-col bg-ink-950 text-ink-50">
      <header className="flex items-center gap-3 border-b border-ink-800 bg-ink-950/80 px-6 py-3 backdrop-blur">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-gold-500/20 to-gold-500/5 ring-1 ring-gold-500/30">
          <Moon size={18} className="text-gold-400" />
        </div>
        <div className="flex flex-col leading-tight">
          <span className="font-serif text-lg font-medium text-ink-50">Alfred</span>
          <span className="font-mono text-[10px] uppercase tracking-widest text-ink-400">
            Meeting Selector
          </span>
        </div>
      </header>

      <main className="flex-1 overflow-auto px-6 py-8">
        <div className="mx-auto max-w-3xl">
          <h2 className="font-serif text-xl text-ink-100">Open a meeting</h2>
          <p className="mt-1 text-sm text-ink-300">
            The dossier requires a <code className="font-mono">chat_thread_id</code> in
            the URL. Pick a live meeting below, or open
            <code className="ml-1 font-mono">/m/&lt;chat_thread_id&gt;</code> directly.
          </p>

          {error ? (
            <div className="mt-6 rounded-md border border-crimson-500/40 bg-crimson-500/10 px-4 py-3 text-sm text-crimson-300">
              Could not load meetings: {error}
            </div>
          ) : null}

          <ul className="mt-6 space-y-2">
            {meetings.length === 0 && !error ? (
              <li className="rounded-md border border-ink-800 bg-ink-900/40 px-4 py-3 text-sm italic text-ink-300">
                No meetings yet, sir. The dossier will appear when the bot
                joins a Teams meeting.
              </li>
            ) : null}
            {meetings.map((m) => (
              <li key={m.chat_thread_id}>
                <Link
                  to={`/m/${encodeURIComponent(m.chat_thread_id)}`}
                  className="flex items-center justify-between rounded-md border border-ink-800 bg-ink-900/40 px-4 py-3 transition hover:border-gold-500/40 hover:bg-ink-900"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-ink-100">
                      {m.candidate_name || m.chat_thread_id}
                    </div>
                    <div className="truncate font-mono text-[11px] text-ink-400">
                      {m.chat_thread_id}
                    </div>
                  </div>
                  <div className="flex flex-col items-end text-right">
                    <span
                      className={`font-mono text-[10px] uppercase tracking-widest ${
                        m.active ? "text-emerald-400" : "text-ink-500"
                      }`}
                    >
                      {m.active ? "live" : "ended"}
                    </span>
                    <span className="text-[11px] text-ink-400">
                      {m.total_events} events
                    </span>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      </main>
    </div>
  );
}
