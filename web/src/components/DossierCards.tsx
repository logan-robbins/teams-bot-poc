import { CheckCircle2, HelpCircle, Zap, AlertTriangle } from "lucide-react";
import type {
  Decision,
  OpenQuestion,
  ActionItem,
  Risk,
} from "../lib/types";
import { relativeMinutes } from "../lib/format";

/*
 * Four card types, one visual rhythm:
 *   - left color rule (channel identity)
 *   - icon + text (the finding)
 *   - metadata row (owner / severity / source / age)
 *
 * Each card supports a one-shot "dossier-new" glow applied by the parent
 * section the first time it appears.
 */

interface BaseProps {
  isNew?: boolean;
}

function CardShell({
  children,
  rail,
  isNew,
}: {
  children: React.ReactNode;
  rail: string;
  isNew?: boolean;
}) {
  return (
    <article
      className={`relative overflow-hidden rounded-xl border border-ink-700 bg-ink-900/70 px-4 py-3 transition hover:border-ink-600 ${
        isNew ? "dossier-new" : ""
      }`}
    >
      <span className={`absolute left-0 top-0 h-full w-0.5 ${rail}`} />
      {children}
    </article>
  );
}

function Meta({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-400">
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------

export function DecisionCardView({
  decision,
  isNew,
}: BaseProps & { decision: Decision }) {
  const committedBy = decision.committed_by?.join(", ");
  return (
    <CardShell rail="bg-emerald-500" isNew={isNew}>
      <div className="flex items-start gap-2">
        <CheckCircle2 size={15} className="mt-[2px] shrink-0 text-emerald-400" />
        <p className="font-serif text-[14.5px] leading-snug text-ink-50">
          {decision.text}
        </p>
      </div>
      <Meta>
        {decision.status ? (
          <Badge tone="emerald">{decision.status}</Badge>
        ) : null}
        {committedBy ? <span>by {committedBy}</span> : null}
        {decision.first_seen_at ? (
          <span className="font-mono">{relativeMinutes(decision.first_seen_at)}</span>
        ) : null}
      </Meta>
    </CardShell>
  );
}

export function QuestionCardView({
  question,
  isNew,
}: BaseProps & { question: OpenQuestion }) {
  return (
    <CardShell rail="bg-amber-500" isNew={isNew}>
      <div className="flex items-start gap-2">
        <HelpCircle size={15} className="mt-[2px] shrink-0 text-amber-400" />
        <p className="font-serif text-[14.5px] leading-snug text-ink-50">
          {question.text}
        </p>
      </div>
      {question.answer ? (
        <p className="mt-1.5 rounded-md bg-ink-800/60 px-3 py-2 text-[12.5px] italic text-ink-200">
          → {question.answer}
        </p>
      ) : null}
      <Meta>
        {question.status ? <Badge tone="amber">{question.status}</Badge> : null}
        {question.raised_by ? <span>raised by {question.raised_by}</span> : null}
        {question.first_seen_at ? (
          <span className="font-mono">{relativeMinutes(question.first_seen_at)}</span>
        ) : null}
      </Meta>
    </CardShell>
  );
}

export function ActionItemCardView({
  item,
  isNew,
}: BaseProps & { item: ActionItem }) {
  return (
    <CardShell rail="bg-azure-500" isNew={isNew}>
      <div className="flex items-start gap-2">
        <Zap size={15} className="mt-[2px] shrink-0 text-azure-400" />
        <p className="font-serif text-[14.5px] leading-snug text-ink-50">
          {item.text}
        </p>
      </div>
      <Meta>
        {item.status ? <Badge tone="azure">{item.status}</Badge> : null}
        {item.owner ? (
          <span>
            owner <span className="text-ink-200">{item.owner}</span>
          </span>
        ) : (
          <span className="italic text-ink-500">owner unassigned</span>
        )}
        {item.due ? <span>due {item.due}</span> : null}
        {item.first_seen_at ? (
          <span className="font-mono">{relativeMinutes(item.first_seen_at)}</span>
        ) : null}
      </Meta>
    </CardShell>
  );
}

export function RiskCardView({
  risk,
  isNew,
}: BaseProps & { risk: Risk }) {
  return (
    <CardShell rail="bg-crimson-500" isNew={isNew}>
      <div className="flex items-start gap-2">
        <AlertTriangle size={15} className="mt-[2px] shrink-0 text-crimson-400" />
        <p className="font-serif text-[14.5px] leading-snug text-ink-50">
          {risk.text}
        </p>
      </div>
      <Meta>
        {risk.severity ? <Badge tone="crimson">{risk.severity}</Badge> : null}
        {risk.first_seen_at ? (
          <span className="font-mono">{relativeMinutes(risk.first_seen_at)}</span>
        ) : null}
      </Meta>
    </CardShell>
  );
}

function Badge({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone: "emerald" | "amber" | "azure" | "crimson";
}) {
  const toneClass = {
    emerald: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
    amber: "border-amber-500/30 bg-amber-500/10 text-amber-300",
    azure: "border-azure-500/30 bg-azure-500/10 text-azure-300",
    crimson: "border-crimson-500/30 bg-crimson-500/10 text-crimson-300",
  }[tone];
  return (
    <span
      className={`rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider ${toneClass}`}
    >
      {children}
    </span>
  );
}
