import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronRight, Radio } from "lucide-react";
import { sink, type V2Meeting } from "../lib/sink";
import { TopNav } from "./TopNav";

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

  const live = sorted.filter(isMeetingLive);
  const past = sorted.filter((m) => !isMeetingLive(m));

  return (
    <div className="flex h-screen flex-col bg-gray-50 text-gray-900">
      {/* Header — Disney blue */}
      <header className="flex items-center gap-3 border-b border-blue-800 bg-blue-900 px-6 py-3">
        <div className="flex flex-col leading-tight">
          <span className="text-lg font-semibold text-white tracking-tight">Alfred</span>
          <span className="text-[10px] uppercase tracking-widest text-blue-300">
            WDI R&D · Meeting Intelligence
          </span>
        </div>
        <TopNav />
      </header>

      {/* Hero */}
      <div className="border-b border-gray-200 bg-white px-6 py-10 text-center">
        <h1 className="text-3xl font-bold text-blue-900 leading-tight">
          Meeting Intelligence,<br />All in One Place.
        </h1>
        <p className="mt-3 text-sm text-gray-500 max-w-xl mx-auto">
          Alfred captures audio, chat, and transcripts from Teams meetings and channels.
          Select a meeting below to open its dossier.
        </p>
      </div>

      <main className="flex-1 overflow-auto px-6 py-8">
        <div className="mx-auto max-w-4xl">
          {error ? (
            <div className="mb-6 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              Could not load meetings: {error}
            </div>
          ) : null}

          {/* Live meetings */}
          {live.length > 0 ? (
            <section className="mb-8">
              <div className="mb-3 flex items-center gap-2">
                <Radio size={14} className="text-emerald-500" />
                <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-500">Live now</h2>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {live.map((m) => (
                  <MeetingCard key={m.meeting_id} meeting={m} live />
                ))}
              </div>
            </section>
          ) : null}

          {/* Past meetings */}
          <section>
            <div className="mb-3">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-500">
                {live.length > 0 ? "Recent meetings" : "Meetings"}
              </h2>
            </div>
            {past.length === 0 && live.length === 0 && !error ? (
              <div className="rounded-lg border border-gray-200 bg-white px-5 py-4 text-sm italic text-gray-400">
                No meetings yet. The dossier will appear when the bot joins a Teams meeting.
              </div>
            ) : null}
            <div className="grid gap-3 sm:grid-cols-2">
              {past.map((m) => (
                <MeetingCard key={m.meeting_id} meeting={m} live={false} />
              ))}
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}

function MeetingCard({ meeting, live }: { meeting: V2Meeting; live: boolean }) {
  const linkKey = meeting.meeting_chat_thread_id || meeting.meeting_id;
  const subject = friendlyMeetingTitle(meeting);
  const start = meeting.actual_start_utc || meeting.scheduled_start_utc;

  return (
    <Link
      to={`/m/${encodeURIComponent(linkKey)}`}
      title={`meeting_id: ${meeting.meeting_id}`}
      className="group flex items-center justify-between rounded-lg border border-gray-200 bg-white px-5 py-4 shadow-sm transition hover:border-blue-400 hover:shadow-md"
    >
      {/* Left accent bar */}
      <div
        className={`mr-4 h-10 w-1 flex-none rounded-full ${
          live ? "bg-emerald-400" : "bg-blue-600"
        }`}
      />

      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold text-gray-800 group-hover:text-blue-700">
          {subject}
        </div>
        <div className="mt-0.5 truncate text-xs text-gray-400">
          {meeting.organizer?.display_name ? (
            <span>{meeting.organizer.display_name}</span>
          ) : null}
          {start ? (
            <>
              {meeting.organizer?.display_name ? <span className="mx-1.5">·</span> : null}
              <span>{formatTime(start)}</span>
            </>
          ) : null}
          {meeting.channel_link?.channel_display_name ? (
            <>
              <span className="mx-1.5">·</span>
              <span>#{meeting.channel_link.channel_display_name}</span>
            </>
          ) : null}
        </div>
      </div>

      <div className="ml-3 flex flex-none items-center gap-2">
        {live ? (
          <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-emerald-600 ring-1 ring-emerald-200">
            Live
          </span>
        ) : null}
        <ChevronRight size={16} className="text-gray-300 group-hover:text-blue-400" />
      </div>
    </Link>
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
