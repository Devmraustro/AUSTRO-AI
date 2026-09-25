"""AUSTRO AI - Personal memory engine.

Extraction -> write gate -> persistence -> bounded retrieval. The AI never
writes memory; everything that enters long-term memory passes the
deterministic ``MemoryWriteGate`` (security, consent, confidence, dedup).
"""

from app.memory.extractors import CandidateExtractor
from app.memory.models import (
    MEMORY_TYPES,
    MemoryCandidate,
    MemoryContextPack,
    MemoryItem,
    TaskContext,
)
from app.memory.retrieval import MemoryRetrieval
from app.memory.service import MemoryService
from app.memory.write_gate import MemoryWriteGate, KNOWN_MEMORY_TYPES

__all__ = [
    "MemoryService",
    "MemoryWriteGate",
    "MemoryRetrieval",
    "CandidateExtractor",
    "MemoryCandidate",
    "MemoryItem",
    "MemoryContextPack",
    "TaskContext",
    "MEMORY_TYPES",
    "KNOWN_MEMORY_TYPES",
]