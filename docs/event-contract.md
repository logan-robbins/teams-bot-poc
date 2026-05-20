# Alfred Event Contract — `alfred-v2`

The canonical contract published by the Alfred bot. The bot is a
Teams platform that captures **channel chat** (team → channel →
thread → message) and **meetings** (audio + chat in private meetings
the bot is added to). Every event POSTs as a versioned JSON envelope.

This contract mirrors **the Microsoft Graph URL hierarchy**. The
shape of every event tells you exactly which Graph resource it
belongs to. No flat keyspace, no overloaded ids, no nullable
routing fields that you have to interpret.

If you are building a consumer:

1. Pick a URL on your network.
2. Register it via `POST $BOT/api/channels/{teamId}/{channelId}/consumers`
   (the only scope the bot currently implements — channel-scope
   consumers also receive `meeting.*` events for meetings linked to
   that channel), or use the read-only blob archive
   ([`docs/retrieving-transcripts.md`](retrieving-transcripts.md)).
3. Accept the envelope shape in §2.
4. To post back into chat, `POST $BOT/api/send-chat` (§5).

---

## 1. Versioning

- Current schema version: **`alfred-v2`**. Every envelope carries
  `"schema_version": "alfred-v2"`.
- v1 (`alfred-events-v1`) is **dead**. The bot does not emit v1. No
  back-compat shims exist on the sink. If you have v1 code, replace
  it.
- Within v2: additive only. New optional fields may appear; new
  event types may appear. Existing fields never change shape inside
  v2. Breaking changes ship as `v3`.

---

## 2. Envelope

```jsonc
{
  "schema_version": "alfred-v2",
  "event_type":     "meeting.transcript.final",
  "event_id":       "8a3f1c0e2b9d4a7e9f12bb0001020304",
  "ts":             "2026-05-14T16:34:12.184Z",

  // EXACTLY ONE of these two blocks is populated. Which one tells
  // you whether this event belongs to channel chat or to a meeting.
  "channel_ref":    null,
  "meeting_ref":    { /* see §2.2 */ },

  // Optional. Echo to /api/send-chat to reply into the source thread.
  "conversation_reference_id": "<bot-framework conv ref id>",

  // Event-type-specific payload. See §3.
  "payload":        { /* ... */ }
}
```

Rules:
- `channel_ref` is populated iff `event_type` starts with `channel.`.
- `meeting_ref` is populated iff `event_type` starts with `meeting.`.
- Both blocks are never populated on the same envelope.

### 2.1 `channel_ref`

Mirrors `/teams/{team_id}/channels/{channel_id}/messages/{thread_id}/replies/{message_id}`.

```jsonc
{
  "team_id":              "d3f5f412-2abf-4300-ac73-019e892c2a05",
  "team_display_name":    "Engineering",
  "channel_id":           "19:abc@thread.tacv2",
  "channel_display_name": "general",
  "thread_id":            "1700000000000",   // root message id; required
  "message_id":           "1700000000123"    // present on *.message.* events; null on system events
}
```

Display names are **best-effort, populated when the bot knows them**.
Resolution happens via `GET /teams/{id}` and `GET /teams/{id}/channels/{id}`
on first sighting and is cached. Consumers should not rely on display
names being present; they should fall back to ids and call the sink's
`/v2/resolve` endpoint if a name is needed.

### 2.2 `meeting_ref`

Mirrors `/me/onlineMeetings/{meeting_id}` (the canonical resource)
plus the meeting's separate `/chats/{thread_id}` chat container.

```jsonc
{
  "meeting_id":             "MSpkYzE3NjY0Mi0...",        // Graph onlineMeeting id; canonical
  "meeting_chat_thread_id": "19:meeting_xxx@thread.v2",  // chat container id
  "call_id":                "abc-123-def",                // present only while bot is in-call

  // Best-effort human-readable metadata (populated when known).
  "subject":                "Sprint planning",
  "organizer": {
    "aad_id":               "accf88ee-...",
    "display_name":         "Jane Doe"
  },
  "scheduled_start_utc":    "2026-05-14T16:00:00Z",
  "scheduled_end_utc":      "2026-05-14T16:30:00Z",

  // Optional channel link (set after the bot learns this meeting
  // belongs to a channel — see meeting.linked event).
  "channel_link": {
    "team_id":              "...",
    "team_display_name":    "Engineering",
    "channel_id":           "19:abc@thread.tacv2",
    "channel_display_name": "general",
    "thread_id":            "1700000000000",  // optional thread granularity
    "linked_at_utc":        "2026-05-14T16:01:30Z",
    "linked_source":        "bot_framework_channeldata"
  }
}
```

`meeting_id` is the stable key — it survives across the meeting chat
thread, the call instance, and the post-meeting transcript fetch.

> **Implementation note.** The bot resolves the canonical Graph
> `onlineMeeting.id` via `GET /chats/{thread_id}` → `joinWebUrl` →
> `GET /users/{organizer}/onlineMeetings?$filter=joinWebUrl eq '...'`.
> When that two-hop resolution fails (organizer not visible to the
> app, expired meeting, Graph transient), the bot falls back to using
> the chat thread id as `meeting_id` and **logs a warning**. Consumers
> that group strictly by `meeting_id` will see two buckets for the
> same meeting in that case. The fallback is observable: a `meeting.*`
> envelope whose `meeting_id` starts with `19:` and `@thread.v2`
> instead of being URL-safe base64. The blob layout under
> `meetings/{meeting_id}/` reflects the same id used in the envelope.

### 2.3 Transport

- `POST application/json`, one envelope per request (not batched).
- 2xx = accepted. 5xx / 408 / 429 = retry (up to 3 attempts, exponential
  backoff 250ms / 1s / 4s). 4xx (other) = permanent drop.
- Per-consumer queue: bounded 1000 entries, drop-oldest.

---

## 3. Event types

### Channel events (`channel.*`)

Requires `channel_ref` with `team_id` + `channel_id` + `thread_id`
populated. `message_id` is required for `.message.*` events.

| Event type                  | Required `channel_ref` fields                          | Payload §  |
|-----------------------------|--------------------------------------------------------|------------|
| `channel.attached`          | `team_id`, `channel_id`                                | §3.1       |
| `channel.detached`          | `team_id`, `channel_id`                                | §3.1       |
| `channel.message.created`   | `team_id`, `channel_id`, `thread_id`, `message_id`     | §3.2       |
| `channel.message.updated`   | `team_id`, `channel_id`, `thread_id`, `message_id`     | §3.2       |
| `channel.message.deleted`   | `team_id`, `channel_id`, `thread_id`, `message_id`     | §3.2       |

### Meeting events (`meeting.*`)

Requires `meeting_ref` with `meeting_id` populated.

| Event type                       | Required `meeting_ref` fields                          | Payload § |
|----------------------------------|--------------------------------------------------------|-----------|
| `meeting.created`                | `meeting_id`, `meeting_chat_thread_id`                 | §3.3      |
| `meeting.linked`                 | `meeting_id`, `channel_link`                           | §3.4      |
| `meeting.call.joined`            | `meeting_id`, `call_id`                                | §3.5      |
| `meeting.call.left`              | `meeting_id`, `call_id`                                | §3.5      |
| `meeting.chat.created`           | `meeting_id`, `meeting_chat_thread_id`                 | §3.6      |
| `meeting.chat.updated`           | `meeting_id`, `meeting_chat_thread_id`                 | §3.6      |
| `meeting.chat.deleted`           | `meeting_id`, `meeting_chat_thread_id`                 | §3.6      |
| `meeting.transcript.partial`     | `meeting_id`                                           | §3.7      |
| `meeting.transcript.final`       | `meeting_id`                                           | §3.7      |
| `meeting.transcript.official`    | `meeting_id`                                           | §3.8      |
| `meeting.ended`                  | `meeting_id`                                           | §3.3      |

> **Channel meetings have no separate chat container.** A channel
> meeting's "chat" IS the channel thread — Microsoft Graph does not
> create a `chatType: "meeting"` chat for channel meetings (the
> `chatType` enum is `oneOnOne | group | meeting`, and meeting-type
> chats only exist for private meetings). Channel meetings surface
> exclusively as `channel.message.*` events on the parent channel
> thread. No `meeting.chat.*` events are emitted for them, and the
> `meeting.*` family as a whole exists only for **private meetings
> the bot was added to via `+ Apps` in the meeting chat**. Consumers
> that want "the chat from a channel meeting" should read the channel
> thread's `channel.message.*` events for the time window of the
> meeting (use `meeting.created` / `meeting.ended` on linked meetings
> as a cue) — there is no separate stream.

### 3.1 `channel.attached` / `channel.detached`

```jsonc
{
  "installed_by":         "<aad object id>",
  "installation_id":      "<teams app installation id>",
  "membership_type":      "standard"   // | "private" | "shared"
}
```

### 3.2 `channel.message.*`

```jsonc
{
  "sender": {
    "aad_id":             "accf88ee-...",
    "display_name":       "Logan Robbins",
    "kind":               "user"   // | "bot" | "application"
  },
  "text":                 "plain text body",
  "html":                 "<div>...</div>",
  "timestamp_utc":        "2026-05-14T18:23:45.401Z",
  "reply_to_message_id":  null,            // populated if this is a reply within the thread
  "is_root":              true,            // true iff message_id == thread_id (this is the thread's root post)
  "from_bot":             false,
  "attachments":          [ /* see §3.9 */ ],
  "mentions":             [ /* raw Bot Framework / Graph mentions */ ],
  "raw":                  { /* full source payload, best-effort */ }
}
```

For `channel.message.updated`: same shape; `text` / `html` reflect
the latest version.

For `channel.message.deleted`: `text` and `html` may be null;
`timestamp_utc` is the deletion time.

### 3.3 `meeting.created` / `meeting.ended`

Emitted when the bot first sees a meeting (`created`) and when it
detects the meeting ended (`ended`, via Graph or call-state).

```jsonc
{
  "subject":              "Sprint planning",
  "organizer": {
    "aad_id":             "...",
    "display_name":       "..."
  },
  "scheduled_start_utc":  "2026-05-14T16:00:00Z",
  "scheduled_end_utc":    "2026-05-14T16:30:00Z",
  "actual_start_utc":     "2026-05-14T16:01:12Z",   // ended only
  "actual_end_utc":       "2026-05-14T16:34:55Z"    // ended only
}
```

### 3.4 `meeting.linked`

The bot learned this meeting belongs to a channel (and optionally a
specific thread within it). The `channel_link` block on `meeting_ref`
is now populated. Consumers should treat this as authoritative and
backfill any prior `meeting.*` events for this `meeting_id` under
the linked channel.

```jsonc
{
  "linked_source":        "bot_framework_channeldata"   // | "manual_command" | "auto_detect"
}
```

### 3.5 `meeting.call.joined` / `meeting.call.left`

```jsonc
{
  "join_url":             "https://teams.microsoft.com/l/meetup-join/...",
  "join_mode":            "graph_join"   // | "policy_auto_invite" | "invite_and_graph_join"
}
```

### 3.6 `meeting.chat.*`

Almost the same shape as `channel.message.*` (§3.2), but **without
`is_root`** (meeting chat has no thread-root concept) and **with a
top-level `message_id` on the payload** (channel messages carry
their `message_id` on `channel_ref` instead — meeting chats don't
have a comparable ref-level slot):

```jsonc
{
  "message_id":           "1779129648463",  // required
  "sender": {
    "aad_id":             "accf88ee-...",
    "display_name":       "Jane Doe",
    "kind":               "user"   // | "bot" | "application"
  },
  "text":                 "plain text body",
  "html":                 "<div>...</div>",
  "timestamp_utc":        "2026-05-14T18:23:45.401Z",
  "reply_to_message_id":  null,
  "from_bot":             false,
  "attachments":          [ /* see §3.9 */ ],
  "mentions":             [ /* raw Bot Framework / Graph mentions */ ],
  "raw":                  { /* full source payload, best-effort */ }
}
```

### 3.7 `meeting.transcript.partial` / `meeting.transcript.final`

Real-time STT output from the bot's media stream.

```jsonc
{
  "text":                 "the transcribed text",
  "timestamp_utc":        "2026-05-14T16:34:12.184Z",
  "speaker": {
    "id":                 "speaker_0",         // STT-provider speaker label
    "aad_id":             "accf88ee-...",      // resolved when MSI ↔ AAD lookup succeeds
    "display_name":       "Jane Doe"
  },
  "audio_start_ms":       1234.5,
  "audio_end_ms":         5678.9,
  "confidence":           0.94,
  "words":                [ /* word-level detail with timestamps */ ],
  "media_source": {
    "dominant_id":        4096,
    "active_ids":         [4096, 4112]
  },
  "provider": {
    "name":               "azure_speech",
    "model":              null,
    "session_id":         "..."
  }
}
```

### 3.8 `meeting.transcript.official`

Microsoft's post-meeting transcript, fetched via Graph after the
meeting ends. **Private chat meetings only** —
`OnlineMeetingTranscript.Read.Chat` does not apply to channel
meetings per Microsoft (confirmed in MS Learn v1.0 docs for
`Get callTranscript`, `List transcripts`, and all
`/transcripts` change-notification subscription pages).

> **Channel-meeting transcript escape hatch.** Microsoft v1.0 docs
> note that `GET /me/onlineMeetings/{meeting_id}/transcripts` is
> *also* available to "users who are part of the meeting calendar
> invite, which applies to both private chat meetings AND channel
> meetings." That is a **delegated user-token** path — it does not
> use our RSC app permission, so this bot cannot call it on its own.
> A downstream consumer that has a calendar-invitee's delegated
> token can fetch channel-meeting transcripts directly from Graph.

```jsonc
{
  "transcript_id":        "MSMjMCMjOWNlOTU2YjAtY...",  // required — Graph callTranscript id
  "organizer_oid":        "accf88ee-...",               // optional — AAD id of the organizer scoping the fetch
  "fetched_at_utc":       "2026-05-14T16:36:42Z",       // required
  "created_at_utc":       "2026-05-14T16:35:01Z",       // optional — Graph createdDateTime on the transcript
  "vtt_url":              "meetings/<meeting_id>/transcripts/official.vtt",  // required
  "cue_count":            142,                          // required — len(cues)
  "cues": [
    {
      "start_ms":         1200,
      "end_ms":           3450,
      "speaker": {
        "aad_id":         "...",
        "display_name":   "..."
      },
      "text":             "..."
    }
  ]
}
```

The raw WebVTT is in the blob archive at the `vtt_url` path. `cues`
is the parsed form for consumers that prefer JSON.

### 3.9 Attachment shape (channel + meeting chat)

```jsonc
{
  "attachment_id":        "<graph drive item id | bot framework id>",
  "name":                 "design.pdf",
  "content_type":         "application/pdf",
  "size_bytes":           184320,
  "graph_drive_item_id":  "01XYZ...",
  "download_url":         "https://...",
  "blob_archive_path":    "teams/{tid}/channels/{cid}/threads/{thrid}/attachments/01XYZ...pdf"
}
```

The bot fetches and mirrors attachments to the blob archive when
possible; `blob_archive_path` is populated after the mirror succeeds.

---

## 4. Routing tables (quick reference for consumers)

Every event lands at exactly one blob path. The folder segment is a
**logical category** — one folder per data type — not the verbatim
`event_type`. The precise `event_type` stays on the envelope JSON,
so consumers that need to distinguish `created` from `updated` /
`deleted` read it from the envelope.

The four categories collapse the v2 `event_type` variants. Folder
names mirror Microsoft Graph sub-resources where one exists
(`messages/` → Graph `/messages`, `transcripts/` → Graph
`/transcripts`); `live_transcript/` and `lifecycle/` are our own
labels because Graph has no equivalent sub-resource:

| Category | Aggregates these `event_type` values | Graph mirror |
|---|---|---|
| `messages` | `channel.message.{created,updated,deleted}`, `meeting.chat.{created,updated,deleted}` | `/teams/{id}/channels/{id}/messages`, `/chats/{id}/messages` |
| `live_transcript` | `meeting.transcript.{partial,final}` | none — this bot's Azure Speech STT of the real-time audio stream |
| `transcripts` | `meeting.transcript.official` (sits alongside the flat `official.txt` / `official.vtt`) | `/onlineMeetings/{id}/transcripts` (callTranscript resource) |
| `lifecycle` | `channel.{attached,detached}`, `meeting.{created,ended,linked,call.joined,call.left}` | none |

```
event_type                        → present block  → blob path
─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
channel.attached                  → channel_ref     → teams/{tid}/channels/{cid_sanitized}/lifecycle/{ts}-{eid}.json
channel.detached                  → channel_ref     → teams/{tid}/channels/{cid_sanitized}/lifecycle/{ts}-{eid}.json
channel.message.created           → channel_ref     → teams/{tid}/channels/{cid_sanitized}/messages/{ts}-{eid}.json
channel.message.updated           → channel_ref     → teams/{tid}/channels/{cid_sanitized}/messages/{ts}-{eid}.json
channel.message.deleted           → channel_ref     → teams/{tid}/channels/{cid_sanitized}/messages/{ts}-{eid}.json
meeting.created                   → meeting_ref     → meetings/{meeting_id}/lifecycle/{ts}-{eid}.json
meeting.ended                     → meeting_ref     → meetings/{meeting_id}/lifecycle/{ts}-{eid}.json
meeting.linked                    → meeting_ref     → meetings/{meeting_id}/lifecycle/{ts}-{eid}.json
meeting.call.joined               → meeting_ref     → meetings/{meeting_id}/lifecycle/{ts}-{eid}.json
meeting.call.left                 → meeting_ref     → meetings/{meeting_id}/lifecycle/{ts}-{eid}.json
meeting.chat.created              → meeting_ref     → meetings/{meeting_id}/messages/{ts}-{eid}.json
meeting.chat.updated              → meeting_ref     → meetings/{meeting_id}/messages/{ts}-{eid}.json
meeting.chat.deleted              → meeting_ref     → meetings/{meeting_id}/messages/{ts}-{eid}.json
meeting.transcript.partial        → meeting_ref     → meetings/{meeting_id}/live_transcript/{ts}-{eid}.json
meeting.transcript.final          → meeting_ref     → meetings/{meeting_id}/live_transcript/{ts}-{eid}.json
meeting.transcript.official       → meeting_ref     → meetings/{meeting_id}/transcripts/{ts}-{eid}.json
```

Plus the two flat post-meeting transcript files (overwritten on each
fetch — these live alongside the `meeting.transcript.official`
envelope in the same `transcripts/` folder):

```
meetings/{meeting_id}/transcripts/official.txt   (clean speaker-per-line plaintext)
meetings/{meeting_id}/transcripts/official.vtt   (raw WebVTT)
```

> **Legacy v1 compat path (preserved, do not consume from new code):**
> The bot also writes `channels/{tid}/{cid_sanitized}/chat.message/{ts}-{eid}.txt`
> for channels listed in `BlobArchive:V1CompatChannelIds`. That path
> uses the legacy `alfred-events-v1` body (header + `---ENVELOPE---`
> separator) and exists only to keep the pre-v2 `server.py` polling
> bridge working through the cutover. New consumers must read the v2
> category layout above.

> **Historical event-type-per-folder blobs.** Blobs written before
> this category refactor live at their old `…/{event_type}/…` paths
> (e.g. `meetings/{mid}/meeting.chat.created/`). They are still
> readable; new writes land at the category paths. No backfill.

See [`docs/retrieving-transcripts.md`](retrieving-transcripts.md) for
recipes + Python helpers.

---

## 5. Posting back to chat

`POST $BOT/api/send-chat`. The send-chat surface is unchanged from
v1; it takes a `conversation_reference_id` (echoed from any envelope)
and the message text. See `src/Controllers/SendChatController.cs` for
the canonical request/response shape.

---

## 6. Registering consumers

**Channel scope is the only scope the bot currently implements.** A
channel-scope consumer receives all `channel.*` events for that
channel AND all `meeting.*` events for meetings linked to that
channel (via the `meeting.linked` event or the
`@Alfred link to <channel-name>` chat command):

```bash
curl -X POST $BOT/api/channels/$TEAM/$CHAN/consumers \
  -H "Content-Type: application/json" \
  -d '{"name":"team-a","url":"https://...","event_kinds":["*"],"enabled":true}'
```

The body field is **`event_kinds`** (matched against
`AlfredEventEnvelope.event_type` in `EventFanoutDispatcher`).
`["*"]` accepts everything. Otherwise filter to specific event
types verbatim — no wildcards inside a type. Sending `event_types`
instead silently produces an empty filter (matches all by default),
which is rarely what you want.

There is no separate per-meeting consumer endpoint; if you need a
narrower stream than a channel, register multiple channel-scope
consumers with disjoint `event_kinds` filters.

Management endpoints (all under `/api/channels/{team}/{channel}/consumers`):

| Method | Body / params | Behavior |
|---|---|---|
| `GET` | — | List current consumers for the channel |
| `POST` | `{name, url, event_kinds, enabled, headers?}` | Upsert one consumer by name |
| `PUT` | `{consumers: [...]}` | Replace the entire list |
| `DELETE /{consumerName}` | — | Remove one by name |

When the consumer list for a channel is empty, the
`EventDispatch.BootstrapConsumerUrl` (configured per-deployment)
receives events as a fallback. To suppress that fallback for a
specific channel without re-pointing the bootstrap, register a
placeholder with `enabled:false` (see README §7.4).

---

## 7. Reference consumer

The Python sink at `python/transcript_sink.py` consumes this contract
at `POST /v2/events` and exposes the hierarchical query API
described in [`docs/retrieving-transcripts.md §3`](retrieving-transcripts.md).
Other teams' backends are free to implement their own consumer —
only the envelope shape and the consumer-registration / send-chat
surfaces are stable.
