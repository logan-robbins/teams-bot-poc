import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";
import { MeetingList } from "./components/MeetingList";
import { MeetingDossier } from "./components/MeetingDossier";

/**
 * Alfred — Meeting Dossier.
 *
 * The dossier is gated behind ``/m/:chatThreadId`` so a viewer can only
 * see the meeting whose chat_thread_id is in the URL. Anyone hitting the
 * root path is shown a meeting picker (``MeetingList``) — there is no
 * "current meeting" fallback.
 */
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<MeetingList />} />
        <Route path="/m" element={<Navigate to="/" replace />} />
        <Route path="/m/*" element={<KeyedDossier />} />
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
