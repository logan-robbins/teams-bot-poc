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

export interface ChannelLedgerEvent {
  session_id?: string;
  event_id: string;
  kind: "speech" | "chat" | "system";
  source: string;
  timestamp_utc: string;
  text: string;
  speaker_id?: string | null;
  display_name?: string | null;
  message_id?: string | null;
  team_id?: string | null;
  channel_id?: string | null;
  channel_thread_id?: string | null;
}

export interface ChannelEventsResponse {
  events: ChannelLedgerEvent[];
}

// -- alfred-v2 (canonical) -----------------------------------------------

export interface V2Organizer {
  aad_id?: string | null;
  display_name?: string | null;
}

export interface V2ChannelLink {
  team_id: string;
  team_display_name?: string | null;
  channel_id: string;
  channel_display_name?: string | null;
  thread_id?: string | null;
  linked_at_utc?: string | null;
  linked_source?: string | null;
}

export interface V2Meeting {
  meeting_id: string;
  meeting_chat_thread_id?: string | null;
  subject?: string | null;
  organizer?: V2Organizer | null;
  scheduled_start_utc?: string | null;
  scheduled_end_utc?: string | null;
  actual_start_utc?: string | null;
  actual_end_utc?: string | null;
  channel_link?: V2ChannelLink | null;
  last_event_utc?: string | null;
  created_at_utc?: string | null;
  updated_at_utc?: string | null;
}

export interface V2MeetingsResponse {
  schema_version: string;
  count: number;
  meetings: V2Meeting[];
}

export interface V2MeetingTranscriptResponse {
  schema_version: string;
  meeting_id: string;
  official_transcript_txt_url: string;
  official_transcript_vtt_url: string;
  available: boolean;
  text?: string | null;
}

export interface V2MeetingEvent {
  event_id: string;
  kind: "speech" | "chat" | "system";
  source: string;
  timestamp_utc: string;
  text?: string | null;
  speaker_id?: string | null;
  display_name?: string | null;
  message_id?: string | null;
}

export interface V2MeetingEventsResponse {
  schema_version: string;
  meeting_id: string;
  count: number;
  events: V2MeetingEvent[];
}

export interface V2ChannelThread {
  thread_id: string;
  last_activity_utc?: string | null;
  message_count: number;
}

export interface V2ChannelResponse {
  schema_version: string;
  team_id: string;
  channel_id: string;
  thread_count: number;
  threads: V2ChannelThread[];
  meeting_count: number;
  meetings: V2Meeting[];
}

export interface V2IndexResponse {
  schema_version: string;
  blob_archive_url: string;
  blob_archive_index_meetings: string;
  blob_archive_meetings_prefix: string;
  blob_archive_channels_prefix: string;
  counts: { meetings: number; channels: number };
  recent_meetings: V2Meeting[];
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

  channelEvents: (
    teamId: string,
    channelId: string,
    opts: { since?: string; kinds?: string; limit?: number } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.since) params.set("since", opts.since);
    if (opts.kinds) params.set("kinds", opts.kinds);
    if (opts.limit !== undefined) params.set("limit", String(opts.limit));
    const qs = params.toString();
    const suffix = qs ? `?${qs}` : "";
    return json<ChannelEventsResponse>(
      `/c/${encodeURIComponent(teamId)}/${encodeURIComponent(channelId)}/events${suffix}`,
    );
  },

  // -- alfred-v2 endpoints (the canonical surface) ---------------------------
  v2Index: () => json<V2IndexResponse>("/v2/index"),

  v2ListMeetings: (opts: { limit?: number; teamId?: string; channelId?: string } = {}) => {
    const params = new URLSearchParams();
    if (opts.limit !== undefined) params.set("limit", String(opts.limit));
    if (opts.teamId) params.set("team_id", opts.teamId);
    if (opts.channelId) params.set("channel_id", opts.channelId);
    const qs = params.toString();
    return json<V2MeetingsResponse>(`/v2/meetings${qs ? `?${qs}` : ""}`);
  },

  v2GetMeeting: (meetingId: string) =>
    json<V2Meeting>(`/v2/meetings/${encodeURIComponent(meetingId)}`),

  v2GetMeetingEvents: (
    meetingId: string,
    opts: { kinds?: string; limit?: number } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.kinds) params.set("kinds", opts.kinds);
    if (opts.limit !== undefined) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return json<V2MeetingEventsResponse>(
      `/v2/meetings/${encodeURIComponent(meetingId)}/events${qs ? `?${qs}` : ""}`,
    );
  },

  v2GetMeetingTranscript: (meetingId: string) =>
    json<V2MeetingTranscriptResponse>(
      `/v2/meetings/${encodeURIComponent(meetingId)}/transcript`,
    ),

  v2GetChannel: (teamId: string, channelId: string, opts: { limit?: number } = {}) => {
    const params = new URLSearchParams();
    if (opts.limit !== undefined) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return json<V2ChannelResponse>(
      `/v2/teams/${encodeURIComponent(teamId)}/channels/${encodeURIComponent(channelId)}${qs ? `?${qs}` : ""}`,
    );
  },

  v2ResolveMeeting: (subject: string, limit = 25) => {
    const params = new URLSearchParams({
      kind: "meeting",
      subject,
      limit: String(limit),
    });
    return json<{ matches: V2Meeting[] }>(`/v2/resolve?${params.toString()}`);
  },

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
