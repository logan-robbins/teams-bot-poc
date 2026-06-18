1. [completed] Discover existing demo sink, Python dependencies, and v2 envelope patterns.
2. [completed] Implement `python/intent.py` as an Intent Alignment consumer with sample source data, search, analysis, and persisted memories.
3. [completed] Add a `uv`-based container build path for the intent consumer.
4. [completed] Add focused tests for search, event ingestion, official transcript exclusion, and memory persistence.
5. [completed] Update `README.md` with launch, container, and registration commands.
6. [completed] Verify with `uv run` commands.
7. [completed] Evolve `intent.py` for real-time speech/chat observation batching, reflection cadence, retrieval-gated action, and chat-response preparation.
8. [completed] Update tests and README for the real-time reflection loop.
9. [completed] Verify the evolved intent consumer with `uv run` commands and a container smoke test.
10. [completed] Clarify that `meeting.transcript.final` means a finalized live STT utterance segment, not every raw STT/interim chunk.
11. [completed] Change Azure Speech final-utterance segmentation silence timeout from 200ms to 500ms.
12. [completed] Verify Python intent consumer, Docker image, and C# compile surface where possible.
13. [completed] Publish the intent consumer endpoint and record the public `/v2/events` URL.
14. [completed] Update README with the 500ms segmentation note.
15. [completed] Add a same-process intent monitor UI with live state polling and manual flush.
16. [completed] Verify and publish the UI-enabled intent container.
17. [completed] Add live activity SSE stream and explicit agent status lines for demo visibility.
18. [in_progress] Verify and publish the streaming intent monitor container.
