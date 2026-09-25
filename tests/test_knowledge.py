"""Phase C knowledge engine tests.

Offline integration + unit tests for the knowledge engine: upload validation,
checksums, duplicate idempotency, the ingestion pipeline state machine,
extraction (txt + PDF), chunking, embeddings, retrieval scoring, cross-user
permission isolation, failure handling, delete lifecycle and the knowledge
facade. The deterministic local embedder and the local grounded-answer
fallback mean no network access and no external API keys are needed.
"""

import dataclasses

import pytest

from app.core.container import build_container
from app.core.errors import NotFoundError, ValidationError
from app.knowledge.chunker import (
    SemanticChunker,
    analyze_structure,
    split_paragraphs,
)
from app.knowledge.cleaner import TextCleaner
from app.knowledge.extractors import TextExtractor, detect_format
from app.knowledge.models import READY_STATE

ARABIC_BOOK = (
    "الفصل الأول\n"
    "سر إدارة الوقت\n\n"
    "إدارة الوقت هي المهارة الأساسية التي تسمح للإنسان بإنجاز أهدافه اليومية بكفاءة.\n"
    "التركيز على مهمة واحدة في كل مرة يزيد الإنتاجية بشكل ملحوظ.\n\n"
    "الفصل الثاني\n"
    "تكوين العادات\n\n"
    "العادات الصغيرة تتراكم وتصنع فرقاً كبيراً على المدى الطويل.\n"
    "الالتزام اليومي المتسق هو سر النجاح في بناء أي عادة جديدة.\n"
)


def _minimal_pdf(text: str) -> bytes:
    """Build a structural one-page ASCII PDF that pypdf can parse."""
    stream = b"BT /F1 18 Tf 72 720 Td (" + text.encode("latin-1") + b") Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >> stream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref_pos).encode()
        + b"\n%%EOF"
    )
    return bytes(out)


@pytest.fixture()
def knowledge(fresh_db):
    return build_container().knowledge


async def _ingest(knowledge, owner, file_name, text, mime_type=None):
    upload = knowledge.register_upload(
        owner_user_id=owner,
        file_name=file_name,
        data=text.encode("utf-8"),
        mime_type=mime_type,
    )
    result = await knowledge.process_source(owner, upload["source_id"])
    return upload, result


# ============================ EXTRACTION ============================


def test_detect_format_by_extension_and_mime():
    assert detect_format("book.pdf", None) == "pdf"
    assert detect_format("book.PDF", None) == "pdf"
    assert detect_format("notes.txt", None) == "txt"
    assert detect_format("page.md", None) == "md"
    assert detect_format("doc.docx", None) == "docx"
    assert detect_format("novel.epub", None) == "epub"
    assert detect_format("book.pdf", "text/plain") == "pdf"
    assert detect_format("book.dat", "application/pdf") == "pdf"
    with pytest.raises(ValidationError):
        detect_format("archive.zip", None)


def test_extract_text_supported_formats():
    extractor = TextExtractor()
    assert extractor.extract(b"hello world", "txt").text == "hello world"
    assert extractor.extract(b"# Title\ncontent", "md").text == "# Title\ncontent"
    assert "hello world" in extractor.extract(_minimal_pdf("hello world"), "pdf").text


def test_extract_text_unknown_data_raises():
    with pytest.raises(ValidationError):
        TextExtractor().extract(b"not a zip", "epub")


# ============================ CLEANER / CHUNKER ============================


def test_cleaner_normalizes_arabic_and_removes_junk():
    cleaner = TextCleaner()
    tokens = cleaner.tokens("إدارةِ الوقتِ وتنظي،مه!! 2024")
    assert "اداره" in tokens
    assert "الوقت" in tokens
    assert "2024" in tokens
    assert "،" not in "".join(tokens)
    # normalize keeps readability, reflows blocks and drops junk
    assert cleaner.normalize("\ufeffنص بسيط\n\n فقرة  ثانية ") == "نص بسيط\n\nفقرة ثانية"
    assert cleaner.normalize("صفحة\n12\nنص") == "صفحة نص"  # page-number line dropped
    assert "\u200b" not in cleaner.normalize("إدارةِ\n\u200bالوقت")


def test_split_paragraphs_resolves_pages_from_raw_offsets():
    cleaner = TextCleaner()
    text = "أ" * 30 + "\n\n" + "ب" * 30
    resolver = lambda raw_start: "1" if raw_start < 30 else "2"
    paragraphs = split_paragraphs(text, cleaner, page_resolver=resolver)
    assert len(paragraphs) == 2
    assert paragraphs[0].body and paragraphs[1].body
    assert paragraphs[0].raw_start < 30
    assert paragraphs[1].raw_start >= 32
    assert paragraphs[0].page == "1"
    assert paragraphs[1].page == "2"


def test_structure_and_semantic_chunker_produce_attributed_chunks():
    cleaner = TextCleaner()
    paragraphs = split_paragraphs(ARABIC_BOOK, cleaner)
    normalized = cleaner.normalize(ARABIC_BOOK)
    structure = analyze_structure(normalized)
    assert structure["sections"], "expected at least one detected heading"

    result = SemanticChunker(chunk_size=180, overlap=40).split(paragraphs, normalized)
    chunks = result["chunks"]
    assert chunks
    assert result["language"] == "ar"
    keys = {chunk["chunk_key"] for chunk in chunks}
    assert len(keys) == len(chunks)
    for chunk in chunks:
        assert chunk["content"]
        assert chunk["token_count"] > 0
        assert chunk["section_title"]


# ============================ UPLOAD VALIDATION ============================


def test_register_upload_rejects_empty_file(knowledge):
    with pytest.raises(ValidationError):
        knowledge.register_upload(owner_user_id=1, file_name="e.txt", data=b"")


def test_register_upload_rejects_unsupported_format(knowledge):
    with pytest.raises(ValidationError):
        knowledge.register_upload(owner_user_id=1, file_name="e.zip", data=b"x")


def test_register_upload_rejects_oversize(knowledge, monkeypatch):
    overrides = dataclasses.replace(knowledge._settings, knowledge_max_file_size_mb=1)
    monkeypatch.setattr(knowledge, "_settings", overrides)
    with pytest.raises(ValidationError):
        knowledge.register_upload(
            owner_user_id=1, file_name="big.txt", data=b"x" * (2 * 1024 * 1024)
        )


def test_register_upload_creates_pending_source(knowledge):
    upload = knowledge.register_upload(
        owner_user_id=5, file_name="book.txt", data=b"hello world"
    )
    assert upload["duplicate"] is False
    source = knowledge.source(5, upload["source_id"])
    assert source["status"] == "pending"
    assert source["ingestion_state"] == "UPLOAD"
    assert source["file_format"] == "txt"
    assert source["file_size_bytes"] == 11
    assert len(source["checksum"]) == 64
    assert source["storage_key"]
    assert knowledge._storage.read(source["storage_key"]) == b"hello world"


# ============================ INGESTION PIPELINE ============================


async def test_ingest_txt_reaches_ready_with_consistent_embeddings(knowledge):
    upload, result = await _ingest(knowledge, 7, "time.txt", ARABIC_BOOK)
    assert result.status == "completed"
    assert result.chunk_count > 0

    source = knowledge.source(7, upload["source_id"])
    assert source["status"] == "completed"
    assert source["ingestion_state"] == READY_STATE
    assert source["char_count"] > 0
    assert source["title"]

    chunks = knowledge._store.chunks.list_candidates(7, limit=500)
    embedding_rows = knowledge._store.embeddings.count_for_source(
        upload["source_id"], "local-hash", "1"
    )
    assert embedding_rows == len(chunks) == result.chunk_count


async def test_ingest_pdf_reaches_ready_with_page_numbers(knowledge):
    pdf = _minimal_pdf("Routine: waking early each day improves focus sharply.")
    upload = knowledge.register_upload(
        owner_user_id=8,
        file_name="routine.pdf",
        data=pdf,
        mime_type="application/pdf",
    )
    result = await knowledge.process_source(8, upload["source_id"])
    assert result.status == "completed"
    assert knowledge.source(8, upload["source_id"])["ingestion_state"] == READY_STATE
    hits = knowledge.search(8, "waking early improves focus")
    assert hits.grounded
    assert hits.chunks[0].page in ("1", None)


async def test_ingest_is_idempotent_by_checksum(knowledge):
    upload1, _ = await _ingest(knowledge, 9, "a.txt", ARABIC_BOOK)
    upload2 = knowledge.register_upload(
        owner_user_id=9, file_name="renamed.txt", data=ARABIC_BOOK.encode()
    )
    assert upload2["duplicate"] is True
    assert upload2["source_id"] == upload1["source_id"]
    assert len(knowledge.list_sources(9)) == 1


async def test_pipeline_failure_is_recorded_and_recovers(knowledge):
    upload = knowledge.register_upload(
        owner_user_id=10, file_name="bad.epub", data=b"not a zip"
    )
    result = await knowledge.process_source(10, upload["source_id"])
    assert result.status == "failed"
    source = knowledge.source(10, upload["source_id"])
    assert source["status"] == "failed"
    assert source["error_message"]
    _, result2 = await _ingest(knowledge, 10, "good.txt", ARABIC_BOOK)
    assert result2.status == "completed"


# ============================ PERMISSION ISOLATION ============================


async def test_knowledge_is_owner_scoped(knowledge):
    upload, _ = await _ingest(knowledge, 11, "priv.txt", ARABIC_BOOK)
    sid = upload["source_id"]
    with pytest.raises(NotFoundError):
        knowledge.source(12, sid)
    assert not knowledge.search(12, "إدارة الوقت").grounded
    with pytest.raises(NotFoundError):
        knowledge.delete_source(12, sid)


# ============================ SEARCH / RETRIEVAL ============================


async def test_search_returns_ranked_evidence(knowledge):
    upload, _ = await _ingest(knowledge, 13, "time2.txt", ARABIC_BOOK)
    hits = knowledge.search(13, "ما هي إدارة الوقت")
    assert hits.grounded
    assert hits.candidates_scanned >= 1
    assert hits.chunks[0].source_id == upload["source_id"]
    assert hits.chunks[0].score >= 0.2


async def test_search_returns_empty_for_unrelated_query(knowledge):
    await _ingest(knowledge, 14, "time3.txt", ARABIC_BOOK)
    hits = knowledge.search(14, "النظرية الكمية الكيميائية ونظرية النسبية")
    assert not hits.grounded
    assert hits.chunks == []


# ============================ COUNTS / DELETE ============================


async def test_counts_and_delete(knowledge):
    upload, _ = await _ingest(knowledge, 15, "c1.txt", ARABIC_BOOK)
    key = knowledge.source(15, upload["source_id"])["storage_key"]
    assert knowledge.counts(15) == {"total": 1, "ready": 1, "processing": 0, "failed": 0}
    assert knowledge.delete_source(15, upload["source_id"]) is True
    assert knowledge.counts(15)["total"] == 0
    assert knowledge._storage.exists(key) is False
    assert not knowledge.search(15, "إدارة الوقت").grounded


# ============================ SETTINGS INFO ============================


def test_settings_info_exposes_engine_limits(knowledge):
    info = knowledge.settings_info()
    assert info["max_file_size_mb"] > 0
    assert info["embedding_model"] == "local-hash"
    assert info["embedding_version"] == "1"
    assert info["dimensions"] == 128
    assert info["top_k"] > 0