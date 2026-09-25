"""AUSTRO AI - AI domain DTOs and capability catalog."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Coarse capability catalog required by the architecture. Services route AI
# work through these categories; the gateway maps each operator to one.
CAPABILITIES = {
    "chat": "general conversational assistant",
    "coaching": "personalized coaching and performance analysis",
    "tutoring": "concept explanations and corrections",
    "planning": "generating structured daily plans",
    "summarization": "condensing information",
    "structured_generation": "generating structured tests/lessons",
    "retrieval": "answering questions grounded in the user's own books with citations",
    "embeddings": "text embeddings (not currently available)",
    "safety_check": "content safety evaluation",
}

# Exact operators -> coarse category (used to route provider behaviour).
OPERATOR_CATEGORIES = {
    "chat": "chat",
    "planning": "planning",
    "explain_concept": "tutoring",
    "generate_test": "structured_generation",
    "correct_english": "tutoring",
    "review_code": "tutoring",
    "english_lesson": "structured_generation",
    "cyber_lesson": "tutoring",
    "coach_advice": "coaching",
    "performance_analysis": "coaching",
    "grounded_answer": "retrieval",
    "summarization": "summarization",
    "embeddings": "embeddings",
    "safety_check": "safety_check",
    "lesson_generation": "structured_generation",
    "assessment_generation": "structured_generation",
    "concept_extraction": "structured_generation",
    "weekly_review": "coaching",
    "coach_focus": "coaching",
    "diagnostic_questions": "tutoring",
}

# Capability groups with no current provider implementation. They degrade
# gracefully (fallback path) instead of crashing the update loop.
UNSUPPORTED_CAPABILITIES = {"embeddings", "safety_check"}


@dataclass
class AIRequest:
    """A generation request handed to the AI gateway.

    `prompt` is the fully assembled instruction for the cloud provider;
    `data` carries structured inputs so the local fallback can build a
    context-aware canned response without re-parsing free text.
    """

    capability: str
    prompt: str
    system_prompt: str = ""
    max_tokens: int = 1024
    temperature: float = 0.7
    user_id: Optional[int] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def category(self) -> str:
        return OPERATOR_CATEGORIES.get(self.capability, self.capability)


@dataclass
class AIMetadata:
    """Telemetry describing a single AI run."""

    run_id: str
    capability: str
    category: str
    provider: str
    model: str
    latency_ms: float
    success: bool
    retry_count: int = 0
    error: Optional[str] = None


@dataclass
class AIResponse:
    """Result of an AI generation plus its telemetry metadata."""

    text: str
    metadata: AIMetadata