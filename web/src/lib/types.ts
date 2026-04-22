/**
 * Alfred TypeScript domain model.
 *
 * Mirrors (and extends) the Python sink shapes in
 *   python/meeting_agent/models.py
 *   python/transcript_sink.py
 *
 * The "intent alignment" extension types (Decision, OpenQuestion, ActionItem,
 * Risk) describe what Alfred is expected to emit as its rolling extraction
 * state once the analyzer is extended. The UI renders them if present and
 * shows honest empty states if not — no mock fallbacks.
 */

export type EventKind = "speech" | "chat" | "system";
export type EventRole =
  | "bot"
  | "candidate"
  | "interviewer"
  | "participant"
  | "unknown";

export interface MeetingEvent {
  id?: string;
  kind: EventKind;
  role: EventRole;
  display_name?: string;
  speaker_id?: string;
  text: string;
  timestamp_utc: string;
  from_bot?: boolean;
}

export interface ChecklistItem {
  id: string;
  label?: string;
  status?: "pending" | "analyzing" | "complete";
}

export interface SessionSummary {
  active: boolean;
  session_id?: string;
  candidate_name?: string;
  meeting_url?: string;
  meeting_history?: MeetingEvent[];
  checklist?: ChecklistItem[];
  alfred_muted?: boolean;
}

export interface SessionStatusResponse {
  session?: SessionSummary;
}

// ---------------------------------------------------------------------------
// Intent-alignment extraction types
// ---------------------------------------------------------------------------

export type DecisionStatus = "tentative" | "committed" | "superseded";
export interface Decision {
  id: string;
  text: string;
  committed_by?: string[];
  confidence?: number;
  first_seen_at?: string;
  source_event_ids?: string[];
  status?: DecisionStatus;
}

export type QuestionStatus = "open" | "answered" | "deferred";
export interface OpenQuestion {
  id: string;
  text: string;
  raised_by?: string;
  answer?: string;
  confidence?: number;
  first_seen_at?: string;
  source_event_ids?: string[];
  status?: QuestionStatus;
}

export type ActionItemStatus = "proposed" | "owned" | "done";
export interface ActionItem {
  id: string;
  text: string;
  owner?: string;
  due?: string;
  confidence?: number;
  first_seen_at?: string;
  source_event_ids?: string[];
  status?: ActionItemStatus;
}

export type RiskSeverity = "low" | "medium" | "high";
export interface Risk {
  id: string;
  text: string;
  severity?: RiskSeverity;
  confidence?: number;
  first_seen_at?: string;
  source_event_ids?: string[];
}

export interface AlfredAnalysisBody {
  running_summary?: string;
  topics?: string[];
  decisions?: Decision[];
  open_questions?: OpenQuestion[];
  action_items?: ActionItem[];
  risks?: Risk[];
  // Legacy/compat fields still emitted by the current sink.
  analysis_items?: Array<{
    alfred_action?: {
      notes?: string[];
      action?: string;
      rationale?: string;
    };
    key_points?: string[];
  }>;
}

export interface SessionAnalysisResponse {
  analysis?: AlfredAnalysisBody;
}

// ---------------------------------------------------------------------------
// UI helper types
// ---------------------------------------------------------------------------

export type DossierKey = "decisions" | "questions" | "actions" | "risks";
