import { Link } from "react-router-dom";
import { Header } from "./Header";
import { Ledger } from "./Ledger";
import { Dossier } from "./Dossier";
import { CompanionRail } from "./CompanionRail";
import { sink } from "../lib/sink";
import { useSessionStore } from "../stores/sessionStore";
import { useSessionStream } from "../hooks/useSessionStream";

interface Props {
  chatThreadId: string;
}

/**
 * Dossier view for a single meeting.
 *
 * Mounted only when the URL carries ``/m/<chat_thread_id>``. The router
 * keys this component on ``chatThreadId`` so navigating between meetings
 * unmounts + remounts cleanly, re-seeding the store from the new
 * meeting's snapshot.
 */
export function MeetingDossier({ chatThreadId }: Props) {
  useSessionStream(chatThreadId);

  const session = useSessionStore((s) => s.session);
  const analysis = useSessionStore((s) => s.analysis);
  const connection = useSessionStore((s) => s.connection);
  const muted = session?.alfred_muted ?? false;

  async function endSession() {
    if (!chatThreadId) return;
    await sink.endMeeting(chatThreadId);
  }

  function handleSetMuted(m: boolean) {
    sink.setMuted(chatThreadId, m).catch(() => {});
  }

  const history = session?.meeting_history ?? [];

  return (
    <div className="flex h-screen flex-col bg-ink-950 text-ink-50">
      <Header
        session={session}
        muted={muted}
        chatThreadId={chatThreadId}
        onEnd={endSession}
      />

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
          setMuted={handleSetMuted}
          onAfterMutation={noop}
        />
      </main>

      <footer className="flex items-center justify-between border-t border-ink-800 bg-ink-950 px-6 py-1.5 font-mono text-[10px] uppercase tracking-widest text-ink-500">
        <span>
          Alfred · meeting dossier ·{" "}
          {session?.active ? "in session" : "standing by"}
        </span>
        <span className="flex items-center gap-3">
          <Link
            to="/"
            className="text-ink-400 transition hover:text-ink-200"
          >
            ← all meetings
          </Link>
          <span>
            /sink · sse <ConnectionDot mode={connection.mode} />
          </span>
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

function noop() {}
