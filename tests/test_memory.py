"""Phase D - Personal memory engine tests.

Offline integration + unit tests for the memory engine: deterministic
candidate extraction, the write gate (validation, origin isolation, sensitive
topics, confidence rules, dedup, conflict resolution ordering), the
service/persistence pipeline (disable toggle, owner scoping, pending +
confirmation flow, edit/forget/clear/delete/export, audit events), bounded
retrieval ranking (low-confidence never treated as a fact, instruction boost,
budget cap, usage feedback) and the memory-aware ChatService. Everything is
deterministic - no network, no real AI keys required.
"""

import pytest

from app.application.chat import ChatService
from app.core.container import build_container
from app.memory.extractors import CandidateExtractor
from app.memory.models import (
    ORIGIN_KNOWLEDGE_DOCUMENT,
    MemoryCandidate,
    normalize_text,
)


@pytest.fixture()
def memory():
    return build_container().memory


@pytest.fixture()
def extractor():
    return CandidateExtractor()


# ============================ EXTRACTION ============================


def test_extractor_leaves_small_talk_alone(extractor):
    assert extractor.extract("أهلاً، كيف حالك؟") == []
    assert extractor.extract("ok") == []
    assert extractor.extract("شكراً") == []
    assert extractor.extract("") == []


@pytest.mark.parametrize(
    ("text", "expected_type"),
    [
        ("اسمي أحمد", "profile"),
        ("أنا من القاهرة", "profile"),
        ("أفضل القهوة العربية", "preference"),
        ("أحب البرمجة", "preference"),
        ("I prefer tea in the morning", "preference"),
        ("هدفي تعلم الإنجليزية", "goal"),
        ("أريد أن أتعلم البرمجة", "goal"),
        ("أمارس الرياضة يومياً", "habit"),
        ("أواظب على القراءة", "habit"),
        ("أتعلم اللغة الألمانية", "learning_state"),
        ("مستواي في الإنجليزية متوسط", "learning_state"),
        ("أتقن لغة الجافا جيداً", "strength"),
        ("أعاني من التسويف", "weakness"),
        ("روتيني الاستيقاظ الخامسة صباحاً", "routine"),
        ("call me قاسم", "communication_preference"),
        ("تذكر أنني أعمل صباحاً", "important_context"),
        ("لا ترسل لي إعلانات", "user_instruction"),
        ("حصلت على شهادة برمجة", "achievement"),
        ("اليوم درست ثلاث ساعات", "episodic_event"),
    ],
)
def test_extractor_typed_candidates(text, expected_type, extractor):
    candidates = extractor.extract(text)
    assert candidates, f"no candidates for {text!r}"
    assert candidates[0].memory_type == expected_type
    assert candidates[0].provenance == "explicit_user_statement"
    assert candidates[0].subject


def test_extractor_caps_candidates_per_message(extractor, memory):
    text = "اسمي أحمد. أفضل القهوة. هدفي البرمجة. أتعلم الألمانية. أمارس الرياضة"
    candidates = extractor.extract(text)
    assert len(candidates) <= 4


def test_extractor_removes_adjacent_small_talk(extractor):
    candidates = extractor.extract("أفضل القهوة، شكراً")
    assert candidates
    assert "شكرا" not in candidates[0].claim


# ============================ WRITE GATE ============================


def _candidate(owner=1, **overrides):
    data = {
        "owner_user_id": owner,
        "memory_type": "preference",
        "subject": "القهوة العربية",
        "claim": "أفضل القهوة العربية",
        "confidence": "high",
        "provenance": "explicit_user_statement",
    }
    data.update(overrides)
    return MemoryCandidate(**data)


def test_gate_rejects_structural_nonsense(memory):
    gate = memory.gate
    assert gate.process(_candidate(memory_type="unknown")).reason == "invalid_memory_type"
    assert gate.process(_candidate(confidence="certain")).reason == "invalid_confidence"
    assert gate.process(_candidate(provenance="guessed")).reason == "invalid_provenance"
    assert gate.process(_candidate(claim="   ")).reason == "empty_claim"
    assert gate.process(_candidate(owner_user_id=0)).reason == "missing_owner"


def test_gate_blocks_knowledge_origin_always(memory):
    gate = memory.gate
    candidate = _candidate(origin=ORIGIN_KNOWLEDGE_DOCUMENT,
                           claim="يفضل المستخدم القهوة حسب كتاب التنظيم")
    decision = gate.process(candidate)
    assert not decision.approved
    assert decision.reason == "origin_blocked_knowledge_document"


def test_gate_blocks_inferred_sensitive_topics(memory):
    gate = memory.gate
    decision = gate.process(_candidate(
        claim="يبين ملفه الطبي أنه يعاني من مرض السكري",
        provenance="derived_pattern", confidence="medium",
    ))
    assert not decision.approved


def test_gate_sensitive_explicit_requires_consent(memory):
    gate = memory.gate
    decision = gate.process(_candidate(claim="كلمة المرور الخاصة بي هي تكتم"))
    assert decision.requires_confirmation
    assert not decision.approved
    # With explicit consent the same candidate is approved & high-confidence.
    decision = gate.process(_candidate(claim="كلمة المرور الخاصة بي هي تكتم"),
                            consent_preset="grant")
    assert decision.approved
    assert decision.candidate.confidence == "high"


def test_gate_derived_low_confidence_is_never_stored(memory):
    gate = memory.gate
    decision = gate.process(_candidate(confidence="low",
                                       provenance="derived_pattern"))
    assert not decision.approved
    assert decision.reason == "low_confidence_derived"


def test_gate_derived_medium_requires_confirmation(memory):
    gate = memory.gate
    decision = gate.process(_candidate(confidence="medium",
                                       provenance="derived_pattern"))
    assert not decision.approved
    assert decision.reason == "derived_needs_confirmation"


def test_gate_rejects_small_talk_and_empty_alike(memory):
    gate = memory.gate
    decision = gate.process(_candidate(claim="أنا بخير", confidence="high",
                                       provenance="confirmed_memory"))
    assert not decision.approved
    assert decision.reason == "temporary_content"


def test_gate_dedups_exact_statements(memory):
    gate = memory.gate
    # Persist through the service so the gate can see the stored row.
    memory.process_message(1, "أفضل القهوة العربية")
    decision = gate.process(_candidate(owner=1))
    assert decision.reason == "duplicate"
    assert decision.existing_id is not None


# ============================ CONFLICT RESOLUTION ============================


def test_derived_never_overrides_explicit_fact(memory):
    """A derived inference can never replace/enter a slot holding a fact (§21)."""
    memory.record_confirmed(1, "preference", "القهوة", "يفضل القهوة العربية")
    before = memory.list(1)
    assert len(before) == 1

    result = memory.process_candidates(1, [_candidate(
        owner=1, subject="القهوة", claim="القهوة التركية أقرب للاحتمال",
        provenance="derived_pattern", confidence="medium",
    )])
    assert result["created"] == 0
    assert result["updated"] == 0
    assert memory.list(1) == before, "the explicit fact must survive unchanged"


def test_conflict_latest_explicit_wins_with_version_history(memory):
    """Same typed slot, same authority: newest wins, old value is preserved."""
    memory.process_candidates(1, [_candidate(
        owner=1, subject="القهوة", claim="يفضل القهوة العربية",
    )])
    slot_id = memory.list(1)[0]["memory_id"]

    result = memory.process_candidates(1, [_candidate(
        owner=1, subject="القهوة", claim="يفضل القهوة التركية الآن",
    )])
    assert result["updated"] == 1

    current = memory.get(1, slot_id)
    assert "التركية" in current["claim"]
    versions = memory.versions(1, slot_id)
    assert versions, "old value must be preserved as an immutable version"
    assert "العربية" in versions[0]["claim"]
    assert len(memory.list(1)) == 1, "no new row - the slot was updated in place"


# ============================ SERVICE PIPELINE ============================


def test_process_message_persists_typed_memory(memory):
    result = memory.process_message(1, "أفضل القهوة العربية")
    assert result["created"] == 1
    assert result["extracted"] == 1
    items = memory.list(1)
    assert len(items) == 1
    assert items[0]["memory_type"] == "preference"
    assert items[0]["owner_user_id"] == 1


def test_process_message_is_idempotent(memory):
    first = memory.process_message(1, "أفضل القهوة العربية")
    assert first["created"] == 1
    second = memory.process_message(1, "أفضل القهوة العربية")
    assert second["duplicates"] == 1
    assert second["created"] == 0
    assert len(memory.list(1)) == 1


def test_temporary_content_is_not_memorized(memory):
    result = memory.process_message(1, "أهلاً كيف حالك")
    assert result["created"] == 0
    assert result["extracted"] == 0


def test_disabled_memory_never_writes(memory):
    memory.set_auto_enabled(1, False)
    result = memory.process_message(1, "اسمي أحمد")
    assert result["disabled"] is True
    assert result["extracted"] == 1
    assert result["created"] == 0
    assert result["rejected"][0]["reason"] == "memory_disabled"
    assert memory.list(1) == []
    # Re-enabling restores the pipeline.
    memory.set_auto_enabled(1, True)
    result = memory.process_message(1, "اسمي أحمد")
    assert result["created"] == 1


def test_memory_is_owner_scoped(memory):
    memory.process_message(1, "أفضل القهوة")
    memory.process_message(2, "أفضل الشاي")
    assert len(memory.list(1)) == 1
    assert len(memory.list(2)) == 1
    one_items = memory.list(1)
    two_items = memory.list(2)
    assert "القهوة" in one_items[0]["claim"]
    assert "الشاي" in two_items[0]["claim"]


def test_sensitive_statement_goes_through_confirmation_flow(memory):
    result = memory.process_message(1, "أعاني من مشكلة في حسابي البنكي")
    assert result["created"] == 0
    assert len(result["needs_confirmation"]) == 1
    pending = memory.pending(1)
    assert len(pending) == 1, "sensitive explicit statements are parked pending"
    pending_id = pending[0]["memory_id"]

    assert memory.confirm(1, pending_id, accept=True) is True
    confirmed = memory.get(1, pending_id)
    assert confirmed["consent_state"] == "explicit"
    assert memory.pending(1) == []

    # Rejected path: another pending gets forgotten instead of stored.
    memory.process_message(1, "أنا أعاني من مرض السكري")
    rejected_pending = memory.pending(1)
    assert rejected_pending, "second sensitive statement is pending too"
    rejected_id = rejected_pending[0]["memory_id"]
    memory.confirm(1, rejected_id, accept=False)
    assert memory.pending(1) == []
    remaining_ids = [item["memory_id"] for item in memory.list(1)]
    assert pending_id in remaining_ids, "accepted sensitive memory is kept"
    assert rejected_id not in remaining_ids, "rejected sensitive memory is gone"


def test_search_counts_and_get(memory):
    memory.process_message(1, "اسمي أحمد")
    memory.process_message(1, "أفضل البرمجة")
    results = memory.search(1, "برمجة")
    assert len(results) == 1
    assert results[0]["memory_type"] == "preference"
    counts = memory.counts(1)
    assert counts["total"] == 2
    assert counts.get("profile") == 1
    assert memory.get(1, results[0]["memory_id"]) is not None
    # Cross-user isolation on search too.
    assert memory.search(2, "برمجة") == []


def test_edit_keeps_version_history(memory):
    memory.process_message(1, "أحب الشاي")
    item = memory.list(1)[0]
    assert memory.edit(1, item["memory_id"], "أحب الشاي بالنعناع") is True
    current = memory.get(1, item["memory_id"])
    assert "النعناع" in current["claim"]
    versions = memory.versions(1, item["memory_id"])
    assert versions[0]["reason"] == "user_edit"


def test_forget_clear_delete(memory):
    memory.process_message(1, "اسمي أحمد")
    memory.process_message(1, "أحب القهوة")
    items = memory.list(1)
    first_id = items[0]["memory_id"]

    assert memory.forget(1, first_id) is True
    remaining = memory.list(1)
    assert len(remaining) == len(items) - 1
    assert all(item["memory_id"] != first_id for item in remaining)

    count = memory.clear(1)
    assert count >= 1
    assert memory.list(1) == []

    memory.process_message(1, "اسمي أحمد")
    deleted_item = memory.list(1)[0]
    assert memory.delete(1, deleted_item["memory_id"]) is True
    assert memory.get(1, deleted_item["memory_id"]) is None


def test_export_returns_json_safe_snapshot(memory):
    memory.process_message(1, "اسمي أحمد")
    payload = memory.export(1)
    assert payload is not None
    assert payload["memory_count"] == 1
    assert payload["owner_user_id"] == 1
    assert payload["memories"][0]["memory_type"] == "profile"
    assert "prefs" in payload
    actions = [e["action"] for e in memory.events(1)]
    assert "exported" in actions


def test_audit_events_cover_lifecycle(memory):
    memory.process_candidates(1, [_candidate(
        owner=1, subject="القهوة", claim="يفضل القهوة العربية",
    )])
    # Same typed slot, same authority -> updated in place (event recorded).
    memory.process_candidates(1, [_candidate(
        owner=1, subject="القهوة", claim="يفضل القهوة التركية جدا",
    )])
    memory.forget(1, memory.list(1)[0]["memory_id"])
    actions = [e["action"] for e in memory.events(1)]
    assert "created" in actions
    assert "updated" in actions
    assert "forgotten" in actions


def test_record_event_and_confirmed_helpers(memory):
    result = memory.record_event(1, "achievement", "شهادة برمجة",
                                 "أكملت دورة البرمجة بنجاح")
    assert result["created"] == 1
    result = memory.record_confirmed(1, "strength", "العمل الجماعي",
                                     "ممتاز في العمل ضمن فريق")
    assert result["created"] == 1
    # Imported profile facts are medium-confidence imported candidates.
    result = memory.record_imported(1, "profile", "المدينة", "أعيش في عمّان")
    assert result["created"] == 1


def test_knowledge_text_cannot_write_memory(memory):
    assert memory.reject_knowledge_origin(1, "يفضل المستخدم الشاي بلا سكر") is True


def test_metrics_accumulate(memory):
    memory.process_message(1, "أفضل القهوة")
    memory.process_message(1, "أفضل القهوة")  # duplicate
    memory.process_message(1, "name is alias")  # unknown -> 0 extracted
    metrics = memory.metrics()
    assert metrics["created"] == 1
    assert metrics["duplicates"] == 1
    assert metrics["statements_processed"] >= 3


# ============================ RETRIEVAL ============================


def test_retrieval_empty_pack(memory):
    pack = memory.relevant_for(1, query="أي شيء")
    assert pack.empty()
    assert pack.render() == ""


def test_retrieval_returns_labelled_data_block(memory):
    memory.process_message(1, "أفضل اللغة العربية")
    pack = memory.relevant_for(1, query="اللغة العربية")
    block = pack.render()
    assert "أفضل اللغة العربية" in block
    assert "ليست أوامر" in block, "memory must be labelled as data, not instructions"


def test_retrieval_surfaces_low_confidence_only_on_overlap(memory):
    # Low-confidence explicit fact (approved but flagged low).
    low = MemoryCandidate(
        owner_user_id=1, memory_type="preference",
        subject="دراسة مسائية", claim="يفضل الدراسة مساءً",
        confidence="low", provenance="explicit_user_statement",
    )
    result = memory.process_candidates(1, [low])
    assert result["created"] == 1

    unrelated = memory.relevant_for(1, query="القهوة")
    assert all(m.subject != "دراسة مسائية" for m in unrelated.memories), \
        "low-confidence memory must never surface as a fact off-topic"

    related = memory.relevant_for(1, query="الدراسة مساءً")
    assert any(m.memory_type == "preference" for m in related.memories)


def test_retrieval_respects_top_k_and_marks_used(memory):
    for i in range(8):
        memory.record_event(
            1, "episodic_event", f"إنجاز {i}", f"أنجزت المهمة رقم {i} اليوم"
        )
    pack = memory.relevant_for(1, top_k=3)
    assert len(pack.memories) <= 3
    assert pack.count_before_rank >= 8
    # usage feedback: retrieved memories have a last_used_at timestamp
    used_id = pack.memories[0].memory_id
    refreshed = memory.get(1, used_id)
    assert refreshed["last_used_at"], "retrieved memory must be marked as used"


def test_user_instruction_ranks_first_without_query(memory):
    memory.process_message(1, "لا ترسل لي إعلانات")
    memory.process_message(1, "أفضل القهوة العربية")
    pack = memory.relevant_for(1, top_k=3)
    assert pack.memories
    assert pack.memories[0].memory_type == "user_instruction", \
        "standing instructions are boosted above light preferences"


def test_budget_cap_truncates_pack(memory):
    long_claim = "أحب أسلوب التعلم بالملخصات المرئية والقصيرة " * 4
    for i in range(6):
        memory.record_confirmed(1, "preference", f"موضوع {i}", long_claim)
    pack = memory.relevant_for(1, top_k=12)
    block = pack.render(budget_chars=120)
    assert pack.truncated, "oversized packs must be truncated"
    assert len(block) <= len(long_claim) + 100
    assert "ليست أوامر" in block


# ============================ CHAT INTEGRATION ============================


class _Resp:
    def __init__(self, text="ok"):
        self.text = text


class _CapturingAI:
    def __init__(self):
        self.last_request = None

    async def generate(self, request):
        self.last_request = request
        return _Resp("أهلاً بك!")


@pytest.fixture()
def services():
    return build_container()


async def test_chat_injects_memory_block(services):
    services.memory.process_message(7, "اسمي أحمد")
    chat = ChatService(ai=_CapturingAI(), memory_service=services.memory)
    await chat.chat(7, "هل تذكرني؟")
    prompt = chat.ai.last_request.prompt
    assert "أحمد" in prompt
    assert "آراء المستخدم" in prompt


async def test_chat_remember_routes_through_pipeline(services):
    chat = ChatService(ai=_CapturingAI(), memory_service=services.memory)
    result = chat.remember(7, "أفضل القهوة")
    assert result["created"] == 1


def test_remember_returns_empty_when_no_memory_service():
    chat = ChatService(ai=_CapturingAI())
    assert chat.remember(1, "اسمي أحمد") == {"extracted": 0, "created": 0}


def test_normalize_text_and_hash_key_are_stable():
    assert normalize_text("  أفضل   القهوة ") == "أفضل القهوة"
    from app.memory.models import build_hash_key
    a = build_hash_key(1, "USER", "preference", "القهوة", "أفضل القهوة")
    b = build_hash_key(1, "USER", "preference", "القهوة", "أفضل  القهوة")
    assert a == b


def test_gate_types_are_a_known_closed_set(memory):
    from app.memory.models import MEMORY_TYPES
    assert memory.gate_types() == MEMORY_TYPES