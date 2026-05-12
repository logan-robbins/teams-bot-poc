import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";
import { MeetingList } from "./components/MeetingList";
import { MeetingDossier } from "./components/MeetingDossier";
import { ChannelsAdmin } from "./components/ChannelsAdmin";
import { ChannelsDebug } from "./components/ChannelsDebug";
import { ChannelCommandCenter } from "./components/ChannelCommandCenter";

/**
 * Alfred — Meeting Dossier.
 *
 * The dossier is gated behind ``/m/:chatThreadId`` so a viewer can only
 * see the meeting whose chat_thread_id is in the URL. Anyone hitting the
 * root path is shown a meeting picker (``MeetingList``) — there is no
 * "current meeting" fallback. ``/channels/admin`` is the operator UI for
 * per-channel consumer config; ``/debug`` tails the bot's per-thread
 * audit logs (every chat / meeting / channel the bot has heard from,
 * not just channels); ``/channels/inspect/:teamId/:channelId`` is the
 * per-channel command center combining status, live transcripts, and
 * Microsoft's official post-meeting transcripts in one view.
 */
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<MeetingList />} />
        <Route path="/m" element={<Navigate to="/" replace />} />
        <Route path="/m/*" element={<KeyedDossier />} />
        <Route path="/channels/admin" element={<ChannelsAdmin />} />
        <Route path="/debug" element={<ChannelsDebug />} />
        {/* Back-compat for any bookmarks of the old path. */}
        <Route
          path="/channels/debug"
          element={<Navigate to="/debug" replace />}
        />
        <Route
          path="/channels/inspect/:teamId/:channelId"
          element={<ChannelCommandCenter />}
        />
      </Routes>
    </BrowserRouter>
  );
}

/**
 * Wraps ``MeetingDossier`` in a key derived from the URL so navigating to a
 * different meeting id remounts the component (and its store seed). The
 * ``*`` splat lets chat_thread_ids that contain ``/`` (rare but possible)
 * survive.
 */
function KeyedDossier() {
  const params = useParams<{ "*": string }>();
  const raw = params["*"] ?? "";
  const chatThreadId = decodeURIComponent(raw);
  if (!chatThreadId) {
    return <Navigate to="/" replace />;
  }
  // Fresh key on each id so the store re-seeds cleanly between meetings.
  return <MeetingDossier key={chatThreadId} chatThreadId={chatThreadId} />;
}
