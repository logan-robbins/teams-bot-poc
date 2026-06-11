# Alfred Event Contract — `alfred-v2`

Canonical reference for the `alfred-v2` event envelope. The C# bot emits one envelope per event: channel chat under `channel.*`, meeting audio + chat under `meeting.*`. Envelope shape mirrors the Microsoft Graph URL hierarchy.

Consumers have two rails:

1. Register a webhook URL with `POST $BOT/api/channels/{teamId}/{channelId}/consumers` for live push delivery.
2. Read the blob archive for replay, bulk export, and offline processing (see `docs/retrieving-transcripts.md`).

The Python sink is the built-in consumer for our Alfred implementation. A client-owned Alfred can be any service that receives these envelopes and optionally calls `$BOT/api/send-chat` to interact in Teams; `server_v2.py` is a local example of that pattern.

---

## 0. Publisher Rails

```
Teams activity / Graph notification / media frame
                  |
                  v
          C# bot builds one alfred-v2 envelope
                  |
                  v
          EventFanoutDispatcher.PublishAsync
                  |
        +---------+----------+------------------+
        |                    |                  |
        v                    v                  v
 local VM audit       BlobEventArchive     HTTP consumer POSTs
 NDJSON               PUT .json blob       to registered URLs
                                           usually /v2/events
```

Blob writes and HTTP consumer POSTs are sibling outputs from the C# bot. The bot does not post only to `/events`; it POSTs to every configured consumer URL, and the URL may be `/v2/events`, `/events`, or any path that consumer owns. The blob archive is written by the C# bot, not by the Python sink.

`meeting.transcript.partial` is throttled before blob + HTTP delivery by `EventDispatch:PartialThrottleSeconds` to avoid flooding consumers. Final transcript chunks, chat, lifecycle, and official transcript events are delivered normally.

---

## 1. Versioning

- Current: `"schema_version": "alfred-v2"`.
- v1 (`alfred-events-v1`) is dead. No back-compat shims on the sink. See [§7 Sidecar clients and v1 compat](#7-sidecar-clients-and-v1-compat) for the local sidecar carve-out.
- Within v2: additive only. New optional fields and new event types are allowed. Breaking changes ship as `v3`.

---

## 2. Envelope

```jsonc
{
  "schema_version": "alfred-v2",
  "event_type":     "meeting.transcript.final",
  "event_id":       "8a3f1c0e2b9d4a7e9f12bb0001020304",
  "ts":             "2026-05-14T16:34:12.184Z",

  // Exactly one of these two blocks is populated.
  "channel_ref":    null,
  "meeting_ref":    { /* §2.2 */ },

  // Optional. Echo to /api/send-chat to reply in the source thread.
  "conversation_reference_id": "<bot-framework conv ref id>",

  "payload":        { /* §3 */ }
}
```

- `channel_ref` populated iff `event_type` starts with `channel.`.
- `meeting_ref` populated iff `event_type` starts with `meeting.`.
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
  "message_id":           "1700000000123"    // required on *.message.*; null on system events
}
```

Display names are best-effort. Resolved via `GET /teams/{id}` and `GET /teams/{id}/channels/{id}` on first sighting; cached. Fall back to ids and `/v2/resolve` if a name is needed.

### 2.2 `meeting_ref`

```jsonc
{
  "meeting_id":             "19:meeting_NmFkYW...@thread.v2",  // see §2.2.1
  "meeting_chat_thread_id": "19:meeting_NmFkYW...@thread.v2",  // same value as meeting_id
  "call_id":                "abc-123-def",                      // present only while bot is in-call

  "subject":                "Sprint planning",
  "organizer": {
    "aad_id":               "accf88ee-...",
    "display_name":         "Jane Doe"
  },
  "scheduled_start_utc":    "2026-05-14T16:00:00Z",
  "scheduled_end_utc":      "2026-05-14T16:30:00Z",

  // Optional channel back-reference, set after meeting.linked.
  "channel_link": {
    "team_id":              "...",
    "team_display_name":    "Engineering",
    "channel_id":           "19:abc@thread.tacv2",
    "channel_display_name": "general",
    "thread_id":            "1700000000000",
    "linked_at_utc":        "2026-05-14T16:01:30Z",
    "linked_source":        "bot_framework_channeldata"
  }
}
```

#### 2.2.1 `meeting_id`

> **`meeting_id` is the Teams chat thread id of the meeting (`19:meeting_<base64>@thread.v2`).**

This is the stable key for everything meeting-scoped: the live transcript stream, the meeting chat, the official transcript blobs, the sink's `/v2/meetings/{meeting_id}/...` endpoints, and the consumer dossier. `meeting_chat_thread_id` carries the same value and exists only because the field name reads more naturally inside chat-payload contexts.

Microsoft Graph exposes a separate identifier called `onlineMeeting.id` (returned by `GET /me/onlineMeetings`). The bot uses that identifier internally only when it calls `/onlineMeetings/{id}/transcripts` to retrieve the post-meeting Microsoft transcript. `onlineMeeting.id` is never written to an envelope, never written to a blob path, and never accepted as a key by the sink. Consumers key on `meeting_id` (the chat thread id) — that is the contract.

### 2.3 Transport

- `POST application/json`, one envelope per request (not batched), to each configured consumer URL.
- 2xx accepted. 5xx / 408 / 429 retry up to 3 with backoff 250ms / 1s / 4s. Other 4xx drops permanently.
- Per-consumer queue: bounded 1000 entries, drop-oldest.

---

## 3. Event types

### Channel events

| Event type                  | Required `channel_ref` fields                          | Payload § |
|-----------------------------|--------------------------------------------------------|-----------|
| `channel.attached`          | `team_id`, `channel_id`                                | §3.1      |
| `channel.detached`          | `team_id`, `channel_id`                                | §3.1      |
| `channel.message.created`   | `team_id`, `channel_id`, `thread_id`, `message_id`     | §3.2      |
| `channel.message.updated`   | `team_id`, `channel_id`, `thread_id`, `message_id`     | §3.2      |
| `channel.message.deleted`   | `team_id`, `channel_id`, `thread_id`, `message_id`     | §3.2      |

### Meeting events

| Event type                       | Required `meeting_ref` fields            | Payload § |
|----------------------------------|------------------------------------------|-----------|
| `meeting.created`                | `meeting_id`, `meeting_chat_thread_id`   | §3.3      |
| `meeting.linked`                 | `meeting_id`, `channel_link`             | §3.4      |
| `meeting.call.joined`            | `meeting_id`, `call_id`                  | §3.5      |
| `meeting.call.left`              | `meeting_id`, `call_id`                  | §3.5      |
| `meeting.chat.created`           | `meeting_id`, `meeting_chat_thread_id`   | §3.6      |
| `meeting.chat.updated`           | `meeting_id`, `meeting_chat_thread_id`   | §3.6      |
| `meeting.chat.deleted`           | `meeting_id`, `meeting_chat_thread_id`   | §3.6      |
| `meeting.transcript.partial`     | `meeting_id`                             | §3.7      |
| `meeting.transcript.final`       | `meeting_id`                             | §3.7      |
| `meeting.transcript.official`    | `meeting_id`                             | §3.8      |
| `meeting.ended`                  | `meeting_id`                             | §3.3      |

The `meeting.*` family exists only for private meetings the bot was added to via `+ Apps`. Channel meetings surface as `channel.message.*` on the parent thread — Graph does not create a `chatType: "meeting"` chat for channel meetings, and no `meeting.chat.*` events are emitted for them. To reconstruct a channel meeting's "chat," read the channel thread's `channel.message.*` events for the meeting's time window (use `meeting.created` / `meeting.ended` on linked meetings as cues).

### 3.1 `channel.attached` / `channel.detached`

```jsonc
{
  "installed_by":    "<aad object id>",
  "installation_id": "<teams app installation id>",
  "membership_type": "standard"   // | "private" | "shared"
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
  "reply_to_message_id":  null,           // populated if this is a reply within the thread
  "is_root":              true,           // true iff message_id == thread_id
  "from_bot":             false,
  "attachments":          [ /* §3.9 */ ],
  "mentions":             [ /* raw Bot Framework / Graph mentions */ ],
  "raw":                  { /* source payload, best-effort */ }
}
```

For `.updated`: same shape, latest text/html. For `.deleted`: text/html may be null; `timestamp_utc` is deletion time.

### 3.3 `meeting.created` / `meeting.ended`

```jsonc
{
  "subject":              "Sprint planning",
  "organizer":            { "aad_id": "...", "display_name": "..." },
  "scheduled_start_utc":  "2026-05-14T16:00:00Z",
  "scheduled_end_utc":    "2026-05-14T16:30:00Z",
  "actual_start_utc":     "2026-05-14T16:01:12Z",  // ended only
  "actual_end_utc":       "2026-05-14T16:34:55Z"   // ended only
}
```

### 3.4 `meeting.linked`

Bot learned this meeting belongs to a channel. `channel_link` on `meeting_ref` is now populated. Consumers should backfill any prior `meeting.*` events for this `meeting_id` under the linked channel.

```jsonc
{ "linked_source": "bot_framework_channeldata" }   // | "manual_command" | "auto_detect"
```

### 3.5 `meeting.call.joined` / `meeting.call.left`

```jsonc
{
  "join_url":  "https://teams.microsoft.com/l/meetup-join/...",
  "join_mode": "graph_join"   // | "policy_auto_invite" | "invite_and_graph_join"
}
```

### 3.6 `meeting.chat.*`

Same shape as `channel.message.*` (§3.2) except no `is_root` and a top-level `message_id` on the payload (channel messages carry `message_id` on `channel_ref`):

```jsonc
{
  "message_id":           "1779129648463",
  "sender":               { "aad_id": "...", "display_name": "Jane Doe", "kind": "user" },
  "text":                 "plain text body",
  "html":                 "<div>...</div>",
  "timestamp_utc":        "2026-05-14T18:23:45.401Z",
  "reply_to_message_id":  null,
  "from_bot":             false,
  "attachments":          [ /* §3.9 */ ],
  "mentions":             [ /* raw mentions */ ],
  "raw":                  { /* source payload, best-effort */ }
}
```

### 3.7 `meeting.transcript.partial` / `meeting.transcript.final`

Real-time STT output from the bot's media stream (Azure Speech).

```jsonc
{
  "text":           "the transcribed text",
  "timestamp_utc": "2026-05-14T16:34:12.184Z",
  "speaker": {
    "id":          "speaker_0",
    "aad_id":      "accf88ee-...",
    "display_name":"Jane Doe"
  },
  "audio_start_ms": 1234.5,
  "audio_end_ms":   5678.9,
  "confidence":     0.94,
  "words":          [ /* word-level detail */ ],
  "media_source":   { "dominant_id": 4096, "active_ids": [4096, 4112] },
  "provider":       { "name": "azure_speech", "model": null, "session_id": "..." }
}
```

### 3.8 `meeting.transcript.official`

Microsoft's post-meeting transcript, fetched via Graph after the meeting ends. **Private chat meetings only** — `OnlineMeetingTranscript.Read.Chat` does not apply to channel meetings (MS Learn v1.0, `Get callTranscript` / `List transcripts`).

A delegated user token belonging to a meeting calendar-invitee can fetch channel-meeting transcripts directly from Graph via `GET /me/onlineMeetings/{meeting_id}/transcripts`. The bot's RSC app permission cannot.

```jsonc
{
  "transcript_id":  "MSMjMCMjOWNlOTU2YjAtY...",
  "organizer_oid":  "accf88ee-...",                       // optional
  "fetched_at_utc": "2026-05-14T16:36:42Z",
  "created_at_utc": "2026-05-14T16:35:01Z",               // optional
  "vtt_url":        "meetings/<meeting_id_sanitized>/transcripts/official.vtt",
  "cue_count":      142,
  "cues": [
    { "start_ms": 1200, "end_ms": 3450,
      "speaker": { "aad_id": "...", "display_name": "..." },
      "text":    "..." }
  ]
}
```

The raw WebVTT lives in the blob archive at `vtt_url`. `cues` is the parsed form for JSON consumers.

### 3.9 Attachment shape

```jsonc
{
  "attachment_id":       "<graph drive item id | bot framework id>",
  "name":                "design.pdf",
  "content_type":        "application/pdf",
  "size_bytes":          184320,
  "graph_drive_item_id": "01XYZ...",
  "download_url":        "https://...",
  "blob_archive_path":   "teams/{tid}/channels/{cid}/threads/{thrid}/attachments/01XYZ...pdf"
}
```

`blob_archive_path` is populated after the attachment mirror succeeds.

---

## 4. Blob path layout

The bot writes each envelope as one pretty-printed JSON file, keyed by scope + category + timestamp + event_id. The category folder is a logical grouping — multiple `event_type` values collapse into one folder; the precise type stays on the envelope.

### Channel scope

```
teams/{team_id_sanitized}/channels/{channel_id_sanitized}/messages/{ts}-{event_id}.json
teams/{team_id_sanitized}/channels/{channel_id_sanitized}/lifecycle/{ts}-{event_id}.json
```

### Meeting scope

```
meetings/{meeting_id_sanitized}/messages/{ts}-{event_id}.json
meetings/{meeting_id_sanitized}/live_transcript/{ts}-{event_id}.json
meetings/{meeting_id_sanitized}/transcripts/{ts}-{event_id}.json
meetings/{meeting_id_sanitized}/transcripts/official.txt
meetings/{meeting_id_sanitized}/transcripts/official.vtt
meetings/{meeting_id_sanitized}/lifecycle/{ts}-{event_id}.json
```

### Categories

| Path                                          | Receives                                                                       |
|-----------------------------------------------|--------------------------------------------------------------------------------|
| `teams/{tid}/channels/{cid}/messages/`        | `channel.message.{created,updated,deleted}`                                    |
| `teams/{tid}/channels/{cid}/lifecycle/`       | `channel.attached`, `channel.detached`                                         |
| `meetings/{mid}/messages/`                    | `meeting.chat.{created,updated,deleted}`                                       |
| `meetings/{mid}/live_transcript/`             | `meeting.transcript.{partial,final}` (Alfred's Azure Speech STT)               |
| `meetings/{mid}/transcripts/`                 | `meeting.transcript.official` envelope + flat `official.txt` / `official.vtt`  |
| `meetings/{mid}/lifecycle/`                   | `meeting.{created,ended,linked,call.joined,call.left}`                         |

No other category folders are used.

### Sanitization

Every `{*_sanitized}` segment is built by the blob writer (`BlobEventArchive` under `src/Services/`):

- Regex: `[^a-zA-Z0-9\-_.]` → `_`
- Truncated to 200 chars

`{ts}` is `yyyyMMddTHHmmssfffZ` (millisecond UTC, lexicographic = time order). `{event_id}` is the 32-char hex from the envelope; use for dedup.

Worked example — production-typical `meeting_id` (chat thread id):

```
19:meeting_NmFkYWM1NDQtYTM3ZC00ZjlmLTk4ZjItZjE0M2YwOWEzODIx@thread.v2
→
19_meeting_NmFkYWM1NDQtYTM3ZC00ZjlmLTk4ZjItZjE0M2YwOWEzODIx_thread.v2
```

Live archive prefix for that meeting (in production now):

```
https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io/archive?prefix=meetings%2F19_meeting_NmFkYWM1NDQtYTM3ZC00ZjlmLTk4ZjItZjE0M2YwOWEzODIx_thread.v2%2Flive_transcript%2F
```

Build blob URLs from the sanitized form, not the raw envelope value.

> **Historical event-type-per-folder blobs.** Blobs written before the category refactor live at their old `…/{event_type}/…` paths (e.g. `meetings/{mid}/meeting.chat.created/`). They remain readable; new writes land at the category paths. No backfill.

See `docs/retrieving-transcripts.md` for read recipes.

---

## 5. Posting back to chat

`POST $BOT/api/send-chat`. Unchanged from v1; takes a `conversation_reference_id` (echoed from any envelope) and the message text. Request/response shape is in `SendChatController` under `src/Controllers/`.

---

## 6. Registering Consumers

Channel scope is the only scope the bot implements. A channel-scope consumer receives every `channel.*` event for the channel AND every `meeting.*` event for meetings linked to that channel (via `meeting.linked` or the `@Alfred link to <channel-name>` chat command).

```bash
curl -X POST $BOT/api/channels/$TEAM/$CHAN/consumers \
  -H "Content-Type: application/json" \
  -d '{"name":"team-a","url":"https://...","event_kinds":["*"],"enabled":true}'
```

The consumer owns the URL path. Use `/v2/events` if you want to match the built-in Python sink and `server_v2.py`, but the bot treats the URL as opaque.

**Filter key is `event_kinds`** (matched against `AlfredEventEnvelope.event_type` in `EventFanoutDispatcher`). `["*"]` accepts everything. Otherwise list exact event types — no wildcards inside a type. Sending `event_types` instead produces a silently-empty filter that matches everything.

For a narrower stream than a channel, register multiple channel-scope consumers with disjoint `event_kinds`.

Management endpoints under `/api/channels/{team}/{channel}/consumers`:

| Method | Body / params | Behavior |
|---|---|---|
| `GET`                       | —                                                | List current consumers |
| `POST`                      | `{name, url, event_kinds, enabled, headers?}`    | Upsert one consumer by name |
| `PUT`                       | `{consumers: [...]}`                             | Replace the entire list |
| `DELETE /{consumerName}`    | —                                                | Remove one by name |

When the consumer list is empty, `EventDispatch.BootstrapConsumerUrl` (per-deployment config) receives events as a fallback. To suppress the fallback for one channel, register a placeholder with `enabled:false`.

---

## 7. Sidecar Clients and V1 Compat

`server_v2.py` at the repo root is a client-side bridge example. It preserves the older `/chat` API, receives live envelopes at `POST /v2/events`, can poll the current blob archive paths for catch-up, and posts back through `$BOT/api/send-chat` when its agent decides to respond. It is not part of the C# bot or the built-in Python sink; it is the shape a client can copy when they want to build their own Alfred on top of the platform rails.

`server.py` and `server_1.py` are legacy local sidecars for the v1 polling bridge. They exist only for pre-v2 compatibility comparison. The legacy compat blob path is `channels/{team_sanitized}/{cid_sanitized}/chat.message/{ts}-{eid}.txt`; new consumers should read the v2 category layout or register a live HTTP consumer.

---

## 8. Reference consumer

The Python sink at `python/transcript_sink.py` consumes this contract at `POST /v2/events`, persists its own PostgreSQL ledger, runs our Alfred agent, and exposes the hierarchical query API described in `docs/retrieving-transcripts.md` §1. Only the envelope shape, consumer registration, blob archive layout, and `$BOT/api/send-chat` surface are platform contracts. Backends can implement their own consumer.
