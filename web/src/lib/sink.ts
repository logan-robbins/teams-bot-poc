/**
 * HTTP client for the Python FastAPI transcript sink.
 *
 * Every call goes through the Vite dev proxy at /sink/* (see vite.config.ts).
 * This keeps the browser on a single origin in dev and lets us swap the real
 * sink URL via the SINK_URL env var without changing client code.
 *
 * The UI is keyed on ``chat_thread_id`` (the meeting id pulled from the
 * URL). Every interactive call goes through the per-meeting routes so the
 * client never sees data from another meeting.
 */

import type {
  SessionStatusResponse,
  SessionAnalysisResponse,
} from "./types";

const BASE = "/sink";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    throw new Error(`${init?.method ?? "GET"} ${path} → ${res.status}`);
  }
  return (await res.json()) as T;
}

export interface MeetingListEntry {
  chat_thread_id: string;
  session_id?: string | null;
  candidate_name?: string | null;
  meeting_url?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  active: boolean;
  total_events: number;
}

export interface MeetingListResponse {
  meetings: MeetingListEntry[];
}

export const sink = {
  // -- Per-meeting (URL-keyed) ------------------------------------------------
  meetingStatus: (chatThreadId: string) =>
    json<SessionStatusResponse>(`/m/${encodeURIComponent(chatThreadId)}/status`),

  endMeeting: (chatThreadId: string) =>
    json<unknown>(`/m/${encodeURIComponent(chatThreadId)}/end`, {
      method: "POST",
      body: "{}",
    }),

  setMuted: (chatThreadId: string, muted: boolean) =>
    json<{ alfred_muted: boolean }>(`/m/${encodeURIComponent(chatThreadId)}/mute`, {
      method: "POST",
      body: JSON.stringify({ muted }),
    }),

  listMeetings: () => json<MeetingListResponse>("/m"),

  // -- Legacy (single-session) endpoints kept for diagnostics + tooling ------
  status: () => json<SessionStatusResponse>("/session/status"),
  analysis: () => json<SessionAnalysisResponse>("/session/analysis"),

  startSession: (body: { candidate_name: string; meeting_url: string; product_id?: string }) =>
    json<unknown>("/session/start", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  endSession: () =>
    json<unknown>("/session/end", { method: "POST", body: "{}" }),
};
