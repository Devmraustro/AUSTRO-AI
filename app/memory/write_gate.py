"""AUSTRO AI - Memory write gate (deterministic).

The gate decides - with pure rules, no LLM - whether a candidate memory may be
persisted. It is the security boundary of the memory engine:

- Validation: typed fields must come from the closed constant sets.
- Origin isolation: knowledge-document text can never write personal memory.
- Sensitive topics (secrets, health, banking...) need explicit consent.
- Confidence rules: derived/inferred low-confidence facts are never stored;
  low-confidence memories may exist but are rank-declined at retrieval.
- Dedup via stable hash; conflicting updates on the same typed slot are
  resolved by the explicit-statement-first priority order (spec §21) and the
  loser's value is preserved as an immutable version row.

Nothing here calls the AI. This class is trivially testable.
"""

from __future__ import annotations

import re
from typing import List

from app.memory.models import (
    CONFIDENCE_LEVELS,
    MEMORY_ACTIONS,
    MEMORY_SCOPES,
    MEMORY_STATUSES,
    MEMORY_TYPES,
    ORIGIN_KNOWLEDGE_DOCUMENT,
    PROVENANCE_SOURCES,
    MemoryCandidate,
    MemoryDecision,
    MemoryItem,
    normalize_text,
)
from app.memory.repositories import MemoryStore

# Default importance per memory type (1-5). Factor-of-5 choices are facts,
# habits and instructions; light preferences are 2-3.
DEFAULT_IMPORTANCE = {
    "profile": 2,
    "preference": 2,
    "goal": 5,
    "habit": 3,
    "learning_state": 3,
    "skill": 3,
    "weakness": 3,
    "strength": 3,
    "routine": 2,
    "communication_preference": 4,
    "coaching_preference": 4,
    "important_context": 4,
    "episodic_event": 2,
    "achievement": 5,
    "user_instruction": 5,
}

# Decay policy per type: "event" memories do not persist; "hold" memories
# never decay; everything else decays slowly.
DEFAULT_DECAY = {
    "episodic_event": "event",
    "achievement": "hold",
    "goal": "hold",
    "user_instruction": "hold",
    "communication_preference": "hold",
    "coaching_preference": "hold",
}

_SENSITIVE_PATTERNS = [
    re.compile(r"باسور[دذ]|كلمة السر|كلمة المرور|الرقم السري|كلمة العبور|password", re.I),
    re.compile(r"حسابي البنكي|رقم الحساب|بطاقة.*ائتمان|بطاقة.*بنك|بطاقة مصرفية|"
               r"credit card|bank account|card number|paypal|IBAN", re.I),
    re.compile(r"فصيلة الدم|ضغط الدم|سكري|مرض السكر|أدوي|دواء|دوي|امراض القلب|"
               r"مرضى|عمليات جراحية|blood type|diabetes|medication", re.I),
    re.compile(r"جواز السفر|بطاقة الهوية|الرقم الوطني|رقم جواز|passport|national id", re.I),
]

_SMALL_TALK = [
    re.compile(r"^(أهلا|اهلا|مرحبا|هاي|سلام|أهلاً)$", re.I),
    re.compile(r"^(ok|okay|تمام|بخير|الحمد لله|لا شيء|عادي|فاضي|ماشي|حسنا)$", re.I),
    re.compile(r"^(شكرا|شكراً|يعطيك العافية|ممتاز|رائع)$", re.I),
    re.compile(r"^(أنا بخير|انا بخير|لا جديد|ماعندي شيء)$", re.I),
    re.compile(r"^(hi|hello|hey|thanks|thank you|nothing|fine|all good)$", re.I),
]

# Provenance levels used by the write rules.
_EXPLICIT = "explicit_user_statement"
_ACTION = "user_action"
_CONFIRMED = "confirmed_memory"
_DERIVED = "derived_pattern"
_IMPORTED = "imported_profile"

# Priority order for conflict resolution (highest wins).
_PRIORITY = {
    _EXPLICIT: 4,
    _CONFIRMED: 3,
    _ACTION: 2,
    _IMPORTED: 2,
    _DERIVED: 1,
}


class MemoryWriteGate:
    """Deterministic, testable gate deciding what may enter long-term memory."""

    def __init__(self, store: MemoryStore):
        self.store = store

    @staticmethod
    def _is_sensitive(candidate: MemoryCandidate) -> bool:
        blob = f"{candidate.subject} {candidate.claim}"
        return any(p.search(blob) for p in _SENSITIVE_PATTERNS)

    @staticmethod
    def _is_small_talk(candidate: MemoryCandidate) -> bool:
        for pattern in _SMALL_TALK:
            if pattern.match(normalize_text(candidate.claim)):
                return True
        if normalize_text(f"{candidate.subject}{candidate.claim}").strip() == "":
            return True
        return False

    def process(self, candidate: MemoryCandidate,
                consent_preset: str = "") -> MemoryDecision:
        """Return the gate's verdict for a single candidate."""

        # --- 1. Structural validation ------------------------------------
        reject = self._validate(candidate)
        if reject:
            return MemoryDecision(candidate=candidate, approved=False, reason=reject)

        # --- 2. Origin isolation -----------------------------------------
        if candidate.origin == ORIGIN_KNOWLEDGE_DOCUMENT:
            return MemoryDecision(
                candidate=candidate, approved=False,
                reason="origin_blocked_knowledge_document",
            )

        # --- 3. Sensitive topic handling ---------------------------------
        if candidate.sensitive or self._is_sensitive(candidate):
            candidate.sensitive = True
            if candidate.provenance != _EXPLICIT:
                return MemoryDecision(
                    candidate=candidate, approved=False,
                    reason="sensitive_inferred_requires_explicit",
                )
            if consent_preset == "grant":
                candidate.requires_consent = True
                candidate.confidence = "high"
                return MemoryDecision(
                    candidate=candidate, approved=True, reason="sensitive_grant"
                )
            # Explicit mention of a sensitive topic -> ask the user.
            return MemoryDecision(
                candidate=candidate, approved=False,
                reason="sensitive_requires_consent",
                requires_confirmation=True,
            )

        # --- 4. Provenance + confidence rules ---------------------------
        gate = self._provenance_confidence(candidate)
        if gate:
            return MemoryDecision(candidate=candidate, approved=False, reason=gate)

        # --- 5. Stability / usefulness ----------------------------------
        if candidate.confidence in ("high", "medium") and self._is_small_talk(candidate):
            return MemoryDecision(
                candidate=candidate, approved=False, reason="temporary_content",
            )

        # --- 6. Dedup (exact content) ------------------------------------
        existing = self.store.memories.by_hash(
            candidate.owner_user_id, candidate.scope, candidate.memory_type,
            candidate.subject, candidate.claim,
        )
        if existing is not None and existing.status == "active":
            return MemoryDecision(
                candidate=candidate, approved=True, reason="duplicate",
                existing_id=existing.memory_id,
            )

        # --- 7. Conflict on the same typed slot --------------------------
        slot = self.store.memories.find_slot(
            candidate.owner_user_id, candidate.scope, candidate.memory_type,
            candidate.subject,
        )
        if slot is not None and normalize_text(slot.claim) != normalize_text(candidate.claim):
            decision = self._resolve_conflict(candidate, slot)
            if decision is not None:
                return decision

        # --- 8. Approved ------------------------------------------------
        return MemoryDecision(candidate=candidate, approved=True, reason="approved")

    def _validate(self, candidate: MemoryCandidate) -> str:
        if not candidate.owner_user_id:
            return "missing_owner"
        if candidate.memory_type not in MEMORY_TYPES:
            return "invalid_memory_type"
        if candidate.confidence not in CONFIDENCE_LEVELS:
            return "invalid_confidence"
        if candidate.provenance not in PROVENANCE_SOURCES:
            return "invalid_provenance"
        if candidate.scope not in MEMORY_SCOPES:
            return "invalid_scope"
        if not normalize_text(candidate.claim):
            return "empty_claim"
        return ""

    def _provenance_confidence(self, candidate: MemoryCandidate) -> str:
        """Enforce the confidence/provenance matrix (spec §21)."""
        prov = candidate.provenance
        conf = candidate.confidence
        if prov == _DERIVED:
            if conf == "low":
                return "low_confidence_derived"
            if conf == "medium":
                # Inferred facts need a human confirmation.
                return "derived_needs_confirmation"
            return "derived_needs_confirmation"
        if prov in (_ACTION, _IMPORTED):
            if conf == "low":
                return "low_confidence"
            return ""
        if prov == _CONFIRMED and conf not in ("high", "medium"):
            return "low_confidence"
        return ""  # explicit_user_statement: always allowed when validated

    def _resolve_conflict(self, candidate: MemoryCandidate,
                          existing: MemoryItem) -> MemoryDecision:
        """Resolve a same-slot value conflict using the priority order.

        Current explicit user statements win over everything; a derived
        pattern loses to any confirmed/explicit value in the slot.
        """
        new_rank = _PRIORITY.get(candidate.provenance, 0)
        old_rank = _PRIORITY.get(existing.provenance, 0)
        if new_rank < old_rank:
            return MemoryDecision(
                candidate=candidate, approved=False,
                reason="conflict_existing_wins",
                existing_id=existing.memory_id,
                is_conflict=True,
            )
        if new_rank == old_rank:
            # Same authority: newest wins, store the old value as a version.
            return MemoryDecision(
                candidate=candidate, approved=True, reason="conflict_latest_wins",
                existing_id=existing.memory_id,
                is_conflict=True,
            )
        return MemoryDecision(
            candidate=candidate, approved=True, reason="conflict_explicit_wins",
            existing_id=existing.memory_id,
            is_conflict=True,
        )

    def process_many(self, candidates: List[MemoryCandidate],
                     consent_preset: str = "") -> List[MemoryDecision]:
        decisions = [self.process(c, consent_preset=consent_preset) for c in candidates]
        deduped: List[MemoryDecision] = []
        seen_hashes = set()
        for decision in decisions:
            key = (decision.candidate.scope, decision.candidate.memory_type,
                   normalize_text(decision.candidate.subject),
                   normalize_text(decision.candidate.claim))
            if key in seen_hashes:
                continue
            seen_hashes.add(key)
            deduped.append(decision)
        return deduped

    def to_item(self, candidate: MemoryCandidate) -> MemoryItem:
        """Materialize an approved candidate into a persistable MemoryItem."""
        confidence = candidate.confidence
        if candidate.requires_consent:
            consent_state = "explicit"
        elif confidence == "low":
            consent_state = "pending"
        else:
            consent_state = "automatic"
        return MemoryItem(
            owner_user_id=candidate.owner_user_id,
            scope=candidate.scope,
            memory_type=candidate.memory_type,
            subject=candidate.subject,
            claim=candidate.claim,
            confidence=confidence,
            importance=candidate.importance or DEFAULT_IMPORTANCE.get(
                candidate.memory_type, 3),
            provenance=candidate.provenance,
            source_note=candidate.source_note,
            consent_state=consent_state,
            decay_policy=DEFAULT_DECAY.get(candidate.memory_type, "default"),
            is_episodic=candidate.is_episodic,
            event_date=candidate.event_date,
            status="active",
            version=1,
            hash_key="",  # filled by the store on create
            metadata={"sensitive": candidate.sensitive},
        )


# Re-exports so callers don't guess string constants.
MEMORY_ACTIONS_RO = MEMORY_ACTIONS
MEMORY_STATUSES_RO = MEMORY_STATUSES
KNOWN_MEMORY_TYPES = MEMORY_TYPES
KNOWLEDGE_ORIGIN = ORIGIN_KNOWLEDGE_DOCUMENT