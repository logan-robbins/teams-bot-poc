import { useEffect, useRef, useState } from "react";
import {
  BookOpen,
  CheckCircle2,
  HelpCircle,
  Zap,
  AlertTriangle,
} from "lucide-react";
import type { AlfredAnalysisBody } from "../lib/types";
import {
  DecisionCardView,
  QuestionCardView,
  ActionItemCardView,
  RiskCardView,
} from "./DossierCards";

interface Props {
  analysis?: AlfredAnalysisBody;
}

/**
 * The Dossier — Alfred's extracted intelligence.
 *
 * Four channels map to the intent-alignment model:
 *   🗝️ Decisions    — emerald
 *   ❓ Questions    — amber
 *   ⚡ Action items — azure
 *   🎯 Risks        — crimson
 *
 * Each section renders its cards, an honest empty state when absent, and a
 * count badge. New items glow softly on first appearance via a seen-set ref.
 */
export function Dossier({ analysis }: Props) {
  const decisions = analysis?.decisions ?? [];
  const questions = analysis?.open_questions ?? [];
  const actions = analysis?.action_items ?? [];
  const risks = analysis?.risks ?? [];

  const seenRef = useRef<Set<string>>(new Set());
  const [, tick] = useState(0);

  useEffect(() => {
    const next = new Set(seenRef.current);
    let added = false;
    for (const d of decisions) if (!next.has(`d:${d.id}`)) { next.add(`d:${d.id}`); added = true; }
    for (const q of questions) if (!next.has(`q:${q.id}`)) { next.add(`q:${q.id}`); added = true; }
    for (const a of actions) if (!next.has(`a:${a.id}`)) { next.add(`a:${a.id}`); added = true; }
    for (const r of risks) if (!next.has(`r:${r.id}`)) { next.add(`r:${r.id}`); added = true; }
    seenRef.current = next;
    if (added) tick((n) => n + 1);
  }, [decisions, questions, actions, risks]);

  const wasSeen = (k: string) => seenRef.current.has(k);

  return (
    <section className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-ink-800 px-5 py-3">
        <div className="flex items-center gap-2">
          <BookOpen size={14} className="text-gold-400" />
          <span className="font-serif text-sm font-medium text-ink-50">The Dossier</span>
          <span className="font-mono text-[10px] uppercase tracking-wider text-ink-500">
            Intent alignment
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px]">
          <MiniStat tone="emerald" count={decisions.length} />
          <MiniStat tone="amber" count={questions.length} />
          <MiniStat tone="azure" count={actions.length} />
          <MiniStat tone="crimson" count={risks.length} />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-6">
        <Section
          title="Decisions"
          subtitle="What we've committed to"
          tone="emerald"
          icon={<CheckCircle2 size={13} />}
          empty="No decisions recorded yet. I'll note them as they are made, sir."
          count={decisions.length}
        >
          {decisions.map((d) => (
            <DecisionCardView
              key={`d:${d.id}`}
              decision={d}
              isNew={wasSeen(`d:${d.id}`)}
            />
          ))}
        </Section>

        <Section
          title="Open questions"
          subtitle="What remains unresolved"
          tone="amber"
          icon={<HelpCircle size={13} />}
          empty="No open questions at present."
          count={questions.length}
        >
          {questions.map((q) => (
            <QuestionCardView
              key={`q:${q.id}`}
              question={q}
              isNew={wasSeen(`q:${q.id}`)}
            />
          ))}
        </Section>

        <Section
          title="Action items"
          subtitle="Who does what, by when"
          tone="azure"
          icon={<Zap size={13} />}
          empty="No actions assigned. I shall surface commitments as they surface."
          count={actions.length}
        >
          {actions.map((a) => (
            <ActionItemCardView
              key={`a:${a.id}`}
              item={a}
              isNew={wasSeen(`a:${a.id}`)}
            />
          ))}
        </Section>

        <Section
          title="Risks"
          subtitle="What could go wrong"
          tone="crimson"
          icon={<AlertTriangle size={13} />}
          empty="No risks flagged."
          count={risks.length}
        >
          {risks.map((r) => (
            <RiskCardView key={`r:${r.id}`} risk={r} isNew={wasSeen(`r:${r.id}`)} />
          ))}
        </Section>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------

function Section({
  title,
  subtitle,
  tone,
  icon,
  count,
  empty,
  children,
}: {
  title: string;
  subtitle: string;
  tone: "emerald" | "amber" | "azure" | "crimson";
  icon: React.ReactNode;
  count: number;
  empty: string;
  children: React.ReactNode;
}) {
  const toneText = {
    emerald: "text-emerald-400",
    amber: "text-amber-400",
    azure: "text-azure-400",
    crimson: "text-crimson-400",
  }[tone];

  return (
    <div>
      <div className="mb-2.5 flex items-baseline justify-between">
        <div className="flex items-center gap-2">
          <span className={toneText}>{icon}</span>
          <h3 className="font-serif text-[15px] font-medium text-ink-50">{title}</h3>
          <span className="font-mono text-[10px] uppercase tracking-wider text-ink-500">
            {subtitle}
          </span>
        </div>
        <span className="font-mono text-[11px] text-ink-500">
          {count.toString().padStart(2, "0")}
        </span>
      </div>
      {count === 0 ? (
        <div className="rounded-lg border border-dashed border-ink-600 bg-ink-900/60 px-4 py-3 text-[13px] italic text-ink-300">
          {empty}
        </div>
      ) : (
        <div className="space-y-2">{children}</div>
      )}
    </div>
  );
}

function MiniStat({
  tone,
  count,
}: {
  tone: "emerald" | "amber" | "azure" | "crimson";
  count: number;
}) {
  const toneClass = {
    emerald: "text-emerald-400",
    amber: "text-amber-400",
    azure: "text-azure-400",
    crimson: "text-crimson-400",
  }[tone];
  return (
    <div className="flex items-center gap-1">
      <span className={`h-1.5 w-1.5 rounded-full ${
        tone === "emerald" ? "bg-emerald-500"
        : tone === "amber" ? "bg-amber-500"
        : tone === "azure" ? "bg-azure-500"
        : "bg-crimson-500"}`} />
      <span className={`font-mono ${toneClass}`}>
        {count.toString().padStart(2, "0")}
      </span>
    </div>
  );
}
