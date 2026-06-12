import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";
import { MeetingList } from "./components/MeetingList";
import { MeetingDossier } from "./components/MeetingDossier";
import { ChannelsAdmin } from "./components/ChannelsAdmin";
import { ClientsAdmin } from "./components/ClientsAdmin";
import { ChannelsDebug } from "./components/ChannelsDebug";
import { ChannelCommandCenter } from "./components/ChannelCommandCenter";
import { ArchiveBrowser } from "./components/ArchiveBrowser";

/**
 * Alfred — Meeting Dossier.
 *
 * Routes:
 *   ``/``                                 meeting picker (MeetingList)
 *   ``/m/:chatThreadId``                  per-meeting dossier
 *   ``/channels``                         operator UI: attached channels +
 *                                         per-channel consumer config +
 *                                         join-any-meeting-by-URL panel
 *   ``/clients``                          operator UI: email-based client
 *                                         routes (email → sink URL +
 *                                         optional storage container)
 *   ``/channels/inspect/:teamId/:channelId``
 *                                         per-channel command center (live
 *                                         transcript + official transcripts)
 *   ``/debug``                            bot's per-thread NDJSON tail
 */
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<MeetingList />} />
        <Route path="/m" element={<Navigate to="/" replace />} />
        <Route path="/m/*" element={<KeyedDossier />} />
        <Route path="/channels" element={<ChannelsAdmin />} />
        <Route path="/clients" element={<ClientsAdmin />} />
        <Route path="/debug" element={<ChannelsDebug />} />
        <Route path="/archive" element={<ArchiveBrowser />} />
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
