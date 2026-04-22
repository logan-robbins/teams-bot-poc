/**
 * HTTP client for the Python FastAPI transcript sink.
 *
 * Every call goes through the Vite dev proxy at /sink/* (see vite.config.ts).
 * This keeps the browser on a single origin in dev and lets us swap the real
 * sink URL via the SINK_URL env var without changing client code.
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

export const sink = {
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
