# Alfred Event Contract — `alfred-events-v1`

This is the canonical contract published by the Alfred bot's
`EventFanoutDispatcher`. The bot is a Teams platform that captures
audio + chat for every channel it is attached to, and POSTs every
event as a versioned JSON envelope to every consumer URL registered
against that channel.

If you are building a backend for an internal team that wants to
consume Alfred data:

1. Pick a URL on your internal network (any HTTP server, any
   language, any framework).
2. Register it against your channel via the bot's
   `POST /api/channels/{teamId}/{channelId}/consumers` endpoint.
3. Accept the envelope shape below at that URL.
4. To talk back into the channel, `POST $BOT/api/send-chat`.

That is the entire contract. The bot does not care what you do with
the data.

---

## 1. Versioning

- The current schema version is **`alfred-events-v1`**. Every
  envelope carries `"schema_version": "alfred-events-v1"`.
- **Within v1: additive only.** New optional fields may appear at any
  time. New event types may appear at any time. Existing fields
  never change shape, semantics, or required-ness inside v1.
- **Breaking changes ship as `v2`.** The bot will continue posting
  v1 to v1-registered consumers in parallel during any migration
  window.
- Consumers should:
  - Ignore unknown top-level fields silently.
  - Ignore unknown `event_type` values silently (or 200-OK them).
  - Not enforce that `schema_version == "alfred-events-v1"` exactly,
    but DO log if it differs from what they were written against.

---

## 2. Envelope shape

Every event POSTed to a consumer URL has this top-level shape:

```jsonc
{
  "schema_version": "alfred-events-v1",

  // What kind of event this is. Routes the consumer's logic.
  "event_type": "transcript.final",

  // Stable per-event id. Use it to dedupe on retry.
  "event_id": "8a3f1c0e2b9d4a7e9f12bb0001020304",

  // ISO-8601 UTC timestamp. May be the source event's time
  // (e.g. transcript ts) rather than dispatch time.
  "ts": "2026-05-07T16:34:12.184Z",

  // Routing keys. team_id + channel_id are populated whenever the
  // bot knows them. chat_thread_id is ALWAYS populated.
  "team_id":          "<team AAD group id>",   // string | null
  "channel_id":       "19:abc@thread.tacv2",   // string | null
  "chat_thread_id":   "19:meeting_xxx@thread.v2",
  "channel_thread_id":"19:abc@thread.tacv2",   // string | null
  "conversation_reference_id": "<...>",        // see §4

  // Event-type-specific payload. See §3.
  "payload": { ... }
}
```

### Routing keys

- `chat_thread_id` is the canonical session key. For meetings it is
  `19:meeting_xxx@thread.v2`. For posts in an attached channel it is
  `19:{channelId}@thread.tacv2`. Always present.
- `(team_id, channel_id)` is populated whenever the bot can determine
  it. For channel posts that's always; for meeting threads that
  haven't been linked to a parent channel yet, both will be null.
- `channel_thread_id` is the parent channel's conversation id when
  this event is from a meeting that was spawned from a channel. It
  lets you roll meetings up under their channel offline. For events
  in the channel itself, it equals `chat_thread_id`.
- `conversation_reference_id` is the Bot Framework conversation
  reference id. Echo it to `POST $BOT/api/send-chat` to reply (§4).

### Transport

- Method: `POST`
- Content type: `application/json`
- Body: a single envelope (NOT an array; one POST per event).
- Auth: by default none; the bot is internal-only and protects
  consumer URLs at the network layer. Optional headers can be
  configured per-consumer in `ConsumerConfig.headers` if a consumer
  wants e.g. a shared bearer.
- The bot expects 2xx for success. Any 4xx (except 408/429) is
  treated as a permanent failure for that envelope and the bot will
  drop it without retry. 5xx, 408, and 429 trigger up to 3 attempts
  with exponential backoff (250ms, 1s, 4s) before drop.
- Slow consumers do not block fast ones — the bot maintains a
  bounded per-consumer queue (capacity 1000) with drop-oldest
  semantics.

---

## 3. Event types

### `transcript.partial`

In-progress STT hypothesis. Often updated mid-utterance.

`payload` shape: see [§3.1 Transcript payload](#31-transcript-payload).

### `transcript.final`

Finalized STT utterance. The canonical truth for "someone said X".

`payload` shape: see [§3.1 Transcript payload](#31-transcript-payload).

### `chat.message`

A user (or bot) chat message in a meeting chat or attached channel.

`payload` shape: see [§3.2 Chat payload](#32-chat-payload).

### `system.session_linked`

The bot just learned that a meeting `chat_thread_id` belongs to
`(team_id, channel_id)`. Useful for backfilling prior events under
the parent channel.

`payload`:
```json
{
  "chat_thread_id":   "19:meeting_xxx@thread.v2",
  "team_id":          "<team AAD group id>",
  "channel_id":       "19:abc@thread.tacv2",
  "channel_thread_id":"19:abc@thread.tacv2",
  "source":           "bot_framework_channeldata"
}
```

### `system.channel_attached` *(reserved)*

Fired when the bot is attached to a channel. May be empty or
informational; no consumer logic should depend on it.

### `system.channel_detached` *(reserved)*

Fired when the bot is detached from a channel.

### `transcript.session_*`, `transcript.error`

STT-internal lifecycle events are intentionally **not** emitted as
envelopes in v1. They are infrastructure noise; consumers receive
only `transcript.partial` and `transcript.final`.

---

### 3.1 Transcript payload

```jsonc
{
  // "partial" | "final" — duplicates the envelope event_type but
  // present here for back-compat with the legacy /transcript shape.
  "event_type": "final",
  "text":       "the actual transcribed text",
  "timestamp_utc":   "2026-05-07T16:34:12.184Z",
  "chat_thread_id":  "19:meeting_xxx@thread.v2",

  // Diarization (if STT provider supports it, otherwise null).
  "speaker_id":      "speaker_0",
  "audio_start_ms":  1234.5,
  "audio_end_ms":    5678.9,
  "confidence":      0.94,
  "words":           [ /* word details with timestamps */ ],

  // Routing context (also on the envelope).
  "team_id":           "...",
  "channel_id":        "...",
  "channel_thread_id": "...",

  // E3 identity hints (Teams MediaSourceId at publish time).
  "dominant_media_source_id":  4096,
  "active_media_source_ids":   [4096, 4112],

  // Provider metadata.
  "metadata": { "provider": "azure_speech", "model": null,
                "session_id": "..." }
}
```

Stable: `event_type`, `text`, `timestamp_utc`, `chat_thread_id`,
`speaker_id`, routing-key fields. Everything else is best-effort and
may be null depending on STT provider + media path.

### 3.2 Chat payload

```jsonc
{
  // "chat_created" | "chat_updated" | "chat_deleted"
  "event_type":        "chat_created",
  "chat_thread_id":    "19:meeting_xxx@thread.v2",
  "message_id":        "<bot-framework-activity-id>",
  "text":              "plain text body",
  "html":              "<div>...</div>",   // when available
  "sender_id":         "<aad object id or app id>",
  "sender_display_name":"Jane Doe",
  "timestamp_utc":     "2026-05-07T16:34:00.000Z",

  "conversation_reference_id": "<echo to /api/send-chat>",
  "reply_to_message_id":       "<id, when this is a reply>",
  "from_bot":                  false,

  "attachments": [ /* raw Bot Framework attachments */ ],
  "mentions":    [ /* raw Bot Framework mentions */ ],
  "raw":         { /* full source payload — best effort */ },

  // Source classification.
  "conversation_kind": "meeting_chat",  // | "channel" | "group_chat" | "personal"

  // Routing context (also on envelope).
  "team_id":           "...",
  "channel_id":        "...",
  "channel_thread_id": "..."
}
```

---

## 4. Posting back to the channel

When your consumer wants Alfred (or anyone) to post a message into
the chat, `POST` to the bot's send-chat endpoint:

```bash
BOT=https://alfred-disney-bot.eastus.cloudapp.azure.com

curl -sS -X POST $BOT/api/send-chat \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
        --arg ref "$CONVERSATION_REFERENCE_ID_FROM_ENVELOPE" \
        --arg text "$YOUR_MESSAGE" \
        '{conversation_reference_id:$ref, text:$text}')"
```

Body:

```jsonc
{
  // Required. Echo the envelope's conversation_reference_id.
  "conversation_reference_id": "...",

  // Required. The message text.
  "text": "Hello from Team A's backend",

  // Optional.
  "reply_to_message_id": "<message id to reply to>",
  "rationale":           "free-form audit string",
  "session_id":          "your internal id",
  "product_id":          "your product id",
  "instance_id":         "your instance id",
  "action":              "tag for de-dupe key (e.g. 'summary')"
}
```

Notes:
- The bot must have seen at least one chat activity in that thread
  before this works (otherwise it has no `ConversationReference` for
  it). For attached channels and active meetings this is always
  true.
- The bot rate-limits to 8 RPS per chat thread server-side.
  Duplicate `(text, action, reply_to_message_id)` within 20 seconds
  is silently de-duplicated.

This endpoint pre-dates the envelope contract and is unchanged.

---

## 5. Registering your URL

You manage your channel's consumer list directly on the bot:

```bash
BOT=https://alfred-disney-bot.eastus.cloudapp.azure.com
TEAM=<team AAD group id>
CHAN=19:abc@thread.tacv2
ENC_CHAN=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$CHAN")

# List
curl -sS "$BOT/api/channels/$TEAM/$ENC_CHAN/consumers"

# Replace
curl -sS -X PUT "$BOT/api/channels/$TEAM/$ENC_CHAN/consumers" \
  -H "Content-Type: application/json" \
  -d '{
    "consumers": [
      {
        "name": "team-a",
        "url": "https://team-a.internal/alfred-events",
        "event_kinds": ["*"],
        "enabled": true
      }
    ]
  }'

# Add or replace one by name
curl -sS -X POST "$BOT/api/channels/$TEAM/$ENC_CHAN/consumers" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "team-b-summarizer",
    "url": "https://team-b.internal/sink",
    "event_kinds": ["transcript.final", "chat.message"],
    "headers": {"X-Team": "B"},
    "enabled": true
  }'

# Remove one by name
curl -sS -X DELETE "$BOT/api/channels/$TEAM/$ENC_CHAN/consumers/team-a"
```

`event_kinds: ["*"]` accepts every event. To narrow, list specific
event types: `["transcript.final", "chat.message"]`.

---

## 6. Reference consumer

This repo's Python sink (`python/transcript_sink.py`) implements the
contract at its `POST /events` endpoint. Use it as a reference if
you want to see the contract exercised end-to-end. Other teams'
backends are free to ignore that implementation entirely — only the
envelope shape and `/api/send-chat` are stable.
