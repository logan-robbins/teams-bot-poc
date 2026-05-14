# HANDOFF — Alfred v2 data-model refactor (in flight)

**For the AI agent picking this up:** this is a multi-session refactor. The
contract + types are done; the integration work is not. Read this file
**in full** plus `docs/event-contract.md` and `docs/retrieving-transcripts.md`
before touching code. Do not re-litigate the design decisions in §3.

---

## 1. Mission

Replace Alfred's flat-ledger event model (`chat_thread_id` as a universal
key, nullable everything-else) with a hierarchical model that **mirrors the
Microsoft Graph URL paths**:

```
Team (team_id)
  └── Channel (team_id, channel_id)
        └── Thread (thread_id = root message id)
              ├── messages
              └── attachments

Meeting (meeting_id = Graph onlineMeeting id)
  ├── meeting_chat (meeting_chat_thread_id)
  │     ├── messages
  │     └── attachments
  ├── transcripts (partial / final / official)
  └── (optional) channel_link → (team_id, channel_id, thread_id?)
```

**End goal:** an AI agent can list meetings, resolve subject/team/channel
names → ids, and pull transcripts/chat via predictable hierarchical paths
(blob archive or sink REST API).

**Rules from the human (do not violate):**
- Destructive changes are OK. We are in development. No production data
  to preserve.
- **NO LEGACY, NO FALLBACK.** Delete v1 code paths outright. Do not add
  back-compat shims, optional v1-vs-v2 modes, or "if old schema, do X
  else Y" branches.
- Storage paths + POST routes are machine-readable / API-optimized.
- UI shows human-readable names; ids on hover/click.
- Push only to `private` + `disney` remotes, never `origin`.
- Never `git add -A` / `git add .` — explicit paths only (working root
  holds untracked credentials per the user's memory).

---

## 2. State of the world (after the foundation commit)

**Done (in the commit you are picking up from):**

| Task | Result |
|---|---|
| 1. v2 event contract | `docs/event-contract.md` — completely rewritten for `alfred-v2`. |
| 2. v2 storage layout | `docs/retrieving-transcripts.md` — completely rewritten for new blob hierarchy. |
| 3. New C# types | New/rewritten files in `src/Models/` — see §4.1. |
| 4. Graph metadata resolver | `src/Services/GraphMetadataResolver.cs` — typed + cached. |

**Not done (your job):** tasks 5–17. See §5.

**Important:** the foundation commit **does not compile** yet. The old C#
publishers (`EventFanoutDispatcher`, `MeetingChannelLinkStore`,
`OfficialTranscriptFetcher`, `AlfredBot`, `CallHandler`, etc.) still
reference the old types (`ChatEventPayload`, `SessionLinkedPayload`,
`OfficialTranscriptPayload`, old `AlfredEventTypes` constants). That's
expected. The next task (Task 5, rewriting `EventFanoutDispatcher`) is
where compilation will be restored.

---

## 3. Design decisions — do NOT re-litigate

The human and prior assistant pinned these. If you find yourself wanting
to change one, **stop and ask the human first**.

1. **Schema name:** `alfred-v2`. v1 is dead. No `alfred-events-v2`, no
   suffix variants. The literal string is in
   `AlfredEventEnvelope.SchemaVersion`.

2. **Discriminated envelope:** every envelope has either
   `channel_ref` populated **or** `meeting_ref` populated, never both,
   never neither. The discriminator is the `event_type` prefix
   (`channel.*` → channel_ref; `meeting.*` → meeting_ref). No
   `chat_thread_id`, no `channel_thread_id`, no nullable top-level
   routing fields.

3. **`meeting_id` is canonical, not `chat_thread_id`.** `meeting_id` is
   the Graph `onlineMeeting` id (URL-safe base64). The
   `meeting_chat_thread_id` (`19:meeting_xxx@thread.v2`) and `call_id`
   are sub-resources on `MeetingRef`. **The bot MUST resolve
   `meeting_id` before publishing any `meeting.*` event.** Use
   `GraphMetadataResolver.GetChatAsync` → `GetOnlineMeetingByJoinUrlAsync`
   at meeting-join time and cache. No fallback to chat-thread-id-as-key.

4. **Composite Graph-style keys, no surrogate ids.** Channel:
   `(team_id, channel_id, thread_id, message_id)`. Meeting:
   `meeting_id` + sub-resource keys. **No internal `session_id`
   surrogate** like the old code's `int_YYYYMMDD_...`. The Python
   sink's SQLite uses these composite PKs directly.

5. **Channel meetings get NO `meeting.*` events.** The bot lacks audio
   permission in channels. A meeting that occurs in a channel surfaces
   only as `channel.message.*` events on the channel thread. The
   `meeting.*` family exists only for private meetings the bot was
   added to via `+ Apps`. Do not try to model channel meetings as
   first-class `Meeting` entities.

6. **Channel link is a property of `MeetingRef`, not a separate
   resource.** When the bot learns a meeting belongs to a channel, it
   stamps `channel_link` on `meeting_ref` for all subsequent events
   and emits one `meeting.linked` event. Consumers backfill prior
   meeting events under the linked channel. No separate
   `session_channel_links` table; in SQLite the link is columns on
   the `meetings` row.

7. **Storage paths mirror the contract exactly** — see
   `docs/retrieving-transcripts.md §2`. Per-event JSON blobs (one event,
   one file). Plus per-entity `meta.json` and three `indexes/*.json`
   files for name resolution. **No append-only NDJSON.**

8. **`indexes/*.json` are the AI-agent's discovery surface.** The bot
   rewrites them in full on each change. They are small (single-digit
   kB at our scale). Don't replace with a database lookup; the whole
   point is that you can `curl indexes/meetings.json` from anywhere.

9. **Sink exposes hierarchical GET, single-ingress POST.** Bot POSTs
   to one URL (`/v2/events`) — keeps fanout simple. Sink offers
   hierarchical reads (`GET /v2/teams/{tid}/channels/{cid}/threads/...`)
   plus name resolution (`GET /v2/resolve`, `GET /v2/index`).

10. **Per-message-update is its own event type.** `channel.message.updated`
    and `channel.message.deleted` are separate envelopes, not modifications
    of the original. The blob archive keeps both — readers see the
    history by listing the `messages/` prefix in time order.

---

## 4. File-by-file state

### 4.1 Done (new shape; ready)

| File | Status |
|---|---|
| `docs/event-contract.md` | Authoritative v2 spec. |
| `docs/retrieving-transcripts.md` | Authoritative v2 blob layout. |
| `src/Models/AlfredEventEnvelope.cs` | New envelope + `AlfredEventTypes` constants. |
| `src/Models/AlfredEventRefs.cs` | `ChannelRef`, `MeetingRef`, `ChannelLink`. |
| `src/Models/AlfredEventCommon.cs` | `SenderRef`, `SpeakerRef`, `AttachmentRef`, `TranscriptWord`, `TranscriptProvider`, `MediaSourceSnapshot`. |
| `src/Models/ChannelMessagePayload.cs` | New. |
| `src/Models/ChannelLifecyclePayload.cs` | New (`channel.attached` / `channel.detached`). |
| `src/Models/MeetingChatPayload.cs` | New. |
| `src/Models/MeetingTranscriptPayload.cs` | New (partial + final). |
| `src/Models/MeetingOfficialTranscriptPayload.cs` | New (rename of old `OfficialTranscriptPayload`; not yet deleted, see §4.3). |
| `src/Models/MeetingLifecyclePayload.cs` | New (`meeting.created`, `meeting.ended`, `meeting.call.joined`, `meeting.call.left`). |
| `src/Models/MeetingLinkedPayload.cs` | New (replaces `SessionLinkedPayload`; old not yet deleted, see §4.3). |
| `src/Services/GraphMetadataResolver.cs` | Cached typed accessor for team/channel/chat/onlineMeeting/channel-message metadata. |

### 4.2 To rewrite (Task 5–7)

| File | What changes |
|---|---|
| `src/Services/EventFanoutDispatcher.cs` | Emit v2 envelopes. Every callsite (chat, transcript chunk, transcript final, channel attach/detach, meeting linked, official transcript) must construct the right envelope with `ChannelRef` or `MeetingRef`. |
| `src/Services/BlobEventArchive.cs` | New blob paths per `docs/retrieving-transcripts.md §2`. Add `meta.json` writers and three `indexes/*.json` rewriters (atomic — write to temp blob, then rename or overwrite). |
| `src/Services/MeetingChannelLinkStore.cs` | Becomes a Meeting metadata store, not a separate "link" abstraction. Owns the in-memory map `meeting_id → ChannelLink` plus persistence. The link is set when the bot detects a `channelData` parent or when `@Alfred link to <channel>` is run. |
| `src/Services/AlfredBot.cs` | All `EventFanoutDispatcher.PublishAsync` call sites change shape (now take `AlfredEventEnvelope` directly, or a typed helper that builds the envelope). |
| `src/Services/CallHandler.cs` | Audio + transcript publish path — emit `meeting.transcript.partial/final` with `MeetingRef`. **Must resolve `meeting_id` via `GraphMetadataResolver.GetChatAsync` → `GetOnlineMeetingByJoinUrlAsync` at call-join time.** Cache the resolved id on the call instance. |
| `src/Services/AzureConversationTranscriber.cs` | The internal `TranscriptEvent` (in `src/Models/TranscriptEvent.cs`) still has envelope-routing fields (`TeamId`, `ChannelId`, `ChannelThreadId`, `ChatThreadId`). Trim those — they belong on `MeetingRef` now. |
| `src/Services/OfficialTranscriptFetcher.cs` | Emit `meeting.transcript.official` with `MeetingRef` (canonical `meeting_id`). |
| `src/Services/ChannelAttachmentService.cs` | Emit `channel.attached` / `channel.detached` with `ChannelRef`. |
| `src/Controllers/MessagesController.cs` | Channel chat events emit `channel.message.created/updated/deleted` with `ChannelRef`; meeting chat events emit `meeting.chat.*` with `MeetingRef`. The discriminator is the Bot Framework `conversation.conversationType` + parent thread id. |
| `src/Controllers/GraphNotificationController.cs` | Same shape change as `MessagesController` for the Graph-subscription path. |
| `src/Controllers/DebugController.cs` | `POST /api/debug/fetch-transcript` payload moves from `(call_id, organizer_oid, team_id, channel_id, channel_thread_id)` → `(meeting_id, organizer_oid)` — the canonical key. |

### 4.3 To delete after §4.2 is done (Task 8)

These are kept around right now so the obsolete publishers still
compile while you do the cutover. **Delete them in the same commit
that completes the publisher rewrites** — never let two parallel
shapes live in `src/Models/`.

- `src/Models/ChatEventPayload.cs`
- `src/Models/SessionLinkedPayload.cs`
- `src/Models/OfficialTranscriptPayload.cs` (replaced by `MeetingOfficialTranscriptPayload.cs`)

Also trim — do not delete:
- `src/Models/TranscriptEvent.cs` — strip the envelope-routing fields
  (`TeamId`, `ChannelId`, `ChannelThreadId`, `ChatThreadId`). Keep the
  STT-internal fields (text, timestamps, words, confidence). The
  envelope's `MeetingRef` carries identifiers now.

### 4.4 Python side — all to rewrite (Tasks 9–14)

Same pattern: drop SQLite tables and recreate against the v2 shape;
no migrations.

| File | What changes |
|---|---|
| `python/meeting_agent/persistence.py` | New schema. Tables: `teams`, `channels`, `threads`, `channel_messages`, `channel_attachments`, `meetings`, `meeting_chat_messages`, `meeting_chat_attachments`, `meeting_transcript_chunks`, `meeting_official_transcript`, `raw_ingest_envelopes`. Composite PKs per the Graph hierarchy. |
| `python/meeting_agent/models.py` | Pydantic models for `Team`, `Channel`, `Thread`, `ChannelMessage`, `Meeting`, `MeetingChatMessage`, `TranscriptChunk`, `OfficialTranscript`, `Attachment`. Drop `InterviewSession`, `MeetingEvent`, `RawIngestEvent`. |
| `python/transcript_sink.py` | New routes — see contract §6 and `docs/retrieving-transcripts.md`. Single ingress `POST /v2/events`; hierarchical GETs; `/v2/resolve`; `/v2/index`. |
| `python/meeting_agent/session.py` | `InterviewSession` goes away. Replace with thin `Meeting` and `ChannelThread` views over the per-entity tables. |
| `python/meeting_agent/tools.py` | Tools become `send_to_meeting_chat(meeting_id, ...)`, `fetch_meeting_transcript(meeting_id)`, plus new `list_meetings(filter?)`, `resolve_meeting_by_name(subject)`, `resolve_channel_by_name(team, channel)`. |
| `python/meeting_agent/agent.py` | `AlfredAnalyzer` context becomes a `Meeting` (with chat + transcripts) or a `ChannelThread` (with messages). |
| `python/batcave_platform/specs/alfred.yaml` | Update tool surfaces in the spec to match new tool signatures. **The yaml is the single source of truth for Alfred's prompt — do not duplicate prompt content in code.** |
| `python/tests/*` | Rewrite against the new model. Delete tests for retired abstractions. |

### 4.5 Web UI (Task 15)

- `web/src/lib/sink.ts` — point at `/v2/*` endpoints.
- `web/src/components/MeetingList.tsx`, `MeetingDossier.tsx` — show
  `subject` first, `meeting_id` on click.
- `web/src/components/ChannelsAdmin.tsx`, `ChannelCommandCenter.tsx`,
  `ArchiveBrowser.tsx` — walk new blob paths, show team/channel
  display names.

---

## 5. Task list (resume here)

The TaskCreate task list in the prior session was:

1. ~~Write v2 event contract spec~~ — done
2. ~~Write v2 storage layout spec~~ — done
3. ~~Define new C# event types + payloads + envelope~~ — done
4. ~~Extend GraphApiClient with meeting + name resolution~~ — done (new `GraphMetadataResolver`)
5. **Rewrite `EventFanoutDispatcher` to emit v2 envelopes**  ← start here
6. Rewrite `BlobEventArchive` for new paths
7. Reshape `MeetingChannelLink` into Meeting property
8. Drop legacy C# code paths (see §4.3)
9. Recreate Python SQLite schema for v2 model
10. Rewrite Python models for v2
11. Rewrite Python persistence layer
12. Rewrite sink ingest + hierarchical query API
13. Rewrite Alfred analyzer + tools for v2 model
14. Drop legacy Python code
15. Update React UI for v2 endpoints + human names
16. Rewrite Python tests for v2 model
17. End-to-end smoke test

Re-create them with `TaskCreate` at session start so progress is tracked.

---

## 6. How to verify each task

### After Task 5–8 (C# bot)

```bash
cd src && rm -rf bin obj
dotnet build -c Release
```

A clean build means the publisher rewrites are wire-correct. Then
run the bot locally and exercise:
- channel chat → blob lands at `teams/{tid}/channels/{cid}/threads/{thrid}/messages/...`
- meeting join → `meetings/{meeting_id}/meta.json` written, transcripts flow

### After Task 9–14 (Python sink)

```bash
cd python && uv sync && uv run pytest
```

### After Task 15 (web)

```bash
cd web && npm run build
```

Plus visual check at `/meetings`, `/channels`, `/archive`.

### Task 17 — end-to-end smoke

Deploy bot VM + container apps per `README.md §6`. Then start a real
meeting; verify:
1. `indexes/meetings.json` lists the new meeting by `meeting_id` with
   subject populated.
2. `meetings/{meeting_id}/transcripts/final/` accumulates blobs.
3. `GET /v2/meetings/{meeting_id}` returns full meeting record with
   transcript chunks.
4. `GET /v2/resolve?kind=meeting&subject=<your subject>` resolves
   to the right `meeting_id`.

---

## 7. Anti-patterns — do not do these

- **Do not** add v1 → v2 translation shims. If you find yourself
  writing `if envelope.get('schema_version') == 'alfred-events-v1'`,
  delete it. v1 is dead.
- **Do not** keep both `chat_thread_id` and `meeting_id` as parallel
  keys "for safety." `meeting_id` is canonical; `meeting_chat_thread_id`
  is a sub-resource lookup, nothing more.
- **Do not** add a `--no-verify` to any git commit to bypass hooks.
- **Do not** push to `origin` (public mirror). `private` + `disney`
  only.
- **Do not** use `git add -A` / `git add .`. Stage explicit paths.
  The working root holds untracked credentials.
- **Do not** treat channel meetings as `Meeting` entities. They are
  channel messages, period.
- **Do not** read `.venv`, `node_modules`, `bin/`, `obj/`,
  `__pycache__/` with glob/grep.
- **Do not** create summary / status markdown files unless explicitly
  asked. This HANDOFF.md exists because the human asked for it; do
  not write a `SESSION-2.md` or similar on your own.

---

## 8. Useful pointers

- Architecture overview: `README.md §1–7`.
- Operational/debug recipes: `AGENTS.md`.
- Authoritative v2 contract: `docs/event-contract.md`.
- Authoritative v2 storage layout: `docs/retrieving-transcripts.md`.
- The user's hard rules + memory: `~/.claude/CLAUDE.md` plus
  `~/.claude/projects/-Users-logan-robbins-research-teams-bot-poc/memory/MEMORY.md`.
- The Alfred prompt + intervention policy is **only** in
  `python/batcave_platform/specs/alfred.yaml`. Do not duplicate.
- Push protocol: `private` + `disney` remotes only.
- Deploy flow: `README.md §6` + `AGENTS.md §3–7`.

---

## 9. First five minutes of the next session

1. Read this file in full. Then `README.md`, `docs/event-contract.md`,
   `docs/retrieving-transcripts.md`.
2. `git log --oneline -5` — confirm the foundation commit is at HEAD.
3. `git status` — should be clean.
4. Re-create the task list with `TaskCreate` (§5).
5. Start Task 5: open `src/Services/EventFanoutDispatcher.cs` and
   rewrite the `PublishAsync` callsites to emit the new envelope
   shape. The dispatcher itself shouldn't need to know about
   `ChannelRef` vs `MeetingRef`; pass it whole envelopes.

Good luck.
