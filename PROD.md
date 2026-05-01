# Alfred POC: productionalization status + open work

This file is a status board. For each enhancement, "what landed" describes
the canonical implementation; "what's left" lists the deferred slice (and
its blocker if any). Treat this file as authoritative for "what's done"
and the README as authoritative for "how to operate."

Six enhancements are tracked.

| # | Enhancement | Status |
|---|---|---|
| E1 | Append-only raw audit store | ✅ Live |
| E2 | Split raw audit from agent memory | ✅ Live |
| E3 | Participant identity layer | ✅ Python live; C# publisher deferred |
| E4 | Explicit proactivity policy (no action enum) | ✅ Live |
| E5 | Per-meeting sink routing | 🟡 Deferred (auto-invite blocks registration) |
| E6 | Per-meeting URL-routed UI | ✅ Live |

---

## E1 — Append-only raw audit store ✅

**Goal:** every inbound event from Teams media, STT, Graph notification,
and Bot Framework is captured to `raw_ingest_events` BEFORE any filter
(partial drop, session-active, echo suppression). Hash-keyed for
deduplication; back-linked to promoted ledger rows.

**What landed**
- New table `raw_ingest_events` (DDL in `python/meeting_agent/persistence.py`).
- `record_raw(...)` helper in `python/transcript_sink.py` called from
  `/transcript`, `/chat`, `/session/participants`, and the manual
  speaker-mapping route.
- `MeetingEvent.source_raw_event_ids: list[str]` carries the back-link.
- Read endpoints: `GET /sessions/{id}/raw-events` (paginated by `since` +
  `limit`) and `GET /sessions/{id}/raw-events/export.ndjson` (streamed).
- Drop reasons recorded: `partial_transcript`, `session_inactive`,
  `echo_suppressed`, `duplicate_message_id`, `malformed`.

**Tests:** `tests/test_sink.py::TestRawIngestAudit` (4 tests covering
partial drop, post-end drop, promoted backlink, NDJSON export).

---

## E2 — Split raw audit from agent memory ✅

**Goal:** allow the working ledger to be cleaned (drop filler, merge
fragments, mark events superseded) without losing audit fidelity, because
the raw layer underneath is immutable.

**What landed**
- `meeting_events.source_raw_event_ids_json` (back-link) and
  `meeting_events.superseded_by` columns; additive migration runs at
  startup.
- `InterviewSessionManager.add_transcript` and `add_chat_message` accept
  and forward `raw_event_ids`.
- `Decision`/`OpenQuestion`/`ActionItem`/`Risk` already carried
  `source_event_ids` — no change.

**Out of scope for the POC:** filler-pruning and fragment-merge are not
wired. The layering and back-links are in place so future cleanup is
non-destructive.

**Tests:** `tests/test_persistence.py::test_store_meeting_event_round_trips_raw_backlinks`.

---

## E3 — Participant identity layer 🟡 (Python live; C# publisher deferred)

**Goal:** use Teams as the source of truth for "who is speaking" — it
already knows. Resolve every speech event to an AAD object id +
display name when possible.

### Python side ✅

- New tables: `meeting_participants`, `participant_msi_bindings`,
  `speaker_identity_links`.
- New module `python/meeting_agent/identity.py` with `ParticipantResolver`.
  Priority order: `manual > teams_msi_unique > teams_msi_group > sole_human > unresolved`.
- `TranscriptEventRequest` accepts `dominant_media_source_id` and
  `active_media_source_ids` (top-level or in `metadata`).
- Endpoints:
  - `POST /session/participants` — bot-side roster snapshot.
  - `POST /sessions/{id}/speaker-mapping` — manual override (sticky).
  - `GET /sessions/{id}/participants`
  - `GET /sessions/{id}/speaker-identity`
- Retroactive ledger backfill on identity upgrade (allowed by E2).

**Tests:** `tests/test_sink.py::TestRawIngestAudit::test_msi_unique_resolves_speaker_to_aad`,
`test_two_speakers_on_one_msi_become_group`,
`test_manual_mapping_overrides_automatic`,
`test_msi_in_metadata_is_picked_up`.

### C# side — partial

Live: `TranscriptEvent` carries `DominantMediaSourceId` +
`ActiveMediaSourceIds`. `CallHandler.OnAudioMediaReceived` snapshots
`buffer.ActiveSpeakers` via `Volatile.Write`; `CallHandler.GetMediaSourceIdHint`
exposes the current dominant + active set.
`AzureConversationTranscriber.SetMediaSourceIdHintProvider(Func<…>)` is
wired from `TeamsCallingBotService.HandleCallAdded` so every published
event stamps the hint.

**What's left**
- `Call.Participants.OnUpdated` subscription + a `PythonParticipantsPublisher`
  that POSTs `{participants: [{aad_object_id, display_name, media_source_ids, …}]}`
  to `POST /session/participants` on each roster delta. Without this,
  `meeting_participants` stays empty and the resolver falls through to
  `sole_human` / `unresolved` for every speaker. The Python receive side
  is fully in place.
- Deepgram cleanup (file deletes, `BotConfiguration` cleanup, csproj
  package removal). Disney config already pins `Stt.Provider=AzureSpeech`,
  so this is dead-code cleanup, not behavioral.

---

## E4 — Explicit proactivity policy ✅

**Goal:** encode intervention rules + a cooldown without reintroducing a
`SEND/ASK/SILENT` enum. Contract stays `AlfredExtraction` +
`send_to_meeting_chat`.

**What landed**
- `python/batcave_platform/specs/alfred.yaml` carries the
  `intervention_policy` block (cooldown_seconds, directly_addressed_bypass,
  mention_strings, four rules: `missing_owner`, `missing_due`,
  `implied_decision`, `unresolved_disagreement`).
- `python/batcave_platform/spec_models.py::AgentSpec` has
  `intervention_policy: dict | None`. Loader is Pydantic — no loader change.
- `AlfredAnalyzer._compose_instructions` appends the rules + cooldown
  language to the agent's stable system prompt (preserves the silence
  bias; rules are rendered as exceptions). Stable prefix → cacheable.
- `send_to_meeting_chat_impl` enforces the cooldown server-side and
  returns `SendResult(ok=False, reason="cooldown_active")` so the LLM
  sees its rate-limited attempt on the next tick. Direct address
  ("alfred" mention in the trigger event text) bypasses cooldown.

**Tests:** `tests/test_tools.py::test_tool_refuses_when_cooldown_active`,
`test_tool_allows_when_directly_addressed_bypasses_cooldown`,
`test_zero_cooldown_does_not_block`. Plus
`tests/test_intervention_policy.py` (3 tests on prompt rendering and
spec parsing).

---

## E5 — Per-meeting sink routing 🟡 Deferred

**Decision:** keep one backend sink for now. The right end-state design
exists but is blocked by the auto-invite path having no place to register
a meeting → sink binding.

### Correct end-state design (when picked up)

One bot, many sinks, routed by `chat_thread_id` — **not** as an override.

```
   vm-alfred-disney  ──►  IMeetingSinkRouter  ──►  Sink A / Sink B / …
                          chat_thread_id → sink_id → URL
```

Single canonical lookup. No default. The publishers do
`_router.Resolve(evt.ChatThreadId).Post(...)` and that's the only path.

`appsettings.production.json` carries a named-sinks table:
```json
"Sinks": {
  "team-a": "https://ca-alfred-team-a.../",
  "team-b": "https://ca-alfred-team-b.../"
}
```

### Blocker

The router needs `chat_thread_id → sink_id` populated **before the first
event** for every meeting, including those auto-invited via Graph chat
notifications.

- Explicit `/api/calling/join` can carry `sinkId` — easy.
- Auto-invite has no request body — needs a separate registration
  source (admin-driven config, calendar webhook, or per-tenant default
  derived from organizer AAD). Each option has tradeoffs that aren't
  worth resolving until multi-tenant is a real requirement.

### Today's posture

Single configured `TranscriptSink` block remains the canonical sink for
the whole bot process. Auto-invite continues to work unchanged.
Additional tenants get their own bot deployment until multi-tenant is
prioritized.

---

## E6 — Per-meeting URL-routed UI ✅

**Goal:** one sink + one web deployment hosts N concurrent meetings,
addressable at `/m/<chat_thread_id>`.

**What landed**
- C# bot stamps `chat_thread_id` on every transcript + chat event.
- Sink: `SessionRegistry` keys on `chat_thread_id`; `/m/*` routes
  (`/m`, `/m/{id}/status`, `/m/{id}/events`, `/m/{id}/ledger`,
  `/m/{id}/dossier`, `/m/{id}/end`, `/m/{id}/mute`). Auto-start on
  first inbound event for an unseen thread.
- UI: React Router with `<Route path="/" />` (MeetingList picker) and
  `<Route path="/m/*" />` (MeetingDossier keyed on `chat_thread_id`).
  `useSessionStream(chatThreadId)` hits `/m/{id}/status` +
  `/m/{id}/events`.

**Tests:** `tests/test_sink.py::TestPerMeetingRouting` (5 tests).

---

## Critical files (cross-cut)

| Folder / file | Role |
|---|---|
| `python/transcript_sink.py` | All ingress routes; `record_raw`, resolver wiring, `/m/*` routes, raw-events read endpoints. |
| `python/meeting_agent/` | Canonical session/agent state. `models.py`, `session.py`, `persistence.py`, `agent.py`, `tools.py`, `identity.py`. |
| `python/batcave_platform/specs/alfred.yaml` | Sole source of truth for Alfred's prompt + intervention policy. |
| `src/Services/CallHandler.cs` | E3: snapshots `ActiveSpeakers`; exposes `GetMediaSourceIdHint()`. **TODO:** subscribe to `Call.Participants.OnUpdated`, publish to `/session/participants`. |
| `src/Services/AzureConversationTranscriber.cs` | E3: stamps `DominantMediaSourceId` + `ActiveMediaSourceIds` on every published event. |
| `src/Models/TranscriptEvent.cs` | Wire shape carrying `chat_thread_id` + MSI hints. |
| `scripts/bootstrap-production-vm.ps1`, `scripts/deploy-azure-vm.sh` | Canonical deploy entrypoints. |

## Verification (live)

```bash
SINK=https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io

# Tests
cd python && uv run pytest tests -v       # 118 passed, 2 skipped

# E1: raw store captures partials and pre-session chats
curl -sS -X POST $SINK/transcript -H 'Content-Type: application/json' \
  -d '{"event_type":"partial","text":"hello wor","timestamp_utc":"2026-04-30T17:00:00Z","speaker_id":"speaker_0","chat_thread_id":"19:test@thread.v2"}'
SID=$(curl -sS "$SINK/m/19:test@thread.v2/status" | jq -r .session.session_id)
curl -sS "$SINK/sessions/$SID/raw-events" | jq '.events[].dropped_reason' | sort -u

# E2: backlinks
curl -sS -X POST $SINK/transcript -H 'Content-Type: application/json' \
  -d '{"event_type":"final","text":"ship by friday","timestamp_utc":"2026-04-30T17:01:00Z","speaker_id":"speaker_0","chat_thread_id":"19:test@thread.v2"}'
curl -sS "$SINK/sessions/$SID/ledger" | jq '.events[-1].source_raw_event_ids'

# E3: MSI → AAD resolution
curl -sS -X POST $SINK/session/participants -H 'Content-Type: application/json' \
  -d '{"session_id":"'$SID'","participants":[{"aad_object_id":"aad-A","display_name":"Alex","media_source_ids":[12345]}]}'
curl -sS -X POST $SINK/transcript -H 'Content-Type: application/json' \
  -d '{"event_type":"final","text":"sounds good","timestamp_utc":"2026-04-30T17:01:30Z","speaker_id":"speaker_0","chat_thread_id":"19:test@thread.v2","dominant_media_source_id":12345}'
curl -sS "$SINK/sessions/$SID/speaker-identity" | jq

# E4: cooldown verified by tests; integration check
cd python && uv run pytest tests/test_intervention_policy.py tests/test_tools.py -v
```
