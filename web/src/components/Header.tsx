import { Moon } from "lucide-react";
import { StatusBadge } from "./StatusBadge";
import type { SessionSummary } from "../lib/types";

interface Props {
  session?: SessionSummary;
  muted: boolean;
  chatThreadId?: string;
  onEnd: () => void;
}

export function Header({ session, muted, chatThreadId, onEnd }: Props) {
  const active = !!session?.active;
  const label = session?.candidate_name || "Awaiting instruction";
  // Show the URL-bound chat_thread_id so operators can confirm at a
  // glance which meeting the dossier is wired to.
  const threadLabel = chatThreadId || session?.meeting_url || "";

  return (
    <header className="flex items-center justify-between border-b border-ink-800 bg-ink-950/80 px-6 py-3 backdrop-blur">
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-gold-500/20 to-gold-500/5 ring-1 ring-gold-500/30">
          <Moon size={18} className="text-gold-400" />
        </div>
        <div className="flex flex-col leading-tight">
          <span className="font-serif text-lg font-medium text-ink-50">Alfred</span>
          <span className="font-mono text-[10px] uppercase tracking-widest text-ink-400">
            Meeting Dossier
          </span>
        </div>
        <div className="mx-4 h-8 w-px bg-ink-700" />
        <div className="flex flex-col leading-tight">
          <span className="text-sm font-medium text-ink-100">{label}</span>
          {threadLabel ? (
            <span
              className="font-mono text-[10px] text-ink-400 truncate max-w-[360px]"
              title={threadLabel}
            >
              {threadLabel}
            </span>
          ) : (
            <span className="text-[11px] italic text-ink-300">
              No meeting in session, sir.
            </span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3">
        <StatusBadge active={active} muted={muted} />
        {active ? (
          <button
            type="button"
            onClick={onEnd}
            className="rounded-md border border-ink-700 bg-ink-900 px-3 py-1.5 text-xs font-medium text-ink-200 transition hover:border-crimson-500/50 hover:text-crimson-400"
          >
            End session
          </button>
        ) : null}
      </div>
    </header>
  );
}
