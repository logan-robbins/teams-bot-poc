import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ChevronRight, Folder, FileText, ExternalLink, Moon } from "lucide-react";
import { bot, type ChannelAttachment } from "../lib/bot";
import { sink, type V2Meeting } from "../lib/sink";
import { TopNav } from "./TopNav";

/**
 * Read-only browser for the Azure Blob archive that mirrors every
 * Alfred event + post-meeting transcript. Pure client-side — calls the
 * storage account's anonymous LIST endpoint directly (the container is
 * public-read and CORS allows browser GETs). No backend involvement.
 *
 * URL state: ``?prefix=foo/bar/`` is the current virtual folder. Click
 * a folder to drill in, click a file to open the raw blob.
 */

const ACCOUNT_BASE = "https://stalfreddisney.blob.core.windows.net";
const CONTAINER = "alfred-events";
const LIST_URL = `${ACCOUNT_BASE}/${CONTAINER}`;

interface BlobEntry {
  name: string;
  lastModified: string;
  contentLength: number;
  contentType: string;
  url: string;
}

interface PrefixEntry {
  name: string;
}

interface ListResult {
  blobs: BlobEntry[];
  prefixes: PrefixEntry[];
  nextMarker: string | null;
}

async function listOnePage(prefix: string, marker: string | null): Promise<ListResult> {
  const params = new URLSearchParams({
    restype: "container",
    comp: "list",
    delimiter: "/",
    maxresults: "200",
  });
  if (prefix) params.set("prefix", prefix);
  if (marker) params.set("marker", marker);
  const res = await fetch(`${LIST_URL}?${params.toString()}`);
  if (!res.ok) {
    throw new Error(`LIST ${res.status} ${await res.text()}`);
  }
  const text = await res.text();
  const doc = new DOMParser().parseFromString(text, "application/xml");
  const blobs: BlobEntry[] = Array.from(doc.querySelectorAll("Blob")).map((node) => ({
    name: node.querySelector("Name")?.textContent ?? "",
    url: node.querySelector("Url")?.textContent ?? "",
    lastModified: node.querySelector("Properties > Last-Modified")?.textContent ?? "",
    contentLength: parseInt(
      node.querySelector("Properties > Content-Length")?.textContent ?? "0",
      10,
    ),
    contentType: node.querySelector("Properties > Content-Type")?.textContent ?? "",
  }));
  const prefixes: PrefixEntry[] = Array.from(doc.querySelectorAll("BlobPrefix > Name")).map(
    (n) => ({ name: n.textContent ?? "" }),
  );
  const nextMarker = doc.querySelector("NextMarker")?.textContent ?? null;
  return { blobs, prefixes, nextMarker: nextMarker?.length ? nextMarker : null };
}

async function listAll(prefix: string): Promise<{ blobs: BlobEntry[]; prefixes: PrefixEntry[] }> {
  const blobs: BlobEntry[] = [];
  const prefixes: PrefixEntry[] = [];
  let marker: string | null = null;
  let safetyHops = 0;
  do {
    const page = await listOnePage(prefix, marker);
    blobs.push(...page.blobs);
    prefixes.push(...page.prefixes);
    marker = page.nextMarker;
    safetyHops += 1;
    if (safetyHops > 50) break; // 50 * 200 = 10k entries; plenty for a debug view
  } while (marker);
  // Sort blobs newest-first. Azure returns Last-Modified as RFC 1123
  // ("Mon, 18 May 2026 18:31:21 GMT") which does NOT sort lexicographically
  // (any "Thu, …" string > any "Mon, …" string). Parse to epoch ms first;
  // fall back to the blob name (ISO yyyyMMddTHHmmssfffZ prefix is sortable)
  // if Date parsing returns NaN.
  const ts = (b: BlobEntry) => {
    const n = new Date(b.lastModified).getTime();
    return Number.isFinite(n) ? n : 0;
  };
  blobs.sort((a, b) => {
    const diff = ts(b) - ts(a);
    return diff !== 0 ? diff : b.name.localeCompare(a.name);
  });
  prefixes.sort((a, b) => a.name.localeCompare(b.name));
  return { blobs, prefixes };
}

function trailingSegment(prefix: string): string {
  if (!prefix) return "";
  const trimmed = prefix.endsWith("/") ? prefix.slice(0, -1) : prefix;
  const i = trimmed.lastIndexOf("/");
  return i >= 0 ? trimmed.slice(i + 1) : trimmed;
}

function parentPrefix(prefix: string): string {
  if (!prefix) return "";
  const trimmed = prefix.endsWith("/") ? prefix.slice(0, -1) : prefix;
  const i = trimmed.lastIndexOf("/");
  return i >= 0 ? trimmed.slice(0, i + 1) : "";
}

function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "?";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function fmtTs(raw: string): string {
  if (!raw) return "?";
  try {
    return new Date(raw).toLocaleString();
  } catch {
    return raw;
  }
}

/**
 * Mirror of the C# BlobEventArchive.SanitizePathSegment + the Teams id
 * shape. Builds two lookup maps so a raw blob folder segment can be
 * rendered as a friendly display name:
 *   teamId GUID  -> "WDI R&D"
 *   sanitizedChannelId -> "alfred_test"
 */
function buildAttachmentMaps(attachments: ChannelAttachment[]) {
  const teamMap = new Map<string, string>();
  const channelMap = new Map<string, string>();
  const sanitize = (raw: string) => raw.replace(/[^a-zA-Z0-9\-_.]/g, "_");
  for (const a of attachments) {
    if (a.team_id && a.team_display_name) {
      teamMap.set(a.team_id, a.team_display_name);
    }
    if (a.channel_id && a.channel_display_name) {
      channelMap.set(sanitize(a.channel_id), a.channel_display_name);
    }
  }
  return { teamMap, channelMap };
}

/**
 * Any-id-the-bot-might-have-written → human-readable subject. Built from
 * sink.v2ListMeetings(). Indexes BOTH:
 *   - canonical Graph onlineMeeting.id (post-canonicalization-fix writes)
 *   - sanitized meeting_chat_thread_id (pre-fix fallback writes, where the
 *     bot used the chat thread id as the meeting_id; folder names sanitize
 *     `:` and `@` to `_` per BlobEventArchive.SanitizePathSegment)
 * So a meetings/{folder-name} segment that matches either form renders as
 * the subject.
 */
function buildMeetingMap(meetings: V2Meeting[]) {
  const meetingMap = new Map<string, string>();
  const sanitize = (raw: string) => raw.replace(/[^a-zA-Z0-9\-_.]/g, "_");
  for (const m of meetings) {
    const subject = (m.subject || "").trim();
    if (!subject) continue;
    if (m.meeting_id) {
      meetingMap.set(m.meeting_id, subject);
      meetingMap.set(sanitize(m.meeting_id), subject);
    }
    if (m.meeting_chat_thread_id) {
      meetingMap.set(m.meeting_chat_thread_id, subject);
      meetingMap.set(sanitize(m.meeting_chat_thread_id), subject);
    }
  }
  return meetingMap;
}

/**
 * Human-readable display names for the alfred-v2 category folders.
 * Blob storage uses machine-readable snake_case (mirrors MS Graph
 * sub-resources where one exists); the UI renders the labels below.
 * Legacy raw event_type folder names (e.g. meeting.transcript.final/)
 * are also mapped so historical blobs render cleanly until they age
 * out.
 */
const CATEGORY_LABELS: Record<string, string> = {
  // v2 category folders (canonical going forward)
  messages: "Chat messages",
  live_transcript: "Live transcript",
  transcripts: "Official transcript",
  lifecycle: "Lifecycle",

  // legacy event-type-per-folder names — pre-refactor blobs still
  // exist at these paths; keep them human-readable too.
  "channel.message.created": "Chat messages (created)",
  "channel.message.updated": "Chat messages (updated)",
  "channel.message.deleted": "Chat messages (deleted)",
  "channel.attached": "Channel attached",
  "channel.detached": "Channel detached",
  "meeting.chat.created": "Chat messages (created)",
  "meeting.chat.updated": "Chat messages (updated)",
  "meeting.chat.deleted": "Chat messages (deleted)",
  "meeting.transcript.partial": "Live transcript (partial)",
  "meeting.transcript.final": "Live transcript (final)",
  "meeting.transcript.official": "Official transcript event",
  "meeting.created": "Meeting created",
  "meeting.ended": "Meeting ended",
  "meeting.linked": "Meeting linked to channel",
  "meeting.call.joined": "Bot joined call",
  "meeting.call.left": "Bot left call",

  // v1 legacy compat path (server.py polls here) — surface to the
  // operator so they understand why this older folder exists.
  "chat.message": "Chat messages (v1 compat)",
};

/**
 * Maps a slash-delimited folder segment within an /archive prefix to a
 * human-friendly display name. Returns the original segment if no
 * lookup matches (so unknown ids still render).
 *
 * Handles three blob layouts that coexist in the container:
 *   - v2 canonical:  teams/{teamId}/channels/{cid}/{category}/...
 *                     meetings/{mid}/{category}/...
 *   - v2 historical: same prefixes but with raw event_type as the
 *                     category folder (pre-category-refactor blobs).
 *   - v1 compat:    channels/{teamId}/{cid}/chat.message/...
 */
function friendlyLabel(
  segment: string,
  positionInPath: number,
  fullPath: string[],
  maps: {
    teamMap: Map<string, string>;
    channelMap: Map<string, string>;
    meetingMap: Map<string, string>;
  },
): string {
  // v1 compat: channels/{teamId}/{sanitizedChannelId}/chat.message/...
  if (fullPath[0] === "channels") {
    if (positionInPath === 1) {
      return maps.teamMap.get(segment) ?? segment;
    }
    if (positionInPath === 2) {
      return maps.channelMap.get(segment) ?? segment;
    }
    if (positionInPath === 3) {
      return CATEGORY_LABELS[segment] ?? segment;
    }
  }
  // v2: teams/{teamId}/channels/{sanitizedChannelId}/{category}/...
  if (fullPath[0] === "teams") {
    if (positionInPath === 1) {
      return maps.teamMap.get(segment) ?? segment;
    }
    if (positionInPath === 3) {
      return maps.channelMap.get(segment) ?? segment;
    }
    if (positionInPath === 4) {
      return CATEGORY_LABELS[segment] ?? segment;
    }
  }
  // v2: meetings/{meeting_id}/{category}/...
  if (fullPath[0] === "meetings") {
    if (positionInPath === 1) {
      return maps.meetingMap.get(segment) ?? segment;
    }
    if (positionInPath === 2) {
      return CATEGORY_LABELS[segment] ?? segment;
    }
  }
  return segment;
}

export function ArchiveBrowser() {
  const [searchParams, setSearchParams] = useSearchParams();
  const prefix = searchParams.get("prefix") ?? "";
  const [blobs, setBlobs] = useState<BlobEntry[]>([]);
  const [prefixes, setPrefixes] = useState<PrefixEntry[]>([]);
  const [attachments, setAttachments] = useState<ChannelAttachment[]>([]);
  const [meetings, setMeetings] = useState<V2Meeting[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const maps = useMemo(() => {
    const { teamMap, channelMap } = buildAttachmentMaps(attachments);
    const meetingMap = buildMeetingMap(meetings);
    return { teamMap, channelMap, meetingMap };
  }, [attachments, meetings]);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const { blobs, prefixes } = await listAll(prefix);
      setBlobs(blobs);
      setPrefixes(prefixes);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to list");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefix]);

  // Fetch the channel-attachments map once. Used to label otherwise-
  // opaque blob path segments (team GUIDs, sanitized channel ids) with
  // their human display names. Idempotent and silently best-effort.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const body = await bot.listChannels();
        if (!cancelled) setAttachments(body.attachments ?? []);
      } catch {
        // No friendly names available — fall back to raw ids in the UI.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Fetch the meeting metadata map once. Used to label otherwise-
  // opaque meeting GUIDs in the meetings/{meeting_id}/... layout with
  // their human-readable subject. Idempotent and silently best-effort.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const body = await sink.v2ListMeetings({ limit: 200 });
        if (!cancelled) setMeetings(body.meetings ?? []);
      } catch {
        // No subjects available — fall back to raw meeting ids in the UI.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const crumbs = useMemo(() => {
    const segs = prefix.split("/").filter(Boolean);
    const acc: { label: string; prefix: string }[] = [{ label: "(root)", prefix: "" }];
    let running = "";
    for (let i = 0; i < segs.length; i++) {
      const s = segs[i];
      running += `${s}/`;
      acc.push({ label: friendlyLabel(s, i, segs, maps), prefix: running });
    }
    return acc;
  }, [prefix, maps]);

  // Folder mtime aggregation: for the current prefix, list ALL blobs
  // (no delimiter) once, then take each blob's first path-segment-after-
  // the-current-prefix as a bucket key and remember the max Last-Modified
  // per bucket. The result lets us sort folders by "most recently
  // touched" — which is what an operator actually wants when looking at
  // a date-organized archive. Falls back to alphabetic for any folder
  // with no observed activity (rare; folders only exist when blobs do).
  const [folderMtimes, setFolderMtimes] = useState<Map<string, number>>(new Map());
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mtimes = new Map<string, number>();
        let marker: string | null = null;
        let pages = 0;
        do {
          const params = new URLSearchParams({
            restype: "container",
            comp: "list",
            maxresults: "5000",
          });
          if (prefix) params.set("prefix", prefix);
          if (marker) params.set("marker", marker);
          const res = await fetch(`${LIST_URL}?${params.toString()}`);
          if (!res.ok) break;
          const text = await res.text();
          const doc = new DOMParser().parseFromString(text, "application/xml");
          for (const node of Array.from(doc.querySelectorAll("Blob"))) {
            const name = node.querySelector("Name")?.textContent ?? "";
            const lm = node.querySelector("Properties > Last-Modified")?.textContent ?? "";
            const ts = new Date(lm).getTime();
            if (!Number.isFinite(ts)) continue;
            // First path segment after the current prefix.
            const rest = name.startsWith(prefix) ? name.slice(prefix.length) : name;
            const slash = rest.indexOf("/");
            const folder = slash > 0 ? rest.slice(0, slash) : "";
            if (!folder) continue;
            const prev = mtimes.get(folder) ?? 0;
            if (ts > prev) mtimes.set(folder, ts);
          }
          marker = (doc.querySelector("NextMarker")?.textContent ?? "").trim() || null;
          pages += 1;
          if (pages > 20) break; // 20 * 5000 = 100k blobs; far beyond typical
        } while (marker);
        if (!cancelled) setFolderMtimes(mtimes);
      } catch {
        // Silent — sort just falls back to alphabetic.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [prefix]);

  // Sort prefixes by newest blob inside them (descending). Folders with
  // no observed activity sink to the bottom, then alphabetic tiebreak.
  const sortedPrefixes = useMemo(() => {
    if (folderMtimes.size === 0) return prefixes;
    const getTs = (entry: PrefixEntry): number => {
      const segment = entry.name.replace(/\/$/, "").split("/").pop() ?? "";
      return folderMtimes.get(segment) ?? 0;
    };
    return [...prefixes].sort((a, b) => {
      const diff = getTs(b) - getTs(a);
      return diff !== 0 ? diff : a.name.localeCompare(b.name);
    });
  }, [prefixes, folderMtimes]);

  function navigateTo(newPrefix: string) {
    if (newPrefix) {
      setSearchParams({ prefix: newPrefix });
    } else {
      setSearchParams({});
    }
  }

  return (
    <div className="flex h-screen flex-col bg-ink-950 text-ink-50">
      <header className="flex items-center gap-3 border-b border-ink-800 bg-ink-950/80 px-6 py-3 backdrop-blur">
        <Link
          to="/"
          className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-gold-500/20 to-gold-500/5 ring-1 ring-gold-500/30"
          aria-label="Back to meetings"
        >
          <Moon size={18} className="text-gold-400" />
        </Link>
        <div className="flex flex-col leading-tight">
          <span className="font-serif text-lg font-medium text-ink-50">
            Event Archive
          </span>
          <span className="font-mono text-[10px] uppercase tracking-widest text-ink-400">
            stalfreddisney / alfred-events · public read
          </span>
        </div>
        <TopNav onRefresh={() => void refresh()} />
      </header>

      <main className="flex-1 overflow-auto px-6 py-6">
        <div className="mx-auto max-w-5xl">
          {/* Breadcrumbs */}
          <nav className="flex flex-wrap items-center gap-1 font-mono text-xs text-ink-300">
            {crumbs.map((c, i) => (
              <span key={c.prefix} className="flex items-center gap-1">
                {i > 0 ? <ChevronRight size={12} className="text-ink-600" /> : null}
                <button
                  type="button"
                  onClick={() => navigateTo(c.prefix)}
                  className={
                    i === crumbs.length - 1
                      ? "text-gold-300"
                      : "text-ink-300 hover:text-ink-50 underline-offset-2 hover:underline"
                  }
                >
                  {c.label}
                </button>
              </span>
            ))}
          </nav>

          {error ? (
            <div className="mt-4 rounded-md border border-crimson-500/40 bg-crimson-500/10 px-4 py-3 text-sm text-crimson-300">
              {error}
            </div>
          ) : null}

          {loading && blobs.length === 0 && prefixes.length === 0 ? (
            <p className="mt-6 text-sm italic text-ink-300">Loading…</p>
          ) : null}

          {!loading && !error && blobs.length === 0 && prefixes.length === 0 ? (
            <p className="mt-6 text-sm italic text-ink-300">
              Empty. Send a message to Alfred in a channel or finish a meeting with
              "Record and Transcribe" to populate this folder.
            </p>
          ) : null}

          {/* Folders */}
          {sortedPrefixes.length > 0 ? (
            <section className="mt-5">
              <h2 className="mb-2 text-[10px] font-mono uppercase tracking-widest text-ink-500">
                Folders ({sortedPrefixes.length})
              </h2>
              <ul className="space-y-1">
                {sortedPrefixes.map((p) => {
                  const trail = trailingSegment(p.name);
                  // Position in the path is "current depth" — strip the
                  // leading prefix that's already visited, count remaining
                  // segments to derive how deep this folder sits.
                  const pathSegs = p.name.split("/").filter(Boolean);
                  const positionInPath = pathSegs.length - 1;
                  const pretty = friendlyLabel(trail, positionInPath, pathSegs, maps);
                  const isFriendly = pretty !== trail;
                  return (
                    <li key={p.name}>
                      <button
                        type="button"
                        onClick={() => navigateTo(p.name)}
                        className="flex w-full items-center gap-2 rounded-md border border-ink-800 bg-ink-900/40 px-3 py-2 text-left text-sm hover:bg-ink-900"
                        title={isFriendly ? `id: ${trail}` : trail}
                      >
                        <Folder size={14} className="text-gold-400" />
                        <span className={isFriendly
                          ? "flex-1 truncate font-serif text-sm text-ink-100"
                          : "flex-1 truncate font-mono text-xs text-ink-100"}>
                          {pretty}
                        </span>
                        <span className="font-mono text-[10px] text-ink-500">/</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>
          ) : null}

          {/* Files */}
          {blobs.length > 0 ? (
            <section className="mt-5">
              <h2 className="mb-2 text-[10px] font-mono uppercase tracking-widest text-ink-500">
                Files ({blobs.length}) · sorted newest first
              </h2>
              <ul className="space-y-1">
                {blobs.map((b) => (
                  <li key={b.name}>
                    <a
                      href={b.url}
                      target="_blank"
                      rel="noreferrer"
                      className="flex items-center gap-2 rounded-md border border-ink-800 bg-ink-900/40 px-3 py-2 text-sm hover:bg-ink-900"
                    >
                      <FileText size={14} className="text-ink-400" />
                      <span className="flex-1 truncate font-mono text-xs text-ink-100">
                        {trailingSegment(b.name)}
                      </span>
                      <span className="font-mono text-[10px] text-ink-500">
                        {fmtBytes(b.contentLength)}
                      </span>
                      <span className="font-mono text-[10px] text-ink-500">
                        {fmtTs(b.lastModified)}
                      </span>
                      <ExternalLink size={12} className="text-ink-500" />
                    </a>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {prefix ? (
            <div className="mt-6">
              <button
                type="button"
                onClick={() => navigateTo(parentPrefix(prefix))}
                className="rounded-md border border-ink-700 bg-ink-900 px-3 py-1.5 text-xs text-ink-300 hover:bg-ink-800"
              >
                ← up to {parentPrefix(prefix) || "(root)"}
              </button>
            </div>
          ) : null}
        </div>
      </main>
    </div>
  );
}
