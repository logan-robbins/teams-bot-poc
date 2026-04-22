import { useEffect } from "react";
import { normalizeDossierKind, useSessionStore } from "../stores/sessionStore";
import type { AlfredAnalysisBody, MeetingEvent } from "../lib/types";

/**
 * Live session stream.
 *
 *  1. Seeds the store from GET /session/status (one-shot snapshot)
 *  2. Opens EventSource at GET /session/events for push updates
 *  3. Reconnects automatically via the native EventSource
 *  4. Updates connection state so the UI can show a connection chip
 *
 * All data flows one-way: sink → EventSource → store → components.
 * Components never poll.
 */

const SINK_BASE = "/sink";

export function useSessionStream(): void {
  const seedFromStatus = useSessionStore((s) => s.seedFromStatus);
  const setConnection = useSessionStore((s) => s.setConnection);
  const appendLedger = useSessionStore((s) => s.appendLedger);
  const applyExtraction = useSessionStore((s) => s.applyExtraction);
  const upsertDossier = useSessionStore((s) => s.upsertDossier);
  const updateSessionState = useSessionStore((s) => s.updateSessionState);
  const markSessionStarted = useSessionStore((s) => s.markSessionStarted);
  const markSessionEnded = useSessionStore((s) => s.markSessionEnded);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const res = await fetch(`${SINK_BASE}/session/status`);
        if (!cancelled && res.ok) {
          const body = await res.json();
          seedFromStatus(body);
        }
      } catch {
        // Leave store empty; SSE will populate as events arrive.
      }
    })();

    setConnection({ mode: "connecting" });
    const source = new EventSource(`${SINK_BASE}/session/events`);

    source.addEventListener("open", () => setConnection({ mode: "open" }));
    source.addEventListener("error", () =>
      setConnection({ mode: "closed", lastError: "connection lost" }),
    );

    source.addEventListener("ledger_append", (e) => {
      const evt = parse<MeetingEvent>((e as MessageEvent).data);
      if (evt) appendLedger(evt);
    });
    source.addEventListener("extraction", (e) => {
      const body = parse<AlfredAnalysisBody>((e as MessageEvent).data);
      if (body) applyExtraction(body);
    });
    source.addEventListener("dossier_upsert", (e) => {
      const body = parse<{ kind: string; item: { id: string } }>((e as MessageEvent).data);
      if (!body) return;
      const storeKind = normalizeDossierKind(body.kind);
      if (!storeKind) return;
      upsertDossier(storeKind, body.item);
    });
    source.addEventListener("session_state", (e) => {
      const body = parse<{
        running_summary?: string;
        topics?: string[];
        alfred_muted?: boolean;
      }>((e as MessageEvent).data);
      if (body) updateSessionState(body);
    });
    source.addEventListener("session_started", (e) => {
      const body = parse<{
        session_id: string;
        candidate_name?: string;
        meeting_url?: string;
        started_at?: string;
      }>((e as MessageEvent).data);
      if (body) {
        markSessionStarted({
          session_id: body.session_id,
          candidate_name: body.candidate_name,
          meeting_url: body.meeting_url,
        });
      }
    });
    source.addEventListener("session_ended", () => markSessionEnded());

    return () => {
      cancelled = true;
      source.close();
      setConnection({ mode: "closed" });
    };
  }, [
    seedFromStatus,
    setConnection,
    appendLedger,
    applyExtraction,
    upsertDossier,
    updateSessionState,
    markSessionStarted,
    markSessionEnded,
  ]);
}

function parse<T>(raw: string): T | null {
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}
