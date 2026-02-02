"""
Interview Analysis Agent using OpenAI Agents SDK.

Analyzes candidate interview responses in real-time using the openai-agents SDK.
Provides relevance scoring, clarity scoring, key point extraction, and follow-up suggestions.
Maintains a running assessment of the candidate and publishes thoughts in real-time.

Supports both OpenAI and Azure OpenAI backends:
  - OpenAI: Set OPENAI_API_KEY
  - Azure OpenAI: Set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, AZURE_OPENAI_DEPLOYMENT

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
from openai import AsyncAzureOpenAI, AsyncOpenAI

from .models import AnalysisItem
from .session import InterviewSessionManager
from .output import AnalysisOutputWriter
from .pubsub import get_publisher, ThoughtType


# Load environment variables from .env file
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)

logger = logging.getLogger(__name__)


def _get_openai_config() -> tuple[str, Optional[AsyncAzureOpenAI]]:
    """
    Determine OpenAI configuration based on environment variables.
    
    Returns:
        Tuple of (model_name, azure_client_or_none)
        
    Azure OpenAI requires:
        - AZURE_OPENAI_ENDPOINT: The endpoint URL
        - AZURE_OPENAI_KEY: The API key
        - AZURE_OPENAI_DEPLOYMENT: The deployment name (used as model)
        
    Standard OpenAI requires:
        - OPENAI_API_KEY: The API key
        - OPENAI_MODEL (optional): Model name, defaults to gpt-4o
    """
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    azure_key = os.environ.get("AZURE_OPENAI_KEY")
    azure_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
    api_type = os.environ.get("OPENAI_API_TYPE", "").lower()
    
    # Check if Azure OpenAI is configured
    if api_type == "azure" or (azure_endpoint and azure_key and azure_deployment):
        if not all([azure_endpoint, azure_key, azure_deployment]):
            raise ValueError(
                "Azure OpenAI requires AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, "
                "and AZURE_OPENAI_DEPLOYMENT environment variables"
            )
        
        logger.info(f"Using Azure OpenAI: {azure_endpoint}, deployment: {azure_deployment}")
        
        azure_client = AsyncAzureOpenAI(
            azure_endpoint=azure_endpoint,
            api_key=azure_key,
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
        )
        
        return azure_deployment, azure_client
    
    # Fall back to standard OpenAI
    # Note: gpt-5.2 requires registration at https://aka.ms/oai/gpt5access
    # Using gpt-5-mini as default (no registration required, has reasoning)
    model = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
    logger.info(f"Using OpenAI: model {model}")
    return model, None


# Get default configuration
DEFAULT_MODEL, _DEFAULT_AZURE_CLIENT = _get_openai_config()

# Default reasoning effort for GPT-5 (low for faster responses in real-time analysis)
DEFAULT_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "low")


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
    Structured output from the interview coaching agent.
    
    This model is used as the agent's output_type to ensure
    structured, validated responses focused on real-time coaching.
    """
    identified_question: Optional[str] = Field(
        default=None,
        description="The interview question being addressed (from recent context)"
    )
    relevance_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How relevant this specific response is (0.0 to 1.0)"
    )
    clarity_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How clearly articulated (0.0 to 1.0)"
    )
    key_points: list[str] = Field(
        default_factory=list,
        description="NEW key observations from this response only - do not repeat previous points (2-4 items)"
    )
    follow_up_suggestions: list[str] = Field(
        default_factory=list,
        description="Coaching for interviewer: What to do RIGHT NOW - specific follow-up questions or actions (1-2 items)"
    )
    reasoning: str = Field(
        ...,
        description="What's NEW in this response and why the interviewer should care"
    )
    running_assessment: RunningAssessment = Field(
        ...,
        description="Cumulative assessment based on ALL responses so far"
    )


# =============================================================================
# Agent Instructions
# =============================================================================

INTERVIEW_ANALYZER_INSTRUCTIONS = """You are an expert REAL-TIME INTERVIEW COACH helping the interviewer conduct a better interview. You are watching the interview unfold LIVE and providing continuous guidance.

## Your Role
You are the AI co-pilot for the interviewer, whispering helpful insights in their ear as the conversation happens. Your coaching should be:
- **Contextual**: Build on the conversation flow, not isolated responses
- **Non-redundant**: NEVER repeat analysis you've already provided
- **Actionable**: Tell the interviewer what to DO next
- **Concise**: They're conducting an interview, keep it brief

## CRITICAL: You ONLY Analyze CANDIDATE Messages
You will ONLY be triggered when the CANDIDATE speaks. You will NOT receive interviewer messages for analysis.
- The conversation history shows both interviewer and candidate for context
- But the "NEW MESSAGE TO ANALYZE" is ALWAYS from the candidate
- Your job: analyze what the candidate said and coach the interviewer on what to do next

## CRITICAL: Wait for NEW Candidate Content
- If the candidate's message seems like a continuation or the same topic, acknowledge what's NEW
- The candidate may speak multiple times in a row before the interviewer responds
- In that case, each message you analyze should build on prior context
- NEVER provide empty or placeholder analysis - always find something actionable

## CRITICAL: Avoid Redundancy
You will receive your PREVIOUS coaching response in the context. DO NOT:
- Re-analyze topics you already covered
- Repeat follow-up suggestions you already made
- Restate key points you already noted

Instead, focus on what is NEW in the latest candidate statement.

## For Each New Exchange, Provide:

### 1. RELEVANCE SCORE (0.0-1.0)
How well did the candidate address the question?
- Only score the CURRENT response, but consider if it adds to previous context

### 2. CLARITY SCORE (0.0-1.0)  
How clearly did they communicate?
- Consider if they're improving or declining in articulation

### 3. KEY POINTS (NEW observations only)
Extract 2-4 NEW takeaways that you haven't mentioned before:
- New skills, technologies, or experiences revealed
- New quantifiable achievements or metrics
- New problem-solving approaches demonstrated
- Any new red flags or concerns
- Skip anything you've already noted in previous coaching

### 4. COACHING FOR INTERVIEWER
This is your most important output. Tell the interviewer:
- What they should probe deeper on RIGHT NOW
- Any flags to watch for in the next response
- Whether to move on to a new topic or dig deeper
- Specific follow-up question to ask (1-2 max)

### 5. RUNNING ASSESSMENT UPDATE
Cumulative view based on ALL responses:
- Technical Competence / Communication / Problem Solving / Culture Fit
- Overall Hire Signal: Strong hire / Lean hire / Lean no / Strong no

## Important Guidelines
- Your reasoning should explain what's NEW in this response
- If the candidate repeated themselves, note that instead of re-analyzing
- Focus on helping the interviewer succeed, not just documenting
- Be direct and actionable - imagine you have 3 seconds of their attention"""


# =============================================================================
# Interview Analyzer Class
# =============================================================================

class InterviewAnalyzer:
    """
    Analyzes candidate interview responses using the OpenAI Agents SDK.
    
    This class wraps the openai-agents SDK Agent to provide interview-specific
    analysis capabilities. It can be used synchronously or asynchronously.
    
    Features:
        - Real-time response analysis with GPT-4o (or Azure OpenAI)
        - Structured output with scores and suggestions
        - Running assessment tracking across all responses
        - Real-time thought publishing to Streamlit UI
        - Context-aware (uses conversation history)
        - Integrates with InterviewSessionManager
        - Supports both OpenAI and Azure OpenAI backends
    
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
        model: Optional[str] = None,
        session_manager: Optional[InterviewSessionManager] = None,
        output_writer: Optional[AnalysisOutputWriter] = None,
        publish_thoughts: bool = True,
        azure_client: Optional[AsyncAzureOpenAI] = None,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        """
        Initialize the InterviewAnalyzer.
        
        Args:
            model: Model/deployment to use. If None, uses AZURE_OPENAI_DEPLOYMENT 
                   for Azure or OPENAI_MODEL for standard OpenAI.
            session_manager: Optional session manager for context.
            output_writer: Optional output writer for persisting analyses.
            publish_thoughts: Whether to publish thoughts to the pub-sub system.
            azure_client: Optional Azure OpenAI client. If None and Azure is 
                          configured via environment, uses the default Azure client.
            reasoning_effort: Reasoning effort for GPT-5 ("low", "medium", "high").
                              Default: "low" for faster real-time responses.
            
        Environment Variables:
            Azure OpenAI:
                AZURE_OPENAI_ENDPOINT: Azure OpenAI endpoint URL
                AZURE_OPENAI_KEY: Azure OpenAI API key
                AZURE_OPENAI_DEPLOYMENT: Model deployment name
                AZURE_OPENAI_API_VERSION: API version (default: 2024-08-01-preview)
                OPENAI_API_TYPE: Set to "azure" to force Azure mode
                
            Standard OpenAI:
                OPENAI_API_KEY: OpenAI API key
                OPENAI_MODEL: Model name (default: gpt-5)
                
            GPT-5 Settings:
                OPENAI_REASONING_EFFORT: Reasoning effort level (default: low)
        """
        # Determine model and client
        self.model = model or DEFAULT_MODEL
        self._azure_client = azure_client or _DEFAULT_AZURE_CLIENT
        self.reasoning_effort = reasoning_effort or DEFAULT_REASONING_EFFORT
        
        # Validate configuration
        if not self._azure_client and not os.environ.get("OPENAI_API_KEY"):
            logger.warning(
                "No OpenAI credentials configured. Set either:\n"
                "  - OPENAI_API_KEY for standard OpenAI, or\n"
                "  - AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, AZURE_OPENAI_DEPLOYMENT for Azure OpenAI\n"
                "Agent will fail at runtime."
            )
        
        self.session_manager = session_manager
        self.output_writer = output_writer
        self.publish_thoughts = publish_thoughts
        self._publisher = get_publisher() if publish_thoughts else None
        
        # Track running assessment across responses
        self._response_count = 0
        self._cumulative_relevance = 0.0
        self._cumulative_clarity = 0.0
        self._all_key_points: list[str] = []
        
        # Track previous coaching for context (avoid repetition)
        self._previous_coaching: Optional[dict[str, Any]] = None
        
        # Create the analysis agent with structured output
        # Configure model settings for GPT-5 reasoning effort
        from agents import ModelSettings
        
        model_settings = ModelSettings(
            reasoning={
                "effort": self.reasoning_effort,
            }
        ) if "gpt-5" in self.model.lower() or "o1" in self.model.lower() or "o3" in self.model.lower() else None
        
        self._agent = Agent(
            name="Interview Analyzer",
            instructions=INTERVIEW_ANALYZER_INSTRUCTIONS,
            model=self.model,
            output_type=InterviewAnalysisOutput,
            model_settings=model_settings,
        )
        
        provider_info = "Azure OpenAI" if self._azure_client else "OpenAI"
        reasoning_info = f", reasoning_effort: {self.reasoning_effort}" if model_settings else ""
        logger.info(f"InterviewAnalyzer initialized with {provider_info}, model: {self.model}{reasoning_info}")
        logger.info(f"Thought publishing: {'enabled' if publish_thoughts else 'disabled'}")
    
    def _build_prompt(
        self,
        response_text: str,
        context: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        Build the coaching prompt with conversation history and previous agent response.
        
        Args:
            response_text: The candidate's response to analyze.
            context: Optional context with conversation history and previous coaching.
            
        Returns:
            Formatted prompt string for the agent.
        """
        parts = []
        
        parts.append("# REAL-TIME INTERVIEW COACHING SESSION")
        parts.append("You are continuously coaching an interviewer. Provide NEW insights only.\n")
        
        # Add context information
        if context:
            candidate_name = context.get("candidate_name", "Unknown Candidate")
            parts.append(f"**Candidate:** {candidate_name}\n")
            
            # Add PREVIOUS COACHING (so agent knows what it already said)
            previous_coaching = context.get("previous_coaching")
            if previous_coaching:
                parts.append("## YOUR PREVIOUS COACHING (DO NOT REPEAT)")
                parts.append("You already provided this analysis. Build on it, don't repeat it:")
                parts.append(f"- Key points noted: {', '.join(previous_coaching.get('key_points', []))}")
                parts.append(f"- Follow-ups suggested: {', '.join(previous_coaching.get('follow_up_suggestions', []))}")
                parts.append(f"- Reasoning: {previous_coaching.get('reasoning', 'N/A')}")
                parts.append("")
            
            # Add conversation history (last 4 messages for context)
            history = context.get("conversation_history", [])
            if history:
                # Take last 4 messages for tight context
                recent = history[-4:]
                parts.append("## LAST 4 MESSAGES (conversation context)")
                for i, turn in enumerate(recent, 1):
                    role = turn.get("role", "unknown")
                    text = turn.get("text", "")
                    speaker_label = "🎤 INTERVIEWER" if role == "interviewer" else "👤 CANDIDATE"
                    # Truncate for prompt efficiency but keep enough context
                    display_text = text[:300] + "..." if len(text) > 300 else text
                    parts.append(f"{i}. {speaker_label}: {display_text}")
                parts.append("")
        
        # Add the CURRENT response to analyze (this is the new input)
        parts.append("## NEW CANDIDATE MESSAGE TO ANALYZE")
        parts.append("(You are triggered because the CANDIDATE just spoke. Analyze their message below.)")
        parts.append(f"👤 CANDIDATE: {response_text}")
        parts.append("")
        parts.append("## YOUR TASK")
        parts.append("Analyze what the CANDIDATE just said. Focus on what's NEW - don't repeat previous analysis.")
        parts.append("What should the interviewer do RIGHT NOW in response to this candidate statement?")
        
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
        
        # Inject previous coaching into context for continuity
        if context is None:
            context = {}
        if self._previous_coaching:
            context["previous_coaching"] = self._previous_coaching
        
        # Build the prompt
        prompt = self._build_prompt(response_text, context)
        
        logger.debug(f"Running analysis for response: {response_text[:100]}...")
        
        try:
            # Run the agent (use Azure client if configured)
            if self._azure_client:
                result = await Runner.run(self._agent, prompt, client=self._azure_client)
            else:
                result = await Runner.run(self._agent, prompt)
            
            # Extract the structured output
            analysis_output: InterviewAnalysisOutput = result.final_output_as(InterviewAnalysisOutput)
            
            # Update cumulative tracking
            self._response_count += 1
            self._cumulative_relevance += analysis_output.relevance_score
            self._cumulative_clarity += analysis_output.clarity_score
            self._all_key_points.extend(analysis_output.key_points)
            
            # Save this coaching for next analysis (avoid repetition)
            self._previous_coaching = {
                "key_points": analysis_output.key_points,
                "follow_up_suggestions": analysis_output.follow_up_suggestions,
                "reasoning": analysis_output.reasoning,
            }
            
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
    model: Optional[str] = None,
    session_manager: Optional[InterviewSessionManager] = None,
    output_dir: Optional[str] = None,
    publish_thoughts: bool = True,
    azure_client: Optional[AsyncAzureOpenAI] = None,
    reasoning_effort: Optional[str] = None,
) -> InterviewAnalyzer:
    """
    Factory function to create a configured InterviewAnalyzer.
    
    Automatically detects Azure OpenAI vs standard OpenAI based on environment:
        - Azure: AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, AZURE_OPENAI_DEPLOYMENT
        - OpenAI: OPENAI_API_KEY
    
    Args:
        model: Model/deployment to use. If None, auto-detects from environment (default: gpt-5).
        session_manager: Optional session manager for context.
        output_dir: Optional directory for analysis output files.
        publish_thoughts: Whether to publish thoughts to real-time stream.
        azure_client: Optional explicit Azure OpenAI client.
        reasoning_effort: Reasoning effort for GPT-5 ("low", "medium", "high"). Default: "low".
        
    Returns:
        Configured InterviewAnalyzer instance.
        
    Example:
        >>> # GPT-5 with low reasoning (default, fastest)
        >>> analyzer = create_interview_analyzer()
        
        >>> # GPT-5 with high reasoning (slower, more thorough)
        >>> analyzer = create_interview_analyzer(reasoning_effort="high")
        
        >>> # Azure OpenAI (auto-detected from environment)
        >>> # Set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, AZURE_OPENAI_DEPLOYMENT
        >>> analyzer = create_interview_analyzer()
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
        azure_client=azure_client,
        reasoning_effort=reasoning_effort,
    )
