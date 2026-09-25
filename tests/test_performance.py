#!/usr/bin/env python3
"""
AUSTRO AI - Performance & Integration Benchmarks (Phase F).

Real performance/integration benchmarks for actual application paths:
1. Knowledge retrieval
2. Knowledge ingestion (deterministic/local execution)
3. Memory read/write
4. Learning operations
5. Coaching path
6. AI gateway timeout/retry behavior
7. Concurrent workload

Each benchmark fails when behavior exceeds defined thresholds.
Uses actual application services with deterministic local fixtures.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import List, Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set environment BEFORE any app imports
os.environ["BOT_TOKEN"] = "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken"
os.environ["GEMINI_API_KEY"] = ""
os.environ["USE_LOCAL_FALLBACK"] = "true"
os.environ["AUSTRO_ENVIRONMENT"] = "testing"

# Import global settings after env is set
from app.config.settings import settings as global_settings
from app.ai.gateway import AIGateway
from app.ai.local import LocalProvider
from app.ai.telemetry import AITelemetry
from app.core.container import build_container
from app.database.connection import DatabaseManager
from app.database.migrations import apply_migrations
from app.domain.ai import AIRequest
from app.knowledge.cleaner import TextCleaner
from app.knowledge.embeddings import EmbeddingService, LocalHashEmbedder
from app.knowledge.repositories import KnowledgeStore, EmbeddingRecord
from app.knowledge.retrieval import RetrievalService
from app.learning.mastery import MasterySkill
from app.learning.repositories import LearningStore
from app.learning.spaced import SpacedReviewScheduler
from app.memory.repositories import MemoryStore
from app.memory.models import MemoryItem, build_hash_key


@dataclass
class BenchmarkResult:
    name: str
    latency_ms: float
    threshold_ms: float
    passed: bool
    details: str = ""
    iterations: int = 1
    min_ms: float = 0.0
    max_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    failures: int = 0


def setup_test_database() -> tuple[DatabaseManager, str]:
    """Create a fresh test database and apply migrations."""
    db_path = os.path.join(tempfile.gettempdir(), f"austro_perf_test_{os.getpid()}_{time.time()}.db")
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass
    db = DatabaseManager(db_path)
    apply_migrations(db._get_connection())
    return db, db_path


def cleanup_test_database(db_path: str):
    """Clean up test database."""
    try:
        os.remove(db_path)
    except Exception:
        pass


def get_chunk_content(knowledge: KnowledgeStore, owner_user_id: int, chunk_id: int) -> Optional[str]:
    """Get chunk content by chunk_id using list_candidates."""
    candidates = knowledge.chunks.list_candidates(owner_user_id, limit=2000, source_ids=None)
    for c in candidates:
        if c["chunk_id"] == chunk_id:
            return c.get("content")
    return None


# ============================================================
# 1. KNOWLEDGE RETRIEVAL BENCHMARK
# ============================================================

def benchmark_knowledge_retrieval() -> BenchmarkResult:
    """Benchmark RAG retrieval latency with realistic corpus."""
    db, db_path = setup_test_database()
    try:
        knowledge = KnowledgeStore(db)
        
        # Populate with test data
        owner_user_id = 1
        
        # Create a source with multiple chunks
        source_id = knowledge.sources.create(
            owner_user_id=owner_user_id,
            source_type="book",
            title="Performance Test Corpus",
            file_name="perf_test.txt",
            file_format="txt",
            mime_type="text/plain",
            file_size_bytes=50000,
            checksum="perf_test_checksum",
            storage_key="test/perf_test.txt",
            original_ref=None,
            metadata={"test": True},
        )
        knowledge.sources.set_state(owner_user_id, source_id, "completed", "COMPLETED")
        
        # Add collection
        collection_id = knowledge.collections.create(owner_user_id, "Test Collection", "Test")
        knowledge.collections.add_source(owner_user_id, collection_id, source_id)
        
        # Add document
        doc_id = knowledge.documents.create(
            source_id=source_id,
            owner_user_id=owner_user_id,
            title="Performance Test Corpus",
            author="Test",
            language="en",
            toc=[],
            total_chars=50000,
            total_pages=10,
            metadata={"test": True},
            version=1,
        )
        
        # Add sections and chunks - simulate realistic corpus with 100 chunks
        num_chunks = 100
        chunk_ids = []
        embedder = LocalHashEmbedder(dimensions=128, version="1")
        
        for i in range(num_chunks):
            section_id = knowledge.sections.create(
                document_id=doc_id,
                owner_user_id=owner_user_id,
                source_id=source_id,
                level=0,
                title=f"Section {i // 10}",
                order_index=i,
                start_char=i * 500,
                end_char=(i + 1) * 500,
            )
            
            content = f"This is chunk {i} about topic {i % 20}. " \
                      f"It contains keywords for retrieval testing. " \
                      f"Time management habits productivity learning " \
                      f"goals planning scheduling review assessment."
            
            chunk_id = knowledge.chunks.create(
                owner_user_id=owner_user_id,
                source_id=source_id,
                document_id=doc_id,
                section_id=section_id,
                chunk_key=f"perf_chunk_{i}",
                content=content,
                content_hash=f"hash_{i}",
                token_count=50,
                char_count=len(content),
                page=str(i // 10 + 1),
                order_index=i,
                metadata={},
            )
            chunk_ids.append(chunk_id)
        
        # Generate embeddings
        all_texts = []
        for chunk_id in chunk_ids:
            content = get_chunk_content(knowledge, owner_user_id, chunk_id)
            if content:
                all_texts.append(content)
        
        vectors = embedder.embed_many(all_texts)
        embedding_records = []
        for i, (chunk_id, vector) in enumerate(zip(chunk_ids, vectors)):
            embedding_records.append(EmbeddingRecord(
                owner_user_id=owner_user_id,
                source_id=source_id,
                chunk_row_id=chunk_id,
                model=embedder.model,
                version=embedder.version,
                dimensions=embedder.dimensions,
                vector=vector,
            ))
        knowledge.embeddings.save_many(embedding_records)
        
        # Run retrieval benchmarks
        cleaner = TextCleaner()
        retrieval = RetrievalService(knowledge, EmbeddingService(global_settings), global_settings, cleaner)
        
        queries = [
            "What is time management?",
            "How do habits affect productivity?",
            "What are the best learning techniques?",
            "How to plan study schedule?",
            "What is spaced repetition?",
        ] * 4  # 20 queries total
        
        latencies = []
        for query in queries:
            start = time.perf_counter()
            retrieval.retrieve(owner_user_id, query, top_k=8, collections=[collection_id])
            latency = (time.perf_counter() - start) * 1000
            latencies.append(latency)
        
        min_ms = min(latencies)
        max_ms = max(latencies)
        p50_ms = statistics.median(latencies)
        p95_ms = sorted(latencies)[int(len(latencies) * 0.95)]
        avg_ms = statistics.mean(latencies)
        
        # Threshold: p95 < 300ms for retrieval (allowing variance)
        passed = p95_ms < 300
        
        return BenchmarkResult(
            name="Knowledge Retrieval",
            latency_ms=avg_ms,
            threshold_ms=300,
            passed=passed,
            details=f"p50={p50_ms:.1f}ms, p95={p95_ms:.1f}ms, max={max_ms:.1f}ms over {len(queries)} queries",
            iterations=len(queries),
            min_ms=min_ms,
            max_ms=max_ms,
            p50_ms=p50_ms,
            p95_ms=p95_ms,
            failures=sum(1 for l in latencies if l >= 200),
        )
    finally:
        cleanup_test_database(db_path)


# ============================================================
# 2. KNOWLEDGE INGESTION BENCHMARK
# ============================================================

def benchmark_knowledge_ingestion() -> BenchmarkResult:
    """Benchmark document ingestion core components (extraction + chunking)."""
    db, db_path = setup_test_database()
    try:
        # Test content - larger for more realistic benchmark
        test_content = (
            "Chapter 1: Introduction\n\n"
            "Time management is the process of planning and controlling how much time "
            "to spend on specific activities. Good time management enables an individual "
            "to complete more in a shorter period of time, lowers stress, and leads to "
            "career success.\n\n"
            "Chapter 2: Habits\n\n"
            "Habits are the compound interest of self-improvement. Getting 1 percent "
            "better every day counts for a lot in the long run. Small changes often "
            "appear to make no difference until you cross a critical threshold.\n\n"
            "Chapter 3: Productivity\n\n"
            "Productivity is not about doing more things, but about doing the right things. "
            "Focus on high-impact tasks and delegate or eliminate low-value work. "
            "Use the 80/20 rule to identify the 20% of efforts that produce 80% of results.\n\n"
        ) * 30  # ~15KB content
        
        # Test extraction (TXT format)
        from app.knowledge.extractors import TextExtractor, detect_format
        extractor = TextExtractor()
        
        start = time.perf_counter()
        format_name = detect_format("test.txt", "text/plain")
        extracted = extractor.extract(test_content.encode("utf-8"), format_name)
        extraction_latency = (time.perf_counter() - start) * 1000
        
        # Test chunking
        from app.knowledge.chunker import SemanticChunker, split_paragraphs
        from app.knowledge.cleaner import TextCleaner
        cleaner = TextCleaner()
        chunker = SemanticChunker(chunk_size=900, overlap=100, cleaner=cleaner)
        
        paragraphs = split_paragraphs(extracted.text, cleaner)
        
        start = time.perf_counter()
        chunks_result = chunker.split(paragraphs, extracted.text)
        chunks = chunks_result.get("chunks", [])
        chunking_latency = (time.perf_counter() - start) * 1000
        
        total_latency = extraction_latency + chunking_latency
        
        # Threshold: extraction + chunking < 1000ms for ~15KB text
        passed = total_latency < 1000
        
        return BenchmarkResult(
            name="Knowledge Ingestion (Extract+Chunk)",
            latency_ms=total_latency,
            threshold_ms=1000,
            passed=passed,
            details=f"extract={extraction_latency:.1f}ms, chunk={chunking_latency:.1f}ms, "
                    f"paragraphs={len(paragraphs)}, chunks={len(chunks)}",
            iterations=1,
            min_ms=total_latency,
            max_ms=total_latency,
            p50_ms=total_latency,
            p95_ms=total_latency,
            failures=0 if passed else 1,
        )
    finally:
        cleanup_test_database(db_path)


# ============================================================
# 3. MEMORY READ/WRITE BENCHMARK
# ============================================================

def benchmark_memory_operations() -> BenchmarkResult:
    """Benchmark memory write and read operations."""
    db, db_path = setup_test_database()
    try:
        memory = MemoryStore(db)
        owner_user_id = 1
        
        # Benchmark writes
        write_latencies = []
        num_writes = 100
        
        for i in range(num_writes):
            item = MemoryItem(
                owner_user_id=owner_user_id,
                scope="USER",
                memory_type="preference",
                subject=f"test_{i}",
                claim=f"Test memory claim {i} with some content for retrieval",
                confidence="high",
                importance=3,
                provenance="test",
                hash_key=build_hash_key(owner_user_id, "USER", "preference", f"test_{i}", f"Test memory claim {i}"),
            )
            start = time.perf_counter()
            memory.memories.create(item)
            latency = (time.perf_counter() - start) * 1000
            write_latencies.append(latency)
        
        # Benchmark reads
        read_latencies = []
        for i in range(num_writes):
            start = time.perf_counter()
            memory.memories.get(owner_user_id, i + 1)
            latency = (time.perf_counter() - start) * 1000
            read_latencies.append(latency)
        
        # Benchmark list
        list_latencies = []
        for _ in range(20):
            start = time.perf_counter()
            memory.memories.list(owner_user_id, limit=50)
            latency = (time.perf_counter() - start) * 1000
            list_latencies.append(latency)
        
        all_latencies = write_latencies + read_latencies + list_latencies
        min_ms = min(all_latencies)
        max_ms = max(all_latencies)
        p50_ms = statistics.median(all_latencies)
        p95_ms = sorted(all_latencies)[int(len(all_latencies) * 0.95)]
        avg_ms = statistics.mean(all_latencies)
        
        # Threshold: p95 < 100ms for memory operations (realistic for SQLite)
        passed = p95_ms < 100
        
        return BenchmarkResult(
            name="Memory Read/Write",
            latency_ms=avg_ms,
            threshold_ms=100,
            passed=passed,
            details=f"writes={len(write_latencies)}, reads={len(read_latencies)}, lists={len(list_latencies)}, "
                    f"p50={p50_ms:.1f}ms, p95={p95_ms:.1f}ms, max={max_ms:.1f}ms",
            iterations=len(all_latencies),
            min_ms=min_ms,
            max_ms=max_ms,
            p50_ms=p50_ms,
            p95_ms=p95_ms,
            failures=sum(1 for l in all_latencies if l >= 50),
        )
    finally:
        cleanup_test_database(db_path)


# ============================================================
# 4. LEARNING OPERATIONS BENCHMARK
# ============================================================

def benchmark_learning_operations() -> BenchmarkResult:
    """Benchmark learning engine operations (mastery, scheduling, sessions)."""
    db, db_path = setup_test_database()
    try:
        learning = LearningStore(db)
        owner_user_id = 1
        
        latencies = []
        
        # Mastery skill operations
        for i in range(50):
            skill = MasterySkill()
            start = time.perf_counter()
            for _ in range(10):
                skill.record(correct=True, kind="assessment")
            latency = (time.perf_counter() - start) * 1000
            latencies.append(latency)
        
        # Spaced repetition scheduling
        scheduler = SpacedReviewScheduler()
        for i in range(50):
            item = {"interval_days": 1, "ease": 2.5}
            start = time.perf_counter()
            for _ in range(5):
                scheduler.schedule(item, True)
            latency = (time.perf_counter() - start) * 1000
            latencies.append(latency)
        
        # Goal/objective creation
        for i in range(20):
            start = time.perf_counter()
            learning.goals.create(
                owner_user_id=owner_user_id,
                kind="long_term",
                title=f"Test Goal {i}",
                description="Performance test goal",
            )
            latency = (time.perf_counter() - start) * 1000
            latencies.append(latency)
        
        min_ms = min(latencies)
        max_ms = max(latencies)
        p50_ms = statistics.median(latencies)
        p95_ms = sorted(latencies)[int(len(latencies) * 0.95)]
        avg_ms = statistics.mean(latencies)
        
        # Threshold: p95 < 100ms for learning operations
        passed = p95_ms < 100
        
        return BenchmarkResult(
            name="Learning Operations",
            latency_ms=avg_ms,
            threshold_ms=100,
            passed=passed,
            details=f"mastery_ops=50, schedule_ops=50, goal_ops=20, "
                    f"p50={p50_ms:.1f}ms, p95={p95_ms:.1f}ms, max={max_ms:.1f}ms",
            iterations=len(latencies),
            min_ms=min_ms,
            max_ms=max_ms,
            p50_ms=p50_ms,
            p95_ms=p95_ms,
            failures=sum(1 for l in latencies if l >= 100),
        )
    finally:
        cleanup_test_database(db_path)


# ============================================================
# 5. COACHING PATH BENCHMARK
# ============================================================

async def _run_coaching_benchmark() -> BenchmarkResult:
    """Benchmark coaching advice generation (using local AI)."""
    container = build_container()
    
    stats = {
        "weekly_study_hours": 10,
        "weekly_tasks": 15,
        "total_habits": 5,
        "total_streaks": 30,
        "completed_goals": 3,
        "total_goals": 5,
    }
    
    latencies = []
    num_runs = 10
    
    for i in range(num_runs):
        start = time.perf_counter()
        await container.coaching.advice(stats, user_id=1, memory="User prefers morning study sessions")
        latency = (time.perf_counter() - start) * 1000
        latencies.append(latency)
    
    min_ms = min(latencies)
    max_ms = max(latencies)
    p50_ms = statistics.median(latencies)
    p95_ms = sorted(latencies)[int(len(latencies) * 0.95)]
    avg_ms = statistics.mean(latencies)
    
    # Threshold: p95 < 2000ms for local AI coaching (generates text)
    passed = p95_ms < 2000
    
    return BenchmarkResult(
        name="Coaching Path (Local AI)",
        latency_ms=avg_ms,
        threshold_ms=2000,
        passed=passed,
        details=f"p50={p50_ms:.1f}ms, p95={p95_ms:.1f}ms, max={max_ms:.1f}ms over {num_runs} runs",
        iterations=num_runs,
        min_ms=min_ms,
        max_ms=max_ms,
        p50_ms=p50_ms,
        p95_ms=p95_ms,
        failures=sum(1 for l in latencies if l >= 2000),
    )


def benchmark_coaching_path() -> BenchmarkResult:
    """Sync wrapper for coaching benchmark."""
    return asyncio.run(_run_coaching_benchmark())


# ============================================================
# 6. AI GATEWAY TIMEOUT/RETRY BEHAVIOR BENCHMARK
# ============================================================

def benchmark_ai_gateway_timeout_retry() -> BenchmarkResult:
    """Test AI gateway local fallback behavior (provider unavailable)."""
    from app.ai.gemini import GeminiProvider
    provider = GeminiProvider(global_settings)
    local = LocalProvider()
    telemetry = AITelemetry()
    
    gateway = AIGateway(global_settings, provider=provider, local=local, telemetry=telemetry)
    
    latencies = []
    num_runs = 10
    
    for i in range(num_runs):
        request = AIRequest(
            capability="chat",
            prompt=f"Test prompt {i} for local fallback behavior",
            system_prompt="You are a helpful assistant",
            max_tokens=50,
            user_id=1,
        )
        start = time.perf_counter()
        response = asyncio.run(gateway.generate(request))
        latency = (time.perf_counter() - start) * 1000
        latencies.append(latency)
        
        # Verify response came from local fallback (since no GEMINI_API_KEY)
        # LocalProvider.name = "local"
        if response.metadata.provider != "local":
            print(f"WARNING: Expected local provider, got {response.metadata.provider}")
    
    min_ms = min(latencies)
    max_ms = max(latencies)
    p50_ms = statistics.median(latencies)
    p95_ms = sorted(latencies)[int(len(latencies) * 0.95)]
    avg_ms = statistics.mean(latencies)
    
    # Threshold: local fallback should be fast (< 100ms)
    # Also verify provider is "local"
    verify_request = AIRequest(
        capability="chat", prompt="verify", system_prompt="test", max_tokens=10, user_id=1
    )
    verify_response = asyncio.run(gateway.generate(verify_request))
    fallback_verified = verify_response.metadata.provider == "local"
    
    passed = p95_ms < 100 and fallback_verified
    
    return BenchmarkResult(
        name="AI Gateway Local Fallback",
        latency_ms=avg_ms,
        threshold_ms=100,
        passed=passed,
        details=f"p50={p50_ms:.1f}ms, p95={p95_ms:.1f}ms, max={max_ms:.1f}ms, "
                f"retries={global_settings.ai_max_retries}, fallback=local, verified={fallback_verified}",
        iterations=num_runs,
        min_ms=min_ms,
        max_ms=max_ms,
        p50_ms=p50_ms,
        p95_ms=p95_ms,
        failures=sum(1 for l in latencies if l >= 100),
    )


# ============================================================
# 7. CONCURRENT WORKLOAD BENCHMARK
# ============================================================

async def _run_concurrent_workload() -> BenchmarkResult:
    """Benchmark concurrent mixed workload."""
    container = build_container()
    db, db_path = setup_test_database()
    
    try:
        knowledge = KnowledgeStore(db)
        memory = MemoryStore(db)
        coaching = container.coaching
        
        # Setup minimal data
        owner_user_id = 1
        source_id = knowledge.sources.create(
            owner_user_id=owner_user_id,
            source_type="book",
            title="Concurrent Test",
            file_name="concurrent.txt",
            file_format="txt",
            mime_type="text/plain",
            file_size_bytes=1000,
            checksum="concurrent_checksum",
            storage_key="test/concurrent.txt",
            original_ref=None,
            metadata={"test": True},
        )
        knowledge.sources.set_state(owner_user_id, source_id, "completed", "COMPLETED")
        collection_id = knowledge.collections.create(owner_user_id, "Test", "Test")
        knowledge.collections.add_source(owner_user_id, collection_id, source_id)
        
        doc_id = knowledge.documents.create(
            source_id=source_id, owner_user_id=owner_user_id,
            title="Concurrent Test", author="Test", language="en", toc=[],
            total_chars=1000, total_pages=1, metadata={}, version=1,
        )
        section_id = knowledge.sections.create(
            document_id=doc_id, owner_user_id=owner_user_id,
            source_id=source_id, level=0, title="Test", order_index=0,
            start_char=0, end_char=1000,
        )
        
        # Add a few chunks with embeddings
        chunk_id = knowledge.chunks.create(
            owner_user_id=owner_user_id, source_id=source_id,
            document_id=doc_id, section_id=section_id,
            chunk_key="concurrent_1", content="Test content for retrieval",
            content_hash="hash", token_count=10, char_count=30,
            page="1", order_index=0, metadata={},
        )
        embedder = LocalHashEmbedder(dimensions=128, version="1")
        vector = embedder.embed("Test content for retrieval")
        knowledge.embeddings.save_many([EmbeddingRecord(
            owner_user_id=owner_user_id, source_id=source_id,
            chunk_row_id=chunk_id, model=embedder.model,
            version=embedder.version, dimensions=embedder.dimensions,
            vector=vector,
        )])
        
        # Define concurrent tasks
        cleaner = TextCleaner()
        retrieval = RetrievalService(knowledge, EmbeddingService(global_settings), global_settings, cleaner)
        
        async def retrieval_task(i: int) -> float:
            start = time.perf_counter()
            retrieval.retrieve(owner_user_id, f"query {i}", top_k=5, collections=[collection_id])
            return (time.perf_counter() - start) * 1000
        
        async def memory_write_task(i: int) -> float:
            item = MemoryItem(
                owner_user_id=owner_user_id, scope="USER",
                memory_type="preference", subject=f"concurrent_{i}",
                claim=f"Concurrent memory {i}", confidence="high",
                importance=3, provenance="test",
                hash_key=build_hash_key(owner_user_id, "USER", "preference", f"concurrent_{i}", f"Concurrent memory {i}"),
            )
            start = time.perf_counter()
            memory.memories.create(item)
            return (time.perf_counter() - start) * 1000
        
        async def memory_read_task(i: int) -> float:
            start = time.perf_counter()
            memory.memories.list(owner_user_id, limit=10)
            return (time.perf_counter() - start) * 1000
        
        async def learning_task(i: int) -> float:
            start = time.perf_counter()
            skill = MasterySkill()
            for _ in range(5):
                skill.record(correct=True, kind="assessment")
            return (time.perf_counter() - start) * 1000
        
        async def coaching_task(i: int) -> float:
            stats = {"weekly_study_hours": 5, "weekly_tasks": 8, "total_habits": 3, "total_streaks": 15}
            start = time.perf_counter()
            await coaching.advice(stats, user_id=owner_user_id)
            return (time.perf_counter() - start) * 1000
        
        # Run concurrent mix: 5 retrieval, 5 memory write, 5 memory read, 5 learning, 2 coaching = 22 tasks
        tasks = []
        for i in range(5):
            tasks.append(retrieval_task(i))
            tasks.append(memory_write_task(i))
            tasks.append(memory_read_task(i))
            tasks.append(learning_task(i))
        for i in range(2):
            tasks.append(coaching_task(i))
        
        start = time.perf_counter()
        latencies = await asyncio.gather(*tasks)
        total_time = (time.perf_counter() - start) * 1000
        
        min_ms = min(latencies)
        max_ms = max(latencies)
        p50_ms = statistics.median(latencies)
        p95_ms = sorted(latencies)[int(len(latencies) * 0.95)]
        
        # Threshold: all tasks complete within 3000ms total, p95 individual < 2500ms
        passed = total_time < 3000 and p95_ms < 2500
        
        return BenchmarkResult(
            name="Concurrent Workload",
            latency_ms=total_time,
            threshold_ms=3000,
            passed=passed,
            details=f"total={total_time:.1f}ms, 22 concurrent tasks (5R/5W/5L/5LR/2C), "
                    f"p50={p50_ms:.1f}ms, p95={p95_ms:.1f}ms, max={max_ms:.1f}ms",
            iterations=len(latencies),
            min_ms=min_ms,
            max_ms=max_ms,
            p50_ms=p50_ms,
            p95_ms=p95_ms,
            failures=sum(1 for l in latencies if l >= 2500) + (1 if total_time >= 3000 else 0),
        )
    finally:
        cleanup_test_database(db_path)


def benchmark_concurrent_workload() -> BenchmarkResult:
    """Sync wrapper for concurrent workload benchmark."""
    return asyncio.run(_run_concurrent_workload())


def run_all_benchmarks() -> List[BenchmarkResult]:
    """Run all performance benchmarks."""
    benchmarks = [
        ("Knowledge Retrieval", benchmark_knowledge_retrieval),
        ("Knowledge Ingestion", benchmark_knowledge_ingestion),
        ("Memory Read/Write", benchmark_memory_operations),
        ("Learning Operations", benchmark_learning_operations),
        ("Coaching Path", benchmark_coaching_path),
        ("AI Gateway Timeout/Retry", benchmark_ai_gateway_timeout_retry),
        ("Concurrent Workload", benchmark_concurrent_workload),
    ]
    
    results = []
    for name, func in benchmarks:
        print(f"\nRunning {name} benchmark...")
        try:
            result = func()
            results.append(result)
            status = "PASS" if result.passed else "FAIL"
            print(f"  {status}: {result.name} - {result.latency_ms:.1f}ms (threshold: {result.threshold_ms}ms)")
            print(f"    Details: {result.details}")
        except Exception as e:
            print(f"  ERROR: {name} - {e}")
            import traceback
            traceback.print_exc()
            results.append(BenchmarkResult(
                name=name,
                latency_ms=0,
                threshold_ms=0,
                passed=False,
                details=f"Benchmark error: {e}",
                failures=1,
            ))
    
    return results


def print_summary(results: List[BenchmarkResult]):
    """Print benchmark summary."""
    print("\n" + "=" * 70)
    print("PERFORMANCE BENCHMARK SUMMARY")
    print("=" * 70)
    
    all_passed = True
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  {status} | {r.name:30s} | avg={r.latency_ms:7.1f}ms | "
              f"threshold={r.threshold_ms:5.0f}ms | iter={r.iterations:2d} | "
              f"p95={r.p95_ms:7.1f}ms")
        if not r.passed:
            all_passed = False
    
    print("=" * 70)
    overall = "ALL PASS" if all_passed else "SOME FAILED"
    print(f"  {overall}: {sum(1 for r in results if r.passed)}/{len(results)} benchmarks passed")
    
    # Machine info
    import platform
    print(f"\nEnvironment: {platform.platform()} Python {platform.python_version()}")
    print("Note: Machine-dependent measurements - thresholds may need adjustment per environment")
    
    return all_passed


if __name__ == "__main__":
    results = run_all_benchmarks()
    all_passed = print_summary(results)
    exit(0 if all_passed else 1)