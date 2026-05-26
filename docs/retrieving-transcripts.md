# Reading Alfred Data — `alfred-v2`

Two read paths over the same `alfred-v2` envelopes:

| Path | Auth | Use when |
|------|------|----------|
| **Sink API** — `https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io` | None (sandbox is public) | one HTTP call → JSON; list / lookup / proxy reads |
| **Blob archive** — `https://stalfreddisney.blob.core.windows.net/alfred-events/` | Anonymous read | raw event stream; replay, bulk, offline |

The sink is a thin PostgreSQL + HTTP layer over the blob archive. **Blob archive is the source of truth.** For envelope shape and event types see `docs/event-contract.md`.

---

## 0. Keys

```
Team (team_id)
  └── Channel (team_id, channel_id)
        └── Thread (thread_id = root message id)
              └── Messages

Meeting (meeting_id)
  ├── Chat (meeting_chat_thread_id) → Messages
  ├── Transcripts (live partial/final + official VTT)
  └── channel_link?  (optional channel + thread back-reference)
```

**`meeting_id` is the Teams chat thread id of the meeting** (`19:meeting_<base64>@thread.v2`). It is the stable key for every meeting-scoped lookup: sink endpoints under `/v2/meetings/{meeting_id}/...`, blob paths under `meetings/{meeting_id_sanitized}/...`, and the consumer dossier.

`meeting_chat_thread_id` carries the same value and exists only because the field name reads more naturally inside chat-payload contexts.

The Graph `onlineMeeting.id` (returned by `GET /me/onlineMeetings`) is a separate identifier that this system does not key on. It never appears on an envelope and is never accepted as a sink key. The bot uses it internally only when calling `/onlineMeetings/{id}/transcripts` to fetch the post-meeting Microsoft transcript. See `docs/event-contract.md` §2.2.1 for the full contract statement.

---

## 1. Sink API

```
SINK="https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io"
```

JSON in, JSON out. No auth in the sandbox.

### 1.1 Endpoints

| Endpoint | Returns |
|----------|---------|
| `GET /v2/index` | counts, recent meetings, blob archive prefixes |
| `GET /v2/meetings?limit=&team_id=&channel_id=` | list meetings (subject, organizer, times, optional channel link) |
| `GET /v2/meetings/{meeting_id}` | one meeting — subject, organizer, channel_link, etc. |
| `GET /v2/meetings/{meeting_id}/events?kinds=speech,chat&limit=` | combined ledger (chat + transcript chunks + system) |
| `GET /v2/meetings/{meeting_id}/transcript` | proxy of the official post-meeting transcript (TXT + VTT URLs, body inline when available) |
| `GET /v2/teams/{team_id}/channels/{channel_id}` | channel — distinct threads + linked meetings |
| `GET /v2/teams/{team_id}/channels/{channel_id}/events?since=&until=&kinds=&limit=` | every event the channel has seen |
| `GET /v2/teams/{team_id}/channels/{channel_id}/threads/{thread_id}/messages?limit=` | every chat message in one thread |
| `GET /v2/resolve?kind=meeting&subject=...&limit=` | name → `meeting_id` (case-insensitive substring) |

### 1.2 Recipes

```bash
SINK="https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io"

# What's in the system?
curl -sS "$SINK/v2/index" | jq

# All known meetings, newest first
curl -sS "$SINK/v2/meetings?limit=50" \
  | jq '.meetings[] | {meeting_id, subject, scheduled_start_utc, channel:.channel_link.channel_display_name}'

# A specific meeting
MID="19:meeting_NmFkYW...@thread.v2"
curl -sS "$SINK/v2/meetings/$MID" | jq

# Its official post-meeting transcript (plaintext, inline)
curl -sS "$SINK/v2/meetings/$MID/transcript" | jq -r '.text'

# Its live-STT + chat ledger (most recent 200)
curl -sS "$SINK/v2/meetings/$MID/events?limit=200" \
  | jq '.events[] | {ts:.timestamp_utc, kind, who:.display_name, text:(.text // .raw)}'

# A channel
TID="d3f5f412-2abf-4300-ac73-019e892c2a05"
CID="19:abc@thread.tacv2"
curl -sS "$SINK/v2/teams/$TID/channels/$CID" | jq

# A specific thread's messages
THRID="1700000000000"
curl -sS "$SINK/v2/teams/$TID/channels/$CID/threads/$THRID/messages" | jq

# Subject → meeting_id
curl -sS "$SINK/v2/resolve?kind=meeting&subject=sprint%20planning" \
  | jq '.matches[] | {meeting_id, subject}'
```

### 1.3 Python helper

```python
import httpx

SINK = "https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io"

with httpx.Client(base_url=SINK, timeout=10.0) as c:
    matches = c.get("/v2/resolve",
                    params={"kind": "meeting", "subject": "sprint planning"}).json()["matches"]
    meeting_id = matches[0]["meeting_id"]

    t = c.get(f"/v2/meetings/{meeting_id}/transcript").json()
    print(t["text"] if t["available"] else f"not yet — try {t['official_transcript_txt_url']}")

    events = c.get(f"/v2/meetings/{meeting_id}/events", params={"limit": 500}).json()["events"]
    for e in events:
        print(e["timestamp_utc"], e["kind"], (e.get("display_name") or "?"), e.get("text", ""))
```

---

## 2. Blob archive

```
SA="https://stalfreddisney.blob.core.windows.net/alfred-events"
```

| | |
|---|---|
| Storage account  | `stalfreddisney` |
| Container        | `alfred-events` |
| Endpoint         | `https://stalfreddisney.blob.core.windows.net/alfred-events/` |
| Auth (read)      | Anonymous public read (container ACL = `container`) |
| Auth (write)     | Bot managed identity only |
| Region           | `eastus` |
| Subscription     | `e02c0038-82c8-4655-9647-38083f301099` (WDI R&D) |

If anonymous read is ever disabled you'll see `403 AuthenticationFailed` — switch to `DefaultAzureCredential` against the subscription.

### 2.1 Path layout

Every per-event blob is a **single alfred-v2 envelope** as pretty-printed JSON. No preamble, no markers. `jq` works directly.

**Channel scope** (literal `/channels/` segment, per the C# writer under `src/Services/`):

```
teams/{team_id_sanitized}/channels/{channel_id_sanitized}/messages/{ts}-{event_id}.json
teams/{team_id_sanitized}/channels/{channel_id_sanitized}/lifecycle/{ts}-{event_id}.json
```

**Meeting scope:**

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

- `{ts}` = `yyyyMMddTHHmmssfffZ` (millisecond UTC; lexicographic = time order)
- `{event_id}` = 32-char hex from the envelope; use for dedup
- `{*_sanitized}` = `re.sub(r"[^a-zA-Z0-9\-_.]", "_", raw)`, truncated to 200 chars

Worked example — production-typical `meeting_id` (chat thread id):

```
19:meeting_NmFkYWM1NDQtYTM3ZC00ZjlmLTk4ZjItZjE0M2YwOWEzODIx@thread.v2
→
19_meeting_NmFkYWM1NDQtYTM3ZC00ZjlmLTk4ZjItZjE0M2YwOWEzODIx_thread.v2
```

Live URL confirming this layout (real production data):

```
https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io/archive?prefix=meetings%2F19_meeting_NmFkYWM1NDQtYTM3ZC00ZjlmLTk4ZjItZjE0M2YwOWEzODIx_thread.v2%2Flive_transcript%2F
```

Build blob URLs from the sanitized form, not the raw envelope value.

> **Historical layout.** Blobs written before the category refactor live at `…/{event_type}/…` (e.g. `meetings/{mid}/meeting.chat.created/`). They remain readable; new writes land at the category paths. No backfill.

### 2.2 Well-known transcript files

For any meeting where Record-and-Transcribe was on, the **post-meeting official Microsoft transcript** lands at exactly two paths (overwritten on each re-fetch):

```
meetings/{meeting_id_sanitized}/transcripts/official.txt   # clean speaker-per-line plaintext
meetings/{meeting_id_sanitized}/transcripts/official.vtt   # raw WebVTT (start/end cues, <v> markup)
```

`official.txt` is what an exec opens. `official.vtt` is what a parser opens.

Channel meetings produce no official transcript via this bot — `OnlineMeetingTranscript.Read.Chat` is private-chat-only. A consumer with a calendar-invitee's delegated user token can fetch channel-meeting transcripts directly from `GET /me/onlineMeetings/{meeting_id}/transcripts` (bypasses this bot).

### 2.3 Blob body shape

```jsonc
// meetings/19_meeting_.../live_transcript/20260514T163412184Z-8a3f...0304.json
{
  "schema_version": "alfred-v2",
  "event_type":     "meeting.transcript.final",
  "event_id":       "8a3f1c0e2b9d4a7e9f12bb0001020304",
  "ts":             "2026-05-14T16:34:12.184Z",
  "meeting_ref": {
    "meeting_id":             "19:meeting_NmFkYW...@thread.v2",
    "meeting_chat_thread_id": "19:meeting_NmFkYW...@thread.v2",
    "subject":                "Sprint planning",
    "organizer":              { "aad_id": "...", "display_name": "Jane Doe" },
    "channel_link":           null
  },
  "payload": {
    "text":          "Let's start with the deploy retro.",
    "timestamp_utc":"2026-05-14T16:34:12.184Z",
    "speaker":       { "id": "speaker_0", "display_name": "Jane Doe" },
    "audio_start_ms":1234.5,
    "audio_end_ms":  5678.9,
    "confidence":    0.94,
    "provider":      { "name": "azure_speech" }
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
    "sender":       { "aad_id": "...", "display_name": "Logan Robbins", "kind": "user" },
    "text":         "Anyone seen the deploy logs?",
    "is_root":      true,
    "from_bot":     false,
    "timestamp_utc":"2026-05-14T18:23:45.401Z"
  }
}
```

### 2.4 curl recipes

> **Pagination.** Azure Blob `?restype=container&comp=list` returns at most **5000 names per page**. If the response XML contains a non-empty `<NextMarker>...</NextMarker>`, repeat the request with `&marker=<value>` until `<NextMarker>` is empty. Skipping pagination silently truncates.

```bash
SA="https://stalfreddisney.blob.core.windows.net/alfred-events"

# Post-meeting transcript
MID="19_meeting_NmFkYW...thread.v2"   # sanitized
curl -sS "$SA/meetings/$MID/transcripts/official.txt"
curl -sS "$SA/meetings/$MID/transcripts/official.vtt"

# Define once: paginated blob listing
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

# Every event for a meeting
list_prefix "meetings/$MID/"

# Every live-STT chunk in order (final-only)
list_prefix "meetings/$MID/live_transcript/" \
  | sort \
  | while read name; do curl -sS "$SA/$name" \
      | jq -r 'select(.event_type == "meeting.transcript.final")
               | "\(.ts)  \(.payload.speaker.display_name // .payload.speaker.id // "?"):  \(.payload.text)"'; done

# Every chat message in one channel
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

# Print every live-STT final chunk for one meeting.
meeting_id = "19_meeting_NmFkYW...thread.v2"   # sanitized
for name in list_prefix(f"meetings/{meeting_id}/live_transcript/"):
    env = get_json(name)
    if env["event_type"] != "meeting.transcript.final":
        continue
    speaker = (env["payload"].get("speaker") or {}).get("display_name") or "?"
    print(env["ts"], speaker + ":", env["payload"]["text"])

# Dump the official transcript
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

meeting_id = "19_meeting_NmFkYW...thread.v2"   # sanitized

for b in sorted(
    c.list_blobs(name_starts_with=f"meetings/{meeting_id}/"),
    key=lambda b: b.name,
):
    if not b.name.endswith(".json"):
        continue
    env = get_json(b.name)
    print(env["ts"], env["event_type"])
```

---

## 3. Meeting dossier

```python
import httpx

SINK = "https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io"
SA   = "https://stalfreddisney.blob.core.windows.net/alfred-events"

with httpx.Client(timeout=10.0) as c:
    r = c.get(f"{SINK}/v2/resolve",
              params={"kind": "meeting", "subject": "sprint planning"}).json()
    meeting_id = r["matches"][0]["meeting_id"]
    subject    = r["matches"][0]["subject"]

    meta            = c.get(f"{SINK}/v2/meetings/{meeting_id}").json()
    transcript_text = c.get(f"{SA}/meetings/{meeting_id}/transcripts/official.txt").text
    ledger          = c.get(f"{SINK}/v2/meetings/{meeting_id}/events",
                            params={"limit": 500}).json()["events"]

print(subject, "—", meta.get("organizer", {}).get("display_name"))
print(transcript_text[:1000], "...")
print(f"{len(ledger)} events in the ledger")
```

---

## 4. Live processing — register a consumer, don't poll

The sink and blob archive are **read-after-the-fact**. For low-latency processing, register a push consumer (see `docs/event-contract.md` §6):

```bash
BOT="https://alfred-disney-bot.eastus.cloudapp.azure.com"

curl -X POST "$BOT/api/channels/$TID/$CID/consumers" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-consumer","url":"https://my-service/v2/events",
       "event_kinds":["meeting.transcript.final","meeting.chat.created"],
       "enabled":true}'
```

Body field is **`event_kinds`** (not `event_types` — wrong key silently produces empty filter = matches all).

Subscribe to `meeting.transcript.final` (3–5 / min / speaker) for live transcript content. `meeting.transcript.partial` is throttled to 1/speaker/60s by default — it is a progress indicator, not a real-time stream.

For an agent that "listens silently until interesting": consume `meeting.transcript.final`, run your analysis per chunk (or debounced batch), stay silent unless a threshold is crossed or the bot is directly mentioned. The canonical Alfred prompt at `python/batcave_platform/specs/alfred.yaml` encodes this policy.

---

## 5. Idempotency + cadence

- `event_id` (32-char hex) is the natural primary key. Re-runs are exactly-once at the blob layer (same path, same body).
- Blob timestamps are millisecond-precision (`HHmmssfff`); `event_id` suffix disambiguates ties.
- Sink writes the same envelope twice (once into `raw_ingest_envelopes` for replay, once into the per-session ledger) — both keyed on `event_id`.

**Per meeting:**

| Event type                          | Cadence |
|-------------------------------------|---------|
| `meeting.transcript.partial`        | throttled to 1/speaker/60s (`PartialThrottleSeconds`) |
| `meeting.transcript.final`          | ~3–5 / min / speaker |
| `meeting.transcript.official`       | exactly 1, ~1–2 min after meeting ends, if record-and-transcribe was on |
| `meeting.chat.*`                    | 1 per message |
| `meeting.created` / `.ended` / etc. | rare |

**Per channel:**

| Event type                       | Cadence |
|----------------------------------|---------|
| `channel.message.*`              | 1 per message |
| `channel.attached` / `.detached` | rare |

Content-only consumers: filter to `meeting.transcript.final`, `meeting.transcript.official`, `meeting.chat.created`, `channel.message.created`.

---

## 6. V1 polling bridge

The repo root holds two untracked sidecar scripts — `server.py` and `server_1.py` — that bridge a pre-v2 polling consumer during the v1→v2 cutover. They poll both the legacy `channels/{team_sanitized}/{cid_sanitized}/chat.message/{ts}-{eid}.txt` path AND the new v2 `teams/{tid}/channels/{cid}/messages/` path. Not part of v2; this doc does not cover v1. For v1 mechanics see comments inside those files.

---

## 7. Built-in UI

`https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io`

- `/` — meeting picker (subject first; hover for `meeting_id`)
- `/m/<meeting_chat_thread_id>` — per-meeting dossier
- `/channels` — operator console (attach / consumer admin)
- `/archive` — blob-archive folder browser; reads the same `.json` envelopes documented here

Thin client over the same sink + blob endpoints — nothing here is private to the UI.
