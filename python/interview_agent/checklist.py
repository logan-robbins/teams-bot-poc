"""
Checklist Agent for Interview Progress Tracking.

Determines which checklist item should be updated based on conversation turns.
Runs in parallel with the main InterviewAnalyzer to track interview progress
without blocking response analysis.

Supports both OpenAI and Azure OpenAI backends (inherits from agent.py config).

Last Grunted: 02/01/2026
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from agents import Agent, Runner, function_tool
from openai import AsyncAzureOpenAI

from .models import AnalysisItem


# Load environment variables from .env file
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

CHECKLIST_ITEMS = [
    "Intro",
    "Role Overview",
    "Background",
    "Python Question",
    "Salary Expectations",
    "Next Steps",
]

CHECKLIST_STATUSES = ["pending", "analyzing", "complete"]

TOPIC_KEYWORDS: dict[str, list[str]] = {
    "Intro": [
        "good morning",
        "welcome",
        "thank you for joining",
        "how are you",
        "nice to meet",
        "glad you could",
        "thanks for coming",
    ],
    "Role Overview": [
        "role",
        "position",
        "responsibilities",
        "job description",
        "team",
        "day to day",
        "what you'll be doing",
        "scope",
    ],
    "Background": [
        "experience",
        "background",
        "tell me about yourself",
        "previous",
        "worked",
        "career",
        "resume",
        "journey",
        "history",
    ],
    "Python Question": [
        "python",
        "coding",
        "technical",
        "algorithm",
        "code",
        "programming",
        "debug",
        "data structures",
        "complexity",
        "function",
    ],
    "Salary Expectations": [
        "salary",
        "compensation",
        "expectations",
        "pay",
        "benefits",
        "offer",
        "package",
        "equity",
        "bonus",
    ],
    "Next Steps": [
        "questions for us",
        "next steps",
        "timeline",
        "process",
        "when will we",
        "hear back",
        "anything else",
        "final thoughts",
        "wrap up",
    ],
}


# =============================================================================
# Pydantic Models
# =============================================================================


class ChecklistUpdate(BaseModel):
    """Model for a checklist status update."""

    item: str = Field(
        ...,
        description="Checklist item name (must be one of the predefined items)",
    )
    status: Literal["analyzing", "complete"] = Field(
        ...,
        description="New status for the checklist item",
    )
    reason: str = Field(
        ...,
        description="Brief explanation for the status change",
    )


class ChecklistAgentOutput(BaseModel):
    """Structured output from the checklist agent."""

    should_update: bool = Field(
        ...,
        description="Whether a checklist update is needed",
    )
    update: Optional[ChecklistUpdate] = Field(
        default=None,
        description="The checklist update details (if should_update is True)",
    )


# =============================================================================
# OpenAI Configuration (shared with agent.py pattern)
# =============================================================================


def _get_openai_config() -> tuple[str, Optional[AsyncAzureOpenAI]]:
    """
    Determine OpenAI configuration based on environment variables.
    Uses a faster/smaller model for checklist updates (gpt-4o-mini preferred).
    """
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    azure_key = os.environ.get("AZURE_OPENAI_KEY")
    # Use mini deployment for checklist if available, else fall back to main deployment
    azure_deployment = os.environ.get(
        "AZURE_OPENAI_MINI_DEPLOYMENT",
        os.environ.get("AZURE_OPENAI_DEPLOYMENT"),
    )
    api_type = os.environ.get("OPENAI_API_TYPE", "").lower()

    # Check if Azure OpenAI is configured
    if api_type == "azure" or (azure_endpoint and azure_key and azure_deployment):
        if not all([azure_endpoint, azure_key, azure_deployment]):
            raise ValueError(
                "Azure OpenAI requires AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, "
                "and AZURE_OPENAI_DEPLOYMENT environment variables"
            )

        logger.info(
            f"ChecklistAgent using Azure OpenAI: {azure_endpoint}, "
            f"deployment: {azure_deployment}"
        )

        azure_client = AsyncAzureOpenAI(
            azure_endpoint=azure_endpoint,
            api_key=azure_key,
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
        )

        return azure_deployment, azure_client

    # Fall back to standard OpenAI - use mini model for speed
    model = os.environ.get("OPENAI_CHECKLIST_MODEL", "gpt-4o-mini")
    logger.info(f"ChecklistAgent using OpenAI: model {model}")
    return model, None


# =============================================================================
# Function Tool
# =============================================================================

# Global callback holder - set by ChecklistAgent instance
_checklist_callback: Optional[Callable[[str, str, str], None]] = None


@function_tool
def update_checklist(item: str, status: str, reason: str) -> str:
    """
    Update the interview checklist status for a specific item.

    Args:
        item: The checklist item to update (e.g., "Intro", "Background")
        status: New status - "analyzing" or "complete"
        reason: Brief explanation for the status change

    Returns:
        Confirmation message
    """
    # Validate item
    if item not in CHECKLIST_ITEMS:
        return f"Error: Invalid item '{item}'. Valid items: {CHECKLIST_ITEMS}"

    # Validate status
    if status not in ["analyzing", "complete"]:
        return f"Error: Invalid status '{status}'. Must be 'analyzing' or 'complete'"

    # Call the callback if registered
    if _checklist_callback is not None:
        try:
            _checklist_callback(item, status, reason)
            logger.info(f"Checklist updated: {item} -> {status} ({reason})")
            return f"Successfully updated '{item}' to '{status}'"
        except Exception as e:
            logger.error(f"Callback failed: {e}")
            return f"Error calling callback: {e}"

    logger.warning("No checklist callback registered")
    return f"Updated '{item}' to '{status}' (no callback registered)"


# =============================================================================
# Agent Instructions
# =============================================================================

CHECKLIST_AGENT_INSTRUCTIONS = f"""You are an interview progress tracker. Your job is to monitor the conversation and determine when checklist items should be updated.

## Checklist Items
{', '.join(f'"{item}"' for item in CHECKLIST_ITEMS)}

## Status Meanings
- "analyzing" (yellow): The topic is currently being discussed
- "complete" (green): The topic has been adequately covered

## Your Decision Process

For each conversation turn, determine:

1. **Is this the START of a new topic?**
   - Look for topic transitions, introductions of new subjects
   - If a new topic is starting → Set that item to "analyzing"

2. **Has a topic been adequately covered?**
   - Look for natural conclusions, summaries, or transitions away
   - If a topic seems complete → Set that item to "complete"

3. **Is this a continuation of the current topic?**
   - If just continuing discussion → No update needed

## Topic Detection Guidelines

Intro: Greetings, welcomes, initial pleasantries
Role Overview: Discussion of the job role, responsibilities, team structure
Background: Candidate's experience, work history, education
Python Question: Technical coding questions, algorithms, programming concepts
Salary Expectations: Compensation discussion, benefits, offer details
Next Steps: Wrap-up, timeline discussion, final questions

## Important Rules

- Only call update_checklist when there's a CLEAR topic change
- Don't update too frequently - wait for meaningful transitions
- The interviewer asking a question usually signals topic start ("analyzing")
- The candidate finishing a substantial answer may signal completion
- Use your judgment - not every utterance requires an update

## Output

If an update is needed, call the update_checklist tool.
If no update is needed, respond that no checklist update is required."""


# =============================================================================
# ChecklistAgent Class
# =============================================================================


class ChecklistAgent:
    """
    Agent that determines checklist updates based on interview conversation.
    Uses tool calling to update the checklist status.
    """

    def __init__(
        self,
        checklist_callback: Callable[[str, str, str], None],
        model: Optional[str] = None,
        azure_client: Optional[AsyncAzureOpenAI] = None,
    ) -> None:
        """
        Initialize the ChecklistAgent.

        Args:
            checklist_callback: Function(item, status, reason) called when updating checklist
            model: Model/deployment to use. If None, auto-detects from environment.
            azure_client: Optional Azure OpenAI client override.
        """
        global _checklist_callback
        _checklist_callback = checklist_callback

        # Get configuration
        default_model, default_azure_client = _get_openai_config()
        self.model = model or default_model
        self._azure_client = azure_client or default_azure_client

        # Track current state to avoid redundant updates
        self._current_topic: Optional[str] = None
        self._completed_topics: set[str] = set()

        # Create the agent with tool
        self._agent = Agent(
            name="Checklist Tracker",
            instructions=CHECKLIST_AGENT_INSTRUCTIONS,
            model=self.model,
            tools=[update_checklist],
        )

        provider_info = "Azure OpenAI" if self._azure_client else "OpenAI"
        logger.info(
            f"ChecklistAgent initialized with {provider_info}, model: {self.model}"
        )

    def _build_prompt(
        self,
        speaker_id: str,
        speaker_role: str,
        text: str,
        conversation_history: list[dict],
    ) -> str:
        """Build the analysis prompt from current turn and context."""
        parts = []

        # Add conversation history (last 5 turns for context)
        if conversation_history:
            parts.append("## Recent Conversation")
            for turn in conversation_history[-5:]:
                role = turn.get("role", "unknown")
                turn_text = turn.get("text", "")
                speaker_label = "INTERVIEWER" if role == "interviewer" else "CANDIDATE"
                parts.append(f"[{speaker_label}]: {turn_text}")
            parts.append("")

        # Add current turn
        parts.append("## Current Turn")
        role_label = "INTERVIEWER" if speaker_role == "interviewer" else "CANDIDATE"
        parts.append(f"[{role_label}] (speaker: {speaker_id}): {text}")
        parts.append("")

        # Add current state info
        parts.append("## Current Checklist State")
        if self._current_topic:
            parts.append(f"Currently analyzing: {self._current_topic}")
        if self._completed_topics:
            parts.append(f"Completed topics: {', '.join(sorted(self._completed_topics))}")
        if not self._current_topic and not self._completed_topics:
            parts.append("No topics started yet")
        parts.append("")

        parts.append(
            "Based on this conversation turn, determine if the checklist should be updated. "
            "Call update_checklist if needed, or explain why no update is necessary."
        )

        return "\n".join(parts)

    def _quick_topic_detection(self, text: str) -> Optional[str]:
        """
        Quick keyword-based topic detection for efficiency.
        Returns detected topic or None if no strong match.
        """
        text_lower = text.lower()

        for topic, keywords in TOPIC_KEYWORDS.items():
            matches = sum(1 for kw in keywords if kw in text_lower)
            # Require at least 2 keyword matches for confidence
            if matches >= 2:
                return topic

        return None

    async def analyze_for_checklist(
        self,
        speaker_id: str,
        speaker_role: str,
        text: str,
        conversation_history: list[dict],
    ) -> Optional[dict[str, Any]]:
        """
        Analyze the conversation turn to determine if checklist should be updated.

        Args:
            speaker_id: ID of the current speaker
            speaker_role: "interviewer" or "candidate"
            text: The spoken text
            conversation_history: List of previous conversation turns

        Returns:
            dict with {item, status, reason} if update needed, None otherwise
        """
        if not text or not text.strip():
            return None

        # Quick keyword check for efficiency
        quick_topic = self._quick_topic_detection(text)
        if quick_topic:
            # If interviewer starts a new topic, mark as analyzing
            if speaker_role == "interviewer" and quick_topic != self._current_topic:
                # Complete previous topic if exists
                if self._current_topic and self._current_topic not in self._completed_topics:
                    self._completed_topics.add(self._current_topic)
                    logger.debug(f"Auto-completing previous topic: {self._current_topic}")

                self._current_topic = quick_topic
                logger.info(f"Quick detection: {quick_topic} -> analyzing")
                return {
                    "item": quick_topic,
                    "status": "analyzing",
                    "reason": f"Interviewer initiated {quick_topic.lower()} discussion",
                }

        # For more nuanced cases, use the agent
        prompt = self._build_prompt(speaker_id, speaker_role, text, conversation_history)

        try:
            if self._azure_client:
                result = await Runner.run(self._agent, prompt, client=self._azure_client)
            else:
                result = await Runner.run(self._agent, prompt)

            # Check if tool was called by examining the run result
            # The tool call happens inside Runner.run via the callback
            # We track state changes via the callback, so just return None here
            # if no explicit update was detected

            logger.debug(f"Agent run complete. Final output: {result.final_output}")
            return None

        except Exception as e:
            logger.error(f"Checklist agent analysis failed: {e}", exc_info=True)
            return None

    def update_state(self, item: str, status: str) -> None:
        """
        Manually update internal state tracking.
        Called by the callback to keep state in sync.
        """
        if status == "analyzing":
            self._current_topic = item
        elif status == "complete":
            self._completed_topics.add(item)
            if self._current_topic == item:
                self._current_topic = None

    def reset(self) -> None:
        """Reset the checklist state for a new interview session."""
        self._current_topic = None
        self._completed_topics = set()
        logger.info("ChecklistAgent state reset")


# =============================================================================
# Parallel Analysis Helper
# =============================================================================


async def parallel_analysis(
    analyzer: Any,  # InterviewAnalyzer - avoid circular import
    checklist_agent: ChecklistAgent,
    response_text: str,
    speaker_id: str,
    speaker_role: str,
    context: Optional[dict[str, Any]] = None,
    conversation_history: Optional[list[dict]] = None,
    response_id: Optional[str] = None,
) -> tuple[AnalysisItem, Optional[dict[str, Any]]]:
    """
    Run both analyses in parallel for maximum efficiency.

    Args:
        analyzer: The main InterviewAnalyzer instance
        checklist_agent: The ChecklistAgent instance
        response_text: The text to analyze
        speaker_id: Speaker identifier
        speaker_role: "interviewer" or "candidate"
        context: Context for the main analyzer
        conversation_history: Conversation history for checklist agent
        response_id: Optional response ID

    Returns:
        Tuple of (AnalysisItem, Optional[ChecklistUpdate dict])
    """
    # Only run main analysis for candidate responses
    # Checklist analysis runs for all speakers
    analysis_task = asyncio.create_task(
        analyzer.analyze_async(
            response_text=response_text,
            context=context,
            response_id=response_id,
            speaker_id=speaker_id,
        )
    )

    checklist_task = asyncio.create_task(
        checklist_agent.analyze_for_checklist(
            speaker_id=speaker_id,
            speaker_role=speaker_role,
            text=response_text,
            conversation_history=conversation_history or [],
        )
    )

    # Run both tasks concurrently
    analysis_result, checklist_result = await asyncio.gather(
        analysis_task, checklist_task, return_exceptions=True
    )

    # Handle exceptions
    if isinstance(analysis_result, Exception):
        logger.error(f"Analysis task failed: {analysis_result}")
        # Return minimal analysis item on failure
        from .models import AnalysisItem
        analysis_result = AnalysisItem(
            response_id=response_id or "error",
            response_text=response_text,
            relevance_score=0.0,
            clarity_score=0.0,
            key_points=["Analysis failed"],
            follow_up_suggestions=[],
        )

    if isinstance(checklist_result, Exception):
        logger.error(f"Checklist task failed: {checklist_result}")
        checklist_result = None

    return analysis_result, checklist_result


# =============================================================================
# Factory Function
# =============================================================================


def create_checklist_agent(
    checklist_callback: Callable[[str, str, str], None],
    model: Optional[str] = None,
    azure_client: Optional[AsyncAzureOpenAI] = None,
) -> ChecklistAgent:
    """
    Factory function to create a configured ChecklistAgent.

    Args:
        checklist_callback: Function(item, status, reason) to call on updates
        model: Optional model override
        azure_client: Optional Azure client override

    Returns:
        Configured ChecklistAgent instance
    """
    return ChecklistAgent(
        checklist_callback=checklist_callback,
        model=model,
        azure_client=azure_client,
    )
