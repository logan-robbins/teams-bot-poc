/**
 * HTTP client for the C# Alfred bot's operator API.
 *
 * Channel attachment + per-channel consumer registry live on the bot
 * (it owns the outbound EventFanoutDispatcher), distinct from the
 * Python reference sink. The dev server proxies /bot/* to BOT_URL
 * (see vite.config.ts); set VITE_BOT_URL in production so calls hit
 * the bot's absolute URL directly.
 */

const BASE: string = (import.meta.env.VITE_BOT_URL as string | undefined) ?? "/bot";

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
};
