"""AUSTRO AI - Learning use cases (study, English, programming, cyber).

Every method builds its prompt, routes the work through the AI gateway by
capability and returns the generated text. Structured inputs travel with the
request (`data`) so the local fallback can answer without re-parsing.
"""

from __future__ import annotations

from app.ai.gateway import AIGateway
from app.config.prompts import (
    GEMINI_CYBER_PROMPT,
    GEMINI_ENGLISH_PROMPT,
    GEMINI_PROGRAMMING_PROMPT,
    GEMINI_STUDY_PROMPT,
)
from app.domain.ai import AIRequest


class LearningService:
    """Study / English / programming / cybersecurity use cases."""

    def __init__(self, ai: AIGateway):
        self.ai = ai

    async def explain_concept(self, concept: str, subject: str) -> str:
        prompt = (
            f"Explain the concept of '{concept}' in {subject}.\n\n"
            "Make it:\n"
            "1. Simple and clear\n"
            "2. With Arabic explanation\n"
            "3. With real-world examples\n"
            "4. With key takeaways\n"
            "5. With a practice suggestion\n\n"
            "Use emojis and engaging format."
        )
        request = AIRequest(
            capability="explain_concept",
            prompt=prompt,
            system_prompt=GEMINI_STUDY_PROMPT,
            max_tokens=2048,
            data={"concept": concept, "subject": subject},
        )
        return (await self.ai.generate(request)).text

    async def generate_test(self, topic: str, difficulty: str = "medium") -> str:
        prompt = (
            f"Create a {difficulty} level test about '{topic}'.\n\n"
            "Include:\n"
            "1. 3 multiple choice questions\n"
            "2. 2 short answer questions\n"
            "3. 1 practical application question\n\n"
            "Format in Arabic with clear numbering. Include answers at the end."
        )
        request = AIRequest(
            capability="generate_test",
            prompt=prompt,
            system_prompt=GEMINI_STUDY_PROMPT,
            max_tokens=2048,
            data={"topic": topic, "difficulty": difficulty},
        )
        return (await self.ai.generate(request)).text

    async def correct_english(self, text: str) -> str:
        prompt = (
            f"Correct this English text and explain the mistakes in Arabic:\n\n"
            f'Text: "{text}"\n\n'
            "Provide:\n"
            "1. Corrected text\n"
            "2. List of mistakes with Arabic explanations\n"
            "3. Tips for improvement\n"
            "4. Alternative better expressions"
        )
        request = AIRequest(
            capability="correct_english",
            prompt=prompt,
            system_prompt=GEMINI_ENGLISH_PROMPT,
            max_tokens=2048,
            data={"text": text},
        )
        return (await self.ai.generate(request)).text

    async def review_code(self, code: str, language: str) -> str:
        prompt = (
            f"Review this {language} code and provide feedback in Arabic:\n\n"
            f"````{language}\n{code}\n````\n\n"
            "Analyze:\n"
            "1. Code quality and style\n"
            "2. Potential bugs or errors\n"
            "3. Performance issues\n"
            "4. Security concerns (if applicable)\n"
            "5. Improvement suggestions\n"
            "6. Best practices recommendations\n\n"
            "Be constructive and educational."
        )
        request = AIRequest(
            capability="review_code",
            prompt=prompt,
            system_prompt=GEMINI_PROGRAMMING_PROMPT,
            max_tokens=2048,
            data={"code": code, "language": language},
        )
        return (await self.ai.generate(request)).text

    async def english_lesson(self, level: str = "beginner") -> str:
        prompt = (
            f"Create a daily English lesson for {level} level.\n\n"
            "Include:\n"
            "1. 5 new vocabulary words with Arabic meanings\n"
            "2. A grammar tip\n"
            "3. A short conversation example\n"
            "4. A practice exercise\n"
            "5. Common mistake to avoid\n\n"
            "Make it engaging and practical."
        )
        request = AIRequest(
            capability="english_lesson",
            prompt=prompt,
            system_prompt=GEMINI_ENGLISH_PROMPT,
            max_tokens=2048,
            data={"level": level},
        )
        return (await self.ai.generate(request)).text

    async def cyber_lesson(self, topic: str) -> str:
        prompt = (
            f"Explain the cybersecurity topic '{topic}' in Arabic.\n\n"
            "Include:\n"
            "1. What it is and why it matters\n"
            "2. Real-world examples\n"
            "3. How to learn/practice it\n"
            "4. Related certifications\n"
            "5. Career relevance\n\n"
            "Make it beginner-friendly but accurate."
        )
        request = AIRequest(
            capability="cyber_lesson",
            prompt=prompt,
            system_prompt=GEMINI_CYBER_PROMPT,
            max_tokens=2048,
            data={"topic": topic},
        )
        return (await self.ai.generate(request)).text