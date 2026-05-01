# Alfred Live Meeting Agent — Plan v2

## Goal

Alfred is an active, continuous meeting observer. Speech and chat flow into
one unified ledger; on each debounced tick the agent appends only the **new
chunks** to a server-side Responses chain, updates the dossier (decisions,
open questions, action items, risks, summary, topics), and optionally replies
via `send_to_meeting_chat`. When directly addressed ("Alfred, make a note
that…"), the silence-default is overridden and Alfred acts.

Whole-meeting context lives in the Responses API chain
(`previous_response_id`), not in the prompt we re-send every tick.

## Findings vs current code

| Plan claim | File / line |
|---|---|
| Analyzer stores `latest_response_id` but never passes it to `Runner.run` | `agent.py:332` — chaining is dead code |
| `latest_response_id` is wired through session + persistence | `models.py:309`, `session.py:425` — read it back |
| Mute UI is local React state; no sink endpoint | `MeetingDossier.tsx:29`, `transcript_sink.py` |
| SSE `session_state` already carries `alfred_muted` | `transcript_sink.py:977-986`, `useSessionStream.ts:74-81` — only the write path is missing |
| C# `SendChatController` keys references on `chat_thread_id`; bot captures the reference on `OnConversationUpdateActivityAsync` (before any chat) | `AlfredBot.cs:38, 69`, `SendChatController.cs:61` — we can seed `session.conversation_reference_id` from a transcript's `chat_thread_id` |
| Local SDK supports `previous_response_id` and exposes `RunResult.last_response_id` | `agents/run.py:299, 323, 384`, `agents/result.py:133-138` |

## Architecture

### Per-turn input is delta-only

The Responses API stores prior user messages and the assistant's structured
outputs server-side. Each new turn appends only:

```
- newly-appended events since last cursor (speech + chat)
- trigger event (last in the debounced batch)
- direct_address bool
- alfred_muted bool (only if it changed)
```

We do **not** resend the meeting history, dossier, summary, or topics each
turn. The chain has them.

### Three turn flows

- **Tick 1 (new meeting):** no `previous_response_id`. Send the system prompt
  + first delta. Persist returned `response_id`.
- **Tick N (steady-state):** `Runner.run(..., previous_response_id=session.latest_response_id)`
  with the delta only. Persist new id.
- **Recovery (stale id):** if `openai.BadRequestError` references
  `previous_response`, clear the id, reseed once with the canonical session
  snapshot (running summary + topics + dossier as currently merged), run with
  no chaining, resume chaining from the new id.

### Code touchpoints

- `agent.py::AlfredAnalyzer.analyze_async`
  - Drop the full-history block from `_build_prompt` (`agent.py:259-265`).
  - Add `_build_delta_prompt(context)` returning `dynamic_tail + trigger + direct_address`.
  - Add `_build_reseed_prompt(context)` returning the canonical snapshot for recovery.
  - Pass `previous_response_id=session.latest_response_id` to `Runner.run`.
  - Catch `openai.BadRequestError` matching the stale-id signature → clear id → retry once with reseed input.
  - Persist returned id via existing `mark_agent_progress`.
- `session.py::get_agent_context_snapshot` already returns the full context.
  The analyzer just chooses which pieces to render.

## Direct-address handling

Today's prompt biases hard toward silence — wrong when explicitly addressed.

- **Detector** (pure function in `meeting_agent/events.py`):
  - `^\s*alfred[\s,:!?]` (case-insensitive)
  - `@alfred\b`
  - `\bhey alfred\b`, `\bok alfred\b`, `\bexcuse me alfred\b`
- **Wiring** — `transcript_sink.py::enqueue_analysis_event` runs the detector
  on the trigger text and attaches `direct_address: bool` to the analysis
  context.
- **Prompt** (added to `alfred.yaml::agent.prompt_template`):
  > When `direct_address` is true the silence-default is overridden. Treat
  > the trigger as a command. Imperatives ("make a note", "remember", "add
  > an action item", "ask the team") map to the corresponding structured
  > field (notes / action_items / decisions / open_questions). Confirm
  > receipt with one sentence via `send_to_meeting_chat(kind='statement')`.

No new tools — existing structured output covers every imperative we expect.

## conversation_reference_id seed

`session.conversation_reference_id` is only set when the first chat arrives
(`session.py:870-873`). If a meeting is voice-only at first, or Alfred is
addressed by voice before anyone types, the send tool fails with
`no_conversation_reference`.

Fix: in `start_session` (and as a fallback in `add_transcript`), if
`chat_thread_id` is provided and `session.conversation_reference_id` is None,
set it to `chat_thread_id`. The C# bot already keys its
`ConversationReferenceStore` on `chat_thread_id`, and
`OnConversationUpdateActivityAsync` captures the reference on bot install —
before any speech.

## Mute endpoint (per-meeting)

Add:

```
POST /m/{chat_thread_id:path}/mute   { "muted": true }
```

Handler resolves manager via `_resolve_meeting_or_404`, sets
`session.alfred_muted`, calls `store.upsert_session`, publishes a
`session_state` event with `alfred_muted`. UI already consumes the SSE
update (`sessionStore.ts:170-173`); replace `useState(false)` in
`MeetingDossier.tsx:29` with the store-driven flag and a `sink.setMuted`
call.

No legacy `/session/mute` analog — multi-meeting routing is the only
supported path.

## Implementation sequence

Each step is testable independently.

1. **`conversation_reference_id` seed** — [x]
   - `session.py::start_session`: when `chat_thread_id` is non-empty and
     `conversation_reference_id` is None, set it to `chat_thread_id`.
   - Confirm `send_to_meeting_chat_impl` works on a transcript-only session.

2. **Direct-address detector + wiring** — [x]
   - Add `meeting_agent/events.py::detect_direct_address(text) -> bool`.
   - `transcript_sink.py::enqueue_analysis_event` attaches `direct_address`
     to the agent context.

3. **Responses chaining + delta prompt** — [x]
   - Drop full-history rendering from `_build_prompt`.
   - Add `_build_delta_prompt` and `_build_reseed_prompt`.
   - Pass `previous_response_id` to `Runner.run`.
   - Catch stale-id `BadRequestError`, clear id, retry once with reseed input.
   - Persist new id.

4. **Prompt rewrite for continuous monitoring + direct address** — [x]
   - `alfred.yaml::agent.prompt_template` is the source of truth.
   - Mirror in `DEFAULT_ALFRED_INSTRUCTIONS` fallback in `agent.py`.

5. **Mute endpoint + React wiring** — [x]
   - `POST /m/{chat_thread_id:path}/mute` route.
   - `sink.setMuted(chatThreadId, muted)` in `web/src/lib/sink.ts`.
   - `MeetingDossier.tsx`: drop `useState`, read `session.alfred_muted` from
     store, call `sink.setMuted` from the toggle handler.

## Verification

- `cd python && uv run pytest tests -v` — baseline 97 passed, 2 skipped must
  hold.
- `cd web && npm run build`.
- `dotnet build`.
- Manual smoke: install in a meeting, speak "Alfred, make a note…", confirm
  it posts back and the note appears on the dossier.
