"""
Interview Analysis Agent using OpenAI Agents SDK.

Analyzes candidate interview responses in real-time using the openai-agents SDK.
Provides relevance scoring, clarity scoring, key point extraction, and follow-up suggestions.
Maintains a running assessment of the candidate and publishes thoughts in real-time.

Last Grunted: 02/01/2026
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from agents import Agent, Runner

from .models import AnalysisItem
from .session import InterviewSessionManager
from .output import AnalysisOutputWriter
from .pubsub import get_publisher, ThoughtType


# Load environment variables from .env file
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)

logger = logging.getLogger(__name__)

# Default model - use gpt-5 for best analysis
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")


# =============================================================================
# Structured Output Models for Agent
# =============================================================================

class RunningAssessment(BaseModel):
    """Running assessment of the candidate based on all responses so far."""
    technical_competence: str = Field(
        ...,
        description="Assessment of technical foundation (e.g., 'Strong', 'Moderate', 'Weak', 'Not yet demonstrated')"
    )
    communication: str = Field(
        ...,
        description="Assessment of communication skills"
    )
    problem_solving: str = Field(
        ...,
        description="Assessment of structured thinking and problem-solving"
    )
    culture_fit: str = Field(
        ...,
        description="Assessment of collaboration and growth orientation"
    )
    overall_signal: str = Field(
        ...,
        description="Current hiring signal: 'Strong hire', 'Lean hire', 'Lean no', 'Strong no', or 'Too early to tell'"
    )
    key_strengths: list[str] = Field(
        default_factory=list,
        description="Top strengths observed so far"
    )
    areas_of_concern: list[str] = Field(
        default_factory=list,
        description="Any concerns or red flags"
    )


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
        description="Key points extracted from the candidate's response (2-4 items)"
    )
    follow_up_suggestions: list[str] = Field(
        default_factory=list,
        description="Suggested follow-up questions for the interviewer (1-2 items)"
    )
    reasoning: str = Field(
        ...,
        description="Brief explanation of the scoring rationale"
    )
    running_assessment: RunningAssessment = Field(
        ...,
        description="Updated running assessment of the candidate based on all responses"
    )


# =============================================================================
# Agent Instructions
# =============================================================================

INTERVIEW_ANALYZER_INSTRUCTIONS = """You are an expert interview analyst providing REAL-TIME analysis during a job interview. You are watching the interview unfold and must maintain a RUNNING ASSESSMENT of the candidate.

## Your Role
You are the AI assistant to the hiring manager, providing instant insights as the candidate speaks. Your analysis should be:
- **Immediate**: React to what was just said
- **Cumulative**: Build on previous responses to form a holistic view
- **Actionable**: Help the interviewer know what to probe next

## For Each Candidate Response, Analyze:

### 1. RELEVANCE SCORE (0.0-1.0)
How well does this response address the question?
- 0.9-1.0: Directly addresses with specific, concrete examples
- 0.7-0.9: Good coverage, minor gaps in specificity
- 0.5-0.7: Partially relevant, some tangential content
- 0.3-0.5: Weak connection to the question
- 0.0-0.3: Off-topic or evasive

### 2. CLARITY SCORE (0.0-1.0)
How well-articulated is this response?
- 0.9-1.0: Exceptionally clear, well-structured, easy to follow
- 0.7-0.9: Clear communication with logical flow
- 0.5-0.7: Understandable but could be more organized
- 0.3-0.5: Confusing, disorganized, hard to follow
- 0.0-0.3: Incoherent or rambling

### 3. KEY POINTS
Extract the 2-4 most important takeaways:
- Specific skills or technologies mentioned
- Quantifiable achievements (numbers, metrics, scale)
- Problem-solving approach demonstrated
- Red flags or concerns raised
- Soft skills or cultural fit indicators

### 4. FOLLOW-UP SUGGESTIONS
Suggest 1-2 probing questions the interviewer should ask:
- Dig deeper into vague claims ("Tell me more about...")
- Verify stated accomplishments ("What was your specific role...")
- Explore gaps or inconsistencies
- Test depth of knowledge

### 5. RUNNING ASSESSMENT UPDATE
Based on ALL responses so far, provide your current overall impression:
- **Technical Competence**: How strong is their technical foundation?
- **Communication**: How well do they articulate complex ideas?
- **Problem Solving**: Do they demonstrate structured thinking?
- **Culture Fit**: Do they seem collaborative, growth-oriented?
- **Overall Hire Signal**: Strong hire / Lean hire / Lean no / Strong no

## Important Guidelines
- Be objective and evidence-based
- Note both strengths AND areas of concern
- Your reasoning field should explain your scoring rationale
- Remember context from previous responses when available
- If this is early in the interview, note that assessment is preliminary"""


# =============================================================================
# Interview Analyzer Class
# =============================================================================

class InterviewAnalyzer:
    """
    Analyzes candidate interview responses using the OpenAI Agents SDK.
    
    This class wraps the openai-agents SDK Agent to provide interview-specific
    analysis capabilities. It can be used synchronously or asynchronously.
    
    Features:
        - Real-time response analysis with GPT-5
        - Structured output with scores and suggestions
        - Running assessment tracking across all responses
        - Real-time thought publishing to Streamlit UI
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
        model: str = DEFAULT_MODEL,
        session_manager: Optional[InterviewSessionManager] = None,
        output_writer: Optional[AnalysisOutputWriter] = None,
        publish_thoughts: bool = True,
    ) -> None:
        """
        Initialize the InterviewAnalyzer.
        
        Args:
            model: OpenAI model to use (default: gpt-5).
            session_manager: Optional session manager for context.
            output_writer: Optional output writer for persisting analyses.
            publish_thoughts: Whether to publish thoughts to the pub-sub system.
            
        Raises:
            ValueError: If OPENAI_API_KEY environment variable is not set.
        """
        if not os.environ.get("OPENAI_API_KEY"):
            logger.warning(
                "OPENAI_API_KEY not set. Agent will fail at runtime. "
                "Set the environment variable or add it to .env file."
            )
        
        self.model = model
        self.session_manager = session_manager
        self.output_writer = output_writer
        self.publish_thoughts = publish_thoughts
        self._publisher = get_publisher() if publish_thoughts else None
        
        # Track running assessment across responses
        self._response_count = 0
        self._cumulative_relevance = 0.0
        self._cumulative_clarity = 0.0
        self._all_key_points: list[str] = []
        
        # Create the analysis agent with structured output
        self._agent = Agent(
            name="Interview Analyzer",
            instructions=INTERVIEW_ANALYZER_INSTRUCTIONS,
            model=self.model,
            output_type=InterviewAnalysisOutput,
        )
        
        logger.info(f"InterviewAnalyzer initialized with model: {model}")
        logger.info(f"Thought publishing: {'enabled' if publish_thoughts else 'disabled'}")
    
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
        a structured AnalysisItem. Also publishes thoughts to the real-time stream.
        
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
            if self._publisher and self.publish_thoughts:
                await self._publisher.publish_observation(
                    "Received empty response - skipping analysis",
                    speaker_id=speaker_id
                )
            return AnalysisItem(
                response_id=response_id or f"resp_{uuid.uuid4().hex[:8]}",
                response_text=response_text or "",
                relevance_score=0.0,
                clarity_score=0.0,
                key_points=[],
                follow_up_suggestions=["Unable to analyze empty response"],
            )
        
        # Publish observation that we're analyzing
        if self._publisher and self.publish_thoughts:
            candidate_name = context.get("candidate_name", "Candidate") if context else "Candidate"
            await self._publisher.publish_observation(
                f"Analyzing response from {candidate_name}...",
                speaker_id=speaker_id
            )
        
        # Build the prompt
        prompt = self._build_prompt(response_text, context)
        
        logger.debug(f"Running analysis for response: {response_text[:100]}...")
        
        try:
            # Run the agent
            result = await Runner.run(self._agent, prompt)
            
            # Extract the structured output
            analysis_output: InterviewAnalysisOutput = result.final_output_as(InterviewAnalysisOutput)
            
            # Update cumulative tracking
            self._response_count += 1
            self._cumulative_relevance += analysis_output.relevance_score
            self._cumulative_clarity += analysis_output.clarity_score
            self._all_key_points.extend(analysis_output.key_points)
            
            # Create AnalysisItem from agent output
            running_assessment_dict = None
            if analysis_output.running_assessment:
                running_assessment_dict = {
                    "technical_competence": analysis_output.running_assessment.technical_competence,
                    "communication": analysis_output.running_assessment.communication,
                    "problem_solving": analysis_output.running_assessment.problem_solving,
                    "culture_fit": analysis_output.running_assessment.culture_fit,
                    "overall_signal": analysis_output.running_assessment.overall_signal,
                    "key_strengths": analysis_output.running_assessment.key_strengths,
                    "areas_of_concern": analysis_output.running_assessment.areas_of_concern,
                    "responses_analyzed": self._response_count,
                    "avg_relevance": self._cumulative_relevance / self._response_count,
                    "avg_clarity": self._cumulative_clarity / self._response_count,
                }
            
            analysis_item = AnalysisItem(
                response_id=response_id or f"resp_{uuid.uuid4().hex[:8]}",
                timestamp_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
                    "running_assessment": running_assessment_dict,
                },
            )
            
            logger.info(
                f"Analysis complete - relevance: {analysis_item.relevance_score:.2f}, "
                f"clarity: {analysis_item.clarity_score:.2f}, "
                f"key_points: {len(analysis_item.key_points)}"
            )
            
            # Publish analysis to real-time stream
            if self._publisher and self.publish_thoughts:
                await self._publisher.publish_analysis(
                    content=analysis_output.reasoning,
                    speaker_id=speaker_id,
                    speaker_role="candidate",
                    response_text=response_text[:200] + ("..." if len(response_text) > 200 else ""),
                    relevance_score=analysis_output.relevance_score,
                    clarity_score=analysis_output.clarity_score,
                    key_points=analysis_output.key_points,
                    follow_up_suggestions=analysis_output.follow_up_suggestions,
                    running_assessment=running_assessment_dict,
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
            
            # Publish error to stream
            if self._publisher and self.publish_thoughts:
                await self._publisher.publish_error(f"Analysis failed: {str(e)}")
            
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
                    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
    model: str = DEFAULT_MODEL,
    session_manager: Optional[InterviewSessionManager] = None,
    output_dir: Optional[str] = None,
    publish_thoughts: bool = True,
) -> InterviewAnalyzer:
    """
    Factory function to create a configured InterviewAnalyzer.
    
    Args:
        model: OpenAI model to use (default: gpt-5).
        session_manager: Optional session manager for context.
        output_dir: Optional directory for analysis output files.
        publish_thoughts: Whether to publish thoughts to real-time stream.
        
    Returns:
        Configured InterviewAnalyzer instance.
        
    Example:
        >>> from pathlib import Path
        >>> analyzer = create_interview_analyzer(
        ...     model="gpt-5",
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
        publish_thoughts=publish_thoughts,
    )
