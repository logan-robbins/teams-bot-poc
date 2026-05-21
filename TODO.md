# TODO

Living backlog. Ordered by impact on the deployed system. Each item
has enough context for an AI agent to pick it up cold — paths,
commands, success criteria.

---

## P0 — 2026 call-join investigation

1. [x] Read `ERROR.md` and extract each explicit assumption about the
   failing call join.
2. [x] Verify current 2026 Microsoft documentation and latest SDK /
   package versions for Teams cloud communications call joining with
   app-hosted media.
3. [x] Compare the repository's join implementation, bot channel
   configuration, Teams manifest scopes, and runtime configuration
   against the documented requirements.
4. [x] Probe the deployed bot, Azure Bot Service channel state, and VM
   logs to validate which gate fails before media establishment.
5. [x] Reconcile the evidence against each `ERROR.md` assumption and
   identify the actual blocker for call joining.
6. [x] Update `README.md` and this checklist with the final diagnosis,
   exact verification commands, and any required operator/admin action.

**Status 2026-05-21:** RSC-scoped meeting joins work, but only when the
bot uses the hidden `meetup-join/{meeting_chat_thread_id}` URL with the
meeting organizer/user OID in the Teams context. The short URL copied
from Teams (`https://teams.microsoft.com/meet/{id}?p=...`) still returns
7504. We proved this with meeting chat
`19:meeting_NmFkYWM1NDQtYTM3ZC00ZjlmLTk4ZjItZjE0M2YwOWEzODIx@thread.v2`:
the short URL failed; the synthesized URL with Logan's OID
`accf88ee-68d1-4df3-a5b0-98fae5d59ef1` returned `200`, then
`/api/calling/health` reached `state=Established`, `readiness=ready`,
and media frames flowed. The earlier "tenant-wide Cloud Communications
blocked everything" theory was too broad; arbitrary URL joins remain
unauthorized, but installed meeting-chat RSC joins can work.

**Code fix:** `src/Services/AlfredBot.cs` now passes the Teams
organizer/sender OID into auto-join URL synthesis instead of the bot
AppId. `src/Services/ChannelMeetingJoinUrls.cs` now fails fast if the
thread, tenant, or organizer OID is missing.

---

## P0 — blocked on external action

### Fix and deploy RSC meeting-chat auto-join URL synthesis

**Status:** Root cause found. The bot's RSC auto-join path synthesized
the hidden Teams join URL with the bot AppId in the context `Oid`.
Teams/Graph expects a real tenant user OID, normally the meeting
organizer or call initiator. With the bot AppId, Graph returns
`403 Insufficient enterprise tenant permissions` / 7504. With the
organizer OID, the same meeting joins and media establishes.

**Action:** Build, commit, push, and deploy the C# bot patch. After
deploy, create a new scheduled private meeting, send one meeting-chat
message so Teams materializes the chat, add Alfred to that meeting, and
mention Alfred. Expected result: auto-join succeeds without manually
constructing a URL.

**Owner:** Us.

**Success:** `POST /api/calling/join` returns `200` with `call_id` set,
`/api/calling/health` shows `activeCalls: 1`, and a fresh
`meeting.transcript.final` envelope lands under
`meetings/{meeting_id}/live_transcript/` within ~30s of audio.

---

## P1 — bot-side gaps for "+Apps" meetings ✓ DONE (commit 594cced)

The bot today auto-handles transcript retrieval and meeting metadata
for both channel AND "+Apps" meetings. Both items below shipped in
commit `594cced` and are live in the deployed bot.

### B. Auto-resolve subject + organizer for "+Apps" meetings → emit `meeting.created` ✓ DONE

**Where:** `src/Services/MeetingChatService.cs` (probably in
`AttachToCallAsync` or wherever a new chat subscription is created)
OR `src/Services/GraphNotificationProcessor.cs` (first time a chat
event arrives for an unknown thread).

**What:** When the bot first sees a meeting chat thread (chat with
`chatType == "meeting"`), call
`GraphMetadataResolver.ResolveCanonicalMeetingIdAsync(chatThreadId)`
to get the canonical `onlineMeeting.id`, then `GET
/users/{org}/onlineMeetings/{id}` to fetch `subject`, `organizer`,
`startDateTime`, `endDateTime`. Emit a `meeting.created` envelope
with `MeetingRef` populated (canonical id) and the full metadata.

**Why:** Right now the sink shows `subject=None organizer=None` for
"+Apps" meetings — `list_meetings` and `resolve_meeting_by_name` are
useless for these. With this fix every "+Apps" meeting becomes
queryable by subject and listable with human-readable metadata.

**Success:** After deploy, sending a chat message in a "+Apps"
meeting causes the sink's `/v2/meetings` to show that meeting with
`subject` and `organizer` populated within ~10s.

### A. Auto-trigger `OfficialTranscriptFetcher.Register(...)` for "+Apps" meetings ✓ DONE

**Where:** `src/Services/GraphNotificationProcessor.cs` — add a
sibling to `MaybeFetchPostMeetingTranscript` (line ~200) that fires
on chat-resource (not channel-resource) system messages indicating
the meeting ended. Look for `callEndedEventMessageDetail` (or
equivalent) in the chat's system messages.

**What:** When the meeting-ended system message lands in a meeting
chat we're tracking, call
`_transcriptFetcher.Register(callId, organizerOid, meetingChatThreadId, registeredAtUtc)`
the same way the channel path does. The `OfficialTranscriptFetcher`
will then poll `installedToOnlineMeetings/getAllTranscripts` and
emit `meeting.transcript.official` when the transcript materializes.

**Why:** Today the fetcher only fires from the channel path. "+Apps"
meetings get no auto-trigger — the manifest RSC
(`OnlineMeetingTranscript.Read.Chat`) is wasted because we never
actually call the API. Manual backfill via `/api/debug/fetch-transcript`
is the only path, and it requires the call_id which we may not have.

**Success:** A "+Apps" meeting that had Record-and-Transcribe on
produces a `meeting.transcript.official` event within ~2 min of the
meeting ending, and the transcript text lands at
`meetings/{meeting_id}/transcripts/official.txt`.

**Bundle B + A in one deploy.** Both touch the chat-notification
path, both ship via the same VM Run Command + `dotnet publish`. Tag
the pre-deploy SHA as `deployed-bot-YYYY-MM-DD` per README §7.6.

---

## P2 — sink-side agent-tool ergonomics ✓ DONE (local, not yet deployed)

All four agent tools shipped together in this session. The
implementation took the "extend the sink data layer" route for
C2 and C4 (rather than client-side filtering or iteration), which
keeps the contract clean and reusable.

### C1. Sort `list_meetings` results by recency descending ✓ DONE

`python/meeting_agent/tools.py:list_meetings_impl` now calls
`_recency_sort_key` (actual_start_utc → scheduled_start_utc → empty)
and sorts descending after the sink response. Defense-in-depth: the
sink's `list_meetings_v2` already `ORDER BY COALESCE(last_event_utc,
scheduled_start_utc, created_at_utc) DESC` so this is a guarantee,
not the primary sort.

### C2. Date-range filter on `list_meetings` ✓ DONE

- Store: `persistence.py:list_meetings_v2` gained `since` / `until`
  kwargs (filters on `COALESCE(actual_start_utc, scheduled_start_utc,
  created_at_utc)`).
- Sink: `GET /v2/meetings?since=...&until=...` — ISO 8601 UTC strings.
- Tool: `list_meetings(limit, since, until)` passes through.

### C3. `resolve_meeting_by_date` tool ✓ DONE

Tool wraps a stdlib date-phrase parser (`parse_date_phrase` in
`tools.py`) — no `dateparser` dependency. Recognized phrases:
`today` / `yesterday` / `this week` / `last week` / `this month` /
`last month` / `YYYY-MM-DD` / `YYYY-MM-DD to YYYY-MM-DD`. Unrecognized
phrases return `ok: false` with a hint listing the supported forms.

### C4. `find_meeting_by_chat_thread_id` tool ✓ DONE

- Store already had `get_meeting_by_chat_thread_id`.
- Sink: `GET /v2/resolve?kind=meeting&chat_thread_id=...` returns
  the matching meeting (0 or 1 row).
- Tool: `find_meeting_by_chat_thread_id(chat_thread_id)` returns a
  `MeetingResolveResult` with at most one entry.

---

## P1.5 — meeting subject is unreliable + filename search has a gap (post-demo 2026-05-20)

The 2026-05-19 demo session surfaced two related bugs we need to fix
properly after the demo:

### The subject problem

For "+Apps"-installed meetings, the bot consistently fails to capture
the meeting's real subject. Every path we tried this session failed:

| Path | Result | Why |
|---|---|---|
| `GraphNotificationProcessor.MaybeEmitMeetingCreatedForChatThreadAsync` → `_metadataResolver.GetChatAsync` | 403 Forbidden | API requires `Chat.ReadBasic.WhereInstalled` / `ChatSettings.Read.Chat`; our manifest has none |
| Bot Framework `activity.Conversation.Name` in `OnMessageActivityAsync` | null | Microsoft doesn't populate this for the chat activities we receive |
| `TeamsInfo.GetMeetingInfoAsync` | 403 (`Microsoft.Rest.HttpOperationException` from `TeamsOperations.FetchMeetingInfoWithHttpMessagesAsync`) | Manifest lacks a meeting-extension capability (would need `OnlineMeeting.ReadBasic.Chat` RSC at minimum) |
| `OnTeamsMeetingStart/EndAsync` handlers (added in commit `5b58285`) | Never fire | Requires `OnlineMeeting.ReadBasic.Chat` RSC; manifest v1.0.12 has it but sideload failed because Sandbox `Upload custom apps` policy is disabled for the user |
| Catalog re-publish with v1.0.12 | Not attempted | Needs Sandbox admin approval; not feasible day-of-demo |

User reported the meeting WAS scheduled with a real subject in their
calendar, so Microsoft has the data — we just can't reach it with
current permissions. Operator-set subject via the
`POST /v2/meetings/{id}/transcript-upload` form field worked, and so
did the new `PATCH /v2/meetings/{id}` endpoint (commit `524062e`),
but both are manual.

**The systematic question to answer first:**
1. Sandbox admin: enable `Upload custom apps` policy for the demo
   AAD user so v1.0.12 sideload works. Validate that `meetingStart` /
   `meetingEnd` events then deliver with `Title` populated.
2. If sideload still doesn't work, go through the catalog re-approval
   path with v1.0.12 (RSC delta is just `+OnlineMeeting.ReadBasic.Chat`,
   `-ChatMessageReadReceipt.Read.Chat`). Once it's live, every team
   that has Alfred installed has to UPDATE the app for the new RSC
   to bind.
3. Once `OnlineMeeting.ReadBasic.Chat` is consented, validate end-to-
   end by:
   a. Scheduling a meeting with a real subject in calendar
   b. Adding Alfred via "+Apps"
   c. Confirming `OnTeamsMeetingStartAsync` fires and the bot log
      shows `Title=<actual subject>`, `MsGraphResourceId=<canonical id>`
   d. Confirming auto transcript fetch works after meeting end

### The filename-search gap

`fetch_transcript_by_filename(filename_substring)` searches blob
filenames only. For operator-uploaded transcripts it works (the user's
original filename is preserved at the canonical path). But for
bot-auto-fetched transcripts, the only files would be the generic
`meetings/{id}/transcripts/official.{vtt,txt}` — filename search by
`"Supermemory"` returns nothing because no file is named "Supermemory".

**The fix (when implementing):**
- Extend `list_meeting_transcript_files_impl` to join each transcript
  file against `/v2/meetings/{meeting_id}` and pull the meeting's
  `subject`.
- Match the query against EITHER the filename OR the meeting subject
  (case-insensitive substring on both).
- The agent's flow becomes: `resolve_meeting_by_name` (primary, sink
  registry only) → `fetch_transcript_by_filename` (fallback, blob +
  registry).
- Note: this only matters once subject is reliable end-to-end (see
  above). If subject is null in the registry too, neither tool helps —
  the upload form prompt is the only escape.

---

## P2.5 — synthesis tick architecture (DESIGN ONLY — not yet approved)

The agent loop today is purely reactive: `drain_with_debounce` in
`python/meeting_agent/debounce.py` pulls items off `agent_queue`,
each item is a real chat / transcript event, and the analyzer runs
once per debounced batch. In a long lull the agent thinks zero
times — even if the dossier has drifted out of sync with reality
("the room verbally agreed on X 8 minutes ago; nobody followed up;
no chat / transcript event has fired since").

### Proposal

Add a **periodic synthesis tick** per active session. Every
`SYNTHESIS_INTERVAL_SECONDS` (proposal: 300 = 5 min) of relative
quiet (no real event in the last `SYNTHESIS_QUIET_REQ_SECONDS`,
proposal: 120 = 2 min), enqueue a sentinel onto `agent_queue` for
that session. The analyzer treats the sentinel as a "re-examine
the dossier" prompt rather than a normal event.

### Code touch points

- `python/transcript_sink.py` — add a background coroutine per
  active session (spawned from `_resolve_analyzer` when first
  initialized, cancelled when the session ends). Tracks
  `last_real_event_utc` on the manager.
- `python/meeting_agent/debounce.py` — extend item shape (or
  use a sentinel value) so the agent loop can tell a synthesis
  tick from a real event.
- `python/meeting_agent/agent.py` — `analyze_async` gains a
  `mode: Literal["reactive", "synthesis"] = "reactive"` kwarg;
  passes through to the prompt context.
- `python/batcave_platform/specs/alfred.yaml` — new Principle C
  for synthesis-mode ticks: ONLY post if you've identified a
  concrete unresolved item (decision needed, missing owner,
  meeting drift) that organic ticks would have missed. Default
  silent even more aggressively than reactive ticks.

### Open questions (need user input before implementing)

1. **Cadence.** 5 min default seems right; configurable per
   product spec (`alfred.yaml`)?
2. **Quiet-period requirement.** Should a recent event suppress
   the synthesis tick (don't double up)?
3. **Scope.** Apply to Surface 2 only (in-call) or also to
   Surface 1 ("+Apps" only) where the agent has only chat
   context?
4. **Posting threshold.** Strict (require concrete action item) or
   same threshold as reactive ticks?
5. **Cost ceiling.** Cap synthesis ticks per session per hour
   (e.g. max 6 ticks/hour even if quiet windows last all hour)?

### Blast radius if implemented

- More LLM calls per active session. Worst case 12/hour/session.
  At gpt-5-mini pricing, marginal cost is small but non-zero.
- More chances for a spurious agent post. Mitigation: tighter
  cooldown for synthesis-mode posts.
- Loop concurrency — sentinel events compete with real events on
  the same queue. Real events should preempt synthesis ticks
  (don't fire a synthesis tick if the queue already has real
  items waiting).

### Not implemented — awaiting approval

Per FIX.md §"Out-of-scope items", this was deliberately deferred
from the path-taxonomy fix. Bring it back when there's bandwidth
and an explicit owner.

---

## P3 — agent autonomy

### D. Add a `request_transcript_backfill` tool to the agent ✓ DONE (commit 594cced)

**What:** New tool that calls `POST $BOT/api/debug/fetch-transcript`
with `meeting_id` (which the DebugController now accepts after the
2026-05-18 fix). The agent calls this when:

- A user asks "alfred, get me the transcript for the X meeting" AND
- `fetch_meeting_transcript()` returns `available: false`

The backfill takes up to 30 min to materialize, so the tool returns
a "I've queued the backfill, ask me again in a couple minutes"
shape and the agent replies accordingly.

**Why:** Today if the transcript wasn't auto-fetched (because B+A
above isn't in yet, or the auto-trigger missed), Alfred is stuck
saying "no transcript available" forever. With this tool, the
agent can ASK for the backfill autonomously.

**Note:** Once B+A land, this becomes a fallback rather than the
primary mechanism — but it's still useful for any auto-trigger
that fails silently.

---

## P4 — cleanups

### Drop V1Compat for `demo_ias_cli` once teammate updates server.py

**Where:** `appsettings.production.json` on `vm-alfred-disney`,
`BlobArchive` section.

**What:** Once Michael's `server.py` reads v2 paths
(`teams/{tid}/channels/{cid}/messages/...`):

1. Edit `BlobArchive.V1CompatEnabled` to `false` (or empty
   `V1CompatChannelIds`) via a Run Command — no redeploy needed
   per `reloadOnChange: true` documented in README §6.
2. Delete the `isolation-placeholder` consumer on `demo_ias_cli`:
   ```bash
   ENC=$(printf %s "19:2043d381f87b468faf2bba9623848a83@thread.tacv2" | jq -sRr @uri)
   curl -X DELETE "$BOT/api/channels/d3f5f412-2abf-4300-ac73-019e892c2a05/$ENC/consumers/isolation-placeholder"
   ```
3. Once verified clean, drop the `V1CompatWriter` code from
   `src/Services/BlobEventArchive.cs` in a follow-up commit.

**Why:** Compat layer is by design temporary. Less code, less
config, less drift.

### Resolve `openai-agents` vs `anthropic-sdk` naming inconsistency ✓ DONE

README §1 now reads "AlfredAnalyzer (Azure OpenAI gpt-5-mini,
OpenAI Agents SDK)". Matches `pyproject.toml` (`openai-agents`) and
`agent.py` (imports `from agents …`).

### Prune VM Run Commands when count exceeds ~20

**Why:** Cap is 25. Deploys silently fail with `BadRequest` near
the cap. We pruned to 8 on 2026-05-18; after several deploys we're
at ~14 now. Easy to forget until a deploy mysteriously fails.

**How:** Document in `README.md §6` Gotchas (or extend an existing
deploy script) to auto-prune Succeeded run-commands older than 7
days at deploy time.

---

## Things explicitly NOT to do

- **Do NOT publish Alfred to AppSource.** Out of scope; would require
  Microsoft RTM-media certification, which is heavy.
- **Do NOT broaden `CsApplicationAccessPolicy` to `-Global`** without
  a Sandbox admin's explicit ask. Current scoped grant to specific
  users is the right blast-radius.
- **Do NOT touch `src/Services/MeetingAuditLogger.cs` key format**
  unless you're also updating `web/src/components/ChannelCommandCenter.tsx`
  and `src/Controllers/DebugController.cs` in the same commit — the
  triple is tightly coupled and historical NDJSON files use the older
  format (handled by the merge logic in `DebugController` line 197).
