"""AUSTRO AI - Memory service (application layer facade).

Coordinates extraction -> write gate -> persistence -> retrieval, and exposes
the audit + export surfaces used by the Telegram layer. The AI never writes
memory; the service accepts text/conversation candidates produced by the
deterministic extractor, or typed action events produced by other AUSTRO
modules (habits, achievements, coaching).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from app.config.settings import Settings
from app.memory.extractors import (
    CandidateExtractor,
    confirmed_fact,
    derive_event,
    imported_profile,
)
from app.memory.models import (
    ORIGIN_KNOWLEDGE_DOCUMENT,
    MemoryCandidate,
    MemoryContextPack,
    MemoryDecision,
    MemoryItem,
    TaskContext,
    build_hash_key,
)
from app.memory.repositories import MemoryStore
from app.memory.retrieval import MemoryRetrieval
from app.memory.write_gate import MemoryWriteGate

logger = logging.getLogger(__name__)


class MemoryService:
    """Application-layer memory engine facade."""

    def __init__(self, store: MemoryStore, gate: MemoryWriteGate,
                 extractor: CandidateExtractor, retrieval: MemoryRetrieval,
                 settings_: Settings):
        self.store = store
        self.gate = gate
        self.extractor = extractor
        self.retrieval = retrieval
        self.settings = settings_
        self._metrics: Dict[str, Any] = {
            "statements_processed": 0,
            "candidates_found": 0,
            "created": 0,
            "updated": 0,
            "duplicates": 0,
            "rejected": {},  # reason -> count
        }

    # ------------------------------------------------------------------
    # Settings / consent
    # ------------------------------------------------------------------

    def enabled(self, owner_user_id: int) -> bool:
        return self.store.prefs.ensure(owner_user_id).auto_memory_enabled

    def set_auto_enabled(self, owner_user_id: int, enabled: bool,
                         actor: Optional[int] = None) -> bool:
        ok = self.store.prefs.set_auto_enabled(owner_user_id, enabled)
        if ok:
            self.store.events.log(
                owner_user_id=owner_user_id, memory_id=None,
                action="enabled" if enabled else "disabled",
                source="settings", actor=actor,
            )
        return ok

    def prefs(self, owner_user_id: int) -> Dict[str, Any]:
        prefs = self.store.prefs.ensure(owner_user_id)
        return {
            "auto_memory_enabled": prefs.auto_memory_enabled,
            "consent_types": dict(prefs.consent_types),
            "consented": [t for t, s in (prefs.consent_types or {}).items()
                          if s == "grant"],
        }

    def settings_info(self) -> Dict[str, Any]:
        return {
            "max_retrieved": self.settings.memory_max_retrieved,
            "context_budget_chars": self.settings.memory_context_budget_chars,
            "enabled_types": len(self.gate_types()),
        }

    @staticmethod
    def gate_types() -> tuple:
        from app.memory.models import MEMORY_TYPES
        return MEMORY_TYPES

    # ------------------------------------------------------------------
    # Writing pipeline
    # ------------------------------------------------------------------

    def process_message(self, owner_user_id: int, text: str,
                        actor: Optional[int] = None,
                        consent_preset: str = "") -> Dict[str, Any]:
        """Extract, gate, and persist candidates from a chat message."""
        started = time.perf_counter()
        self._metrics["statements_processed"] += 1
        result: Dict[str, Any] = {
            "disabled": not self.enabled(owner_user_id),
            "created": 0, "updated": 0, "duplicates": 0, "rejected": [],
            "needs_confirmation": [], "extracted": 0, "latency_ms": 0.0,
        }

        candidates = self.extractor.extract(text)
        result["extracted"] = len(candidates)
        self._metrics["candidates_found"] += len(candidates)

        if result["disabled"]:
            result["rejected"].append({"reason": "memory_disabled"})
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            return result

        for candidate in candidates:
            candidate.owner_user_id = owner_user_id

        decisions = self.gate.process_many(candidates, consent_preset=consent_preset)
        outcomes = self._persist_approved(owner_user_id, decisions, actor=actor)

        result["created"] = outcomes["created"]
        result["updated"] = outcomes["updated"]
        result["duplicates"] = outcomes["duplicates"]
        result["needs_confirmation"] = [
            self._decision_payload(d) for d in decisions if d.requires_confirmation
        ]
        for decision in decisions:
            if not decision.approved and not decision.requires_confirmation:
                result["rejected"].append({
                    "reason": decision.reason,
                    "subject": decision.candidate.subject,
                    "claim": decision.candidate.claim[:80],
                })
                self._metrics["rejected"][decision.reason] = (
                    self._metrics["rejected"].get(decision.reason, 0) + 1
                )
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return result

    def process_candidates(self, owner_user_id: int,
                           candidates: List[MemoryCandidate],
                           actor: Optional[int] = None,
                           consent_preset: str = "") -> Dict[str, Any]:
        """Gate + persist an explicit candidate list (typed callers)."""
        for candidate in candidates:
            candidate.owner_user_id = owner_user_id
            candidate.scope = candidate.scope or "USER"
        decisions = self.gate.process_many(candidates, consent_preset=consent_preset)
        outcomes = self._persist_approved(owner_user_id, decisions, actor=actor)
        return {
            "created": outcomes["created"],
            "updated": outcomes["updated"],
            "duplicates": outcomes["duplicates"],
            "needs_confirmation": outcomes["pending"],
        }

    def record_event(self, owner_user_id: int, memory_type: str, subject: str,
                     claim: str, actor: Optional[int] = None,
                     source_note: str = "app_action") -> Dict[str, Any]:
        """Record a user-action memory (e.g. habit completed, achievement)."""
        candidate = derive_event(owner_user_id, memory_type, subject, claim,
                                 source_note=source_note)
        return self.process_candidates(owner_user_id, [candidate], actor=actor)

    def record_imported(self, owner_user_id: int, memory_type: str,
                        subject: str, claim: str,
                        actor: Optional[int] = None) -> Dict[str, Any]:
        candidate = imported_profile(owner_user_id, memory_type, subject, claim)
        return self.process_candidates(owner_user_id, [candidate], actor=actor)

    def record_confirmed(self, owner_user_id: int, memory_type: str,
                         subject: str, claim: str,
                         actor: Optional[int] = None) -> Dict[str, Any]:
        candidate = confirmed_fact(owner_user_id, memory_type, subject, claim)
        return self.process_candidates(owner_user_id, [candidate], actor=actor)

    def reject_knowledge_origin(self, owner_user_id: int, text: str) -> bool:
        """Proof that knowledge text can never enter memory (spec §14)."""
        candidate = MemoryCandidate(
            owner_user_id=owner_user_id, memory_type="profile",
            subject="text", claim=text[:200], confidence="high",
            provenance="explicit_user_statement",
            origin=ORIGIN_KNOWLEDGE_DOCUMENT,
        )
        decision = self.gate.process(candidate)
        return not decision.approved

    # ------------------------------------------------------------------
    # Persistence internals
    # ------------------------------------------------------------------

    def _persist_approved(self, owner_user_id: int,
                          decisions: List[MemoryDecision],
                          actor: Optional[int] = None) -> Dict[str, Any]:
        outcomes = {"created": 0, "updated": 0, "duplicates": 0, "pending": []}
        for decision in decisions:
            if decision.requires_confirmation:
                item = self._materialize_pending(decision.candidate)
                memory_id = self.store.memories.create(item)
                if memory_id:
                    outcomes["pending"].append(memory_id)
                    self.store.events.log(
                        owner_user_id=owner_user_id, memory_id=memory_id,
                        action="pending", source="write_gate", actor=actor,
                    )
                continue
            if not decision.approved:
                continue
            if decision.reason == "duplicate" and decision.existing_id:
                outcomes["duplicates"] += 1
                self._metrics["duplicates"] += 1
                self.store.memories.touch(owner_user_id, decision.existing_id)
                continue
            if decision.is_conflict and decision.existing_id:
                stored = self._persist_conflict(decision, owner_user_id, actor)
                if stored:
                    outcomes["updated"] += 1
                    self._metrics["updated"] += 1
                continue
            item = self.gate.to_item(decision.candidate)
            item.owner_user_id = owner_user_id
            item.hash_key = build_hash_key(
                owner_user_id, item.scope, item.memory_type,
                item.subject, item.claim,
            )
            existing_any_status = self.store.memories.by_hash(
                owner_user_id, item.scope, item.memory_type,
                item.subject, item.claim,
            )
            if existing_any_status is not None:
                # Same fact existed before but was forgotten: reactivate it.
                resurrected = self.store.memories.resurrect(
                    owner_user_id, existing_any_status.memory_id,
                    consent_state=item.consent_state,
                )
                if resurrected:
                    outcomes["created"] += 1
                    self._metrics["created"] += 1
                    self.store.events.log(
                        owner_user_id=owner_user_id,
                        memory_id=existing_any_status.memory_id,
                        action="created", source=item.provenance,
                        reason="re_remembered", actor=actor,
                    )
                continue
            memory_id = self.store.memories.create(item)
            if memory_id:
                outcomes["created"] += 1
                self._metrics["created"] += 1
                self.store.events.log(
                    owner_user_id=owner_user_id, memory_id=memory_id,
                    action="created", source=item.provenance, actor=actor,
                )
        return outcomes

    def _persist_conflict(self, decision: MemoryDecision, owner_user_id: int,
                          actor: Optional[int]) -> bool:
        """Apply a conflict resolution: snapshot old value, store new value."""
        existing = self.store.memories.get(owner_user_id, decision.existing_id)
        if existing is None:
            return False
        self.store.versions.add(
            memory_id=existing.memory_id, owner_user_id=owner_user_id,
            version=existing.version, claim=existing.claim,
            confidence=existing.confidence, importance=existing.importance,
            reason=decision.reason, actor=actor,
        )
        cand = decision.candidate
        confidence = cand.confidence
        if cand.requires_consent:
            consent_state = "explicit"
        elif confidence == "low":
            consent_state = "pending"
        else:
            consent_state = "automatic"
        updated = self.store.memories.update(
            owner_user_id, existing.memory_id,
            claim=cand.claim, confidence=confidence,
            importance=cand.importance or existing.importance,
            source_note=cand.source_note or existing.source_note,
            consent_state=consent_state,
        )
        if updated:
            new_key = build_hash_key(owner_user_id, cand.scope, cand.memory_type,
                                     cand.subject, cand.claim)
            self.store.memories.update_hash_key(owner_user_id,
                                                existing.memory_id, new_key)
            self.store.events.log(
                owner_user_id=owner_user_id, memory_id=existing.memory_id,
                action="updated", source=cand.provenance,
                reason=decision.reason, actor=actor,
            )
            return True
        return False

    def _materialize_pending(self, candidate: MemoryCandidate) -> MemoryItem:
        item = self.gate.to_item(candidate)
        item.owner_user_id = candidate.owner_user_id
        item.consent_state = "pending"
        item.metadata = {"sensitive": candidate.sensitive,
                         "needs_confirmation": True}
        item.hash_key = build_hash_key(
            candidate.owner_user_id, item.scope, item.memory_type,
            item.subject, item.claim,
        )
        return item

    @staticmethod
    def _decision_payload(decision: MemoryDecision) -> Dict[str, Any]:
        return {
            "memory_type": decision.candidate.memory_type,
            "subject": decision.candidate.subject,
            "claim": decision.candidate.claim[:120],
            "reason": decision.reason,
        }

    # ------------------------------------------------------------------
    # Confirmation flow
    # ------------------------------------------------------------------

    def pending(self, owner_user_id: int) -> List[Dict[str, Any]]:
        rows = self.store.memories.list(owner_user_id, status="active")
        return [
            item.as_dict() for item in rows
            if item.consent_state == "pending"
            and item.metadata.get("needs_confirmation")
        ]

    def confirm(self, owner_user_id: int, memory_id: int, accept: bool,
                actor: Optional[int] = None) -> bool:
        """User decides on a pending / flagged memory (accept or reject)."""
        item = self.store.memories.get(owner_user_id, memory_id)
        if item is None:
            return False
        if accept:
            ok = self.store.memories.confirm(owner_user_id, memory_id)
            if ok:
                self.store.events.log(
                    owner_user_id=owner_user_id, memory_id=memory_id,
                    action="confirmed", source="user", actor=actor,
                )
            return ok
        self.store.memories.forget(owner_user_id, memory_id)
        self.store.events.log(
            owner_user_id=owner_user_id, memory_id=memory_id,
            action="rejected", source="user", actor=actor,
        )
        return True

    # ------------------------------------------------------------------
    # Reading / management
    # ------------------------------------------------------------------

    def list(self, owner_user_id: int,
             memory_type: Optional[str] = None) -> List[Dict[str, Any]]:
        return [item.as_dict() for item in self.store.memories.list(
            owner_user_id, memory_type=memory_type, status="active")]

    def get(self, owner_user_id: int, memory_id: int) -> Optional[Dict[str, Any]]:
        item = self.store.memories.get(owner_user_id, memory_id)
        return item.as_dict() if item else None

    def search(self, owner_user_id: int, text: str) -> List[Dict[str, Any]]:
        return [item.as_dict() for item in self.store.memories.search(
            owner_user_id, text)]

    def versions(self, owner_user_id: int, memory_id: int) -> List[Dict[str, Any]]:
        return self.store.versions.list(owner_user_id, memory_id)

    def events(self, owner_user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        return [
            {
                "event_id": e.event_id, "memory_id": e.memory_id,
                "action": e.action, "source": e.source, "reason": e.reason,
                "created_at": e.created_at,
            }
            for e in self.store.events.list(owner_user_id, limit=limit)
        ]

    def counts(self, owner_user_id: int) -> Dict[str, int]:
        return self.store.memories.counts(owner_user_id)

    def edit(self, owner_user_id: int, memory_id: int, new_claim: str,
             actor: Optional[int] = None) -> bool:
        """User edits a memory value; old value is preserved as a version."""
        item = self.store.memories.get(owner_user_id, memory_id)
        if item is None or not new_claim.strip():
            return False
        self.store.versions.add(
            memory_id=memory_id, owner_user_id=owner_user_id,
            version=item.version, claim=item.claim,
            confidence=item.confidence, importance=item.importance,
            reason="user_edit", actor=actor,
        )
        claim = " ".join(new_claim.strip().split())
        updated = self.store.memories.update(
            owner_user_id, memory_id, claim=claim,
            confidence=item.confidence, importance=item.importance,
            source_note="user_edited", consent_state=item.consent_state,
        )
        if updated:
            new_key = build_hash_key(owner_user_id, item.scope, item.memory_type,
                                     item.subject, claim)
            self.store.memories.update_hash_key(owner_user_id, memory_id, new_key)
            self.store.events.log(
                owner_user_id=owner_user_id, memory_id=memory_id,
                action="updated", source="user_edit", actor=actor,
            )
        return updated

    def forget(self, owner_user_id: int, memory_id: int,
               actor: Optional[int] = None) -> bool:
        ok = self.store.memories.forget(owner_user_id, memory_id)
        if ok:
            self.store.events.log(
                owner_user_id=owner_user_id, memory_id=memory_id,
                action="forgotten", source="user", actor=actor,
            )
        return ok

    def clear(self, owner_user_id: int, memory_type: Optional[str] = None,
              actor: Optional[int] = None) -> int:
        count = self.store.memories.clear(owner_user_id, memory_type=memory_type)
        if count:
            self.store.events.log(
                owner_user_id=owner_user_id, memory_id=None,
                action="cleared", source="user", actor=actor,
            )
        return count

    def delete(self, owner_user_id: int, memory_id: int,
               actor: Optional[int] = None) -> bool:
        ok = self.store.memories.delete(owner_user_id, memory_id)
        if ok:
            self.store.events.log(
                owner_user_id=owner_user_id, memory_id=memory_id,
                action="deleted", source="user", actor=actor,
            )
        return ok

    # ------------------------------------------------------------------
    # Retrieval / context pack
    # ------------------------------------------------------------------

    def relevant_for(self, owner_user_id: int, *, task_type: str = "chat",
                     query: str = "", subject: str = "",
                     top_k: Optional[int] = None) -> MemoryContextPack:
        task = TaskContext(task_type=task_type, query=query, subject=subject)
        return self.retrieval.relevant(owner_user_id, task=task, top_k=top_k,
                                       purpose="context_pack")

    def memory_block(self, owner_user_id: int, query: str = "",
                     subject: str = "") -> str:
        """Rendered, bounded memory block (DATA) for prompt injection."""
        return self.relevant_for(owner_user_id, query=query,
                                 subject=subject).render()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export(self, owner_user_id: int, actor: Optional[int] = None
               ) -> Optional[Dict[str, Any]]:
        memories = self.store.memories.export(owner_user_id)
        if not memories:
            return None
        payload = {
            "schema": "austro-memory/v1",
            "owner_user_id": owner_user_id,
            "exported_at": self.store.events._now(),
            "memory_count": len(memories),
            "memories": memories,
            "prefs": self.prefs(owner_user_id),
        }
        self.store.access.log(owner_user_id=owner_user_id, purpose="export")
        self.store.events.log(
            owner_user_id=owner_user_id, memory_id=None,
            action="exported", source="user", actor=actor,
        )
        self._metrics["exports"] = self._metrics.get("exports", 0) + 1
        return payload

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def metrics(self) -> Dict[str, Any]:
        metrics = dict(self._metrics)
        metrics["rejected"] = dict(self._metrics["rejected"])
        return metrics