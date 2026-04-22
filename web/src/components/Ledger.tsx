import { useEffect, useRef, useState } from "react";
import { ScrollText } from "lucide-react";
import { LedgerEntry } from "./LedgerEntry";
import type { MeetingEvent } from "../lib/types";

interface Props {
  history: MeetingEvent[];
}

/**
 * The Ledger — canonical append-only meeting timeline.
 *
 * Mirrors `InterviewSession.meeting_events` from the Python sink.
 * New entries fade-in via the ink-in animation when they arrive; we track
 * previously-seen keys in a ref so only genuinely new entries animate.
 */
export function Ledger({ history }: Props) {
  const seenRef = useRef<Set<string>>(new Set());
  const [, forceRender] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const next = new Set(seenRef.current);
    let added = false;
    for (const e of history) {
      const k = keyFor(e);
      if (!next.has(k)) {
        next.add(k);
        added = true;
      }
    }
    seenRef.current = next;
    if (added) forceRender((n) => n + 1);
  }, [history]);

  // Auto-scroll to bottom when new entries arrive — but only if the user is
  // already near the bottom. Honors the "butler does not interrupt" principle.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom < 120) {
      el.scrollTop = el.scrollHeight;
    }
  }, [history]);

  return (
    <section className="flex h-full flex-col">
      <SectionHeader
        icon={<ScrollText size={14} />}
        title="The Ledger"
        subtitle="Live meeting record"
        count={history.length}
      />

      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-4 pb-4 pt-2 space-y-2.5"
      >
        {history.length === 0 ? (
          <EmptyState />
        ) : (
          history.map((e, i) => {
            const k = keyFor(e, i);
            const isNew = seenRef.current.has(k);
            return <LedgerEntry key={k} entry={e} isNew={isNew} />;
          })
        )}
      </div>
    </section>
  );
}

function keyFor(e: MeetingEvent, idx?: number): string {
  return e.id ?? `${e.timestamp_utc}::${e.speaker_id ?? ""}::${idx ?? 0}::${e.text.slice(0, 40)}`;
}

function SectionHeader({
  icon,
  title,
  subtitle,
  count,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  count?: number;
}) {
  return (
    <div className="flex items-center justify-between border-b border-ink-800 px-4 py-3">
      <div className="flex items-center gap-2">
        <span className="text-ink-400">{icon}</span>
        <span className="font-serif text-sm font-medium text-ink-50">{title}</span>
        <span className="font-mono text-[10px] uppercase tracking-wider text-ink-500">
          {subtitle}
        </span>
      </div>
      {typeof count === "number" ? (
        <span className="font-mono text-[11px] text-ink-500">
          {count.toString().padStart(3, "0")}
        </span>
      ) : null}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex h-full min-h-[200px] flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-ink-600 p-8 text-center">
      <p className="font-serif text-base italic text-ink-200">
        The meeting has not yet begun.
      </p>
      <p className="max-w-xs text-xs text-ink-400">
        Once speech or chat arrives, I shall record each turn faithfully in this
        ledger, sir.
      </p>
    </div>
  );
}
