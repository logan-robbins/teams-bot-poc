# TODO

Prioritized backlog with code-level paths. Newest at top.

---

## 1. Post-meeting transcript auto-fetch — hardening (DONE)

Auto-fetch was partially wired (five Register call sites, two of them
channel-only and guaranteed to 404). This session closed the
reliability gaps. Landed changes:

- `src/Services/OfficialTranscriptFetcher.cs` — full rewrite:
  - `PollSession` (mutable per-meeting state) replaces the old
    `_activeFetches`/`_activeFetchOrganizers` pair. Deadline is
    mutable; `RunAsync` re-reads it each iteration. Repeat `Register`
    calls for the same `botCallId` extend the deadline in place,
    backfill missing `OrganizerOid`/`MeetingChatThreadId`, and keep
    the earliest `RegisteredAtUtc` (so the createdDateTime filter in
    `TryFindTranscriptAsync` doesn't tighten on re-entry).
  - Bounded retry: on first deadline lapse the session sleeps 1h,
    flips `RetryUsed=true`, pushes the deadline 30 min forward, and
    polls once more. Second lapse → log warn + drop, leave it to
    operator backfill.
  - File-backed persistence: `OfficialTranscriptFetcherOptions.FilePath`
    points at the on-disk JSON. `IHostedService.StartAsync` →
    `LoadFromDiskAsync` resumes every record whose
    `(retry_used && deadline_utc < now)` filter isn't already
    terminal. Every state mutation (register, extend, retry, emit,
    timeout) triggers a fire-and-forget atomic temp+rename write.
  - Channel-meeting guard: `Register` skips when `botCallId` or
    `meetingChatThreadId` looks like a tacv2 channel thread. The
    `OnlineMeetingTranscript.Read.Chat` RSC is private-chat-only;
    polling channel ids only burns the 30-min budget on guaranteed
    404s.
- `src/Models/BotConfiguration.cs` — new
  `MeetingChatConfiguration.PendingTranscriptFetchStorePath` (default
  `C:\teams-bot-poc\state\pending-transcript-fetches.json`).
- `src/Program.cs` — registers `OfficialTranscriptFetcherOptions`
  and adds the fetcher to the hosted-service set so its disk state
  is loaded before any Register call can land.
- `src/Services/GraphNotificationProcessor.cs` — removes the two
  channel-only Register call sites (post-channel-auto-join,
  channel-system-payload). The latter's
  `MaybeFetchPostMeetingTranscript`, `ExtractMeetingMetadata`,
  `_attemptedTranscriptFetches`, `_callIdRegex`, `_organizerRegex`,
  and the unused `LooksLikeTeamsMeetingSystemPayload` predicate were
  all pure dead weight after the Register removal — deleted.
- `README.md` §10 — replaced the stale "Auto-trigger for `+Apps`
  meetings is not yet wired" paragraph with the current behaviour
  (two triggers, deadline extension, bounded retry, disk
  persistence).
- `docs/retrieving-transcripts.md` §2.2 — added an "Auto-fetch
  lifecycle" paragraph so consumers know when to expect the
  well-known files to appear.

Verified by local docker compile (`docker run … dotnet build … /v:m`)
clean — no warnings, no errors. End-to-end behavior verifies in
production once the next +Apps meeting ends and the official VTT
shows up at `meetings/{mid_sanitized}/transcripts/official.{txt,vtt}`
without an operator-issued backfill.

### Out of scope (unchanged from prior version)

- Channel-meeting transcripts. RSC surface doesn't enable a public
  endpoint. Documented as a deliberate skip; not a TODO.
- Tenant-wide `OnlineMeetingTranscript.Read.All`. Sandbox tenant
  scoping puts this out of reach (README §2).
- Webhook-based "meeting ended" via Graph `communications/callRecords`.
  Same tenant-grant blocker.

---

## 2. Reserved

Add new items above this line as the backlog grows. Keep the
prioritized-newest-at-top convention. One H2 per item.
