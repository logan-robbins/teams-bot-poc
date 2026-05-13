# Retrieving transcripts from the blob archive

The Alfred bot mirrors every event it captures into an Azure Blob
Storage container as individual `.txt` files. Any engineer with the
team's channel id (or a meeting's chat-thread id) can read these
directly — no API key, no per-team auth, no consumer to register.
This document is the contract for that read path.

## Where the data lives

| | |
|---|---|
| Storage account | `stalfreddisney` |
| Container | `alfred-events` |
| Endpoint | `https://stalfreddisney.blob.core.windows.net/alfred-events/` |
| Auth (read) | Anonymous public read (container ACL = `container`) |
| Auth (write) | Restricted to the bot/sink managed identities (or account key) |
| Region | `eastus` |
| Subscription | `e02c0038-82c8-4655-9647-38083f301099` (WDI R&D) |

Reads work from any browser, any HTTP client, any region — there is
no IP firewall on the storage account today. If that changes you'll
get `403 AuthenticationFailed` and need to come in over the WDI VNet.

## Path layout

Every blob is one event, one `.txt` file. Folders are derived from
blob name prefixes — Azure Blob has no real folders.

```
channels/{teamId}/{sanitizedChannelId}/{eventKind}/{utcTimestamp}-{eventId}.txt
meetings/{sanitizedChatThreadId}/{eventKind}/{utcTimestamp}-{eventId}.txt
meetings/{sanitizedChatThreadId}/_official-transcript.txt
```

| Segment | Meaning |
|---|---|
| `teamId` | AAD group id for the team (GUID, lowercase, dashes). |
| `sanitizedChannelId` | Teams channel id with non-alphanumeric chars (`:`, `@`) replaced by `_`. Example: `19:abc@thread.tacv2` → `19_abc_thread.tacv2`. |
| `sanitizedChatThreadId` | Same sanitization applied to a `chat_thread_id` (used for meeting-only events that aren't keyed to a channel). |
| `eventKind` | One of `chat.message`, `transcript.partial`, `transcript.final`, `transcript.official`, `system.channel_attached`, `system.session_linked`. |
| `utcTimestamp` | ISO-8601 compact, `yyyyMMddTHHmmssfffZ`. Sorts in time order. |
| `eventId` | 32-char hex Guid, stable for dedupe. |

### The post-meeting transcript

After a Teams meeting ends with Record-and-Transcribe enabled,
Alfred fetches Microsoft's official transcript from Graph and writes
it as a single flat blob:

```
meetings/{sanitizedChatThreadId}/_official-transcript.txt
```

Body is the raw WebVTT (`text/plain`). The underscore prefix sorts
it to the top of any listing. The corresponding per-event envelope
also lands at
`channels/.../transcript.official/{ts}-{eventId}.txt` (or
`meetings/.../transcript.official/...` for non-channel meetings) with
the parsed cues.

## File contents

A streaming event blob is a pretty-printed `alfred-events-v1`
envelope:

```json
{
  "schema_version": "alfred-events-v1",
  "event_type": "chat.message",
  "event_id": "8cdad195eeea41c7a28ace09b0e3f998",
  "ts": "2026-05-13T18:23:45.401Z",
  "team_id": "d3f5f412-2abf-4300-ac73-019e892c2a05",
  "channel_id": "19:abc@thread.tacv2",
  "chat_thread_id": "19:abc@thread.tacv2",
  "channel_thread_id": "19:abc@thread.tacv2",
  "payload": {
    "event_type": "chat_created",
    "text": "hey alfred, what's up",
    "sender_id": "accf88ee-...",
    "sender_display_name": "Logan Robbins",
    "timestamp_utc": "2026-05-13T18:23:45.401Z",
    "from_bot": false,
    "conversation_kind": "channel",
    "team_id": "d3f5f412-...",
    "channel_id": "19:abc@thread.tacv2"
  }
}
```

The envelope contract is stable: additive-only within
`alfred-events-v1`, breaking changes ship as `v2`. Full schema in
[`docs/event-contract.md`](event-contract.md).

## How to find your folder

You need either:
- a Teams team's AAD group id + channel id (for channel events), or
- a meeting's `chat_thread_id` (for meeting-only events)

### Get the team + channel id from Teams

In the Teams client:
1. Right-click the team name → **Get link to team** →
   the URL contains `groupId=<teamId>`.
2. Right-click any channel → **Get link to channel** →
   the URL contains `channelId=19%3Axxx%40thread.tacv2`.
   URL-decode it (you want `19:xxx@thread.tacv2`).

### Sanitize the channel id for the path

The blob path uses underscores for `:` and `@`. Here's the rule, in
Python:

```python
import re

def sanitize(raw: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\-_.]", "_", raw)

team_id    = "d3f5f412-2abf-4300-ac73-019e892c2a05"
channel_id = "19:abc@thread.tacv2"

prefix = f"channels/{team_id}/{sanitize(channel_id)}/"
# → channels/d3f5f412-.../19_abc_thread.tacv2/
```

## Listing and downloading

### curl (anonymous)

```bash
SA="https://stalfreddisney.blob.core.windows.net/alfred-events"

# List everything under a channel
curl -s "$SA?restype=container&comp=list&delimiter=/&prefix=channels/$TEAM_ID/$SAN_CHAN/"

# List only chat.message events for that channel
curl -s "$SA?restype=container&comp=list&prefix=channels/$TEAM_ID/$SAN_CHAN/chat.message/"

# Download one event
curl -s "$SA/channels/$TEAM_ID/$SAN_CHAN/chat.message/20260513T182345401Z-8cdad195eeea41c7a28ace09b0e3f998.txt" | jq

# Download the post-meeting transcript
curl -s "$SA/meetings/$SAN_THREAD/_official-transcript.txt"
```

Azure's list endpoint returns XML; use `xmllint` or parse with your
language of choice. Pagination is via the `<NextMarker>` element —
include it as `&marker=<value>` to get the next page.

### Python (azure-storage-blob)

```python
from azure.storage.blob import ContainerClient

URL = "https://stalfreddisney.blob.core.windows.net/alfred-events"

# Anonymous: pass an explicit credential=None to make it clear.
client = ContainerClient.from_container_url(URL, credential=None)

team_id = "d3f5f412-2abf-4300-ac73-019e892c2a05"
san_chan = "19_abc_thread.tacv2"   # sanitize() the channel id first
prefix = f"channels/{team_id}/{san_chan}/chat.message/"

# Newest first, last 50
blobs = sorted(client.list_blobs(name_starts_with=prefix),
               key=lambda b: b.name, reverse=True)[:50]

for b in blobs:
    body = client.download_blob(b.name).readall().decode()
    print(body)
```

### Python (zero deps)

```python
import urllib.request, json, re
from xml.etree import ElementTree as ET

SA = "https://stalfreddisney.blob.core.windows.net/alfred-events"

def list_prefix(prefix: str):
    url = f"{SA}?restype=container&comp=list&prefix={prefix}"
    xml = urllib.request.urlopen(url).read()
    root = ET.fromstring(xml)
    for b in root.iter("Blob"):
        yield b.findtext("Name")

def download(name: str) -> str:
    return urllib.request.urlopen(f"{SA}/{name}").read().decode()

for name in list_prefix("channels/d3f5f412-.../19_abc_thread.tacv2/chat.message/"):
    env = json.loads(download(name))
    p = env["payload"]
    print(f"{env['ts']} {p['sender_display_name']:<20} {p['text']}")
```

### Azure CLI

```bash
az storage blob list \
  --account-name stalfreddisney --container-name alfred-events \
  --prefix "channels/$TEAM_ID/$SAN_CHAN/" \
  --auth-mode login \
  --query "[].{name:name,modified:properties.lastModified}" -o table
```

You'll need either `--auth-mode login` (and the `Storage Blob Data
Reader` role on the storage account) or `--account-key <key>` for
listing via the CLI. The anonymous REST endpoint above is the simpler
read path when the listing has fewer than ~200 entries.

## Built-in UI

The web app has an in-browser folder browser at
[`/archive`](https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io/archive)
— same data, walks the same anonymous LIST endpoint, click any file
to open it. It's the easiest path for a quick spot-check; the
programmatic recipes above are for ingestion pipelines.

## Cadence and volume expectations

Per channel, per active meeting:

| Event kind | Approximate cadence |
|---|---|
| `chat.message` | one blob per message, in either direction |
| `transcript.partial` | throttled to one per active speaker per 60s (configurable via `EventDispatch__PartialThrottleSeconds`) |
| `transcript.final` | one per spoken utterance, ~3–5/minute per active speaker |
| `transcript.official` | exactly one (the full Microsoft transcript), ~1–2 minutes after the meeting ends, only if Record-and-Transcribe was enabled |
| `system.*` | rare lifecycle markers (channel attached, session linked) |

If you only care about content-bearing events, filter for
`event_type ∈ {chat.message, transcript.final, transcript.official}`
and skip the partial / system prefixes entirely.

## Idempotency / dedupe

- `event_id` is stable per event. Use it as your primary key when
  ingesting.
- Blobs are written exactly once. If your job crashes mid-batch, just
  re-run — the same blob names will produce the same entries.
- Timestamps in path names are second-precision; if two events share
  one second the `eventId` suffix disambiguates.

## What you do NOT need

- No `ConsumerConfig` registration on the bot — the archive runs
  parallel to the consumer fan-out; if you only need history, the
  blobs are sufficient.
- No tenant Graph permissions — the storage account is in our
  subscription, not behind any M365 consent surface.
- No per-team auth — anonymous read is enabled at the container level.

If you need a real-time push instead of polling the archive, register
a consumer URL via the bot's per-channel consumer API — see
[`docs/event-contract.md`](event-contract.md) and the
`/channels` operator UI.
