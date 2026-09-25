"""AUSTRO AI - Lesson generation (Phase E).

Produces structured lessons for a learning objective. Without an LLM (or when
it is unavailable / returns invalid output), a deterministic Arabic lesson is
built from templates enriched with grounded evidence (retrieved book snippets
with real citations). A generated lesson is cached per (owner, objective,
curriculum); the cache is the engine's only persistent lesson store.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.domain.ai import AIRequest
from app.learning.models import Lesson
from app.learning.repositories import LearningStore
from app.learning.schemas import (
    LESSON_FIELDS,
    extract_json,
    validate_schema,
    valid_lesson,
)

logger = logging.getLogger(__name__)

_NEXT_STEP_BY_MODE = {
    "READ": "مراجعة سريعة ثم اختبار قصير.",
    "DEEP": "حل تمرين تطبيقي ثم اختبار.",
    "EXAM": "اختبار مباشر بعد الشرح.",
    "PRACTICAL": "تطبيق عملي فوري ثم اختبار.",
    "FAST": "اختبار سريع ثم الانتقال للهدف التالي.",
}


class LessonGenerator:
    def __init__(self, store: LearningStore, ai=None, settings=None,
                 prompt_builder=None):
        self._store = store
        self._ai = ai
        self._settings = settings or {}
        self._prompt_builder = prompt_builder

    # -- public API ---------------------------------------------------------
    def generate(self, *, owner_user_id: int, curriculum_id: int,
                 objective_id: int, mode: str = "READ",
                 evidence: Optional[List[Dict[str, Any]]] = None,
                 force: bool = False) -> Optional[Lesson]:
        cached = self._store.lessons.get_in_curriculum(
            owner_user_id, curriculum_id, objective_id,
        )
        if cached is not None and not force:
            return _as_lesson(cached)

        objective = self._store.objectives.get(objective_id)
        if objective is None:
            return None
        content = self._template(objective, mode, evidence or [])
        if self._use_llm():
            llm_content = self._llm_content(owner_user_id, objective, mode, evidence)
            if llm_content is not None:
                content = llm_content

        content["schema_version"] = 1
        evidence = evidence or []
        grounded = bool(evidence)
        source_id = evidence[0].get("source_id") if evidence else None
        lesson_id = self._store.lessons.create(
            owner_user_id=owner_user_id, curriculum_id=curriculum_id,
            objective_id=objective_id, source_id=source_id,
            grounded=grounded, content=content, sources=evidence,
        )
        if lesson_id is None:
            return None
        row = self._store.lessons.get_in_curriculum(
            owner_user_id, curriculum_id, objective_id,
        )
        return _as_lesson(row)

    def get(self, owner_user_id: int, lesson_id: int) -> Optional[Lesson]:
        row = self._store.lessons.get(owner_user_id, lesson_id)
        return _as_lesson(row) if row else None

    def get_cached(self, owner_user_id: int, objective_id: int,
                   curriculum_id: int) -> Optional[Lesson]:
        row = self._store.lessons.get_in_curriculum(
            owner_user_id, curriculum_id, objective_id,
        )
        return _as_lesson(row) if row else None

    # -- template content ---------------------------------------------------
    def _use_llm(self) -> bool:
        if not bool(self._settings.get("learning_use_llm", False)):
            return False
        if self._ai is None or not getattr(self._ai, "provider", None):
            return False
        try:
            return bool(self._ai.provider.is_available)
        except Exception:
            return False

    def _template(self, objective: Dict[str, Any], mode: str,
                  evidence: List[Dict[str, Any]]) -> Dict[str, Any]:
        title = objective.get("title") or ""
        description = objective.get("description") or ""
        if mode not in _NEXT_STEP_BY_MODE:
            mode = "READ"
        essence = _strip_quotes(title)
        recap = objective.get("description") or \
            f"الهدف الأساسي: فهم «{title}» وتطبيقه في السياقات العملية."
        if evidence:
            recap = _evidence_grounded_recap(evidence, recap)
        explanation = _evidence_explanation(evidence)
        if not explanation:
            explanation = _start_explanation(description, title)

        return {
            "title": f"درس: {title}",
            "mode": mode,
            "essence": essence,
            "explanation": explanation,
            "example": _template_example(title),
            "recap": recap,
            "quick_check": [
                {"question": f"ما النقطة الأساسية في درس «{title}»؟",
                 "answer": _strip_quotes(recap)[:120]},
                {"question": f"ما أول ما ستفعله لتطبيق «{title}»؟",
                 "answer": "التعرف على تعريف المفهوم وتطبيقه أولاً"},
            ],
            "independent_practice": self._practice_block(title),
            "bibliography": [s.get("title", "") for s in evidence][:5],
            "next_step": _NEXT_STEP_BY_MODE[mode],
        }

    def _practice_block(self, title: str) -> List[Dict[str, Any]]:
        return [
            {"prompt": f"قالب عملي: حاول تجربة «{title}» في مهمة بسيطة",
             "answer": "تطبيق المفهوم في سياق حقيقي ثم تقييم النتيجة"},
        ]

    def _llm_content(self, owner_user_id: int, objective: Dict[str, Any],
                     mode: str, evidence: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if self._prompt_builder is None:
            return None
        prompt = self._prompt_builder.lesson(objective, mode, evidence)
        request = AIRequest(
            capability="lesson_generation",
            prompt=prompt,
            system_prompt="أنت مؤلف دروس عربية في تطبيق تعليمي. أعد JSON فقط.",
            max_tokens=1400, temperature=0.3, user_id=owner_user_id,
            data={"objective": objective.get("title"), "mode": mode},
        )
        try:
            import asyncio
            response = asyncio.new_event_loop().run_until_complete(
                self._ai.generate(request)
            )
            data = extract_json(response.text)
            if validate_schema(data, LESSON_FIELDS) or not valid_lesson(data):
                logger.warning("LLM lesson failed validation, using template")
                return None
            return data
        except Exception as exc:  # pragma: no cover - fallback safety
            logger.warning(f"LLM lesson generation failed, using template: {exc}")
            return None


def _strip_quotes(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("«") and text.endswith("»"):
        text = text[1:-1]
    return text


def _as_lesson(row: Dict[str, Any]) -> Lesson:
    """Row -> Lesson, dropping extra DB columns not on the DTO."""
    known = {f for f in Lesson.__dataclass_fields__}
    return Lesson(**{k: v for k, v in row.items() if k in known})


def _start_explanation(description: str, title: str) -> str:
    if description and description != title:
        return f"{description} ابدأ بتعريف «{title}» ثم انتقل إلى أمثلة تطبيقية."
    return f"المفهوم «{title}» يُبنى على تعريف واضح، ثم أمثلة، ثم تطبيق عملي."


def _template_example(title: str) -> str:
    return f"مثال توضيحي: استخدم «{title}» في موقف يومي صغير ولاحظ أثره."


def _evidence_grounded_recap(evidence: List[Dict[str, Any]], default: str) -> str:
    if not evidence:
        return default
    snippets = []
    for item in evidence[:3]:
        snippet = _strip_quotes(item.get("content") or "")
        if len(snippet) > 220:
            snippet = snippet[:220] + "…"
        if snippet:
            snippets.append(snippet)
    if not snippets:
        return default
    body = " ".join(snippets)
    return f"{default} — مأخوذ من مرجع موثوق: {body}"


def _evidence_explanation(evidence: List[Dict[str, Any]]) -> str:
    """Explain using grounded, cited snippets (facts only, never invented)."""
    if not evidence:
        return ""
    parts = []
    for item in evidence[:3]:
        section = (item.get("section_title") or "").strip()
        page = item.get("page")
        citation = item.get("title") or "المرجع"
        if page:
            citation = f"{citation} (ص. {page})"
        elif section:
            citation = f"{citation} — {section}"
        snippet = _strip_quotes(item.get("content") or "")
        if len(snippet) > 300:
            snippet = snippet[:300] + "…"
        if snippet:
            parts.append(f"{snippet}\n({citation})")
    return "\n\n".join(parts)


__all__ = ["LessonGenerator", "_as_lesson"]