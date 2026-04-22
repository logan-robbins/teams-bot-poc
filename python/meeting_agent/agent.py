"""
Alfred meeting agent using the OpenAI Agents SDK.

The live-turn agent consumes the unified meeting ledger (speech + chat)
and produces, on each trigger event:

  - Exactly one structured ``AlfredExtraction`` (his "mind"): rolling
    notes, decisions, open questions, action items, risks, topics,
    running summary.
  - Zero or more tool calls. Today the only tool is
    ``send_to_meeting_chat``; silence is the default — simply do not call
    the tool.

There is no more magic ``action`` enum and no more "SEND/ASK/SILENT"
post-hoc router. The agent acts via tools; it thinks via structured output.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agents import Agent, ModelSettings, Runner, set_default_openai_client
from dotenv import load_dotenv
from openai import AsyncOpenAI
from openai.types.shared import Reasoning
from pydantic import BaseModel

from .models import AlfredExtraction, AnalysisItem
from .output import AnalysisOutputWriter
from .pubsub import get_publisher
from .session import InterviewSessionManager
from .tools import AlfredAgentContext, build_alfred_tools

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)

logger = logging.getLogger(__name__)


def _build_azure_base_url(endpoint: str) -> str:
    trimmed = endpoint.rstrip("/")
    if trimmed.endswith("/openai/v1"):
        return f"{trimmed}/"
    return f"{trimmed}/openai/v1/"


def _configure_openai_client() -> tuple[str, bool]:
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    azure_key = os.environ.get("AZURE_OPENAI_KEY")
    azure_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
    api_type = os.environ.get("OPENAI_API_TYPE", "").lower()

    if api_type == "azure" or (azure_endpoint and azure_key and azure_deployment):
        if not all([azure_endpoint, azure_key, azure_deployment]):
            raise ValueError(
                "Azure OpenAI requires AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, "
                "and AZURE_OPENAI_DEPLOYMENT environment variables"
            )
        base_url = _build_azure_base_url(azure_endpoint)
        azure_client = AsyncOpenAI(base_url=base_url, api_key=azure_key)
        set_default_openai_client(azure_client)
        logger.info("Using Azure OpenAI v1: %s deployment=%s", base_url, azure_deployment)
        return azure_deployment, True

    model = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    logger.info("Using OpenAI model=%s", model)
    return model, False


DEFAULT_MODEL, _IS_AZURE_CONFIGURED = _configure_openai_client()
DEFAULT_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "medium")


class RunningAssessment(BaseModel):
    """Legacy compatibility placeholder retained for package exports."""

    summary: str = ""


# Public alias so callers can import ``InterviewAnalysisOutput`` without
# knowing the concrete name; it always refers to the current extraction shape.
InterviewAnalysisOutput = AlfredExtraction


DEFAULT_ALFRED_INSTRUCTIONS = """You are Alfred, a quiet meeting assistant joining a Teams meeting.

You can hear everything said (diarized transcript) and see every chat
message. On each tick you receive the full meeting context.

Two things happen per tick, independently:

1. You ALWAYS return a structured ``AlfredExtraction`` describing your
   current thinking: updated running_summary, topics, new notes, and
   — most importantly — any new or updated decisions, open_questions,
   action_items, and risks you can identify from the conversation.

2. You MAY call the ``send_to_meeting_chat`` tool to post into the meeting
   chat. Silence is the default. Only call the tool when you have
   concrete value to add (a decision, a missing link, a clarifying
   question that is genuinely blocking progress). Never call to recap
   or narrate. If you would post, prefer one or two sentences.

Rules:
- Strong bias toward silence. Not calling the tool is the right answer
  most of the time.
- Never interrupt flow just to acknowledge.
- If the meeting is muted (`context.alfred_muted == true`), never call
  the tool.
- Intent-alignment matters: when participants commit to something,
  record it as a decision. When they leave something unresolved,
  record it as an open_question. When someone agrees to do something,
  record it as an action_item with an owner if stated.
- Keep notes terse. Lists are deltas, not full history.
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_response_id(result: Any) -> str | None:
    for attr in ("response_id", "last_response_id", "id"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    raw_response = getattr(result, "raw_response", None)
    if raw_response is not None:
        value = getattr(raw_response, "id", None)
        if isinstance(value, str) and value.strip():
            return value
    return None


class AlfredAnalyzer:
    """Analyze a live meeting event and emit one AlfredExtraction plus any tool calls."""

    def __init__(
        self,
        model: Optional[str] = None,
        session_manager: Optional[InterviewSessionManager] = None,
        output_writer: Optional[AnalysisOutputWriter] = None,
        publish_thoughts: bool = True,
        reasoning_effort: Optional[str] = None,
        instructions: Optional[str] = None,
        send_chat_url: Optional[str] = None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.reasoning_effort = reasoning_effort or DEFAULT_REASONING_EFFORT
        self.instructions = instructions or DEFAULT_ALFRED_INSTRUCTIONS
        self.session_manager = session_manager
        self.output_writer = output_writer
        self.publish_thoughts = publish_thoughts
        self.send_chat_url = send_chat_url
        self._publisher = get_publisher() if publish_thoughts else None

        if not _IS_AZURE_CONFIGURED and not os.environ.get("OPENAI_API_KEY"):
            logger.warning("No OpenAI credentials configured. Alfred analysis will fail at runtime.")

        model_settings: ModelSettings | None = None
        if any(prefix in self.model.lower() for prefix in ("gpt-5", "o1", "o3")):
            model_settings = ModelSettings(reasoning=Reasoning(effort=self.reasoning_effort))

        self._agent = Agent[AlfredAgentContext](
            name="Alfred Live Turn Agent",
            instructions=self.instructions,
            model=self.model,
            output_type=AlfredExtraction,
            tools=list(build_alfred_tools()),
            model_settings=model_settings,
        )
        logger.info("AlfredAnalyzer initialized model=%s reasoning=%s", self.model, self.reasoning_effort)

    def _format_history_line(self, index: int, event: dict[str, Any]) -> str:
        kind = str(event.get("kind") or "system").upper()
        role = str(event.get("role") or "unknown")
        display_name = str(event.get("display_name") or event.get("speaker_id") or role)
        prefix = f"{index}. [{event.get('timestamp_utc')}] {kind} {display_name} ({role})"
        text = str(event.get("text") or "").strip()
        if not text:
            return prefix
        return f"{prefix}: {text}"

    def _build_prompt(self, response_text: str, context: Optional[dict[str, Any]]) -> str:
        ctx = context or {}
        stable_prefix = dict(ctx.get("stable_prefix") or {})
        meeting_history = list(ctx.get("meeting_history") or [])
        dynamic_tail = list(ctx.get("dynamic_tail") or [])
        trigger_event = dict(ctx.get("trigger_event") or {})

        parts: list[str] = [
            "# Alfred Meeting Context",
            "## Stable Prefix",
            f"- session_id: {stable_prefix.get('session_id') or ctx.get('session_id') or 'unknown'}",
            f"- candidate_name: {stable_prefix.get('candidate_name') or ctx.get('candidate_name') or 'unknown'}",
            f"- meeting_url: {stable_prefix.get('meeting_url') or ctx.get('meeting_url') or 'unknown'}",
            f"- started_at: {stable_prefix.get('started_at') or ctx.get('started_at') or 'unknown'}",
            f"- prompt_cache_key: {stable_prefix.get('prompt_cache_key') or ctx.get('prompt_cache_key') or ''}",
            f"- latest_response_id: {stable_prefix.get('latest_response_id') or ctx.get('latest_response_id') or ''}",
            f"- alfred_muted: {bool(stable_prefix.get('alfred_muted') or ctx.get('alfred_muted'))}",
            "",
            "## Current Alfred State",
            f"running_summary:\n{stable_prefix.get('running_summary') or ctx.get('running_summary') or '(empty)'}",
            "",
            f"topics: {', '.join(stable_prefix.get('topics') or ctx.get('topics') or []) or '(none)'}",
            f"notes: {' | '.join(stable_prefix.get('notes') or ctx.get('notes') or []) or '(none)'}",
            "",
            "## Full Meeting History",
        ]
        if meeting_history:
            parts.extend(
                self._format_history_line(index, event)
                for index, event in enumerate(meeting_history, start=1)
            )
        else:
            parts.append("(no meeting history yet)")

        parts.extend(["", "## Newly Appended Events"])
        if dynamic_tail:
            parts.extend(
                self._format_history_line(index, event)
                for index, event in enumerate(dynamic_tail, start=1)
            )
        else:
            parts.append("(none)")

        parts.extend(
            [
                "",
                "## Trigger Event",
                self._format_history_line(1, trigger_event) if trigger_event else "(none)",
                "",
                "## Text To Analyze",
                response_text,
                "",
                "Return an AlfredExtraction. Optionally call send_to_meeting_chat.",
            ]
        )
        return "\n".join(parts)

    async def analyze_async(
        self,
        response_text: str,
        context: Optional[dict[str, Any]] = None,
        response_id: Optional[str] = None,
        speaker_id: Optional[str] = None,
    ) -> AnalysisItem:
        if self.session_manager is None:
            raise ValueError(
                "AlfredAnalyzer.analyze_async requires a configured session_manager"
                " so tools can target the active session."
            )

        if not response_text or not response_text.strip():
            fallback = AlfredExtraction(
                rationale="Empty event text; nothing to analyze.",
                running_summary=str((context or {}).get("running_summary") or ""),
                topics=list((context or {}).get("topics") or []),
            )
            return AnalysisItem(
                response_id=response_id or f"resp_{uuid.uuid4().hex[:8]}",
                response_text=response_text or "",
                speaker_id=speaker_id,
                extraction=fallback,
                key_points=fallback.notes,
                raw_model_output={"warning": "empty_response_text"},
            )

        if self._publisher and self.publish_thoughts:
            await self._publisher.publish_observation(
                "Analyzing new Alfred meeting event...",
                speaker_id=speaker_id,
            )

        prompt = self._build_prompt(response_text, context)

        run_ctx = AlfredAgentContext(
            session_manager=self.session_manager,
            send_chat_url=self.send_chat_url,
        )

        try:
            result = await Runner.run(self._agent, prompt, context=run_ctx)
            extraction = result.final_output_as(AlfredExtraction)
            latest_response_id = _extract_response_id(result)

            analysis_item = AnalysisItem(
                response_id=response_id or f"resp_{uuid.uuid4().hex[:8]}",
                timestamp_utc=_utc_now(),
                response_text=response_text,
                speaker_id=speaker_id,
                trigger_event_id=str(((context or {}).get("trigger_event") or {}).get("event_id") or "") or None,
                key_points=list(extraction.notes),
                follow_up_suggestions=[],
                extraction=extraction,
                tool_calls=list(run_ctx.tool_records),
                raw_model_output={
                    "model": self.model,
                    "latest_response_id": latest_response_id,
                },
            )

            if self._publisher and self.publish_thoughts:
                await self._publisher.publish_analysis(
                    content=extraction.rationale,
                    speaker_id=speaker_id,
                    speaker_role=str(((context or {}).get("trigger_event") or {}).get("role") or "unknown"),
                    response_text=response_text[:240],
                    key_points=extraction.notes,
                    follow_up_suggestions=[],
                    running_assessment={
                        "running_summary": extraction.running_summary,
                        "topics": extraction.topics,
                        "decisions": [d.model_dump() for d in extraction.decisions],
                        "open_questions": [q.model_dump() for q in extraction.open_questions],
                        "action_items": [a.model_dump() for a in extraction.action_items],
                        "risks": [r.model_dump() for r in extraction.risks],
                        "tool_calls": [tc.model_dump() for tc in run_ctx.tool_records],
                    },
                )

            return analysis_item
        except Exception as exc:
            logger.error("Alfred analysis failed: %s", exc, exc_info=True)
            if self._publisher and self.publish_thoughts:
                await self._publisher.publish_error(f"Alfred analysis failed: {exc!s}")

            fallback = AlfredExtraction(
                rationale="Analysis failed; Alfred stayed silent.",
                running_summary=str((context or {}).get("running_summary") or ""),
                topics=list((context or {}).get("topics") or []),
            )
            return AnalysisItem(
                response_id=response_id or f"resp_{uuid.uuid4().hex[:8]}",
                response_text=response_text,
                speaker_id=speaker_id,
                trigger_event_id=str(((context or {}).get("trigger_event") or {}).get("event_id") or "") or None,
                key_points=fallback.notes,
                extraction=fallback,
                raw_model_output={"error": str(exc), "model": self.model},
            )

    def analyze(
        self,
        transcript: str,
        context: Optional[dict[str, Any]] = None,
        response_id: Optional[str] = None,
        speaker_id: Optional[str] = None,
    ) -> AnalysisItem:
        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "Cannot call sync analyze() from an async context. Use 'await analyze_async()' instead."
            )
        except RuntimeError as exc:
            if "Cannot call sync analyze" in str(exc):
                raise
            return asyncio.run(
                self.analyze_async(
                    response_text=transcript,
                    context=context,
                    response_id=response_id,
                    speaker_id=speaker_id,
                )
            )

    async def analyze_with_session(
        self,
        response_text: str,
        speaker_id: Optional[str] = None,
    ) -> Optional[AnalysisItem]:
        if not self.session_manager:
            raise ValueError("No session_manager configured. Pass one to __init__.")
        if not self.session_manager.is_active:
            logger.warning("No active session - skipping Alfred analysis")
            return None
        context = self.session_manager.get_agent_context_snapshot()
        return await self.analyze_async(
            response_text=response_text,
            context=context,
            speaker_id=speaker_id,
        )


InterviewAnalyzer = AlfredAnalyzer


def create_alfred_analyzer(
    model: Optional[str] = None,
    session_manager: Optional[InterviewSessionManager] = None,
    output_dir: Optional[str] = None,
    publish_thoughts: bool = True,
    reasoning_effort: Optional[str] = None,
    instructions: Optional[str] = None,
    send_chat_url: Optional[str] = None,
) -> AlfredAnalyzer:
    output_writer: AnalysisOutputWriter | None = None
    if output_dir:
        output_writer = AnalysisOutputWriter(Path(output_dir))

    return AlfredAnalyzer(
        model=model,
        session_manager=session_manager,
        output_writer=output_writer,
        publish_thoughts=publish_thoughts,
        reasoning_effort=reasoning_effort,
        instructions=instructions,
        send_chat_url=send_chat_url,
    )


def create_interview_analyzer(
    model: Optional[str] = None,
    session_manager: Optional[InterviewSessionManager] = None,
    output_dir: Optional[str] = None,
    publish_thoughts: bool = True,
    reasoning_effort: Optional[str] = None,
    instructions: Optional[str] = None,
    send_chat_url: Optional[str] = None,
) -> AlfredAnalyzer:
    return create_alfred_analyzer(
        model=model,
        session_manager=session_manager,
        output_dir=output_dir,
        publish_thoughts=publish_thoughts,
        reasoning_effort=reasoning_effort,
        instructions=instructions,
        send_chat_url=send_chat_url,
    )
