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

> **Canonical-id fallback.** If you see a `meeting_id` that starts
> with `19:` and ends with `@thread.v2`, the bot couldn't resolve
> the chat thread to a canonical Graph `onlineMeeting.id` and fell
> back to the thread id. The blob layout under
> `meetings/{meeting_id}/` reflects whatever id the envelope carried.
> Consumers that build dossiers should index by **both** the
> canonical id AND the `meeting_chat_thread_id` from `meeting_ref`
> so the two buckets can be merged after the fact.

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

The folder segment is a **logical category** (machine-readable
snake_case), one per data type. Folder names mirror Microsoft Graph
sub-resources where one exists (`messages/` → Graph `/messages`,
`transcripts/` → Graph `/transcripts`); `live_transcript/` and
`lifecycle/` are our own labels because Graph has no equivalent
sub-resource. The precise `event_type` stays on the envelope JSON
so consumers that need to tell `created` from `updated`/`deleted`
read it from there. There are exactly four categories:

| Category folder | Aggregates these `event_type` values | Graph mirror |
|---|---|---|
| `messages` | `channel.message.{created,updated,deleted}`, `meeting.chat.{created,updated,deleted}` | `/teams/{id}/channels/{id}/messages`, `/chats/{id}/messages` |
| `live_transcript` | `meeting.transcript.{partial,final}` | none — this bot's Azure Speech STT of the real-time audio stream |
| `transcripts` | `meeting.transcript.official` (envelope blob sits alongside the flat `official.txt` + `official.vtt`) | `/onlineMeetings/{id}/transcripts` (callTranscript) |
| `lifecycle` | `channel.{attached,detached}`, `meeting.{created,ended,linked,call.joined,call.left}` | none |

```
# Channel scope (channel meetings have no audio per RSC limit — messages + lifecycle only)
teams/{team_id}/channels/{channel_id_sanitized}/messages/{utcTs}-{event_id}.json
teams/{team_id}/channels/{channel_id_sanitized}/lifecycle/{utcTs}-{event_id}.json

# Meeting scope
meetings/{meeting_id}/messages/{utcTs}-{event_id}.json
meetings/{meeting_id}/live_transcript/{utcTs}-{event_id}.json
meetings/{meeting_id}/transcripts/{utcTs}-{event_id}.json      # meeting.transcript.official envelope
meetings/{meeting_id}/transcripts/official.txt                  # flat plaintext (overwritten on each fetch)
meetings/{meeting_id}/transcripts/official.vtt                  # flat WebVTT     (overwritten on each fetch)
meetings/{meeting_id}/lifecycle/{utcTs}-{event_id}.json
```

> **Three retrievable data types — one prefix each:**
> 1. **Chat messages** → `…/messages/`
> 2. **Real-time transcript** → `meetings/{mid}/live_transcript/`
>    (Alfred's Azure Speech STT of the audio stream — NOT Microsoft
>    "live captions")
> 3. **Official (final) transcript** → `meetings/{mid}/transcripts/`
>    (event envelope + flat `official.txt` + flat `official.vtt`, all
>    in the same folder; matches Graph's `callTranscript` resource).

- `{utcTs}` is `yyyyMMddTHHmmssfffZ` — lexicographic order ≡ time order.
- `{event_id}` is the 32-char hex id from the envelope; use it for dedup.
- `{channel_id_sanitized}`, `{meeting_id}`, `{team_id}` — **all run
  through the same sanitizer**: `re.sub(r"[^a-zA-Z0-9\-_.]", "_", raw)`
  (the bot uses the same regex in C# at `BlobEventArchive.cs`'s
  `SanitizePathSegment`). For a canonical Graph `onlineMeeting.id`
  (URL-safe base64) this is a no-op. For a `meeting_id` in the
  fallback shape `19:meeting_xxx@thread.v2` (when the bot couldn't
  resolve the canonical id — see §0 canonical-id fallback note), the
  `:` and `@` are replaced by `_`. Build blob URLs from the sanitized
  form, not the raw envelope value.

> **Legacy v1 compat path (preserved, do not consume from new code).**
> The bot also writes
> `channels/{team_id}/{channel_id_sanitized}/chat.message/{utcTs}-{event_id}.txt`
> for channel ids listed in `BlobArchive:V1CompatChannelIds`. That
> path uses the legacy `alfred-events-v1` body (text header +
> `---ENVELOPE---` separator + flat fields) and exists only to keep
> the pre-v2 `server.py` polling bridge working through the cutover.
> New consumers must read the v2 category layout above.

> **Historical event-type-per-folder blobs.** Blobs written before
> the category refactor live at their old `…/{event_type}/…` paths
> (e.g. `meetings/{mid}/meeting.chat.created/`). They remain readable
> at those legacy paths; new writes land at the category paths. No
> backfill — both layouts coexist in the container until the
> historical blobs age out.

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

> **Channel meetings produce no official transcript** via this bot.
> Microsoft's `OnlineMeetingTranscript.Read.Chat` permission only
> applies to private chat meetings, not channel meetings — see
> README §7.2. **Escape hatch:** `GET /me/onlineMeetings/{meeting_id}
> /transcripts` *is* available to a calendar-invitee's delegated
> token for both private and channel meetings (per MS Graph v1.0
> docs for `Get callTranscript`). That path bypasses this bot
> entirely — a consumer with the right user token can fetch
> channel-meeting transcripts directly from Microsoft Graph.

### 2.3 Blob body shape

Every per-event blob is the v2 envelope, pretty-printed:

```jsonc
// meetings/MSpkYzE3.../live_transcript/20260514T163412184Z-8a3f...0304.json
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
// teams/d3f5f412-.../channels/19_abc_thread.tacv2/messages/20260514T182345401Z-...json
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

> **Pagination warning.** Azure Blob `?restype=container&comp=list`
> returns at most **5000 blob names per page**. A busy meeting can
> exceed that. If the response XML contains a non-empty
> `<NextMarker>...</NextMarker>` element, you MUST repeat the
> request with `&marker=<value>` until `<NextMarker>` is empty.
> The §2.5 Python helper does this; the curl recipes below show
> how. Skipping pagination silently truncates results.

```bash
SA="https://stalfreddisney.blob.core.windows.net/alfred-events"

# I have meeting_id, give me the transcript (plaintext)
MID="MSpkYzE3..."
curl -sS "$SA/meetings/$MID/transcripts/official.txt"

# I have meeting_id, give me the transcript (WebVTT, with cue timings)
curl -sS "$SA/meetings/$MID/transcripts/official.vtt"

# List every blob under a prefix, honoring <NextMarker> pagination.
# Define once, reuse below.
list_prefix() {
  local prefix="$1" marker=""
  while :; do
    local body
    body=$(curl -sS "$SA?restype=container&comp=list&maxresults=5000&prefix=$(printf %s "$prefix" | jq -sRr @uri)${marker:+&marker=$(printf %s "$marker" | jq -sRr @uri)}")
    echo "$body" | xmllint --xpath '//Blob/Name/text()' - 2>/dev/null | tr ' ' '\n'
    marker=$(echo "$body" | xmllint --xpath 'string(//NextMarker)' - 2>/dev/null)
    [ -z "$marker" ] && break
  done
}

# I have meeting_id, list every event the bot ever published for it
list_prefix "meetings/$MID/"

# I have meeting_id, give me every live-STT chunk in order.
# (live_transcript/ aggregates partial + final. Filter on event_type
#  in the envelope if you only want one or the other — final is
#  "meeting.transcript.final".)
list_prefix "meetings/$MID/live_transcript/" \
  | sort \
  | while read name; do curl -sS "$SA/$name" \
      | jq -r 'select(.event_type == "meeting.transcript.final")
               | "\(.ts)  \(.payload.speaker.display_name // .payload.speaker.id // "?"):  \(.payload.text)"'; done

# I have team_id + channel_id, list every chat message (created+updated+deleted)
TID="d3f5f412-2abf-4300-ac73-019e892c2a05"
CID_SAN="19_abc_thread.tacv2"
list_prefix "teams/$TID/channels/$CID_SAN/messages/"
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

# Example: print every live-STT final chunk for one meeting.
# live_transcript/ aggregates partial+final; filter on event_type
# to get just finals.
meeting_id = "MSpkYzE3..."
for name in list_prefix(f"meetings/{meeting_id}/live_transcript/"):
    env = get_json(name)
    if env["event_type"] != "meeting.transcript.final":
        continue
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

## 3.5 Live processing — register a consumer URL, don't poll

The sink + blob archive are **read-after-the-fact** stores. If you
want low-latency processing of meeting audio chunks (e.g. "react
the moment the meeting transcribes something interesting"), do
**not** poll `/v2/meetings/{mid}/events` in a tight loop. Register
a push consumer instead — the bot will POST every envelope to your
URL as it's emitted.

```bash
BOT="https://alfred-disney-bot.eastus.cloudapp.azure.com"

# Channel scope: receive all channel.* events for the channel
# AND all meeting.* events for meetings linked to that channel.
# (Channel scope is the only scope the bot implements — there is
# no per-meeting consumer endpoint.)
curl -X POST "$BOT/api/channels/$TID/$CID/consumers" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-consumer","url":"https://my-service/v2/events",
       "event_kinds":["meeting.transcript.final","meeting.chat.created"],
       "enabled":true}'
```

The body field is **`event_kinds`** (not `event_types` — the
dispatcher matches on `event_kinds`; sending the wrong key produces
an empty filter that silently matches everything).

Pick **`meeting.transcript.final`** (3–5/min/speaker) for live
transcript chunks. `meeting.transcript.partial` is throttled to
1/speaker/60s by default — it is a "progress indicator," not a
real-time stream. See [event-contract.md §6](event-contract.md) for
the full consumer-registration surface, transport retry rules
(2xx accept, 5xx/408/429 retry x3, bounded-1000 drop-oldest queue),
and event filter syntax.

For an agent that "listens silently until something interesting
happens": consume `meeting.transcript.final` envelopes, run your
analysis on each chunk (or on a debounced batch), and stay silent
unless a threshold is crossed or the bot is directly mentioned. The
canonical Alfred prompt (`python/batcave_platform/specs/alfred.yaml`)
encodes this policy — read it as a reference if you're writing a
sibling agent.

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
