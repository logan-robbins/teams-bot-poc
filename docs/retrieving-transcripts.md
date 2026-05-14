# Blob archive — `alfred-v2` layout

The Alfred bot mirrors every event into Azure Blob Storage as
individual JSON blobs, organized as a **direct mirror of the
Microsoft Graph URL hierarchy**. Anyone with the team's group id
(or a meeting's id) can read events directly with HTTP GET — no
API key, no SDK, no per-team auth.

This document is the contract for the read path. The event schema
itself is in [`docs/event-contract.md`](event-contract.md).

---

## 1. Where the data lives

|                  |                                                                  |
|------------------|------------------------------------------------------------------|
| Storage account  | `stalfreddisney`                                                 |
| Container        | `alfred-events`                                                  |
| Endpoint         | `https://stalfreddisney.blob.core.windows.net/alfred-events/`    |
| Auth (read)      | Anonymous public read (container ACL = `container`)              |
| Auth (write)     | Bot / sink managed identities only                               |
| Region           | `eastus`                                                         |
| Subscription     | `e02c0038-82c8-4655-9647-38083f301099` (WDI R&D)                 |

Anonymous read works from any browser, any region. If that ever
changes you'll see `403 AuthenticationFailed`; access then requires
the WDI VNet.

---

## 2. Path layout

### 2.1 Channel side — mirrors `/teams/{tid}/channels/{cid}/messages/{thrid}/replies/{mid}`

```
teams/{team_id}/meta.json
teams/{team_id}/channels/{sanitized_channel_id}/meta.json
teams/{team_id}/channels/{sanitized_channel_id}/threads/{thread_id}/meta.json
teams/{team_id}/channels/{sanitized_channel_id}/threads/{thread_id}/messages/{utc_ts}-{message_id}.json
teams/{team_id}/channels/{sanitized_channel_id}/threads/{thread_id}/attachments/{attachment_id}.{ext}
teams/{team_id}/channels/{sanitized_channel_id}/system/{utc_ts}-{event_id}.json
```

`system/` holds `channel.attached` / `channel.detached` envelopes.

### 2.2 Meeting side — mirrors `/me/onlineMeetings/{meeting_id}` + the meeting chat container

```
meetings/{meeting_id}/meta.json
meetings/{meeting_id}/chat/messages/{utc_ts}-{message_id}.json
meetings/{meeting_id}/chat/attachments/{attachment_id}.{ext}
meetings/{meeting_id}/system/{utc_ts}-{event_id}.json
meetings/{meeting_id}/transcripts/partial/{utc_ts}-{event_id}.json
meetings/{meeting_id}/transcripts/final/{utc_ts}-{event_id}.json
meetings/{meeting_id}/transcripts/official.vtt
meetings/{meeting_id}/transcripts/official.json
```

`system/` holds `meeting.created`, `meeting.ended`, `meeting.linked`,
`meeting.call.joined`, `meeting.call.left` envelopes.

### 2.3 Indexes — the name → id lookup surface

```
indexes/teams.json        # { team_id: { display_name, last_seen_utc } }
indexes/channels.json     # { channel_id: { team_id, display_name, membership_type, last_seen_utc } }
indexes/meetings.json     # { meeting_id: { subject, organizer, scheduled_start_utc, scheduled_end_utc,
                          #                 actual_start_utc, actual_end_utc, channel_link? } }
```

These are **rewritten in full** on every change (small files, low
cadence — single-digit kB each at our scale). They are the
authoritative answer to "what's in this archive, by name". An AI
agent doing first-time discovery should GET `indexes/meetings.json`,
not list the container.

### 2.4 Per-entity `meta.json`

Each `meta.json` is the human-readable face of an id:

```jsonc
// teams/{team_id}/meta.json
{
  "team_id":          "d3f5f412-...",
  "display_name":     "Engineering",
  "first_seen_utc":   "2026-05-01T00:00:00Z",
  "last_seen_utc":    "2026-05-14T18:00:00Z"
}

// teams/{team_id}/channels/{sanitized_channel_id}/meta.json
{
  "team_id":          "d3f5f412-...",
  "channel_id":       "19:abc@thread.tacv2",
  "display_name":     "general",
  "membership_type":  "standard",
  "first_seen_utc":   "...",
  "last_seen_utc":    "..."
}

// teams/{team_id}/channels/{cid}/threads/{thread_id}/meta.json
{
  "thread_id":        "1700000000000",
  "root_message_preview":  "Anyone seen the deploy logs?",
  "started_at_utc":   "...",
  "last_activity_utc":"..."
}

// meetings/{meeting_id}/meta.json
{
  "meeting_id":             "MSpkYzE3...",
  "meeting_chat_thread_id": "19:meeting_xxx@thread.v2",
  "subject":                "Sprint planning",
  "organizer": {
    "aad_id":               "...",
    "display_name":         "Jane Doe"
  },
  "scheduled_start_utc":    "2026-05-14T16:00:00Z",
  "scheduled_end_utc":      "2026-05-14T16:30:00Z",
  "actual_start_utc":       "2026-05-14T16:01:12Z",
  "actual_end_utc":         "2026-05-14T16:34:55Z",
  "channel_link": {
    "team_id":              "...",
    "channel_id":           "...",
    "thread_id":            null,
    "linked_at_utc":        "...",
    "linked_source":        "manual_command"
  }
}
```

### 2.5 Sanitization

| Token                    | Format                                  | Sanitize?  |
|--------------------------|-----------------------------------------|------------|
| `team_id`                | AAD group GUID, lowercase, dashes       | No         |
| `channel_id`             | `19:{guid}@thread.tacv2`                | Yes — `:`, `@` → `_` |
| `thread_id`              | Numeric string (root message id)        | No         |
| `message_id`             | Numeric string                          | No         |
| `meeting_id`             | URL-safe base64                         | No         |
| `meeting_chat_thread_id` | `19:meeting_xxx@thread.v2`              | Not used in paths (we key by `meeting_id`) |
| `utc_ts`                 | `yyyyMMddTHHmmssfffZ`                   | n/a        |
| `event_id`               | 32-char hex                             | No         |

Sanitization rule, in Python:

```python
import re
def sanitize(raw: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\-_.]", "_", raw)
```

---

## 3. Blob bodies

Each event blob is a single pretty-printed `alfred-v2` envelope.
Every blob is one envelope. Folders are pure prefixes — Azure Blob
has no real folders.

Example — a finalized transcript chunk:

```jsonc
// meetings/MSpkYzE3.../transcripts/final/20260514T163412184Z-8a3f1c0e2b9d4a7e9f12bb0001020304.json
{
  "schema_version": "alfred-v2",
  "event_type":     "meeting.transcript.final",
  "event_id":       "8a3f1c0e2b9d4a7e9f12bb0001020304",
  "ts":             "2026-05-14T16:34:12.184Z",
  "channel_ref":    null,
  "meeting_ref": {
    "meeting_id":             "MSpkYzE3...",
    "meeting_chat_thread_id": "19:meeting_xxx@thread.v2",
    "subject":                "Sprint planning",
    "organizer":              { "aad_id": "...", "display_name": "Jane Doe" },
    "channel_link":           null
  },
  "payload": {
    "text":          "Let's start with the deploy retro.",
    "timestamp_utc": "2026-05-14T16:34:12.184Z",
    "speaker":       { "id": "speaker_0", "display_name": "Jane Doe" },
    "audio_start_ms": 1234.5,
    "audio_end_ms":   5678.9,
    "confidence":     0.94,
    "provider":       { "name": "azure_speech" }
  }
}
```

Example — a channel message:

```jsonc
// teams/d3f5f412-.../channels/19_abc_thread.tacv2/threads/1700000000000/messages/20260514T182345401Z-1700000000123.json
{
  "schema_version": "alfred-v2",
  "event_type":     "channel.message.created",
  "event_id":       "8cdad195eeea41c7a28ace09b0e3f998",
  "ts":             "2026-05-14T18:23:45.401Z",
  "channel_ref": {
    "team_id":              "d3f5f412-...",
    "team_display_name":    "Engineering",
    "channel_id":           "19:abc@thread.tacv2",
    "channel_display_name": "general",
    "thread_id":            "1700000000000",
    "message_id":           "1700000000123"
  },
  "meeting_ref":    null,
  "payload": {
    "sender":        { "aad_id": "...", "display_name": "Logan Robbins", "kind": "user" },
    "text":          "Anyone seen the deploy logs?",
    "is_root":       true,
    "from_bot":      false,
    "timestamp_utc": "2026-05-14T18:23:45.401Z"
  }
}
```

---

## 4. Recipes for an AI agent

### 4.1 "Give me every meeting Alfred has ever seen"

```bash
SA="https://stalfreddisney.blob.core.windows.net/alfred-events"
curl -s "$SA/indexes/meetings.json" | jq 'to_entries | map({id:.key, subject:.value.subject, start:.value.scheduled_start_utc})'
```

### 4.2 "Find the meeting where the subject contains 'sprint planning'"

```bash
curl -s "$SA/indexes/meetings.json" \
  | jq -r 'to_entries[] | select(.value.subject | test("sprint planning"; "i")) | .key'
```

Then for each meeting id:

```bash
MID=<from-above>
curl -s "$SA/meetings/$MID/meta.json" | jq
curl -s "$SA/meetings/$MID/transcripts/official.vtt"
```

### 4.3 "Resolve channel name 'engineering / general' to its ids"

```bash
curl -s "$SA/indexes/channels.json" \
  | jq -r --arg t "Engineering" --arg c "general" '
      to_entries[]
      | select(.value.display_name == $c)
      | . as $entry
      | $teams[.value.team_id] as $tname
      | select($tname == $t)
      | "\(.value.team_id) \(.key)"
    ' --slurpfile teams "$SA/indexes/teams.json"
```

(Or just hit the sink's `GET /v2/resolve?kind=channel&team=Engineering&channel=general` —
the sink keeps a SQLite-indexed copy of these maps.)

### 4.4 "Get the live transcript chunks for the most recent meeting"

```bash
LATEST=$(curl -s "$SA/indexes/meetings.json" \
  | jq -r 'to_entries | sort_by(.value.actual_start_utc) | reverse | .[0].key')

curl -s "$SA?restype=container&comp=list&prefix=meetings/$LATEST/transcripts/final/"
```

### 4.5 "Get every channel message in a specific thread"

```bash
curl -s "$SA?restype=container&comp=list&prefix=teams/$TID/channels/$SAN_CID/threads/$THRID/messages/"
```

Listing returns blob names in lexicographic order, which by
construction is time order (paths embed `yyyyMMddTHHmmssfffZ` first).

### 4.6 Pagination

Azure Blob list returns up to 5000 entries per page; for more, parse
`<NextMarker>` from the XML and pass `&marker=<value>`.

---

## 5. Python helpers

### 5.1 With `azure-storage-blob`

```python
from azure.storage.blob import ContainerClient
import json, re

URL = "https://stalfreddisney.blob.core.windows.net/alfred-events"
c = ContainerClient.from_container_url(URL, credential=None)

def get_json(name: str):
    return json.loads(c.download_blob(name).readall().decode())

# All meetings, newest first
meetings = get_json("indexes/meetings.json")
sorted_meetings = sorted(meetings.items(),
                         key=lambda kv: kv[1].get("actual_start_utc") or "",
                         reverse=True)

# Latest meeting's final transcript chunks
latest_mid = sorted_meetings[0][0]
prefix = f"meetings/{latest_mid}/transcripts/final/"
for b in sorted(c.list_blobs(name_starts_with=prefix), key=lambda b: b.name):
    env = get_json(b.name)
    print(env["ts"], env["payload"]["speaker"].get("display_name", "?"), env["payload"]["text"])
```

### 5.2 Zero-deps

```python
import urllib.request, json
from xml.etree import ElementTree as ET

SA = "https://stalfreddisney.blob.core.windows.net/alfred-events"

def get(name: str) -> bytes:
    return urllib.request.urlopen(f"{SA}/{name}").read()

def list_prefix(prefix: str):
    xml = urllib.request.urlopen(
        f"{SA}?restype=container&comp=list&prefix={prefix}"
    ).read()
    root = ET.fromstring(xml)
    for b in root.iter("Blob"):
        yield b.findtext("Name")

meetings = json.loads(get("indexes/meetings.json").decode())
```

---

## 6. Cadence and volume

Per active meeting:

| Event type                         | Cadence                                                |
|------------------------------------|--------------------------------------------------------|
| `meeting.chat.*`                   | 1 per message                                          |
| `meeting.transcript.partial`       | throttled to 1/speaker/60s (configurable)              |
| `meeting.transcript.final`         | ~3–5/min/speaker                                       |
| `meeting.transcript.official`      | exactly 1, ~1–2 min after meeting ends, only if record-and-transcribe was on |
| `meeting.system/*`                 | rare (create/end/link/call-joined/left)                |

Per channel:

| Event type                         | Cadence                                                |
|------------------------------------|--------------------------------------------------------|
| `channel.message.*`                | 1 per message                                          |
| `channel.system/*`                 | rare (attached/detached)                               |

For content-only consumption, filter to:
`meeting.transcript.final`, `meeting.transcript.official`,
`meeting.chat.created`, `channel.message.created`.

---

## 7. Idempotency

- `event_id` is stable per event; use it as your primary key.
- Blobs are written exactly once. Re-runs produce the same paths.
- Timestamps in path names are millisecond-precision (`HHmmssfff`);
  the `event_id` suffix disambiguates ties.

---

## 8. Built-in UI

The web app's archive browser at `/archive` walks this layout and
renders human-readable names from `meta.json` + the index files.
It's a thin client; same data the recipes above produce.
