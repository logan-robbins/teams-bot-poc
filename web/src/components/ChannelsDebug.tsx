import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Moon, Mic, MessageSquare, RefreshCw, ChevronDown, ChevronRight } from "lucide-react";
import {
  bot,
  type DebugThreadSummary,
  type DebugTailResponse,
} from "../lib/bot";

const POLL_MS = 2000;
const LIVE_THRESHOLD_MS = 10_000;

/**
 * Live debug view of every chat_thread_id that has produced audited
 * events on the bot. Each row is one thread, sorted newest-first, with
 * counts per stream and the most recent final transcript text. Click
 * to expand and tail the last N transcript entries from the bot's
 * NDJSON audit files.
 */
export function ChannelsDebug() {
  const [threads, setThreads] = useState<DebugThreadSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState<Date>(new Date());

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function tick() {
      try {
        const body = await bot.listDebugThreads();
        if (!cancelled) {
          setThreads(body.threads ?? []);
          setNow(new Date(body.now_utc));
          setError(null);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load debug threads");
        }
      } finally {
        if (!cancelled) {
          timer = setTimeout(tick, POLL_MS);
        }
      }
    }
    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
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
            Bot Debug
          </span>
          <span className="font-mono text-[10px] uppercase tracking-widest text-ink-400">
            per-thread audit · {threads.length} thread{threads.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="ml-auto flex items-center gap-2 font-mono text-[10px] text-ink-500">
          <RefreshCw size={10} className="animate-spin-slow" />
          polling every {POLL_MS / 1000}s
        </div>
      </header>

      <main className="flex-1 overflow-auto px-6 py-6">
        <div className="mx-auto max-w-5xl">
          <p className="text-sm text-ink-300">
            Every event Alfred sees is appended to a per-thread NDJSON file
            on the bot VM before any consumer fan-out. This view tails
            those files — useful for verifying STT is firing and the bot
            is receiving real audio.
          </p>
          <p className="mt-1 text-[11px] text-ink-500">
            Threads modified within {LIVE_THRESHOLD_MS / 1000}s are flagged{" "}
            <span className="rounded bg-emerald-500/20 px-1 text-emerald-300">live</span>.
          </p>

          {error ? (
            <div className="mt-4 rounded-md border border-crimson-500/40 bg-crimson-500/10 px-4 py-2 text-sm text-crimson-300">
              {error}
            </div>
          ) : null}

          {threads.length === 0 && !error ? (
            <div className="mt-6 rounded-md border border-ink-800 bg-ink-900/40 px-4 py-3 text-sm italic text-ink-300">
              No audited threads yet. The bot writes a directory the first
              time it sees an event for a chat_thread_id.
            </div>
          ) : null}

          <ul className="mt-4 space-y-3">
            {threads.map((t) => (
              <ThreadRow key={t.chat_thread_id_sanitized} summary={t} now={now} />
            ))}
          </ul>
        </div>
      </main>
    </div>
  );
}

function ThreadRow({
  summary,
  now,
}: {
  summary: DebugThreadSummary;
  now: Date;
}) {
  const [expanded, setExpanded] = useState(false);
  const [tail, setTail] = useState<DebugTailResponse | null>(null);
  const [tailKind, setTailKind] = useState<"transcript" | "chat" | "system">("transcript");
  const [tailErr, setTailErr] = useState<string | null>(null);

  const lastModifiedMs = summary.last_modified_utc
    ? new Date(summary.last_modified_utc).getTime()
    : 0;
  const ageMs = now.getTime() - lastModifiedMs;
  const isLive = lastModifiedMs > 0 && ageMs < LIVE_THRESHOLD_MS;

  useEffect(() => {
    if (!expanded) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function tick() {
      try {
        const body = await bot.tailDebug(summary.chat_thread_id_sanitized, tailKind, 50);
        if (!cancelled) {
          setTail(body);
          setTailErr(null);
        }
      } catch (e) {
        if (!cancelled) {
          setTailErr(e instanceof Error ? e.message : "Failed to tail");
        }
      } finally {
        if (!cancelled) {
          timer = setTimeout(tick, POLL_MS);
        }
      }
    }
    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [expanded, tailKind, summary.chat_thread_id_sanitized]);

  return (
    <li className="rounded-md border border-ink-800 bg-ink-900/40">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-ink-900/80"
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <div className="flex flex-col">
          <span className="font-mono text-xs text-ink-100">
            {summary.chat_thread_id}
          </span>
          <span className="mt-0.5 text-[10px] text-ink-500">
            {summary.last_modified_utc
              ? `last ${formatAge(ageMs)} ago`
              : "never modified"}
          </span>
        </div>
        <div className="ml-auto flex items-center gap-3 text-[10px] font-mono text-ink-300">
          <span className="flex items-center gap-1">
            <Mic size={10} className="text-gold-400" />
            {summary.transcript_lines}
          </span>
          <span className="flex items-center gap-1">
            <MessageSquare size={10} className="text-sky-400" />
            {summary.chat_lines}
          </span>
          <span>sys {summary.system_lines}</span>
          {isLive ? (
            <span className="rounded bg-emerald-500/20 px-1.5 py-0.5 text-emerald-300">
              live
            </span>
          ) : null}
        </div>
      </button>

      {summary.last_final_text ? (
        <div className="border-t border-ink-800 px-4 py-2 text-[11px] italic text-ink-300">
          “{truncate(summary.last_final_text, 220)}”
        </div>
      ) : null}

      {expanded ? (
        <div className="border-t border-ink-800 px-4 py-3">
          <div className="mb-2 flex items-center gap-2 text-[10px] font-mono uppercase tracking-wider text-ink-500">
            stream:
            {(["transcript", "chat", "system"] as const).map((k) => (
              <button
                key={k}
                type="button"
                onClick={() => setTailKind(k)}
                className={
                  k === tailKind
                    ? "rounded bg-gold-500/20 px-2 py-0.5 text-gold-200 ring-1 ring-gold-500/40"
                    : "rounded px-2 py-0.5 text-ink-300 hover:bg-ink-800"
                }
              >
                {k}
              </button>
            ))}
          </div>

          {tailErr ? (
            <div className="rounded-md border border-crimson-500/40 bg-crimson-500/10 px-3 py-2 text-xs text-crimson-300">
              {tailErr}
            </div>
          ) : null}

          {tail && tail.entries.length === 0 ? (
            <div className="text-xs italic text-ink-400">
              No entries in {tailKind}.ndjson yet.
            </div>
          ) : null}

          {tail && tail.entries.length > 0 ? (
            <ol className="space-y-1">
              {tail.entries.map((entry, idx) => (
                <li
                  key={idx}
                  className="rounded border border-ink-800 bg-ink-950/60 px-3 py-2 font-mono text-[11px] leading-relaxed text-ink-100"
                >
                  <EntryRow entry={entry} kind={tailKind} />
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

function EntryRow({
  entry,
  kind,
}: {
  entry: Record<string, unknown>;
  kind: "transcript" | "chat" | "system";
}) {
  const ts = (entry.ts as string | undefined) ?? "?";
  const eventType = (entry.event_type as string | undefined) ?? "?";
  const payload = (entry.payload as Record<string, unknown> | undefined) ?? {};

  if (kind === "transcript") {
    const text = (payload.text as string | undefined) ?? "";
    const speaker = (payload.speaker_id as string | undefined) ?? "—";
    return (
      <div className="flex flex-col gap-0.5">
        <div className="flex items-center gap-2 text-[10px] text-ink-500">
          <span>{ts}</span>
          <span className="text-gold-400">{eventType}</span>
          <span>{speaker}</span>
        </div>
        <div className="text-ink-100">{text || <em className="text-ink-500">(empty)</em>}</div>
      </div>
    );
  }

  if (kind === "chat") {
    const text = (payload.text as string | undefined) ?? "";
    const sender = (payload.sender_display_name as string | undefined) ?? "?";
    return (
      <div className="flex flex-col gap-0.5">
        <div className="flex items-center gap-2 text-[10px] text-ink-500">
          <span>{ts}</span>
          <span className="text-sky-400">{eventType}</span>
          <span>{sender}</span>
        </div>
        <div className="text-ink-100">{text || <em className="text-ink-500">(empty)</em>}</div>
      </div>
    );
  }

  // system: render the whole envelope compactly.
  return (
    <pre className="whitespace-pre-wrap break-all text-[10px] text-ink-300">
      {JSON.stringify(entry, null, 2)}
    </pre>
  );
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1) + "…";
}

function formatAge(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)}m`;
  return `${Math.round(ms / 3_600_000)}h`;
}
