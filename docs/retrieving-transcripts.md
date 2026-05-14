# Consuming Alfred data — `alfred-v2`

Alfred captures every meeting transcript, meeting chat, and team-channel
message Microsoft Teams routes through it, and persists each event two
ways. **Pick whichever path fits your consumer**:

| Path | Auth | Best for |
|------|------|----------|
| **Sink API** — [`https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io`](https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io) | None (public for now) | "I want one HTTP call → JSON" — list / lookup / proxy reads |
| **Blob archive** — [`https://stalfreddisney.blob.core.windows.net/alfred-events/`](https://stalfreddisney.blob.core.windows.net/alfred-events/) | Anonymous read | "I want the raw event stream forever" — replay, bulk, offline |

Both serve the **same `alfred-v2` envelopes**. The sink is a thin SQLite
+ HTTP layer over the blob archive; the blob archive is the source of
truth.

> **AI coding agent shortcut:** if all you need is "list meetings, get
> a transcript by meeting id, get chat for a channel", use the sink —
> §1.1 below. If you need the entire event stream, use the blob
> archive — §2.

---

## 0. Mental model

Two canonical keys, mirroring Microsoft Graph:

```
Team (team_id)
  └── Channel (team_id, channel_id)
        └── Thread (thread_id = root message id)
              └── Messages

Meeting (meeting_id = Graph onlineMeeting id)
  ├── Chat (meeting_chat_thread_id) → Messages
  ├── Transcripts (live partial/final + post-meeting official VTT)
  └── channel_link?  (optional back-reference to a channel + thread)
```

`meeting_id` and `(team_id, channel_id, thread_id)` are URL-safe
strings you can drop straight into any sink or blob URL — Alfred does
not invent surrogate keys.

`meeting_chat_thread_id` (`19:meeting_xxx@thread.v2`) is a *sub-
resource* of the meeting, not a substitute for `meeting_id`. Never
key on the chat thread when you mean the meeting.

The event contract — envelope shape, every `event_type`, every
payload — is in [`docs/event-contract.md`](event-contract.md).

---

## 1. Sink API (the easy path)

Base URL (Disney sandbox):

```
SINK="https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io"
```

Everything under `/v2/*` is the canonical consumer surface. JSON in,
JSON out. No auth in the sandbox.

### 1.1 Endpoints

| Endpoint | Returns |
|----------|---------|
| `GET /v2/index` | top-level discovery — counts, recent meetings, blob archive prefixes |
| `GET /v2/meetings?limit=&team_id=&channel_id=` | list meetings (subject, organizer, scheduled / actual times, optional channel link) |
| `GET /v2/meetings/{meeting_id}` | one meeting — subject, organizer, channel_link, etc. |
| `GET /v2/meetings/{meeting_id}/events?kinds=speech,chat&limit=` | the meeting's combined ledger (chat + transcript chunks + system) |
| `GET /v2/meetings/{meeting_id}/transcript` | proxy of the official post-meeting transcript (TXT + VTT URLs, body inline when available) |
| `GET /v2/teams/{team_id}/channels/{channel_id}` | one channel — distinct threads + linked meetings |
| `GET /v2/teams/{team_id}/channels/{channel_id}/events?since=&until=&kinds=&limit=` | every event the channel has seen (channel posts + linked meeting events) |
| `GET /v2/teams/{team_id}/channels/{channel_id}/threads/{thread_id}/messages?limit=` | every chat message in a specific thread |
| `GET /v2/resolve?kind=meeting&subject=...&limit=` | name → `meeting_id` resolver (case-insensitive substring) |

### 1.2 Recipes

```bash
SINK="https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io"

# What's in the system?
curl -sS "$SINK/v2/index" | jq

# All known meetings, newest first
curl -sS "$SINK/v2/meetings?limit=50" | jq '.meetings[] | {meeting_id, subject, scheduled_start_utc, channel:.channel_link.channel_display_name}'

# A specific meeting (e.g. fetched from the list above)
MID="MSpkYzE3NjY0Mi0..."
curl -sS "$SINK/v2/meetings/$MID" | jq

# Its official post-meeting transcript (plaintext, inline)
curl -sS "$SINK/v2/meetings/$MID/transcript" | jq -r '.text'

# Its live-STT + chat ledger (most recent 200)
curl -sS "$SINK/v2/meetings/$MID/events?limit=200" \
  | jq '.events[] | {ts:.timestamp_utc, kind, who:.display_name, text:(.text // .raw)}'

# A channel by id
TID="d3f5f412-2abf-4300-ac73-019e892c2a05"
CID="19:abc@thread.tacv2"
curl -sS "$SINK/v2/teams/$TID/channels/$CID" | jq

# A specific thread's messages
THRID="1700000000000"
curl -sS "$SINK/v2/teams/$TID/channels/$CID/threads/$THRID/messages" | jq

# I only know the subject — give me the meeting_id
curl -sS "$SINK/v2/resolve?kind=meeting&subject=sprint%20planning" | jq '.matches[] | {meeting_id, subject}'
```

### 1.3 Python helper

```python
import httpx

SINK = "https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io"

with httpx.Client(base_url=SINK, timeout=10.0) as c:
    # 1. Resolve subject → meeting_id
    matches = c.get("/v2/resolve", params={"kind": "meeting", "subject": "sprint planning"}).json()["matches"]
    meeting_id = matches[0]["meeting_id"]

    # 2. Get the official transcript
    t = c.get(f"/v2/meetings/{meeting_id}/transcript").json()
    print(t["text"] if t["available"] else f"not available yet — try {t['official_transcript_txt_url']}")

    # 3. Walk the ledger
    events = c.get(f"/v2/meetings/{meeting_id}/events", params={"limit": 500}).json()["events"]
    for e in events:
        print(e["timestamp_utc"], e["kind"], (e.get("display_name") or "?"), e.get("text", ""))
```

---

## 2. Blob archive (the raw event stream)

```
SA="https://stalfreddisney.blob.core.windows.net/alfred-events"
```

|                  |                                                                  |
|------------------|------------------------------------------------------------------|
| Storage account  | `stalfreddisney`                                                 |
| Container        | `alfred-events`                                                  |
| Endpoint         | `https://stalfreddisney.blob.core.windows.net/alfred-events/`    |
| Auth (read)      | Anonymous public read (container ACL = `container`)              |
| Auth (write)     | Bot managed identity only                                        |
| Region           | `eastus`                                                         |
| Subscription     | `e02c0038-82c8-4655-9647-38083f301099` (WDI R&D)                 |

If anonymous read is ever turned off you'll see `403
AuthenticationFailed`. Switch to AAD auth at that point (any
`DefaultAzureCredential`-capable identity in the subscription works).

### 2.1 Path layout

Every per-event blob is a **single alfred-v2 envelope** as pretty-
printed JSON. No preamble, no markers — `jq` works on every file.

```
teams/{team_id}/channels/{channel_id_sanitized}/channel.attached/{utcTs}-{event_id}.json
teams/{team_id}/channels/{channel_id_sanitized}/channel.detached/{utcTs}-{event_id}.json
teams/{team_id}/channels/{channel_id_sanitized}/channel.message.created/{utcTs}-{event_id}.json
teams/{team_id}/channels/{channel_id_sanitized}/channel.message.updated/{utcTs}-{event_id}.json
teams/{team_id}/channels/{channel_id_sanitized}/channel.message.deleted/{utcTs}-{event_id}.json

meetings/{meeting_id}/meeting.created/{utcTs}-{event_id}.json
meetings/{meeting_id}/meeting.ended/{utcTs}-{event_id}.json
meetings/{meeting_id}/meeting.linked/{utcTs}-{event_id}.json
meetings/{meeting_id}/meeting.call.joined/{utcTs}-{event_id}.json
meetings/{meeting_id}/meeting.call.left/{utcTs}-{event_id}.json
meetings/{meeting_id}/meeting.chat.created/{utcTs}-{event_id}.json
meetings/{meeting_id}/meeting.chat.updated/{utcTs}-{event_id}.json
meetings/{meeting_id}/meeting.chat.deleted/{utcTs}-{event_id}.json
meetings/{meeting_id}/meeting.transcript.partial/{utcTs}-{event_id}.json
meetings/{meeting_id}/meeting.transcript.final/{utcTs}-{event_id}.json
meetings/{meeting_id}/meeting.transcript.official/{utcTs}-{event_id}.json

meetings/{meeting_id}/transcripts/official.txt
meetings/{meeting_id}/transcripts/official.vtt
```

- `{utcTs}` is `yyyyMMddTHHmmssfffZ` — lexicographic order ≡ time order.
- `{event_id}` is the 32-char hex id from the envelope; use it for dedup.
- `{channel_id_sanitized}` replaces `:` `@` `;` `%` with `_` (the
  sanitizer is `re.sub(r"[^a-zA-Z0-9\-_.]", "_", raw)`).
- `{meeting_id}` and `{team_id}` are URL-safe already and are not
  sanitized.

### 2.2 The two well-known transcript files

For any meeting where Record-and-Transcribe was on, the **post-meeting
official Microsoft transcript** lands at exactly two paths (overwriting
on each re-fetch):

```
meetings/{meeting_id}/transcripts/official.txt   # clean speaker-per-line plaintext
meetings/{meeting_id}/transcripts/official.vtt   # raw WebVTT (start/end cues, <v> markup)
```

`official.txt` is the file an exec opens. `official.vtt` is the file a
parser opens.

> **Channel meetings produce no official transcript.** Microsoft's
> `OnlineMeetingTranscript.Read.Chat` permission only applies to
> private chat meetings, not channel meetings — see README §7.2.

### 2.3 Blob body shape

Every per-event blob is the v2 envelope, pretty-printed:

```jsonc
// meetings/MSpkYzE3.../meeting.transcript.final/20260514T163412184Z-8a3f...0304.json
{
  "schema_version": "alfred-v2",
  "event_type":     "meeting.transcript.final",
  "event_id":       "8a3f1c0e2b9d4a7e9f12bb0001020304",
  "ts":             "2026-05-14T16:34:12.184Z",
  "meeting_ref": {
    "meeting_id":             "MSpkYzE3...",
    "meeting_chat_thread_id": "19:meeting_xxx@thread.v2",
    "subject":                "Sprint planning",
    "organizer":              { "aad_id": "...", "display_name": "Jane Doe" },
    "channel_link":           null
  },
  "payload": {
    "text":           "Let's start with the deploy retro.",
    "timestamp_utc":  "2026-05-14T16:34:12.184Z",
    "speaker":        { "id": "speaker_0", "display_name": "Jane Doe" },
    "audio_start_ms": 1234.5,
    "audio_end_ms":   5678.9,
    "confidence":     0.94,
    "provider":       { "name": "azure_speech" }
  }
}
```

```jsonc
// teams/d3f5f412-.../channels/19_abc_thread.tacv2/channel.message.created/20260514T182345401Z-...json
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
  "payload": {
    "sender":        { "aad_id": "...", "display_name": "Logan Robbins", "kind": "user" },
    "text":          "Anyone seen the deploy logs?",
    "is_root":       true,
    "from_bot":      false,
    "timestamp_utc": "2026-05-14T18:23:45.401Z"
  }
}
```

### 2.4 Recipes — direct from blob storage

```bash
SA="https://stalfreddisney.blob.core.windows.net/alfred-events"

# I have meeting_id, give me the transcript (plaintext)
MID="MSpkYzE3..."
curl -sS "$SA/meetings/$MID/transcripts/official.txt"

# I have meeting_id, give me the transcript (WebVTT, with cue timings)
curl -sS "$SA/meetings/$MID/transcripts/official.vtt"

# I have meeting_id, list every event the bot ever published for it
curl -sS "$SA?restype=container&comp=list&prefix=meetings/$MID/" \
  | xmllint --xpath '//Blob/Name/text()' - 2>/dev/null

# I have meeting_id, give me every live-STT final chunk in order
curl -sS "$SA?restype=container&comp=list&prefix=meetings/$MID/meeting.transcript.final/" \
  | xmllint --xpath '//Blob/Name/text()' - 2>/dev/null \
  | tr ' ' '\n' \
  | sort \
  | while read name; do curl -sS "$SA/$name" | jq -r '"\(.ts)  \(.payload.speaker.display_name // .payload.speaker.id // "?"):  \(.payload.text)"'; done

# I have team_id + channel_id, list every chat message
TID="d3f5f412-2abf-4300-ac73-019e892c2a05"
CID_SAN="19_abc_thread.tacv2"
curl -sS "$SA?restype=container&comp=list&prefix=teams/$TID/channels/$CID_SAN/channel.message.created/"
```

### 2.5 Python — zero deps

```python
import urllib.request, urllib.parse, json
from xml.etree import ElementTree as ET

SA = "https://stalfreddisney.blob.core.windows.net/alfred-events"

def get(name: str) -> bytes:
    return urllib.request.urlopen(f"{SA}/{name}").read()

def get_json(name: str) -> dict:
    return json.loads(get(name).decode("utf-8"))

def list_prefix(prefix: str):
    """Yield blob names under `prefix`, ascending order = time order."""
    marker = ""
    while True:
        qs = urllib.parse.urlencode({
            "restype": "container",
            "comp": "list",
            "prefix": prefix,
            "maxresults": "5000",
            **({"marker": marker} if marker else {}),
        })
        xml = urllib.request.urlopen(f"{SA}?{qs}").read()
        root = ET.fromstring(xml)
        for b in root.iter("Blob"):
            yield b.findtext("Name") or ""
        marker = (root.findtext("NextMarker") or "").strip()
        if not marker:
            return

# Example: print every transcript final chunk for one meeting
meeting_id = "MSpkYzE3..."
for name in list_prefix(f"meetings/{meeting_id}/meeting.transcript.final/"):
    env = get_json(name)
    speaker = (env["payload"].get("speaker") or {}).get("display_name") or "?"
    print(env["ts"], speaker + ":", env["payload"]["text"])

# Example: dump the official transcript
print(get(f"meetings/{meeting_id}/transcripts/official.txt").decode("utf-8"))
```

### 2.6 Python — with `azure-storage-blob`

```python
from azure.storage.blob import ContainerClient
import json

URL = "https://stalfreddisney.blob.core.windows.net/alfred-events"
c = ContainerClient.from_container_url(URL, credential=None)

def get_json(name: str) -> dict:
    return json.loads(c.download_blob(name).readall().decode("utf-8"))

meeting_id = "MSpkYzE3..."

# Walk every event for a meeting, chronologically
for b in sorted(
    c.list_blobs(name_starts_with=f"meetings/{meeting_id}/"),
    key=lambda b: b.name,
):
    if not b.name.endswith(".json"):
        continue
    env = get_json(b.name)
    print(env["ts"], env["event_type"])
```

### 2.7 Listing pagination

Azure Blob list returns up to 5000 entries per page; parse
`<NextMarker>` from the XML and pass `&marker=<value>` for the next
page. The Python helper in §2.5 does this automatically.

---

## 3. Putting them together — "build me a meeting dossier"

Given a meeting subject string, build a dossier with the canonical
transcript, the meeting chat, and any back-linked channel context.

```python
import httpx

SINK = "https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io"
SA = "https://stalfreddisney.blob.core.windows.net/alfred-events"

with httpx.Client(timeout=10.0) as c:
    # 1. Subject → meeting_id
    r = c.get(f"{SINK}/v2/resolve", params={"kind": "meeting", "subject": "sprint planning"}).json()
    meeting_id = r["matches"][0]["meeting_id"]
    subject = r["matches"][0]["subject"]

    # 2. Canonical metadata + channel link (the sink already merged everything)
    meta = c.get(f"{SINK}/v2/meetings/{meeting_id}").json()

    # 3. Post-meeting transcript (plaintext)
    transcript_text = c.get(f"{SA}/meetings/{meeting_id}/transcripts/official.txt").text

    # 4. Full ledger (chat + transcript chunks), most recent N
    ledger = c.get(f"{SINK}/v2/meetings/{meeting_id}/events", params={"limit": 500}).json()["events"]

print(subject, "—", meta.get("organizer", {}).get("display_name"))
print(transcript_text[:1000], "...")
print(f"{len(ledger)} events in the meeting ledger")
```

---

## 4. Idempotency + cadence

- `event_id` (32-char hex) is the natural primary key. Re-runs are
  exactly-once at the blob layer; same path, same body.
- Blob timestamps are millisecond-precision (`HHmmssfff`); the
  `event_id` suffix disambiguates ties.
- The sink writes the same envelope twice (once into `raw_ingest_envelopes`
  for replay, once into the per-session ledger for queries) — both are
  keyed on `event_id`.

Per meeting:

| Event type                          | Cadence                                                                |
|-------------------------------------|------------------------------------------------------------------------|
| `meeting.transcript.partial`        | throttled to 1/speaker/60s by default (`PartialThrottleSeconds`)      |
| `meeting.transcript.final`          | ~3–5 / min / speaker                                                   |
| `meeting.transcript.official`       | exactly 1, ~1–2 min after meeting ends, if record-and-transcribe was on |
| `meeting.chat.*`                    | 1 per message                                                          |
| `meeting.created` / `.ended` / etc. | rare                                                                   |

Per channel:

| Event type                          | Cadence                                                                |
|-------------------------------------|------------------------------------------------------------------------|
| `channel.message.*`                 | 1 per message                                                          |
| `channel.attached` / `.detached`    | rare                                                                   |

For content-only consumers, filter to `meeting.transcript.final`,
`meeting.transcript.official`, `meeting.chat.created`,
`channel.message.created`.

---

## 5. Built-in UI

[`https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io`](https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io)

- `/` — meeting picker (subject first; hover for `meeting_id`).
- `/m/<meeting_chat_thread_id>` — per-meeting dossier (live ledger + dossier panel).
- `/channels` — operator console (attach / consumer admin).
- `/archive` — blob-archive folder browser; reads the same `.json`
  envelopes documented above.

This UI is a thin client over the same sink + blob endpoints — nothing
here is private to the UI.
