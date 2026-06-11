import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Moon } from "lucide-react";
import { sink, type V2Meeting } from "../lib/sink";
import { TopNav } from "./TopNav";

/**
 * Meeting picker shown at the root path (alfred-v2).
 *
 * Lists every meeting the sink knows about by canonical Graph
 * ``meeting_id``. The display surface shows the meeting's subject
 * first (with organizer / scheduled time) and the canonical
 * ``meeting_id`` appears on hover so an operator can copy it for
 * tooling without leaving the UI.
 *
 * Clicking a row opens the dossier at
 * ``/m/<meeting_chat_thread_id>`` — the chat thread id is the
 * internal session key the dossier reads from.
 */
export function MeetingList() {
  const [meetings, setMeetings] = useState<V2Meeting[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function tick() {
      try {
        const body = await sink.v2ListMeetings({ limit: 100 });
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

  // Most recent first. Live calls often arrive before Teams metadata fills
  // actual/scheduled times, so use the same activity-driven fields the sink
  // uses for its newest-first ordering.
  const sorted = useMemo(() => {
    const ts = (m: V2Meeting): number => {
      const raw =
        m.last_event_utc ||
        m.actual_start_utc ||
        m.scheduled_start_utc ||
        m.created_at_utc ||
        m.updated_at_utc;
      if (!raw) return 0;
      const n = new Date(raw).getTime();
      return Number.isFinite(n) ? n : 0;
    };
    return [...meetings].sort((a, b) => {
      const diff = ts(b) - ts(a);
      return diff !== 0 ? diff : (b.meeting_id || "").localeCompare(a.meeting_id || "");
    });
  }, [meetings]);

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
        <TopNav />
      </header>

      <main className="flex-1 overflow-auto px-6 py-8">
        <div className="mx-auto max-w-3xl">
          <h2 className="font-serif text-xl text-ink-100">Open a meeting</h2>
          <p className="mt-1 text-sm text-ink-300">
            alfred-v2 — meetings keyed by Graph{" "}
            <code className="font-mono">meeting_id</code>. Most recent first.
            Hover a row to see the canonical id.
          </p>

          {error ? (
            <div className="mt-6 rounded-md border border-crimson-500/40 bg-crimson-500/10 px-4 py-3 text-sm text-crimson-300">
              Could not load meetings: {error}
            </div>
          ) : null}

          <ul className="mt-6 space-y-2">
            {sorted.length === 0 && !error ? (
              <li className="rounded-md border border-ink-800 bg-ink-900/40 px-4 py-3 text-sm italic text-ink-300">
                No meetings yet, sir. The dossier will appear when the bot
                joins a Teams meeting.
              </li>
            ) : null}
            {sorted.map((m) => (
              <MeetingRow key={m.meeting_id} meeting={m} />
            ))}
          </ul>
        </div>
      </main>
    </div>
  );
}

function MeetingRow({ meeting }: { meeting: V2Meeting }) {
  // Dossier UI is still keyed by chat_thread_id internally; the meeting
  // chat thread id is the right surrogate. Fall back to meeting_id when
  // a meeting has never had a chat (rare).
  const linkKey = meeting.meeting_chat_thread_id || meeting.meeting_id;
  const subject = friendlyMeetingTitle(meeting);
  const start = meeting.actual_start_utc || meeting.scheduled_start_utc;
  const end = meeting.actual_end_utc || meeting.scheduled_end_utc;
  const isLive = isMeetingLive(meeting);
  return (
    <li>
      <Link
        to={`/m/${encodeURIComponent(linkKey)}`}
        title={`meeting_id: ${meeting.meeting_id}`}
        className="flex items-center justify-between rounded-md border border-ink-800 bg-ink-900/40 px-4 py-3 transition hover:border-gold-500/40 hover:bg-ink-900"
      >
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-ink-100">
            {subject}
          </div>
          <div className="truncate font-mono text-[11px] text-ink-400">
            {meeting.organizer?.display_name ? (
              <>
                <span>{meeting.organizer.display_name}</span>
                <span className="mx-1.5 text-ink-600">·</span>
              </>
            ) : null}
            {start ? <span>{formatTime(start)}</span> : null}
            {end ? (
              <>
                <span className="mx-1.5 text-ink-600">→</span>
                <span>{formatTime(end)}</span>
              </>
            ) : null}
            {meeting.channel_link?.channel_display_name ? (
              <>
                <span className="mx-1.5 text-ink-600">·</span>
                <span>#{meeting.channel_link.channel_display_name}</span>
              </>
            ) : null}
          </div>
          <div className="truncate font-mono text-[10px] text-ink-500" title={meeting.meeting_id}>
            id: {meeting.meeting_id}
          </div>
        </div>
        <div className="flex flex-col items-end text-right">
          <span
            className={`font-mono text-[10px] uppercase tracking-widest ${
              isLive ? "text-emerald-400" : "text-ink-500"
            }`}
          >
            {isLive ? "live" : "ended"}
          </span>
        </div>
      </Link>
    </li>
  );
}

function formatTime(iso: string): string {
  try {
    const date = new Date(iso);
    if (Number.isNaN(date.valueOf())) return iso;
    return date.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

/**
 * Pick the most human-readable label we can synthesize for a meeting.
 * Preference order:
 *   1. Explicit `subject` (set by the bot when Bot Framework's
 *      Conversation.Name is available, or by an operator via the
 *      transcript-upload form).
 *   2. Organizer display name (e.g. "Logan Robbins's meeting") when we
 *      know who scheduled it.
 *   3. A date-derived placeholder ("Meeting on May 19") so the list never
 *      shows the raw `19:meeting_...@thread.v2` id.
 *   4. The raw meeting_id as the absolute last resort.
 */
export function friendlyMeetingTitle(meeting: V2Meeting): string {
  const subj = meeting.subject?.trim();
  if (subj) return subj;

  const organizerName = meeting.organizer?.display_name?.trim();
  if (organizerName) return `${organizerName}'s meeting`;

  const rawTs =
    meeting.actual_start_utc ||
    meeting.scheduled_start_utc ||
    meeting.last_event_utc ||
    meeting.created_at_utc;
  if (rawTs) {
    const date = new Date(rawTs);
    if (!Number.isNaN(date.valueOf())) {
      return `Meeting on ${date.toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
      })}`;
    }
  }
  return meeting.meeting_id;
}

function isMeetingLive(meeting: V2Meeting): boolean {
  if (meeting.actual_end_utc || meeting.scheduled_end_utc) return false;
  if (meeting.actual_start_utc) return true;

  const raw = meeting.last_event_utc || meeting.updated_at_utc || meeting.created_at_utc;
  if (!raw) return false;

  const ts = new Date(raw).getTime();
  if (!Number.isFinite(ts)) return false;

  return Date.now() - ts < 60 * 60 * 1000;
}
