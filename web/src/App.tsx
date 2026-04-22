import { useState } from "react";
import { Header } from "./components/Header";
import { Ledger } from "./components/Ledger";
import { Dossier } from "./components/Dossier";
import { CompanionRail } from "./components/CompanionRail";
import { sink } from "./lib/sink";
import { useSessionStore } from "./stores/sessionStore";
import { useSessionStream } from "./hooks/useSessionStream";

/**
 * Alfred — Meeting Dossier.
 *
 * Three-column console, driven end-to-end by SSE:
 *   Left    The Ledger       live append-only meeting record
 *   Center  The Dossier      intent-alignment extraction (hero)
 *   Right   Companion Rail   summary, topics, controls
 */
export default function App() {
  useSessionStream();

  const session = useSessionStore((s) => s.session);
  const analysis = useSessionStore((s) => s.analysis);
  const connection = useSessionStore((s) => s.connection);
  const [muted, setMuted] = useState(false);

  async function endSession() {
    await sink.endSession();
    // Store will pick up the session_ended SSE event.
  }

  const history = session?.meeting_history ?? [];

  return (
    <div className="flex h-screen flex-col bg-ink-950 text-ink-50">
      <Header session={session} muted={muted} onEnd={endSession} />

      <main
        className="grid flex-1 min-h-0 overflow-hidden"
        style={{
          gridTemplateColumns:
            "minmax(320px, 1fr) minmax(420px, 1.25fr) minmax(280px, 0.75fr)",
        }}
      >
        <div className="min-w-0 border-r border-ink-800">
          <Ledger history={history} />
        </div>
        <div className="min-w-0 border-r border-ink-800">
          <Dossier analysis={analysis} />
        </div>
        <CompanionRail
          session={session}
          analysis={analysis}
          muted={muted}
          setMuted={setMuted}
          onAfterMutation={noop}
        />
      </main>

      <footer className="flex items-center justify-between border-t border-ink-800 bg-ink-950 px-6 py-1.5 font-mono text-[10px] uppercase tracking-widest text-ink-500">
        <span>
          Alfred · meeting dossier ·{" "}
          {session?.active ? "in session" : "standing by"}
        </span>
        <span>
          /sink · sse{" "}
          <ConnectionDot mode={connection.mode} />
        </span>
      </footer>
    </div>
  );
}

function ConnectionDot({ mode }: { mode: string }) {
  const color =
    mode === "open"
      ? "bg-emerald-500"
      : mode === "connecting"
      ? "bg-amber-500"
      : mode === "closed"
      ? "bg-crimson-500"
      : "bg-ink-500";
  return (
    <span
      className={`ml-1 inline-block h-1.5 w-1.5 rounded-full align-middle ${color}`}
    />
  );
}

function noop() {
  // Mutations are now pushed by the sink via SSE; we no longer need
  // handlers to trigger a refetch.
}
