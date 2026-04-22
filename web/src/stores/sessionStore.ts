import { create } from "zustand";
import type {
  ActionItem,
  AlfredAnalysisBody,
  Decision,
  MeetingEvent,
  OpenQuestion,
  Risk,
  SessionStatusResponse,
  SessionSummary,
} from "../lib/types";

/**
 * Central session store.
 *
 * Seeded by a one-shot GET /session/status on mount, then kept live by the
 * SSE stream at GET /session/events. The store is the single source of
 * truth the UI reads from — components no longer own their own polling.
 */

type DossierKey = "decisions" | "open_questions" | "action_items" | "risks";

interface ConnectionState {
  mode: "idle" | "connecting" | "open" | "closed";
  lastError?: string;
}

interface State {
  session?: SessionSummary;
  analysis?: AlfredAnalysisBody;
  connection: ConnectionState;

  // -- mutators -----------------------------------------------------------
  seedFromStatus: (status: SessionStatusResponse) => void;
  setConnection: (c: ConnectionState) => void;

  appendLedger: (event: MeetingEvent) => void;
  applyExtraction: (extraction: AlfredAnalysisBody) => void;
  upsertDossier: (kind: DossierKey, item: unknown) => void;
  updateSessionState: (patch: {
    running_summary?: string;
    topics?: string[];
    alfred_muted?: boolean;
  }) => void;
  markSessionStarted: (patch: Partial<SessionSummary>) => void;
  markSessionEnded: () => void;
}

const emptyAnalysis: AlfredAnalysisBody = {
  running_summary: "",
  topics: [],
  decisions: [],
  open_questions: [],
  action_items: [],
  risks: [],
};

function upsertById<T extends { id: string }>(list: T[], item: T): T[] {
  const next = list.slice();
  const idx = next.findIndex((existing) => existing.id === item.id);
  if (idx >= 0) {
    next[idx] = { ...next[idx], ...item };
  } else {
    next.push(item);
  }
  return next;
}

export const useSessionStore = create<State>((set) => ({
  session: undefined,
  analysis: undefined,
  connection: { mode: "idle" },

  seedFromStatus: (status) => {
    const session = status.session;
    if (!session) {
      set({ session: undefined, analysis: emptyAnalysis });
      return;
    }
    const analysisBody: AlfredAnalysisBody = {
      running_summary: session.running_summary ?? "",
      topics: session.topics ?? [],
      decisions: session.decisions ?? [],
      open_questions: session.open_questions ?? [],
      action_items: session.action_items ?? [],
      risks: session.risks ?? [],
    };
    set({ session, analysis: analysisBody });
  },

  setConnection: (connection) => set({ connection }),

  appendLedger: (event) =>
    set((s) => {
      if (!s.session) return {};
      const history = s.session.meeting_history ?? [];
      // Dedup by event_id-ish key (the ledger upstream can re-send on reconnect).
      const key = eventKey(event);
      if (history.some((e) => eventKey(e) === key)) return {};
      return {
        session: { ...s.session, meeting_history: [...history, event] },
      };
    }),

  applyExtraction: (extraction) =>
    set((s) => {
      const prev = s.analysis ?? emptyAnalysis;
      // running_summary / topics replace; lists upsert by id.
      let decisions = prev.decisions ?? [];
      for (const d of extraction.decisions ?? []) decisions = upsertById(decisions, d as Decision);
      let questions = prev.open_questions ?? [];
      for (const q of extraction.open_questions ?? [])
        questions = upsertById(questions, q as OpenQuestion);
      let actions = prev.action_items ?? [];
      for (const a of extraction.action_items ?? [])
        actions = upsertById(actions, a as ActionItem);
      let risks = prev.risks ?? [];
      for (const r of extraction.risks ?? []) risks = upsertById(risks, r as Risk);
      return {
        analysis: {
          ...prev,
          running_summary: extraction.running_summary ?? prev.running_summary,
          topics: extraction.topics ?? prev.topics,
          decisions,
          open_questions: questions,
          action_items: actions,
          risks,
        },
      };
    }),

  upsertDossier: (kind, item) =>
    set((s) => {
      const prev = s.analysis ?? emptyAnalysis;
      const typed = item as { id: string };
      if (!typed?.id) return {};
      switch (kind) {
        case "decisions":
          return {
            analysis: { ...prev, decisions: upsertById(prev.decisions ?? [], typed as Decision) },
          };
        case "open_questions":
          return {
            analysis: {
              ...prev,
              open_questions: upsertById(prev.open_questions ?? [], typed as OpenQuestion),
            },
          };
        case "action_items":
          return {
            analysis: {
              ...prev,
              action_items: upsertById(prev.action_items ?? [], typed as ActionItem),
            },
          };
        case "risks":
          return { analysis: { ...prev, risks: upsertById(prev.risks ?? [], typed as Risk) } };
      }
    }),

  updateSessionState: (patch) =>
    set((s) => {
      const prev = s.analysis ?? emptyAnalysis;
      return {
        analysis: {
          ...prev,
          running_summary: patch.running_summary ?? prev.running_summary,
          topics: patch.topics ?? prev.topics,
        },
        session: s.session
          ? {
              ...s.session,
              alfred_muted: patch.alfred_muted ?? s.session.alfred_muted,
            }
          : s.session,
      };
    }),

  markSessionStarted: (patch) =>
    set((s) => {
      const base: SessionSummary = s.session ?? {
        active: true,
        meeting_history: [],
      };
      return {
        session: { ...base, ...patch, active: true, meeting_history: [] },
        analysis: emptyAnalysis,
      };
    }),

  markSessionEnded: () =>
    set((s) => ({
      session: s.session ? { ...s.session, active: false } : s.session,
    })),
}));

function eventKey(event: MeetingEvent): string {
  return (
    event.id ??
    `${event.timestamp_utc}::${event.kind}::${event.speaker_id ?? ""}::${(event.text ?? "").slice(0, 40)}`
  );
}

// Dossier upsert key mapping between SSE server-side `kind` and store field.
const DOSSIER_KIND_MAP: Record<string, DossierKey> = {
  decision: "decisions",
  open_question: "open_questions",
  action_item: "action_items",
  risk: "risks",
};

export function normalizeDossierKind(k: string): DossierKey | null {
  return DOSSIER_KIND_MAP[k] ?? null;
}
