import { Mic, MessageSquare, Sparkles } from "lucide-react";
import type { MeetingEvent } from "../lib/types";
import { clock } from "../lib/format";

interface Props {
  entry: MeetingEvent;
  isNew: boolean;
}

const roleLabel: Record<string, string> = {
  bot: "Alfred",
  candidate: "Subject",
  interviewer: "Host",
  participant: "Participant",
  unknown: "—",
};

export function LedgerEntry({ entry, isNew }: Props) {
  const isBot = !!entry.from_bot || entry.role === "bot";
  const isChat = entry.kind === "chat";
  const name = entry.display_name || entry.speaker_id || roleLabel[entry.role] || "—";

  const Icon = isBot ? Sparkles : isChat ? MessageSquare : Mic;

  const frameClass = isBot
    ? "border-gold-500/40 bg-gradient-to-br from-gold-500/[0.07] to-transparent"
    : isChat
    ? "border-ink-700 bg-ink-800/60"
    : "border-ink-700 bg-ink-900/50";

  const iconClass = isBot
    ? "text-gold-400"
    : isChat
    ? "text-azure-400"
    : "text-ink-300";

  return (
    <article
      className={`group rounded-xl border ${frameClass} px-4 py-3 transition hover:border-ink-600 ${
        isNew ? "ink-in" : ""
      }`}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className={`flex h-5 w-5 items-center justify-center ${iconClass}`}>
            <Icon size={13} />
          </span>
          <span
            className={`text-xs font-semibold ${
              isBot ? "font-serif text-gold-300" : "text-ink-100"
            }`}
          >
            {isBot ? "Alfred" : name}
          </span>
          {!isBot ? (
            <span className="rounded-full bg-ink-800 px-2 py-0.5 text-[10px] uppercase tracking-wider text-ink-400">
              {roleLabel[entry.role] ?? entry.role}
            </span>
          ) : null}
        </div>
        <span className="font-mono text-[10px] text-ink-500">{clock(entry.timestamp_utc)}</span>
      </div>
      <p
        className={`mt-1.5 whitespace-pre-wrap text-[13.5px] leading-relaxed ${
          isBot ? "font-serif text-ink-50" : "text-ink-100"
        }`}
      >
        {entry.text}
      </p>
    </article>
  );
}
