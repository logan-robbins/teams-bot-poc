interface Props {
  active: boolean;
  muted: boolean;
}

/**
 * Listening / Muted / Idle indicator.
 * Gold pulsing dot when Alfred is listening — he does not announce his attention,
 * merely signals it.
 */
export function StatusBadge({ active, muted }: Props) {
  const state = !active ? "idle" : muted ? "muted" : "listening";

  const label = {
    listening: "Listening",
    muted: "Muted",
    idle: "Idle",
  }[state];

  const dotClass = {
    listening: "bg-gold-500 pulse-gold",
    muted: "bg-crimson-500",
    idle: "bg-ink-500",
  }[state];

  const textClass = {
    listening: "text-gold-400",
    muted: "text-crimson-400",
    idle: "text-ink-400",
  }[state];

  return (
    <div className="flex items-center gap-2 rounded-full border border-ink-700 bg-ink-900/60 px-3 py-1.5 backdrop-blur">
      <span className={`h-2 w-2 rounded-full ${dotClass}`} />
      <span className={`text-xs font-medium uppercase tracking-wider ${textClass}`}>
        {label}
      </span>
    </div>
  );
}
