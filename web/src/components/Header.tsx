import { useRef, useState } from "react";
import { Moon, Upload } from "lucide-react";
import { StatusBadge } from "./StatusBadge";
import { sink } from "../lib/sink";
import type { SessionSummary } from "../lib/types";

interface Props {
  session?: SessionSummary;
  muted: boolean;
  chatThreadId?: string;
  onEnd: () => void;
  /** Called after a successful transcript upload so the dossier refreshes. */
  onTranscriptUploaded?: () => void;
}

type UploadState =
  | { kind: "idle" }
  | { kind: "uploading" }
  | { kind: "ok"; subject_updated: boolean; txt_bytes: number }
  | { kind: "error"; message: string };

export function Header({ session, muted, chatThreadId, onEnd, onTranscriptUploaded }: Props) {
  const active = !!session?.active;
  const label = session?.candidate_name || "Awaiting instruction";
  // Show the URL-bound chat_thread_id so operators can confirm at a
  // glance which meeting the dossier is wired to.
  const threadLabel = chatThreadId || session?.meeting_url || "";

  const fileInput = useRef<HTMLInputElement>(null);
  const [uploadState, setUploadState] = useState<UploadState>({ kind: "idle" });

  async function handleFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || !chatThreadId) return;

    // Always prompt for a title so the resolver's substring matcher
    // can find the meeting by name later. Seed the prompt with whatever
    // we already have (candidate_name or empty); operator can confirm
    // or override.
    const seed = session?.candidate_name?.trim() ?? "";
    const promptResult = window.prompt(
      "Give this meeting a title so the agent can find it by name (e.g. 'Supermemory Meeting').\n\nThis becomes the meeting subject Alfred matches against when asked to pull this meeting's transcript.",
      seed,
    );
    // Browser prompt returns null when the user cancels (treat as
    // "don't change subject"), empty string when they cleared the
    // field (also no change), or the actual title.
    const subject = promptResult?.trim() ? promptResult.trim() : undefined;

    setUploadState({ kind: "uploading" });
    try {
      const res = await sink.v2UploadMeetingTranscript(chatThreadId, file, subject);
      setUploadState({
        kind: "ok",
        subject_updated: res.subject_updated,
        txt_bytes: res.txt_bytes,
      });
      onTranscriptUploaded?.();
      // Auto-clear the status after a few seconds.
      window.setTimeout(() => setUploadState({ kind: "idle" }), 4000);
    } catch (err) {
      setUploadState({
        kind: "error",
        message: err instanceof Error ? err.message : "Upload failed",
      });
    }
  }

  return (
    <header className="flex items-center justify-between border-b border-ink-800 bg-ink-950/80 px-6 py-3 backdrop-blur">
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-gold-500/20 to-gold-500/5 ring-1 ring-gold-500/30">
          <Moon size={18} className="text-gold-400" />
        </div>
        <div className="flex flex-col leading-tight">
          <span className="font-serif text-lg font-medium text-ink-50">Alfred</span>
          <span className="font-mono text-[10px] uppercase tracking-widest text-ink-400">
            Meeting Dossier
          </span>
        </div>
        <div className="mx-4 h-8 w-px bg-ink-700" />
        <div className="flex flex-col leading-tight">
          <span className="text-sm font-medium text-ink-100">{label}</span>
          {threadLabel ? (
            <span
              className="font-mono text-[10px] text-ink-400 truncate max-w-[360px]"
              title={threadLabel}
            >
              {threadLabel}
            </span>
          ) : (
            <span className="text-[11px] italic text-ink-300">
              No meeting in session, sir.
            </span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3">
        <StatusBadge active={active} muted={muted} />

        {/* Upload transcript — manual fallback when Microsoft Graph fetch
            can't auto-retrieve. Accepts the .vtt or .txt the user
            downloaded from the Teams meeting chat. */}
        {chatThreadId ? (
          <>
            <input
              ref={fileInput}
              type="file"
              accept=".vtt,.txt,text/vtt,text/plain"
              hidden
              onChange={handleFile}
            />
            <button
              type="button"
              onClick={() => fileInput.current?.click()}
              disabled={uploadState.kind === "uploading"}
              title="Upload a transcript file (.vtt or .txt) that you downloaded from the Teams meeting chat"
              className="flex items-center gap-1.5 rounded-md border border-gold-500/40 bg-gold-500/10 px-3 py-1.5 text-xs font-medium text-gold-300 transition hover:bg-gold-500/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Upload size={12} />
              {uploadState.kind === "uploading"
                ? "Uploading…"
                : uploadState.kind === "ok"
                ? "Uploaded ✓"
                : "Upload transcript"}
            </button>
            {uploadState.kind === "error" ? (
              <span
                className="font-mono text-[10px] text-crimson-400 max-w-[200px] truncate"
                title={uploadState.message}
              >
                {uploadState.message}
              </span>
            ) : null}
          </>
        ) : null}

        {active ? (
          <button
            type="button"
            onClick={onEnd}
            className="rounded-md border border-ink-700 bg-ink-900 px-3 py-1.5 text-xs font-medium text-ink-200 transition hover:border-crimson-500/50 hover:text-crimson-400"
          >
            End session
          </button>
        ) : null}
      </div>
    </header>
  );
}
