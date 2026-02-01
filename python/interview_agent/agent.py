"""
Interview Analysis Agent using OpenAI Agents SDK.

Analyzes candidate interview responses in real-time using the openai-agents SDK.
Provides relevance scoring, clarity scoring, key point extraction, and follow-up suggestions.

Last Grunted: 01/31/2026
"""

import logging
import os
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from agents import Agent, Runner

from .models import AnalysisItem
from .session import InterviewSessionManager
from .output import AnalysisOutputWriter


logger = logging.getLogger(__name__)


# =============================================================================
# Structured Output Models for Agent
# =============================================================================

class InterviewAnalysisOutput(BaseModel):
    """
    Structured output from the interview analysis agent.
    
    This model is used as the agent's output_type to ensure
    structured, validated responses.
    """
    identified_question: Optional[str] = Field(
        default=None,
        description="The interview question the candidate is responding to (extracted from interviewer statements)"
    )
    relevance_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How relevant the response is to the question (0.0 to 1.0)"
    )
    clarity_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How clearly the response is articulated (0.0 to 1.0)"
    )
    key_points: list[str] = Field(
        default_factory=list,
        description="Key points extracted from the candidate's response"
    )
    follow_up_suggestions: list[str] = Field(
        default_factory=list,
        description="Suggested follow-up questions for the interviewer"
    )
    reasoning: str = Field(
        ...,
        description="Brief explanation of the scoring rationale"
    )


# =============================================================================
# Agent Instructions
# =============================================================================

INTERVIEW_ANALYZER_INSTRUCTIONS = """You are an expert interview analyst. Your job is to analyze candidate responses during job interviews in real-time.

For each candidate response, you must:

1. **Identify the Question**: Look at the recent conversation context to determine what question or topic the candidate is responding to. The interviewer's statements typically contain questions or prompts.

2. **Score Relevance (0.0-1.0)**:
   - 0.9-1.0: Directly addresses the question with specific, relevant examples
   - 0.7-0.9: Addresses the question well but may lack some specificity
   - 0.5-0.7: Partially addresses the question, some tangential content
   - 0.3-0.5: Weakly connected to the question
   - 0.0-0.3: Does not address the question at all

3. **Score Clarity (0.0-1.0)**:
   - 0.9-1.0: Crystal clear, well-structured, easy to follow
   - 0.7-0.9: Clear with good structure
   - 0.5-0.7: Understandable but could be clearer
   - 0.3-0.5: Somewhat confusing or disorganized
   - 0.0-0.3: Very unclear or incoherent

4. **Extract Key Points**: List the most important points the candidate made. Focus on:
   - Skills and experiences mentioned
   - Specific examples or achievements
   - Technical knowledge demonstrated
   - Soft skills or personality traits revealed

5. **Suggest Follow-ups**: Provide 1-3 follow-up questions that would help the interviewer:
   - Dig deeper into vague claims
   - Clarify technical details
   - Explore related experiences
   - Assess skills not yet demonstrated

Be objective and professional. Provide constructive analysis that helps assess the candidate fairly."""


# =============================================================================
# Interview Analyzer Class
# =============================================================================

class InterviewAnalyzer:
    """
    Analyzes candidate interview responses using the OpenAI Agents SDK.
    
    This class wraps the openai-agents SDK Agent to provide interview-specific
    analysis capabilities. It can be used synchronously or asynchronously.
    
    Features:
        - Real-time response analysis
        - Structured output with scores and suggestions
        - Context-aware (uses conversation history)
        - Integrates with InterviewSessionManager
    
    Example:
        >>> analyzer = InterviewAnalyzer()
        >>> result = await analyzer.analyze_async(
        ...     response_text="I have 5 years of Python experience...",
        ...     context={
        ...         "candidate_name": "John Smith",
        ...         "conversation_history": [
        ...             {"role": "interviewer", "text": "Tell me about your Python experience."}
        ...         ]
        ...     }
        ... )
        >>> print(result.relevance_score)
        0.85
    """
    
    def __init__(
        self,
        model: str = "gpt-4o",
        session_manager: Optional[InterviewSessionManager] = None,
        output_writer: Optional[AnalysisOutputWriter] = None,
    ) -> None:
        """
        Initialize the InterviewAnalyzer.
        
        Args:
            model: OpenAI model to use (default: gpt-4o).
            session_manager: Optional session manager for context.
            output_writer: Optional output writer for persisting analyses.
            
        Raises:
            ValueError: If OPENAI_API_KEY environment variable is not set.
        """
        if not os.environ.get("OPENAI_API_KEY"):
            logger.warning(
                "OPENAI_API_KEY not set. Agent will fail at runtime. "
                "Set the environment variable before calling analyze methods."
            )
        
        self.model = model
        self.session_manager = session_manager
        self.output_writer = output_writer
        
        # Create the analysis agent with structured output
        self._agent = Agent(
            name="Interview Analyzer",
            instructions=INTERVIEW_ANALYZER_INSTRUCTIONS,
            model=self.model,
            output_type=InterviewAnalysisOutput,
        )
        
        logger.info(f"InterviewAnalyzer initialized with model: {model}")
    
    def _build_prompt(
        self,
        response_text: str,
        context: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        Build the analysis prompt from response and context.
        
        Args:
            response_text: The candidate's response to analyze.
            context: Optional context with conversation history.
            
        Returns:
            Formatted prompt string for the agent.
        """
        parts = []
        
        # Add context information
        if context:
            candidate_name = context.get("candidate_name", "Unknown Candidate")
            parts.append(f"## Interview Context\nCandidate: {candidate_name}\n")
            
            # Add conversation history
            history = context.get("conversation_history", [])
            if history:
                parts.append("## Recent Conversation")
                for turn in history[-10:]:  # Last 10 turns
                    role = turn.get("role", "unknown")
                    text = turn.get("text", "")
                    speaker_label = "INTERVIEWER" if role == "interviewer" else "CANDIDATE"
                    parts.append(f"[{speaker_label}]: {text}")
                parts.append("")  # Blank line
        
        # Add the current response to analyze
        parts.append("## Response to Analyze")
        parts.append(f"[CANDIDATE]: {response_text}")
        parts.append("")
        parts.append("Please analyze this candidate response and provide your assessment.")
        
        return "\n".join(parts)
    
    async def analyze_async(
        self,
        response_text: str,
        context: Optional[dict[str, Any]] = None,
        response_id: Optional[str] = None,
        speaker_id: Optional[str] = None,
    ) -> AnalysisItem:
        """
        Analyze a candidate response asynchronously.
        
        This is the primary analysis method. It runs the agent and returns
        a structured AnalysisItem.
        
        Args:
            response_text: The candidate's response text.
            context: Optional dict with candidate_name and conversation_history.
            response_id: Optional unique ID for this response (auto-generated if not provided).
            speaker_id: Optional speaker ID from diarization.
            
        Returns:
            AnalysisItem with scores, key points, and suggestions.
            
        Raises:
            Exception: If agent execution fails.
            
        Example:
            >>> result = await analyzer.analyze_async(
            ...     response_text="In my previous role, I led a team of 5 engineers...",
            ...     context={"candidate_name": "Jane Doe", "conversation_history": [...]}
            ... )
        """
        if not response_text or not response_text.strip():
            logger.warning("Empty response text provided, returning minimal analysis")
            return AnalysisItem(
                response_id=response_id or f"resp_{uuid.uuid4().hex[:8]}",
                response_text=response_text or "",
                relevance_score=0.0,
                clarity_score=0.0,
                key_points=[],
                follow_up_suggestions=["Unable to analyze empty response"],
            )
        
        # Build the prompt
        prompt = self._build_prompt(response_text, context)
        
        logger.debug(f"Running analysis for response: {response_text[:100]}...")
        
        try:
            # Run the agent
            result = await Runner.run(self._agent, prompt)
            
            # Extract the structured output
            analysis_output: InterviewAnalysisOutput = result.final_output_as(InterviewAnalysisOutput)
            
            # Create AnalysisItem from agent output
            analysis_item = AnalysisItem(
                response_id=response_id or f"resp_{uuid.uuid4().hex[:8]}",
                timestamp_utc=datetime.utcnow().isoformat() + "Z",
                question_text=analysis_output.identified_question,
                response_text=response_text,
                speaker_id=speaker_id,
                relevance_score=analysis_output.relevance_score,
                clarity_score=analysis_output.clarity_score,
                key_points=analysis_output.key_points,
                follow_up_suggestions=analysis_output.follow_up_suggestions,
                raw_model_output={
                    "reasoning": analysis_output.reasoning,
                    "model": self.model,
                },
            )
            
            logger.info(
                f"Analysis complete - relevance: {analysis_item.relevance_score:.2f}, "
                f"clarity: {analysis_item.clarity_score:.2f}, "
                f"key_points: {len(analysis_item.key_points)}"
            )
            
            # Optionally persist the analysis
            if self.output_writer and self.session_manager and self.session_manager.session:
                self.output_writer.append_item(
                    self.session_manager.session.session_id,
                    analysis_item
                )
            
            return analysis_item
            
        except Exception as e:
            logger.error(f"Agent analysis failed: {e}", exc_info=True)
            # Return a fallback analysis item on error
            return AnalysisItem(
                response_id=response_id or f"resp_{uuid.uuid4().hex[:8]}",
                response_text=response_text,
                relevance_score=0.5,
                clarity_score=0.5,
                key_points=["Analysis failed - manual review required"],
                follow_up_suggestions=[],
                raw_model_output={"error": str(e)},
            )
    
    def analyze(
        self,
        transcript: str,
        context: Optional[dict[str, Any]] = None,
        response_id: Optional[str] = None,
        speaker_id: Optional[str] = None,
    ) -> AnalysisItem:
        """
        Analyze a candidate response synchronously.
        
        This is a convenience wrapper that runs the async analysis
        in a new event loop. Prefer analyze_async when possible.
        
        Args:
            transcript: The candidate's response text (alias for response_text).
            context: Optional dict with candidate_name and conversation_history.
            response_id: Optional unique ID for this response.
            speaker_id: Optional speaker ID from diarization.
            
        Returns:
            AnalysisItem with scores, key points, and suggestions.
            
        Note:
            This method creates a new event loop if one is not running.
            Use analyze_async in async contexts to avoid overhead.
        """
        import asyncio
        
        try:
            # Try to get the running loop
            loop = asyncio.get_running_loop()
            # If we're in an async context, we can't use run_until_complete
            # This case is handled by the caller using asyncio.to_thread
            raise RuntimeError("Cannot call sync analyze from async context")
        except RuntimeError:
            # No running loop, create one
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
        """
        Analyze using context from the attached session manager.
        
        Requires a session_manager to be configured at initialization.
        Automatically extracts context from the current session.
        
        Args:
            response_text: The candidate's response text.
            speaker_id: Optional speaker ID from diarization.
            
        Returns:
            AnalysisItem if analysis succeeds, None if no active session.
            
        Raises:
            ValueError: If no session_manager is configured.
        """
        if not self.session_manager:
            raise ValueError("No session_manager configured. Pass one to __init__.")
        
        if not self.session_manager.is_active:
            logger.warning("No active session - skipping analysis")
            return None
        
        # Build context from session
        context = self.session_manager.get_session_context()
        
        # Get the last interviewer question for better context
        last_question = self.session_manager.get_last_interviewer_question()
        if last_question:
            # Ensure it's in the conversation history
            history = context.get("recent_conversation", [])
            if not any(h.get("text") == last_question for h in history):
                history.append({
                    "role": "interviewer",
                    "text": last_question,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                })
                context["recent_conversation"] = history
        
        return await self.analyze_async(
            response_text=response_text,
            context=context,
            speaker_id=speaker_id,
        )


# =============================================================================
# Factory Function
# =============================================================================

def create_interview_analyzer(
    model: str = "gpt-4o",
    session_manager: Optional[InterviewSessionManager] = None,
    output_dir: Optional[str] = None,
) -> InterviewAnalyzer:
    """
    Factory function to create a configured InterviewAnalyzer.
    
    Args:
        model: OpenAI model to use.
        session_manager: Optional session manager for context.
        output_dir: Optional directory for analysis output files.
        
    Returns:
        Configured InterviewAnalyzer instance.
        
    Example:
        >>> from pathlib import Path
        >>> analyzer = create_interview_analyzer(
        ...     model="gpt-4o",
        ...     output_dir="./analysis_output"
        ... )
    """
    output_writer = None
    if output_dir:
        from pathlib import Path
        output_writer = AnalysisOutputWriter(Path(output_dir))
    
    return InterviewAnalyzer(
        model=model,
        session_manager=session_manager,
        output_writer=output_writer,
    )
