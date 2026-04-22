import { useState } from "react";
import { Header } from "./components/Header";
import { Ledger } from "./components/Ledger";
import { Dossier } from "./components/Dossier";
import { CompanionRail } from "./components/CompanionRail";
import { useSessionStatus } from "./hooks/useSessionStatus";
import { useSessionAnalysis } from "./hooks/useSessionAnalysis";
import { sink } from "./lib/sink";

/**
 * Alfred — Meeting Dossier.
 *
 * Three-column console:
 *   Left    The Ledger       live append-only meeting record
 *   Center  The Dossier      intent-alignment extraction (hero)
 *   Right   Companion Rail   summary, topics, controls, compose
 *
 * State lives server-side in the Python sink; we simply poll and render.
 */
export default function App() {
  const { session, refresh: refreshStatus } = useSessionStatus();
  const { analysis, refresh: refreshAnalysis } = useSessionAnalysis();
  const [muted, setMuted] = useState(false);

  const refreshAll = () => {
    refreshStatus();
    refreshAnalysis();
  };

  async function endSession() {
    await sink.endSession();
    refreshAll();
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
          onAfterMutation={refreshAll}
        />
      </main>

      <footer className="flex items-center justify-between border-t border-ink-800 bg-ink-950 px-6 py-1.5 font-mono text-[10px] uppercase tracking-widest text-ink-500">
        <span>Alfred · meeting dossier · {session?.active ? "in session" : "standing by"}</span>
        <span>/sink → 127.0.0.1 · polling 500ms / 2s</span>
      </footer>
    </div>
  );
}
