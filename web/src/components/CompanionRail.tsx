import { useState } from "react";
import { VolumeX, Volume2, Play, Square, Hash, FileText } from "lucide-react";
import { sink } from "../lib/sink";
import type { AlfredAnalysisBody, SessionSummary } from "../lib/types";

interface Props {
  session?: SessionSummary;
  analysis?: AlfredAnalysisBody;
  muted: boolean;
  setMuted: (m: boolean) => void;
  onAfterMutation: () => void;
}

/**
 * The Companion Rail — right-side narrow column.
 *
 *  1. Running summary    (Alfred's rolling narrative)
 *  2. Topics             (chips)
 *  3. Session controls   (start / mute / end)
 *
 * The UI is read-only with respect to the meeting. Only Alfred speaks into
 * the meeting chat, and he does so via the `send_to_meeting_chat` agent tool.
 */
export function CompanionRail({
  session,
  analysis,
  muted,
  setMuted,
  onAfterMutation,
}: Props) {
  const summary = analysis?.running_summary?.trim();
  const topics = analysis?.topics ?? [];

  return (
    <aside className="flex h-full flex-col overflow-hidden border-l border-ink-800 bg-ink-950/40">
      <div className="flex-1 overflow-y-auto p-5 space-y-6">
        <SummaryPanel summary={summary} />
        <TopicsPanel topics={topics} />
        <SessionPanel
          session={session}
          muted={muted}
          setMuted={setMuted}
          onAfterMutation={onAfterMutation}
        />
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------

function SummaryPanel({ summary }: { summary?: string }) {
  return (
    <section>
      <RailHeader icon={<FileText size={12} />} title="Running summary" />
      <div className="rounded-xl border border-ink-700 bg-ink-900/60 px-4 py-3">
        {summary ? (
          <p className="font-serif text-[13.5px] leading-relaxed text-ink-50">
            {summary}
          </p>
        ) : (
          <p className="font-serif text-[13px] italic text-ink-300">
            I shall begin summarising once the meeting is underway.
          </p>
        )}
      </div>
    </section>
  );
}

function TopicsPanel({ topics }: { topics: string[] }) {
  return (
    <section>
      <RailHeader icon={<Hash size={12} />} title="Topics" />
      {topics.length === 0 ? (
        <p className="text-[12px] italic text-ink-300">
          Topics will emerge as the conversation unfolds.
        </p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {topics.slice(0, 24).map((t) => (
            <span
              key={t}
              className="rounded-full border border-ink-700 bg-ink-900/60 px-2.5 py-0.5 text-[11px] text-ink-200"
            >
              {t}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}

function SessionPanel({
  session,
  muted,
  setMuted,
  onAfterMutation,
}: {
  session?: SessionSummary;
  muted: boolean;
  setMuted: (m: boolean) => void;
  onAfterMutation: () => void;
}) {
  const active = !!session?.active;
  const [label, setLabel] = useState(session?.candidate_name ?? "");
  const [url, setUrl] = useState(
    session?.meeting_url ?? "https://teams.microsoft.com/l/meetup-join/",
  );
  const [busy, setBusy] = useState(false);

  async function start() {
    setBusy(true);
    try {
      await sink.startSession({
        candidate_name: label || "Meeting",
        meeting_url: url || "https://teams.microsoft.com/l/meetup-join/",
      });
      onAfterMutation();
    } finally {
      setBusy(false);
    }
  }

  async function end() {
    setBusy(true);
    try {
      await sink.endSession();
      onAfterMutation();
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <RailHeader icon={<Play size={12} />} title="Session" />
      <div className="space-y-2">
        {!active ? (
          <>
            <TextField label="Meeting label" value={label} onChange={setLabel} placeholder="Weekly staff sync" />
            <TextField label="Meeting URL" value={url} onChange={setUrl} mono />
            <button
              type="button"
              onClick={start}
              disabled={busy}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-gold-500 px-3 py-2 text-sm font-medium text-ink-950 transition hover:bg-gold-400 disabled:opacity-50"
            >
              <Play size={13} /> Begin session
            </button>
          </>
        ) : (
          <>
            <div className="rounded-lg border border-ink-700 bg-ink-900/60 px-3 py-2 text-[12px]">
              <div className="text-[10px] uppercase tracking-wider text-ink-500">session id</div>
              <div className="mt-0.5 font-mono text-ink-200 truncate">{session?.session_id}</div>
            </div>
            <button
              type="button"
              onClick={end}
              disabled={busy}
              className="flex w-full items-center justify-center gap-2 rounded-lg border border-ink-700 bg-ink-900 px-3 py-2 text-sm text-ink-200 transition hover:border-crimson-500/50 hover:text-crimson-400 disabled:opacity-50"
            >
              <Square size={13} /> End session
            </button>
          </>
        )}

        <button
          type="button"
          onClick={() => setMuted(!muted)}
          className={`flex w-full items-center justify-center gap-2 rounded-lg border px-3 py-2 text-sm transition ${
            muted
              ? "border-crimson-500/40 bg-crimson-500/10 text-crimson-300"
              : "border-ink-700 bg-ink-900 text-ink-200 hover:border-ink-600"
          }`}
        >
          {muted ? <VolumeX size={13} /> : <Volume2 size={13} />}
          {muted ? "Alfred is muted" : "Mute Alfred"}
        </button>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------

function RailHeader({
  icon,
  title,
}: {
  icon: React.ReactNode;
  title: string;
}) {
  return (
    <div className="mb-2 flex items-center gap-1.5">
      <span className="text-ink-400">{icon}</span>
      <h4 className="font-mono text-[10px] uppercase tracking-widest text-ink-300">
        {title}
      </h4>
    </div>
  );
}

function TextField({
  label,
  value,
  onChange,
  placeholder,
  mono,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
}) {
  return (
    <label className="block">
      <span className="font-mono text-[10px] uppercase tracking-widest text-ink-500">
        {label}
      </span>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={`mt-1 w-full rounded-md border border-ink-700 bg-ink-900 px-2.5 py-1.5 text-[13px] text-ink-100 placeholder:text-ink-500 focus:border-gold-500/50 focus:outline-none ${
          mono ? "font-mono text-[11.5px]" : ""
        }`}
      />
    </label>
  );
}
