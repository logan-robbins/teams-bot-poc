# PLAN.md - Email-Based Client Routing

## Goal

Make client-owned Alfred implementations easy to route without requiring
users to know Teams meeting ids, chat thread ids, team ids, or channel ids.

User-facing rule:

1. A client registers an email and sink URL once.
2. That person adds Alfred to a meeting or interacts with Alfred.
3. The C# bot resolves that person's email, binds the meeting to the
   registered client route, and sends all future events/transcripts for
   that meeting to the client's sink.

Example:

```text
michael.barron@disney.com -> https://michael-agent.example.com/v2/events
```

Michael should not need to know anything else.

## Current Behavior

The C# bot already has the platform rails:

1. It builds one `alfred-v2` envelope per event.
2. It writes the envelope to Azure Blob Storage.
3. It POSTs the envelope to matching HTTP consumers.
4. If no channel/meeting route matches, it falls back to
   `EventDispatch.BootstrapConsumerUrl`, which is currently our Python sink.

The missing piece is a simple person-based route:

```text
person email -> sink URL
meeting chat thread -> person email
```

## Routing Principle

Use email as the public `client_id`.

Use AAD object ids internally because Teams events reliably expose AAD ids
more often than SMTP email. Email remains the admin/user-facing key.

Do not ask users for meeting ids, team ids, channel ids, or chat thread ids.
The bot must infer and persist those bindings.

## Data Model

### `client_routes`

One row per client-owned Alfred destination.

```text
email              lowercased email; primary key
sink_url           absolute HTTPS URL, usually /v2/events
event_kinds        list of event_type filters; ["*"] allowed
headers            optional outbound HTTP headers
enabled            bool
created_at_utc
updated_at_utc
```

### `client_identity_aliases`

Internal identity mapping.

```text
email              lowercased email
tenant_id          AAD tenant id when known
aad_object_id      AAD user object id
source             graph_user_lookup | teams_activity | manual
created_at_utc
updated_at_utc
```

Unique key:

```text
tenant_id + aad_object_id
```

### `meeting_routes`

Sticky binding from a Teams meeting/chat to a client route.

```text
meeting_chat_thread_id   19:meeting_...@thread.v2
meeting_id               same meeting key used on envelopes when known
email                    lowercased client route email
source                   installer | organizer | sender | manual
created_at_utc
updated_at_utc
```

Why this table matters:

Live transcript chunks may only carry the meeting/chat id. They should not
need to re-resolve Michael's identity on every chunk.

## Resolution Flow

### 1. Client Registers

Add one route:

```text
michael.barron@disney.com -> https://michael-agent.example.com/v2/events
```

This should be possible from:

1. Web UI under `/channels` or a new `/clients` view.
2. Operator API.
3. Later: Teams command, for example:
   `@Alfred route me to https://michael-agent.example.com/v2/events`

### 2. Alfred Sees a Meeting or Chat

As soon as the bot receives a meeting/chat activity, resolve the best
candidate person:

1. Person who added Alfred to the meeting chat, if available.
2. Meeting organizer from TeamsInfo / meeting metadata.
3. First non-bot sender in the meeting chat.
4. Call join organizer metadata, as a backup.

If the event provides only AAD object id, resolve email:

```text
AAD object id -> Graph /users/{id}?$select=id,mail,userPrincipalName
```

Email choice:

1. `mail`, if populated.
2. `userPrincipalName`, if `mail` is empty.

Normalize:

```text
trim + lowercase
```

### 3. Bind the Meeting

If the resolved email exists in `client_routes` and is enabled:

```text
meeting_chat_thread_id -> email
```

Persist this immediately in `meeting_routes`.

### 4. Route Future Events

Route order should be:

1. Explicit `meeting_routes[meeting_chat_thread_id]`.
2. Explicit channel consumers.
3. Person route by installer/adder email.
4. Person route by organizer email.
5. Person route by first non-bot sender email.
6. Existing `EventDispatch.BootstrapConsumerUrl`.

Blob archive writes still happen independently for every event.

## Failure Behavior

Fail open to the existing fallback path.

If email cannot be resolved:

1. Write the blob as usual.
2. POST to bootstrap fallback.
3. Log a clear structured event:

```text
client_route_unresolved
meeting_chat_thread_id=<id>
aad_object_id=<id or null>
candidate_source=<installer|organizer|sender|call_join>
reason=<graph_404|graph_forbidden|missing_mail|no_client_route>
```

If email resolves but no route exists:

```text
client_route_missing email=<email>
```

If route exists but the client's sink fails:

Use the existing per-consumer retry behavior. Do not block blob archive
writes or other consumers.

## Security Notes

1. Sink URLs must be absolute HTTPS URLs.
2. Optional headers should support a per-client shared secret.
3. Do not put secrets in logs.
4. Email route ownership should be controlled by operator API/UI first.
   Teams self-service command can come later after ownership rules are clear.

## Implementation Checklist

1. Add file-backed or durable store on the C# bot for `client_routes`,
   `client_identity_aliases`, and `meeting_routes`.
2. Add `GraphMetadataResolver.GetUserAsync(aadObjectId)` returning
   `id`, `mail`, and `userPrincipalName`.
3. Add route manager service:
   `ResolveClientRouteAsync(envelope, candidateIdentities)`.
4. In meeting/chat handlers, resolve candidate person email as early as
   possible and persist `meeting_routes`.
5. Update `EventFanoutDispatcher.ResolveConsumers` to check
   `meeting_routes` before bootstrap fallback.
6. Add operator endpoints:
   - `GET /api/client-routes`
   - `POST /api/client-routes`
   - `DELETE /api/client-routes/{email}`
   - `GET /api/client-routes/{email}/meetings`
7. Add web UI controls for email -> sink URL registration.
8. Add tests for:
   - email route wins for private meeting
   - meeting route sticks for transcript events
   - missing email falls back to bootstrap
   - disabled client route falls back
   - multiple consumers do not block blob archive writes
9. Update README and docs after implementation with exact API shapes.

## Open Decisions

1. Storage backend for the C# bot route tables:
   - File-backed JSON matches existing channel/conversation stores.
   - Postgres would be cleaner long-term but introduces another dependency
     to the Windows VM.
2. Whether client route registration should be operator-only initially.
3. Whether one email can have multiple sink URLs.
   Initial recommendation: one enabled route per email.
4. Whether channel consumers should override person routes.
   Initial recommendation: explicit `meeting_routes` first, then channel
   consumers, then person routes, then bootstrap fallback.
