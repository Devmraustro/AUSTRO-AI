"""AUSTRO AI - Structured AI output schemas (Phase E).

Every schema-validated LLM feature in the adaptive learning engine declares
its contract here. Output is ALWAYS validated before use; on invalid output
the caller repairs once, then falls back to a deterministic generator. These
specs are plain data so the behaviour is fully testable without a network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Schema specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    name: str
    types: Tuple[type, ...]
    required: bool = True
    default: Any = None
    # When the type is a list, the element type is checked.
    element_type: Optional[type] = None


def _field(name: str, types, required: bool = True,
           default: Any = None, element_type: Optional[type] = None) -> FieldSpec:
    return FieldSpec(name=name, types=tuple(types), required=required,
                     default=default, element_type=element_type)


# Structured-generation contracts used by the learning engine.
LESSON_FIELDS = [
    _field("title", (str,)),
    _field("objective", (str,)),
    _field("prerequisites", (list,), default=[], element_type=str),
    _field("explanation", (str,)),
    _field("example", (str,)),
    _field("guided_practice", (str,)),
    _field("independent_practice", (str,)),
    _field("quick_check", (list,), default=[], element_type=str),
    _field("recap", (str,)),
    _field("next_step", (str,)),
]

ASSESSMENT_FIELDS = [
    _field("kind", (str,)),
    _field("concept", (str,)),
    _field("prompt", (str,)),
    _field("options", (list,), default=[], element_type=str),
    _field("answer", (str,), required=False, default=""),
    _field("keywords", (list,), default=[], element_type=str),
    _field("explanation", (str,), required=False, default=""),
]

CONCEPT_EXTRACTION_FIELDS = [
    _field("concepts", (list,), element_type=dict),
]

CURRICULUM_FIELDS = [
    _field("modules", (list,), element_type=dict),
]

COACH_ADVICE_FIELDS = [
    _field("focus", (str,)),
    _field("blocker", (str,), required=False, default=""),
    _field("smallest_action", (str,)),
    _field("review_items", (list,), default=[], element_type=str),
    _field("reason", (str,), required=False, default=""),
]

WEEKLY_REVIEW_FIELDS = [
    _field("improved", (list,), element_type=str),
    _field("failed", (list,), element_type=str),
    _field("reasons", (list,), element_type=str),
    _field("changes", (list,), element_type=str),
    _field("priorities", (list,), element_type=str),
]

# ASPECTS the engine actually consumes from a lesson object.
LESSON_ASPECTS = {
    "title", "objective", "prerequisites", "explanation", "example",
    "guided_practice", "independent_practice", "quick_check", "recap",
    "next_step",
}


def extract_json(text: str) -> Optional[Any]:
    """Best-effort extraction of the first JSON object/array from a response.

    Handles plain JSON, markdown code fences and trailing prose. Returns None
    when nothing parseable was found.
    """
    if not text:
        return None
    text = text.strip()

    for candidate in _candidate_spans(text):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, (dict, list)):
                return parsed
        except (ValueError, TypeError):
            continue
    return None


def _candidate_spans(text: str) -> List[str]:
    candidates: List[str] = []
    # 1. Full text if it already is/contains JSON.
    if text.startswith(("{", "[")):
        candidates.append(text)
    # 2. Content inside ```json ... ``` or ``` ... ``` fences.
    for match in re.finditer(r"```(?:json|JSON)?\s*(.*?)```", text, re.DOTALL):
        candidates.append(match.group(1).strip())
    # 3. Balances braces/brackets scanning from the first opener.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            char = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:idx + 1])
                    break
    return candidates


def validate_schema(data: Any, fields: List[FieldSpec]) -> List[str]:
    """Return a list of validation errors (empty == valid).

    - `data` must be a dict.
    - Every required field must exist with a non-empty value of an allowed type.
    - Optional fields default automatically (missing -> default).
    """
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["output must be a JSON object"]

    for spec in fields:
        value = data.get(spec.name, spec.default)
        if spec.name not in data or data[spec.name] is None:
            if spec.required:
                errors.append(f"missing required field '{spec.name}'")
            elif spec.default is not None:
                data[spec.name] = spec.default
            continue
        if not isinstance(value, spec.types):
            errors.append(
                f"field '{spec.name}' has wrong type {type(value).__name__}"
            )
            continue
        if spec.element_type is not None:
            if not isinstance(value, list):
                errors.append(f"field '{spec.name}' must be a list")
            elif not all(isinstance(item, spec.element_type) for item in value):
                errors.append(f"field '{spec.name}' must only contain "
                              f"{spec.element_type.__name__} items")
        if isinstance(value, str) and not value.strip():
            errors.append(f"field '{spec.name}' must not be empty")
    return errors


def repair_json(text: str) -> Optional[Any]:
    """Try cheap repairs: fences, truncation, unquoted keys, trailing commas."""
    extracted = extract_json(text)
    if extracted is not None:
        return extracted
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = min(
        [idx for idx in (cleaned.find("{"), cleaned.find("[")) if idx >= 0],
        default=-1,
    )
    if start >= 0:
        cleaned = cleaned[start:]
    trailing = min(
        [idx for idx in (cleaned.rfind("}"), cleaned.rfind("]")) if idx >= 0],
        default=-1,
    )
    if trailing >= 0:
        cleaned = cleaned[:trailing + 1]
    obj = extract_json(cleaned)
    return obj


# ---------------------------------------------------------------------------
# Schema validity helpers (used by the engine and by tests)
# ---------------------------------------------------------------------------


def valid_lesson(data: Any) -> bool:
    return not validate_schema(data, LESSON_FIELDS)


def valid_assessment(data: Any) -> bool:
    if validate_schema(data, ASSESSMENT_FIELDS):
        return False
    if data.get("kind") not in (
        "multiple_choice", "true_false", "short_answer", "explanation",
        "practical",
    ):
        return False
    if data.get("kind") in ("multiple_choice", "true_false") and not data.get(
        "options"
    ):
        return False
    if data.get("kind") in ("multiple_choice", "true_false") and not data.get(
        "answer"
    ):
        return False
    return True


def valid_concepts(data: Any) -> bool:
    errors = validate_schema(data, CONCEPT_EXTRACTION_FIELDS)
    if errors:
        return False
    for concept in data.get("concepts", []):
        if not isinstance(concept, dict):
            return False
        if not concept.get("title"):
            return False
    return True


def valid_curriculum(data: Any) -> bool:
    errors = validate_schema(data, CURRICULUM_FIELDS)
    if errors:
        return False
    for module in data.get("modules", []):
        if not isinstance(module, dict) or not module.get("title"):
            return False
    return True


def valid_coach_advice(data: Any) -> bool:
    return not validate_schema(data, COACH_ADVICE_FIELDS)


def valid_weekly_review(data: Any) -> bool:
    return not validate_schema(data, WEEKLY_REVIEW_FIELDS)


__all__ = [
    "LESSON_FIELDS",
    "ASSESSMENT_FIELDS",
    "CONCEPT_EXTRACTION_FIELDS",
    "CURRICULUM_FIELDS",
    "COACH_ADVICE_FIELDS",
    "WEEKLY_REVIEW_FIELDS",
    "LESSON_ASPECTS",
    "FieldSpec",
    "extract_json",
    "validate_schema",
    "repair_json",
    "valid_lesson",
    "valid_assessment",
    "valid_concepts",
    "valid_curriculum",
    "valid_coach_advice",
    "valid_weekly_review",
]