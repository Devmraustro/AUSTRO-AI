"""AUSTRO AI - Knowledge domain catalog.

Maps each AUSTRO AI system/mode to its AI system prompt. Used by the chat
service to give the assistant domain-specific behaviour while keeping the
copy in one place.
"""

from __future__ import annotations

from app.config.prompts import (
    AUSTRO_COACH_PROMPT,
    AUSTRO_CYBER_PROMPT,
    AUSTRO_ENGLISH_PROMPT,
    AUSTRO_PROGRAMMING_PROMPT,
    AUSTRO_STUDY_PROMPT,
    AUSTRO_SYSTEM_PROMPT,
)

# system_key -> system prompt
SYSTEM_PROMPTS_BY_SYSTEM = {
    "general": AUSTRO_SYSTEM_PROMPT,
    "goal_system": AUSTRO_SYSTEM_PROMPT,
    "smart_planner": AUSTRO_SYSTEM_PROMPT,
    "discipline_mode": AUSTRO_SYSTEM_PROMPT,
    "study_mode": AUSTRO_STUDY_PROMPT,
    "english_mode": AUSTRO_ENGLISH_PROMPT,
    "programming_mode": AUSTRO_PROGRAMMING_PROMPT,
    "cybersecurity_mode": AUSTRO_CYBER_PROMPT,
    "productivity_mode": AUSTRO_SYSTEM_PROMPT,
    "habit_builder": AUSTRO_SYSTEM_PROMPT,
    "knowledge_assistant": AUSTRO_SYSTEM_PROMPT,
    "progress_tracker": AUSTRO_SYSTEM_PROMPT,
    "ai_coach": AUSTRO_COACH_PROMPT,
    "life_dashboard": AUSTRO_SYSTEM_PROMPT,
}

DOMAIN_DESCRIPTIONS = {
    "goal_system": "Goal definition, breakdown and tracking.",
    "smart_planner": "Daily planning, priorities and reviews.",
    "discipline_mode": "Anti-procrastination and discipline checks.",
    "study_mode": "Concept explanations, tests and study sessions.",
    "english_mode": "English grammar, vocabulary and corrections.",
    "programming_mode": "Code reviews, concepts and projects.",
    "cybersecurity_mode": "Security concepts, labs and certifications.",
    "productivity_mode": "Focus, routines and productivity.",
    "habit_builder": "Habit creation and consistency.",
    "knowledge_assistant": "General knowledge Q&A.",
    "progress_tracker": "Progress logging and reports.",
    "ai_coach": "Coaching advice and performance analysis.",
    "life_dashboard": "Unified life statistics dashboard.",
}


def system_prompt_for(system_key: str) -> str:
    return SYSTEM_PROMPTS_BY_SYSTEM.get(system_key, AUSTRO_SYSTEM_PROMPT)