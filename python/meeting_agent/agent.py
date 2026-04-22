"""
Alfred meeting agent using the OpenAI Agents SDK.

The live-turn agent consumes the unified meeting ledger (speech + chat),
maintains Alfred's running notes/summary/topics, and emits one structured
AlfredAction per analyzed event.
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

from .models import AlfredAction, AnalysisItem
from .output import AnalysisOutputWriter
from .pubsub import get_publisher
from .session import InterviewSessionManager

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


InterviewAnalysisOutput = AlfredAction


DEFAULT_ALFRED_INSTRUCTIONS = """You are Alfred, a quiet meeting assistant joining a Teams meeting.

You can hear everything said (diarized transcript) and see every chat
message. On each tick you receive the full meeting context and must emit
exactly one AlfredAction.

Rules:
- Strong bias toward SILENT.
- Never interrupt flow just to recap.
- SEND only when you add concrete value.
- ASK only when material ambiguity is blocking progress.
- Keep SEND/ASK concise.
- Update running_summary, notes, and topics on every useful turn.
- If context.alfred_muted is true, emit SILENT.
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
    """Analyze a live meeting event and emit one AlfredAction."""

    def __init__(
        self,
        model: Optional[str] = None,
        session_manager: Optional[InterviewSessionManager] = None,
        output_writer: Optional[AnalysisOutputWriter] = None,
        publish_thoughts: bool = True,
        reasoning_effort: Optional[str] = None,
        instructions: Optional[str] = None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.reasoning_effort = reasoning_effort or DEFAULT_REASONING_EFFORT
        self.instructions = instructions or DEFAULT_ALFRED_INSTRUCTIONS
        self.session_manager = session_manager
        self.output_writer = output_writer
        self.publish_thoughts = publish_thoughts
        self._publisher = get_publisher() if publish_thoughts else None

        if not _IS_AZURE_CONFIGURED and not os.environ.get("OPENAI_API_KEY"):
            logger.warning("No OpenAI credentials configured. Alfred analysis will fail at runtime.")

        model_settings: ModelSettings | None = None
        if any(prefix in self.model.lower() for prefix in ("gpt-5", "o1", "o3")):
            model_settings = ModelSettings(reasoning=Reasoning(effort=self.reasoning_effort))

        self._agent = Agent(
            name="Alfred Live Turn Agent",
            instructions=self.instructions,
            model=self.model,
            output_type=AlfredAction,
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
                "Return AlfredAction only.",
            ]
        )
        return "\n".join(parts)

    def _sanitize_action(self, action: AlfredAction, context: Optional[dict[str, Any]]) -> AlfredAction:
        ctx = context or {}
        muted = bool(ctx.get("alfred_muted") or (ctx.get("stable_prefix") or {}).get("alfred_muted"))
        if muted:
            return AlfredAction(
                action="SILENT",
                rationale="Alfred is muted for this meeting.",
                chat_text=None,
                notes=action.notes,
                running_summary=action.running_summary,
                topics=action.topics,
            )

        if action.action == "SILENT":
            action.chat_text = None
            return action

        if not (action.chat_text or "").strip():
            return AlfredAction(
                action="SILENT",
                rationale=f"{action.action} was downgraded because no chat_text was provided.",
                chat_text=None,
                notes=action.notes,
                running_summary=action.running_summary,
                topics=action.topics,
            )

        if action.action == "ASK" and not action.chat_text.rstrip().endswith("?"):
            action.chat_text = f"{action.chat_text.rstrip()}?"

        action.chat_text = action.chat_text.strip()
        return action

    async def analyze_async(
        self,
        response_text: str,
        context: Optional[dict[str, Any]] = None,
        response_id: Optional[str] = None,
        speaker_id: Optional[str] = None,
    ) -> AnalysisItem:
        if not response_text or not response_text.strip():
            fallback = AlfredAction(
                action="SILENT",
                rationale="Empty event text; nothing to analyze.",
                running_summary=str((context or {}).get("running_summary") or ""),
                topics=list((context or {}).get("topics") or []),
            )
            return AnalysisItem(
                response_id=response_id or f"resp_{uuid.uuid4().hex[:8]}",
                response_text=response_text or "",
                speaker_id=speaker_id,
                alfred_action=fallback,
                key_points=fallback.notes,
                raw_model_output={"warning": "empty_response_text"},
            )

        if self._publisher and self.publish_thoughts:
            await self._publisher.publish_observation(
                "Analyzing new Alfred meeting event...",
                speaker_id=speaker_id,
            )

        prompt = self._build_prompt(response_text, context)

        try:
            result = await Runner.run(self._agent, prompt)
            action = result.final_output_as(AlfredAction)
            action = self._sanitize_action(action, context)
            latest_response_id = _extract_response_id(result)

            analysis_item = AnalysisItem(
                response_id=response_id or f"resp_{uuid.uuid4().hex[:8]}",
                timestamp_utc=_utc_now(),
                response_text=response_text,
                speaker_id=speaker_id,
                trigger_event_id=str(((context or {}).get("trigger_event") or {}).get("event_id") or "") or None,
                key_points=list(action.notes),
                follow_up_suggestions=[action.chat_text] if action.chat_text else [],
                alfred_action=action,
                raw_model_output={
                    "model": self.model,
                    "latest_response_id": latest_response_id,
                },
            )

            if self._publisher and self.publish_thoughts:
                await self._publisher.publish_analysis(
                    content=action.rationale,
                    speaker_id=speaker_id,
                    speaker_role=str(((context or {}).get("trigger_event") or {}).get("role") or "unknown"),
                    response_text=response_text[:240],
                    key_points=action.notes,
                    follow_up_suggestions=[action.chat_text] if action.chat_text else [],
                    running_assessment={
                        "running_summary": action.running_summary,
                        "topics": action.topics,
                        "action": action.action,
                    },
                )

            return analysis_item
        except Exception as exc:
            logger.error("Alfred analysis failed: %s", exc, exc_info=True)
            if self._publisher and self.publish_thoughts:
                await self._publisher.publish_error(f"Alfred analysis failed: {exc!s}")

            fallback = AlfredAction(
                action="SILENT",
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
                alfred_action=fallback,
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
    )


def create_interview_analyzer(
    model: Optional[str] = None,
    session_manager: Optional[InterviewSessionManager] = None,
    output_dir: Optional[str] = None,
    publish_thoughts: bool = True,
    reasoning_effort: Optional[str] = None,
    instructions: Optional[str] = None,
) -> AlfredAnalyzer:
    return create_alfred_analyzer(
        model=model,
        session_manager=session_manager,
        output_dir=output_dir,
        publish_thoughts=publish_thoughts,
        reasoning_effort=reasoning_effort,
        instructions=instructions,
    )
