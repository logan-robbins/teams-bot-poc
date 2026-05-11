/**
 * HTTP client for the C# Alfred bot's operator API.
 *
 * Channel attachment + per-channel consumer registry live on the bot
 * (it owns the outbound EventFanoutDispatcher), distinct from the
 * Python reference sink. In dev the Vite dev server proxies /bot/* to
 * BOT_URL (see vite.config.ts); in prod the page is served from a
 * different origin than the bot, so we default to the bot's absolute
 * URL. Override at build time with VITE_BOT_URL if needed.
 */

const PROD_BOT_URL = "https://alfred-disney-bot.eastus.cloudapp.azure.com";
const BASE: string =
  (import.meta.env.VITE_BOT_URL as string | undefined) ??
  (import.meta.env.DEV ? "/bot" : PROD_BOT_URL);

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = "";
    try {
      detail = await res.text();
    } catch {
      // ignore
    }
    throw new Error(`${init?.method ?? "GET"} ${path} → ${res.status} ${detail}`);
  }
  return (await res.json()) as T;
}

export interface ChannelAttachment {
  team_id: string;
  channel_id: string;
  conversation_thread_id?: string | null;
  team_display_name?: string | null;
  channel_display_name?: string | null;
  source?: string | null;
  attached_at_utc?: string;
  subscription_id?: string | null;
  subscription_expires_at_utc?: string | null;
  auto_join_enabled?: boolean;
}

export interface JoinNowResult {
  ok: boolean;
  call_id?: string | null;
  join_mode?: string;
  deferred?: boolean;
  message?: string;
  join_url?: string;
}

export interface ChannelAttachmentsResponse {
  count: number;
  attachments: ChannelAttachment[];
}

export interface ConsumerConfig {
  name: string;
  url: string;
  event_kinds?: string[];
  headers?: Record<string, string>;
  enabled?: boolean;
}

export interface ConsumersResponse {
  team_id: string;
  channel_id: string;
  consumers: ConsumerConfig[];
}

function encodeChannelId(channelId: string): string {
  return encodeURIComponent(channelId);
}

export interface DebugThreadSummary {
  chat_thread_id: string;
  chat_thread_id_sanitized: string;
  transcript_lines: number;
  chat_lines: number;
  system_lines: number;
  last_modified_utc: string | null;
  first_final_text: string | null;
  last_final_text: string | null;
}

export interface DebugThreadsResponse {
  count: number;
  base_dir: string;
  now_utc: string;
  threads: DebugThreadSummary[];
}

export interface DebugTailResponse {
  chat_thread_id: string;
  chat_thread_id_sanitized: string;
  kind: "transcript" | "chat" | "system";
  count: number;
  entries: Array<Record<string, unknown>>;
}

export const bot = {
  listChannels: () => json<ChannelAttachmentsResponse>("/api/channels"),

  listConsumers: (teamId: string, channelId: string) =>
    json<ConsumersResponse>(
      `/api/channels/${encodeURIComponent(teamId)}/${encodeChannelId(channelId)}/consumers`,
    ),

  replaceConsumers: (
    teamId: string,
    channelId: string,
    consumers: ConsumerConfig[],
  ) =>
    json<{ ok: boolean; consumers: ConsumerConfig[] }>(
      `/api/channels/${encodeURIComponent(teamId)}/${encodeChannelId(channelId)}/consumers`,
      { method: "PUT", body: JSON.stringify({ consumers }) },
    ),

  upsertConsumer: (
    teamId: string,
    channelId: string,
    consumer: ConsumerConfig,
  ) =>
    json<{ ok: boolean; consumers: ConsumerConfig[] }>(
      `/api/channels/${encodeURIComponent(teamId)}/${encodeChannelId(channelId)}/consumers`,
      { method: "POST", body: JSON.stringify(consumer) },
    ),

  removeConsumer: (teamId: string, channelId: string, name: string) =>
    json<{ ok: boolean }>(
      `/api/channels/${encodeURIComponent(teamId)}/${encodeChannelId(channelId)}/consumers/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),

  setAutoJoin: (teamId: string, channelId: string, enabled: boolean) =>
    json<{ ok: boolean; auto_join_enabled: boolean }>(
      `/api/channels/${encodeURIComponent(teamId)}/${encodeChannelId(channelId)}/auto-join`,
      { method: "PATCH", body: JSON.stringify({ enabled }) },
    ),

  joinNow: (teamId: string, channelId: string) =>
    json<JoinNowResult>(
      `/api/channels/${encodeURIComponent(teamId)}/${encodeChannelId(channelId)}/join`,
      { method: "POST", body: "{}" },
    ),

  listDebugThreads: () => json<DebugThreadsResponse>("/api/debug/transcripts"),

  tailDebug: (
    sanitizedChatThreadId: string,
    kind: "transcript" | "chat" | "system" = "transcript",
    tail = 100,
  ) =>
    json<DebugTailResponse>(
      `/api/debug/transcripts/${encodeURIComponent(sanitizedChatThreadId)}?kind=${kind}&tail=${tail}`,
    ),
};
