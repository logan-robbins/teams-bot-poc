# TODO

Living backlog. Ordered by impact on the deployed system. Each item
has enough context for an AI agent to pick it up cold — paths,
commands, success criteria.

---

## P0 — blocked on external action

### Wait for `CsApplicationAccessPolicy` propagation in Sandbox tenant

**Status:** Bound (per `Get-CsUserPolicyAssignment` screenshot from
Eric.Ortiz on 2026-05-19 11:55) but not yet enforced by the
Teams/Skype calling backbone. Manual join still returns
`502 CALL_JOIN_FAILED_7504_OR_7505`.

**Action:** Retry the join probe periodically (`POST $BOT/api/calling/join`
with any meeting URL whose organizer is on the policy list — Logan or
Michael). When 7504/7505 disappears, the gate has opened. Per IT,
expected retry timing is "tomorrow morning" (i.e. 2026-05-20 ish);
MS-side propagation can also take up to 24h.

**Owner:** External (Eric.Ortiz to re-bind if needed; us to verify).

**Success:** `POST /api/calling/join` returns `200` with `call_id` set,
`/api/calling/health` shows `activeCalls: 1`, and a fresh
`meeting.transcript.final` blob lands under
`meetings/{meeting_id}/meeting.transcript.final/` within ~30s of audio.

---

## P1 — bot-side gaps for "+Apps" meetings

The bot today auto-handles transcript retrieval and meeting metadata
only for **channel** meetings. Meetings where Alfred is added via
"+Apps" in the meeting chat (the most common installation pattern)
are sparse in the sink — no subject, no organizer, no auto-fetched
transcript. See README §10 for the operator-facing description of
the gap; the items below close it on the bot side.

### B. Auto-resolve subject + organizer for "+Apps" meetings → emit `meeting.created`

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

### A. Auto-trigger `OfficialTranscriptFetcher.Register(...)` for "+Apps" meetings

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

## P2 — sink-side agent-tool ergonomics

Tools live in `python/meeting_agent/tools.py`. All four changes are
Python-only — sink container rebuild, no bot deploy.

### C1. Sort `list_meetings` results by recency descending

**What:** Today the tool returns whatever order
`GET /v2/meetings?limit=N` returns. Sort by
`actual_start_utc ?? scheduled_start_utc` descending in
`list_meetings_impl` before returning. Matches the new MeetingList
UI sort.

**Why:** When the agent calls "the most recent meeting" or scans the
list, the top entry should be the freshest. Removes ambiguity.

### C2. Add date-range filter to `list_meetings`

**What:** Optional args `since: str | None = None, until: str | None = None`
on `list_meetings`. Pass through to the sink as
`?since=...&until=...` (sink already supports these per
retrieving-transcripts.md §1.1). Document in the tool docstring.

**Why:** Lets the agent answer "show me yesterday's meetings",
"meetings from last week", etc.

### C3. Add a `resolve_meeting_by_date` tool

**What:** New tool that takes a natural-language date phrase
("yesterday", "last Friday", "2026-05-15") and returns matching
meetings. Use `dateparser` or write a small phrase-to-range
translator. Probably wraps `list_meetings` with a `since/until`
range derived from the phrase.

**Why:** Bridges the gap between conversational queries ("alfred,
what was decided in yesterday's standup?") and the structured tools.

### C4. Add a `find_meeting_by_chat_thread_id` tool

**What:** Reverse-lookup tool that takes a `chat_thread_id`
(`19:abc@thread.v2`) and returns the meeting (canonical
`meeting_id` + subject). The sink's `V2Meeting` already carries
`meeting_chat_thread_id`; iterate `list_meetings` for a match, OR
add a new sink endpoint `GET /v2/resolve?kind=meeting&chat_thread_id=...`.

**Why:** Some upstream code paths know the chat thread id but not
the canonical meeting id. Without this tool the agent can't bridge
the two reliably (the canonicalization fix at the bot helps for
new events but historical data may still have only the chat thread id).

---

## P3 — agent autonomy

### D. Add a `request_transcript_backfill` tool to the agent

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
(`teams/{tid}/channels/{cid}/channel.message.created/...`):

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

### Resolve `openai-agents` vs `anthropic-sdk` naming inconsistency

**Where:** `README.md:65` says "AlfredAnalyzer (claude-haiku-4-5,
Anthropic Agents SDK)". `python/pyproject.toml` pins
`openai-agents>=0.0.16`. These are different SDK families.

**What:** Pick one truth and update the other. The deployed code
actually uses OpenAI Agents SDK against Azure OpenAI (per
`agent.py` imports and the `aoai-alfred-disney.openai.azure.com`
responses endpoint in the logs). README is wrong.

**Why:** Mis-attribution confuses anyone trying to understand the
architecture or migrate the model.

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
