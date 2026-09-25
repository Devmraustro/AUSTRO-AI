"""AUSTRO AI - Memory candidate extraction (deterministic rules).

Turns a user message into typed ``MemoryCandidate`` objects using pure pattern
rules - no AI, no model calls. The write gate then filters these candidates.

Only *first-person, present-tense or explicit* signals are matched, so casual
chat ("ok", "شكرا") and third-party text never become memory. Extracting with
rules also guarantees prompt-injection cannot steer what gets written: the AI
never produces candidates and never writes rows.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from app.memory.models import MemoryCandidate, normalize_text

_MAX_CANDIDATES_PER_MESSAGE = 4
_MAX_CLAIM_LENGTH = 220

_SENTENCE_BREAKS = "،,؛;.؟?!/\\\n\r"


def _clause(text: str, start: int) -> Tuple[str, int]:
    """Return the clause starting at ``start`` (bounded by sentence breaks)."""
    end = start
    while end < len(text) and text[end] not in _SENTENCE_BREAKS:
        end += 1
    return text[start:end].strip(), end


def _clean_capture(value: str) -> str:
    value = re.sub(r"\s{2,}", " ", value.strip()).strip(" :-،،")
    return value[:120]


class _Rule:
    def __init__(self, memory_type: str, pattern: str,
                 subject_is_first: bool = True, min_subject_len: int = 2,
                 subject_label: str = ""):
        self.memory_type = memory_type
        self.regex = re.compile(pattern, re.I)
        self.subject_is_first = subject_is_first
        self.min_subject_len = min_subject_len
        self.subject_label = subject_label

    def match(self, text: str):
        return self.regex.search(text)


# Rules are ordered; each produces subject + claim from the matched clause.
_RULES: List[_Rule] = [
    # ---- profile ----------------------------------------------------
    _Rule("profile", r"(?:my name is|اسمي|أنا اسمي|انا اسمي)\s+(.+)"),
    _Rule("profile", r"(?:عمري|عندي|i am|i'm)\s+(\d+\s*(?:سنة|سنه|عام)?\s*(?:old)?)", subject_is_first=False),
    _Rule("profile", r"(?:أنا من|انا من|i am from|i'm from)\s+(.+)"),
    _Rule("profile", r"(?:لغتي الأم|لغتي الام|mother tongue|native language)\s+(.+)"),
    _Rule("profile", r"(?:أعيش في|اعيش في|أسكن في|اسكن في|ساكن في|i live in)\s+(.+)"),
    # ---- preference -------------------------------------------------
    _Rule("preference", r"(?:أفضل|افضل|أفضّل|i prefer)\s+(.+)"),
    _Rule("preference", r"(?:أحب|احب|يعجبني|i like|i love|أعشق|اعشق)\s+(.+)"),
    # ---- goal -------------------------------------------------------
    _Rule("goal", r"(?:هدفي|هدفى|my goal(?: is)?)\s+(.+)"),
    _Rule("goal", r"(?:أريد أن|اريد ان|أريد|اريد|i want to|أتمنى أن|اتمنى ان)\s+(.+)"),
    _Rule("goal", r"(?:أطمح إلى|اطمح الى|أطمح ل|اطمح ل|i aim to)\s+(.+)"),
    _Rule("goal", r"(?:أحاول أن|احاول ان|i try to|أخطط ل|اخطط ل)\s+(.+)"),
    # ---- habit ------------------------------------------------------
    _Rule("habit", r"(?:أمارس|امارس)\s+(.+?)\s*(?:يومياً|يوميا|كل يوم|daily|every day)?$"),
    _Rule("habit", r"(?:أواظب على|اواظب على|أحرص على|احرص على)\s+(.+)"),
    _Rule("habit", r"(?:عادة|common habit|habit)\s+(.+)"),
    _Rule("habit", r"(?:أكتب|اكتب)\s+(.+?)\s*(?:يومياً|كل يوم|daily|every day)$"),
    _Rule("habit", r"(?:i (?:practice|do)) (.+?) (?:every day|daily|regularly)"),
    # ---- routine ----------------------------------------------------
    _Rule("routine", r"(?:روتيني|روتينى|my routine)\s+(.+)"),
    _Rule("routine", r"(?:أستيقظ|استيقظ)\s+(.+)"),
    _Rule("routine", r"(?:أنام|انام)\s+(.+)"),
    _Rule("routine", r"(?:جدولي اليومي|جدولى اليومى)\s+(.+)"),
    # ---- weakness ---------------------------------------------------
    _Rule("weakness", r"(?:أعاني من|اعاني من|i suffer from)\s+(.+)"),
    _Rule("weakness", r"(?:أضعف في|اضعف في|ضعيف في|ضعيفه في|weak in)\s+(.+)"),
    _Rule("weakness", r"(?:مشكلتي أن|مشكلتي ان|مشكلتي|my problem)\s+(.+)"),
    _Rule("weakness", r"(?:صعوبة في|صعوبة|i struggle with)\s+(.+)"),
    # ---- strength ---------------------------------------------------
    _Rule("strength", r"(?:أجيد|اجيد|i am good at|i'm good at)\s+(.+)"),
    _Rule("strength", r"(?:قوتي|قوتى|my strength)\s+(.+)"),
    _Rule("strength", r"(?:ممتاز في|مميز في|ممتازه في)\s+(.+)"),
    _Rule("strength", r"(?:أتقن|اتقن)\s+(.+)"),
    # ---- skill ------------------------------------------------------
    _Rule("skill", r"(?:لدي خبرة في|لدي خبرة|خبرتي|خبرتى|i have experience (?:in|with))\s+(.+)"),
    _Rule("skill", r"(?:أعرف|اعرف|i can|أستطيع أن أستخدم|استطيع ان استخدم)\s+(.+)"),
    # ---- learning state ---------------------------------------------
    _Rule("learning_state", r"(?:مستواي(?: في)?|مستواى)\s+(.+)"),
    _Rule("learning_state", r"(?:أدرس|ادرس)\s+(.+?)\s*(?:حالياً|الان|الآن|currently)?$"),
    _Rule("learning_state", r"(?:أتعلم|اتعلم|i am learning|i'm learning)\s+(.+)"),
    _Rule("learning_state", r"(?:وصلت في|وصلت الى|وصلت إلى|reached)\s+(.+)"),
    # ---- communication preference -----------------------------------
    _Rule("communication_preference", r"(?:افضل ان تتحدث معي|افضل ان تخاطبني)\s+(.+)"),
    _Rule("communication_preference", r"(?:اتصل بي|اتصل بي عبر|contact me)\s+(.+)"),
    _Rule("communication_preference", r"(?:نادني|call me)\s+(.+)"),
    # ---- coaching preference -----------------------------------------
    _Rule("coaching_preference", r"(?:افضل ان يكون|افضل ان يكون المدرب)\s+(.+)"),
    _Rule("coaching_preference", r"(?:حفزني|شجعني|push me|motivate me)\s+(.+)"),
    _Rule("coaching_preference", r"(?:لا تكن قاسيا معي|لا تكون قاسي معي|لا تتساهل معي|be strict with me)",
          subject_label="أسلوب التدريب معي"),
    # ---- important context -------------------------------------------
    _Rule("important_context", r"(?:مهم أن تعرف أن|مهم ان تعرف ان|من المهم أن تعلم|من المهم ان تعلم|"
                                r"it is important for you to know that)\s+(.+)"),
    _Rule("important_context", r"(?:تذكر أنني|تذكر انني|remember that i|أذكّرك أن|اذكرك ان)\s+(.+)"),
    _Rule("important_context", r"(?:كنت أعمل في|كنت اعمل في|i used to work)\s+(.+)"),
    # ---- user instruction --------------------------------------------
    _Rule("user_instruction", r"(?:عاملني|treat me)\s+(.+)"),
    _Rule("user_instruction", r"(?:لا ترسل لي|لا تراسلني|لا تراسل لي|لا ترسل|don't send me)\s+(.+)"),
    _Rule("user_instruction", r"(?:أرسل لي|ارسل لي|always send me)\s+(.+?)\s*(?:دائما|دائماً|every day)?$"),
    _Rule("user_instruction", r"(?:لا تخاطبني|don't talk to me)\s+(.+)"),
    # ---- achievement ---------------------------------------------------
    _Rule("achievement", r"(?:حصلت على|حصلت علي|حققت|i achieved)\s+(.+)"),
    _Rule("achievement", r"(?:أنجزت|انجزت|أكملت|اكملت|finished|completed)\s+(.+?)\s*(?:اليوم|الان|الآن)?$"),
    _Rule("achievement", r"(?:نجحت في|نجحت|i passed)\s+(.+?)\s*(?:الامتحان|الاختبار)?$"),
    # ---- episodic event (past-tense happenings) -----------------------
    _Rule("episodic_event", r"(?:اليوم|امس|أمس|البارحة|this week|yesterday|today)\s+(.+)"),
    _Rule("episodic_event", r"(?:سجلت|سجّلت)\s+(.+)"),
]


class CandidateExtractor:
    """Extracts typed memory candidates from a single user message."""

    def extract(self, text: str) -> List[MemoryCandidate]:
        if not text or not text.strip():
            return []
        raw = normalize_text(text)
        if len(raw) < 3:
            return []
        candidates: List[MemoryCandidate] = []
        seen: set = set()
        for rule in _RULES:
            match = rule.match(raw)
            if not match:
                continue
            captured = match.group(1) if match.lastindex else None
            if captured is None:
                clause, _ = _clause(raw, match.start())
                captured = rule.subject_label
            else:
                captured, _ = _clause(raw, match.start(1))
            captured = _clean_capture(captured)
            if len(captured) < rule.min_subject_len and not rule.subject_label == captured:
                continue
            claim_start = match.start()
            clause, _ = _clause(raw, claim_start)
            claim = _clean_capture(clause)
            if len(claim) > _MAX_CLAIM_LENGTH:
                claim = claim[:_MAX_CLAIM_LENGTH]
            if len(claim) < len(captured.strip()):
                continue
            dedup_key = (rule.memory_type, captured)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            candidates.append(MemoryCandidate(
                owner_user_id=0,  # assigned by the service
                memory_type=rule.memory_type,
                subject=captured,
                claim=claim,
                confidence="high",
                provenance="explicit_user_statement",
                source_note="conversation",
            ))
            if len(candidates) >= _MAX_CANDIDATES_PER_MESSAGE:
                break
        return candidates


def derive_event(owner_user_id: int, memory_type: str, subject: str,
                 claim: str, source_note: str = "app_action") -> MemoryCandidate:
    """Build a high-confidence user-action candidate (habits, achievements...)."""
    return MemoryCandidate(
        owner_user_id=owner_user_id,
        memory_type=memory_type,
        subject=subject,
        claim=claim,
        confidence="high",
        provenance="user_action",
        source_note=source_note,
        is_episodic=memory_type in ("episodic_event",),
        event_date=None,
    )


def imported_profile(owner_user_id: int, memory_type: str, subject: str,
                     claim: str) -> MemoryCandidate:
    """Build a candidate from onboarding/imported user profile data."""
    return MemoryCandidate(
        owner_user_id=owner_user_id,
        memory_type=memory_type,
        subject=subject,
        claim=claim,
        confidence="medium",
        provenance="imported_profile",
        source_note="imported_profile",
    )


def confirmed_fact(owner_user_id: int, memory_type: str, subject: str,
                   claim: str) -> MemoryCandidate:
    """Build a candidate explicitly confirmed by the user."""
    return MemoryCandidate(
        owner_user_id=owner_user_id,
        memory_type=memory_type,
        subject=subject,
        claim=claim,
        confidence="high",
        provenance="confirmed_memory",
        source_note="user_confirmed",
    )


def from_conversation(user_message: str) -> Optional[List[MemoryCandidate]]:
    """Convenience: extract candidates from a chat message."""
    return CandidateExtractor().extract(user_message) or None