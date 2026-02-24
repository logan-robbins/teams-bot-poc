"""
Shared interview simulation content used by variant UIs.
"""

from __future__ import annotations


INTERVIEWER_ID = "speaker_0"
CANDIDATE_ID = "speaker_1"


DEFAULT_INTERVIEW_SCRIPT: tuple[tuple[str, str], ...] = (
    (
        INTERVIEWER_ID,
        "Good morning and thanks for joining. Can you introduce yourself and your Python background?",
    ),
    (
        CANDIDATE_ID,
        "Good morning. I have six years of Python experience building backend systems and data services.",
    ),
    (
        INTERVIEWER_ID,
        "Tell me about a project where you improved performance at scale.",
    ),
    (
        CANDIDATE_ID,
        "I redesigned our event pipeline and cut end-to-end latency from minutes to under 40 seconds.",
    ),
    (
        INTERVIEWER_ID,
        "What was the hardest engineering tradeoff in that redesign?",
    ),
    (
        CANDIDATE_ID,
        "Balancing consistency and throughput. We chose idempotent consumers with strict retry boundaries.",
    ),
    (
        INTERVIEWER_ID,
        "How do you approach debugging production incidents at 3 AM?",
    ),
    (
        CANDIDATE_ID,
        "I start with blast radius and telemetry, identify bottlenecks quickly, and communicate status continuously.",
    ),
    (
        INTERVIEWER_ID,
        "How do you ensure code quality and team alignment?",
    ),
    (
        CANDIDATE_ID,
        "I rely on tests, strict typing, focused reviews, and design docs for high-risk changes.",
    ),
    (
        INTERVIEWER_ID,
        "Any questions for me about the role or the team?",
    ),
    (
        CANDIDATE_ID,
        "Yes. What does success look like in six months, and how does the team handle technical debt?",
    ),
)
