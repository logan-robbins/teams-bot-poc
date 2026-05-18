import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Mic,
  MessageSquare,
  Phone,
  PhoneOff,
  RefreshCw,
  Radio,
  FileText,
  Power,
  AlertCircle,
} from "lucide-react";
import {
  bot,
  type AutoJoinAttempt,
  type CallReadiness,
  type ChannelAttachment,
  type DebugTailResponse,
} from "../lib/bot";
import { sink, type ChannelLedgerEvent } from "../lib/sink";

const POLL_MS = 2000;
const OFFICIAL_POLL_MS = 15_000;
const TAIL_LINES = 50;

/**
 * Per-channel command center. Platform-agnostic: this page is for any
 * team consuming the alfred-events-v1 stream, not specifically for the
 * Alfred note-taker agent. Surfaces, in priority order:
 *
 *   1. Is the bot actually picking up "Meeting started" events for this
 *      channel? (last_auto_join_attempt + active call)
 *   2. What is being said RIGHT NOW? (live STT audit tail from the bot)
 *   3. What was the official Microsoft transcript for finished
 *      meetings? (sink's channel ledger, source=graph_notification)
 *   4. Configuration (consumers, toggle, manual trigger).
 *
 * Agent-specific consumer state (running summary, notes, dossier) is
 * intentionally NOT rendered here — other teams may ingest the
 * transcripts and build their own downstream UIs.
 */
export function ChannelCommandCenter() {
  const params = useParams<{ teamId: string; channelId: string }>();
  const navigate = useNavigate();
  const teamId = decodeURIComponent(params.teamId ?? "");
  const channelId = decodeURIComponent(params.channelId ?? "");

  const [attachment, setAttachment] = useState<ChannelAttachment | null>(null);
  const [calls, setCalls] = useState<CallReadiness[]>([]);
  const [liveTail, setLiveTail] = useState<DebugTailResponse | null>(null);
  const [chatTail, setChatTail] = useState<DebugTailResponse | null>(null);
  const [officialEvents, setOfficialEvents] = useState<ChannelLedgerEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [joining, setJoining] = useState(false);
  const [joinMessage, setJoinMessage] = useState<string | null>(null);

  const sanitizedThreadId = useMemo(
    () => sanitizeForAuditDir(teamId, channelId),
    [teamId, channelId],
  );

  useEffect(() => {
    if (!teamId || !channelId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function tick() {
      try {
        const [a, h, t, c] = await Promise.all([
          bot.getChannel(teamId, channelId),
          bot.callingHealth().catch(() => ({ status: "down", calls: [] })),
          bot
            .tailDebug(sanitizedThreadId, "transcript", TAIL_LINES)
            .catch(() => null),
          bot
            .tailDebug(sanitizedThreadId, "chat", TAIL_LINES)
            .catch(() => null),
        ]);
        if (cancelled) return;
        setAttachment(a);
        setCalls(h.calls ?? []);
        setLiveTail(t);
        setChatTail(c);
        setError(null);
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load channel");
        }
      } finally {
        if (!cancelled) timer = setTimeout(tick, POLL_MS);
      }
    }
    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [teamId, channelId, sanitizedThreadId]);

  // Official transcripts poll less often.
  useEffect(() => {
    if (!teamId || !channelId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function tick() {
      try {
        const body = await sink.channelEvents(teamId, channelId, {
          kinds: "speech",
        });
        if (cancelled) return;
        setOfficialEvents(
          (body.events ?? []).filter((e) => e.source === "graph_notification"),
        );
      } catch {
        // sink may be down or no events yet; ignore
      } finally {
        if (!cancelled) timer = setTimeout(tick, OFFICIAL_POLL_MS);
      }
    }
    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [teamId, channelId]);

  if (!teamId || !channelId) {
    return <Navigate />;
  }

  async function joinNow() {
    setJoining(true);
    setJoinMessage(null);
    setError(null);
    try {
      const r = await bot.joinNow(teamId, channelId);
      setJoinMessage(
        r.deferred
          ? `Deferred (${r.join_mode}): ${r.message ?? "Waiting for Teams."}`
          : r.call_id
            ? `Joining. callId=${r.call_id}`
            : (r.message ?? "OK"),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Join failed");
    } finally {
      setJoining(false);
    }
  }

  async function toggleAutoJoin(next: boolean) {
    if (!attachment) return;
    setError(null);
    try {
      await bot.setAutoJoin(teamId, channelId, next);
      setAttachment({ ...attachment, auto_join_enabled: next });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Toggle failed");
    }
  }

  const matchingCall = calls.find(
    (c) => c.callId === attachment?.last_auto_join_attempt?.call_id,
  );

  return (
    <div className="flex h-screen flex-col bg-ink-950 text-ink-50">
      <header className="flex items-center gap-3 border-b border-ink-800 bg-ink-950/80 px-6 py-3 backdrop-blur">
        <button
          type="button"
          onClick={() => navigate("/channels")}
          className="flex h-9 w-9 items-center justify-center rounded-lg bg-ink-900 ring-1 ring-ink-800 hover:bg-ink-800"
          aria-label="Back to channels"
        >
          <ArrowLeft size={16} className="text-ink-300" />
        </button>
        <div className="flex flex-col leading-tight">
          <span className="font-serif text-lg font-medium text-ink-50">
            {attachment?.channel_display_name ?? (
              <span className="italic text-ink-400">Channel (name unknown)</span>
            )}
          </span>
          <span className="font-mono text-[10px] uppercase tracking-widest text-ink-400">
            {attachment?.team_display_name ?? "team (unknown)"} · channel command center
          </span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Link
            to="/channels"
            className="flex items-center gap-1 rounded-md border border-ink-700 bg-ink-900 px-3 py-1.5 text-xs text-ink-200 hover:bg-ink-800"
          >
            All channels
          </Link>
        </div>
      </header>

      <main className="flex-1 overflow-auto px-6 py-6">
        <div className="mx-auto max-w-6xl space-y-5">
          {error ? (
            <div className="rounded-md border border-crimson-500/40 bg-crimson-500/10 px-4 py-2 text-sm text-crimson-300">
              <AlertCircle size={12} className="mr-1 inline" />
              {error}
            </div>
          ) : null}

          <StatusPanel
            attachment={attachment}
            matchingCall={matchingCall}
            allCalls={calls}
            joining={joining}
            joinMessage={joinMessage}
            onJoinNow={joinNow}
            onToggleAutoJoin={(v) => void toggleAutoJoin(v)}
          />

          <ChatPanel tail={chatTail} />

          <LivePanel tail={liveTail} />

          <OfficialPanel events={officialEvents} />
        </div>
      </main>
    </div>
  );
}

function Navigate() {
  return (
    <div className="flex h-screen items-center justify-center bg-ink-950 text-ink-50">
      Missing team_id or channel_id in URL.
      <Link to="/channels" className="ml-2 text-gold-400 underline">
        Back to channels
      </Link>
    </div>
  );
}

function StatusPanel({
  attachment,
  matchingCall,
  allCalls,
  joining,
  joinMessage,
  onJoinNow,
  onToggleAutoJoin,
}: {
  attachment: ChannelAttachment | null;
  matchingCall: CallReadiness | undefined;
  allCalls: CallReadiness[];
  joining: boolean;
  joinMessage: string | null;
  onJoinNow: () => void;
  onToggleAutoJoin: (enabled: boolean) => void;
}) {
  const last = attachment?.last_auto_join_attempt ?? null;
  const subscriptionOk = Boolean(attachment?.subscription_id);
  const autoJoinOn = attachment?.auto_join_enabled !== false;

  return (
    <section className="rounded-lg border border-ink-800 bg-ink-900/40 px-5 py-4">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <Badge
          icon={<Power size={10} />}
          label={subscriptionOk ? "Subscription" : "No subscription"}
          tone={subscriptionOk ? "ok" : "warn"}
        />
        <Badge
          icon={<Radio size={10} />}
          label={autoJoinOn ? "Auto-join ON" : "Auto-join OFF"}
          tone={autoJoinOn ? "ok" : "muted"}
        />
        {matchingCall ? (
          <Badge
            icon={<Phone size={10} />}
            label={`Live call · readiness=${matchingCall.readiness ?? "?"}`}
            tone="live"
          />
        ) : (
          <Badge
            icon={<PhoneOff size={10} />}
            label={allCalls.length === 0 ? "No active calls" : `${allCalls.length} other call(s) on bot`}
            tone="muted"
          />
        )}
        <div className="ml-auto flex items-center gap-2">
          <label className="flex items-center gap-2 text-xs text-ink-200">
            <input
              type="checkbox"
              checked={autoJoinOn}
              onChange={(e) => onToggleAutoJoin(e.target.checked)}
            />
            Auto-join meetings
          </label>
          <button
            type="button"
            onClick={onJoinNow}
            disabled={joining}
            className="flex items-center gap-1 rounded-md bg-emerald-500/20 px-3 py-1.5 text-xs text-emerald-200 ring-1 ring-emerald-500/40 hover:bg-emerald-500/30 disabled:opacity-50"
          >
            <Phone size={12} />
            {joining ? "Joining…" : "Join now"}
          </button>
        </div>
      </div>

      {joinMessage ? (
        <div className="mt-3 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 font-mono text-[11px] text-emerald-200">
          {joinMessage}
        </div>
      ) : null}

      <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="rounded-md border border-ink-800 bg-ink-950/60 px-3 py-2 text-xs">
          <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-ink-500">
            Last join attempt
          </div>
          {last ? (
            <LastAttempt attempt={last} />
          ) : (
            <div className="italic text-ink-400">
              No join attempts yet. Waiting for "Meeting started" event.
            </div>
          )}
        </div>

        <div className="rounded-md border border-ink-800 bg-ink-950/60 px-3 py-2 text-xs">
          <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-ink-500">
            Active call (this channel)
          </div>
          {matchingCall ? (
            <ActiveCall call={matchingCall} />
          ) : (
            <div className="italic text-ink-400">
              No active call matching the last join attempt.
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function LastAttempt({ attempt }: { attempt: AutoJoinAttempt }) {
  const ts = fmtTs(attempt.ts);
  const statusTone =
    attempt.status === "success"
      ? "text-emerald-300"
      : attempt.status === "failure"
        ? "text-crimson-300"
        : "text-gold-300";
  return (
    <div className="space-y-1">
      <div>
        <span className={`font-mono ${statusTone}`}>
          {attempt.status.toUpperCase()}
        </span>
        <span className="ml-2 text-ink-300">
          via {attempt.trigger} · {ts}
        </span>
      </div>
      {attempt.call_id ? (
        <div className="font-mono text-[10px] text-ink-400">
          callId: {attempt.call_id}
        </div>
      ) : null}
      {attempt.error_code || attempt.error_message ? (
        <div className="font-mono text-[10px] text-crimson-300">
          {attempt.error_code ? `[${attempt.error_code}] ` : ""}
          {attempt.error_message ?? ""}
        </div>
      ) : null}
    </div>
  );
}

function ActiveCall({ call }: { call: CallReadiness }) {
  return (
    <div className="space-y-0.5">
      <div className="font-mono text-[10px] text-ink-400">
        callId: {call.callId}
      </div>
      <div>
        readiness:{" "}
        <span
          className={
            call.readiness === "ready"
              ? "text-emerald-300"
              : "text-gold-300"
          }
        >
          {call.readiness ?? "?"}
        </span>
      </div>
      <div className="text-ink-300">
        frames: {call.unmixed_audio_frames ?? 0} unmixed ·{" "}
        {call.primary_mixed_audio_frames ?? 0} mixed · peak{" "}
        {call.recent_peak_sample ?? 0}
      </div>
    </div>
  );
}

/**
 * True when this entry is a Teams meeting-lifecycle system payload that
 * was misrouted into chat.ndjson before the C# bot started classifying
 * them as `system.meeting_lifecycle`. Teams emits two shapes:
 *   • JSON: `{"scopeId":"...","callId":"..."}` (call started / ended /
 *     exported to ODSP)
 *   • XML: `<URIObject type="Video.2/CallRecording.1">...` (recording
 *     chunk / transcript ready notifications)
 * Both are noise in an operator's live-chat view; filter them out
 * defensively so historical NDJSON files don't pollute the panel.
 */
function isTeamsSystemPayload(entry: Record<string, unknown>): boolean {
  const payload = entry.payload as Record<string, unknown> | undefined;
  if (!payload) return false;
  const text = payload.text as string | undefined;
  if (!text) return false;
  const trimmed = text.trimStart();
  if (trimmed.startsWith("{")) {
    try {
      const parsed = JSON.parse(text);
      return (
        parsed !== null &&
        typeof parsed === "object" &&
        "scopeId" in parsed &&
        "callId" in parsed
      );
    } catch {
      return false;
    }
  }
  if (trimmed.startsWith("<URIObject")) {
    return (
      trimmed.includes('type="Video.2/CallRecording.1"') ||
      trimmed.includes("type='Video.2/CallRecording.1'")
    );
  }
  return false;
}

function ChatPanel({ tail }: { tail: DebugTailResponse | null }) {
  const entries = (tail?.entries ?? []).filter((e) => !isTeamsSystemPayload(e));
  // Most recent message first — the bot writes oldest-first; reverse here
  // so the panel reads like a chat scroll-back.
  const reversed = [...entries].reverse();
  return (
    <section className="rounded-lg border border-ink-800 bg-ink-900/40">
      <header className="flex items-center gap-2 border-b border-ink-800 px-5 py-2.5">
        <MessageSquare size={14} className="text-emerald-400" />
        <span className="font-serif text-sm text-ink-100">Live chat</span>
        <span className="font-mono text-[10px] uppercase tracking-wider text-ink-500">
          channel + meeting chat · {reversed.length} message{reversed.length === 1 ? "" : "s"} captured
        </span>
        <span className="ml-auto flex items-center gap-1 font-mono text-[10px] text-ink-500">
          <RefreshCw size={10} className="animate-spin-slow" />
          {POLL_MS / 1000}s
        </span>
      </header>
      <div className="max-h-96 overflow-auto px-5 py-3">
        {reversed.length === 0 ? (
          <div className="text-xs italic text-ink-400">
            No chat messages captured yet. Post in the channel or meeting
            chat — the bot mirrors every chat into the audit + the per-channel
            blob folder.
          </div>
        ) : (
          <ol className="space-y-1">
            {reversed.map((e, i) => (
              <li
                key={i}
                className="rounded border border-ink-800 bg-ink-950/60 px-3 py-1.5 font-mono text-[11px] leading-relaxed text-ink-100"
              >
                <ChatCue entry={e} />
              </li>
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}

function ChatCue({ entry }: { entry: Record<string, unknown> }) {
  const ts = (entry.ts as string | undefined) ?? "?";
  const payload = (entry.payload as Record<string, unknown> | undefined) ?? {};
  const sender = (payload.sender_display_name as string | undefined) ?? "—";
  const fromBot = (payload.from_bot as boolean | undefined) ?? false;
  const text = (payload.text as string | undefined) ?? "";
  const convKind = (payload.conversation_kind as string | undefined) ?? "";
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex items-center gap-2 text-[10px] text-ink-500">
        <span>{ts}</span>
        <span className={fromBot ? "text-gold-400" : "text-emerald-300"}>
          {sender}
          {fromBot ? " (bot)" : ""}
        </span>
        {convKind ? <span className="text-ink-500">{convKind}</span> : null}
      </div>
      <div className="text-ink-100">
        {text ? text : <em className="text-ink-500">(empty)</em>}
      </div>
    </div>
  );
}

function LivePanel({ tail }: { tail: DebugTailResponse | null }) {
  const entries = tail?.entries ?? [];
  return (
    <section className="rounded-lg border border-ink-800 bg-ink-900/40">
      <header className="flex items-center gap-2 border-b border-ink-800 px-5 py-2.5">
        <Mic size={14} className="text-gold-400" />
        <span className="font-serif text-sm text-ink-100">
          Live transcripts
        </span>
        <span className="font-mono text-[10px] uppercase tracking-wider text-ink-500">
          bot audit · last {entries.length} cues · auto-refresh
        </span>
        <span className="ml-auto flex items-center gap-1 font-mono text-[10px] text-ink-500">
          <RefreshCw size={10} className="animate-spin-slow" />
          {POLL_MS / 1000}s
        </span>
      </header>
      <div className="max-h-96 overflow-auto px-5 py-3">
        {entries.length === 0 ? (
          <div className="text-xs italic text-ink-400">
            Nothing yet. The bot writes a transcript file the first time STT
            fires for this channel — start a meeting and speak.
          </div>
        ) : (
          <ol className="space-y-1">
            {entries.map((e, i) => (
              <li
                key={i}
                className="rounded border border-ink-800 bg-ink-950/60 px-3 py-1.5 font-mono text-[11px] leading-relaxed text-ink-100"
              >
                <LiveCue entry={e} />
              </li>
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}

function LiveCue({ entry }: { entry: Record<string, unknown> }) {
  const ts = (entry.ts as string | undefined) ?? "?";
  const eventType = (entry.event_type as string | undefined) ?? "?";
  const payload = (entry.payload as Record<string, unknown> | undefined) ?? {};
  const text = (payload.text as string | undefined) ?? "";
  const speaker = (payload.speaker_id as string | undefined) ?? "—";
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex items-center gap-2 text-[10px] text-ink-500">
        <span>{ts}</span>
        <span
          className={
            eventType === "transcript.final" ? "text-gold-400" : "text-ink-400"
          }
        >
          {eventType}
        </span>
        <span>{speaker}</span>
      </div>
      <div className="text-ink-100">
        {text || <em className="text-ink-500">(empty)</em>}
      </div>
    </div>
  );
}

function OfficialPanel({ events }: { events: ChannelLedgerEvent[] }) {
  return (
    <section className="rounded-lg border border-ink-800 bg-ink-900/40">
      <header className="flex items-center gap-2 border-b border-ink-800 px-5 py-2.5">
        <FileText size={14} className="text-sky-400" />
        <span className="font-serif text-sm text-ink-100">
          Official transcripts
        </span>
        <span className="font-mono text-[10px] uppercase tracking-wider text-ink-500">
          Microsoft Teams · post-meeting · {events.length} cue{events.length === 1 ? "" : "s"}
        </span>
        <span className="ml-auto flex items-center gap-1 font-mono text-[10px] text-ink-500">
          refresh {OFFICIAL_POLL_MS / 1000}s
        </span>
      </header>
      <div className="max-h-96 overflow-auto px-5 py-3">
        {events.length === 0 ? (
          <div className="text-xs italic text-ink-400">
            None yet. Microsoft's transcript becomes available a minute or two
            after a recorded+transcribed meeting ends; the bot polls Graph
            and posts it here.
          </div>
        ) : (
          <ol className="space-y-1">
            {events.map((e) => (
              <li
                key={e.event_id}
                className="rounded border border-ink-800 bg-ink-950/60 px-3 py-1.5 font-mono text-[11px] leading-relaxed text-ink-100"
              >
                <div className="flex items-center gap-2 text-[10px] text-ink-500">
                  <span>{e.timestamp_utc}</span>
                  <span className="text-sky-400">{e.display_name ?? "?"}</span>
                </div>
                <div className="text-ink-100">{e.text}</div>
              </li>
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}

function Badge({
  icon,
  label,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  tone: "ok" | "warn" | "muted" | "live";
}) {
  const classes =
    tone === "ok"
      ? "bg-emerald-500/15 text-emerald-200 ring-emerald-500/30"
      : tone === "warn"
        ? "bg-gold-500/15 text-gold-200 ring-gold-500/30"
        : tone === "live"
          ? "bg-emerald-500/20 text-emerald-200 ring-emerald-500/50 animate-pulse"
          : "bg-ink-800/40 text-ink-300 ring-ink-700";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] ring-1 ${classes}`}
    >
      {icon}
      {label}
    </span>
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

/**
 * Mirror of MeetingAuditLogger.Sanitize on the bot — replaces Windows-
 * illegal path chars in the channel thread id so we can open the
 * matching audit directory.
 */
function sanitizeForAuditDir(teamId: string, channelId: string): string {
  // Must mirror EventFanoutDispatcher's audit key for channel events:
  //   auditKey = $"{TeamId}|{ChannelId}"
  // …then MeetingAuditLogger replaces Windows-invalid chars with `_`.
  // Windows invalid: < > : " / \ | ? *
  return `${teamId}|${channelId}`.replace(/[<>:"/\\|?*]/g, "_");
}
