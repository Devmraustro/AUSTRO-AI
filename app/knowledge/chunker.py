"""AUSTRO AI - Structure detection and semantic chunking.

The ingestion pipeline normalizes raw extracted text into *paragraphs*, then
hands them to this module for section detection + overlapping chunking. A
paragraph carries its normalized body, the raw char offset (used to resolve
the page in a PDF) and a resolved page string, so normalization never breaks
page attribution. Chunk keys are sha1 of the aggressive-normalized content,
making re-ingestion of identical text idempotent.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.knowledge.cleaner import TextCleaner

logger = logging.getLogger(__name__)

_CHAPTER_MARKERS = [
    "الفصل", "الباب", "الجزء", "القسم", "المقدمة", "الخاتمة",
    "الفهرس", "تمهيد",
]
_EN_CHAPTER_MARKERS = [
    "chapter", "part", "section", "introduction", "preface", "appendix",
    "conclusion", "contents",
]
_HEADING_NUMBER = re.compile(
    r"^\s*(?:"
    r"\d{1,3}(?:[\.\-:\s]\d{1,3}){0,3}[\.\-\):\s]\s+[A-Za-z\u0600-\u06FF]"
    r"|#+[ \t]+\S"
    r"|[A-Za-z\u0600-\u06FF][A-Za-z0-9\u0600-\u06FF\s\-،]{3,80}"
    r")",
)
_NO_TRAILING_SENTENCE = re.compile(r"(?<![\u06d4.?!؟])\s*$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.?!\u06d4؟])\s+")
_PAGE_NUMBER_ONLY = re.compile(r"^\s*\d{1,4}[همهo ]{0,4}\s*$")


@dataclass
class PreparedParagraph:
    body: str
    raw_start: int = 0
    page: Optional[str] = None


def detect_language(text: str) -> str:
    """Heuristic: Arabic if the Arabic-letter ratio passes a threshold."""
    if not text:
        return "unknown"
    sample = text[:2000]
    arabic = sum(1 for ch in sample if "\u0600" <= ch <= "\u06ff")
    latin = sum(1 for ch in sample if ch.isascii() and ch.isalpha())
    total = arabic + latin
    if total == 0:
        return "unknown"
    return "ar" if arabic / total >= 0.4 else "en"


def split_paragraphs(raw_text: str, cleaner: TextCleaner,
                     page_resolver: Any = None) -> List[PreparedParagraph]:
    """Normalize raw text into paragraphs, resolving pages per paragraph.

    Page resolution uses the RAW char offset of each paragraph via
    `page_resolver(raw_start)`, so normalization that changes text length
    never corrupts page attribution.
    """
    raw = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    parts: List[str] = re.split(r"(\n\s*\n)", raw)
    paragraphs: List[PreparedParagraph] = []
    offset = 0
    for part in parts:
        if part.startswith("\n"):
            offset += len(part)
            continue
        if _PAGE_NUMBER_ONLY.match(part):
            offset += len(part) + 2
            continue
        raw_start = offset
        body = cleaner.normalize(part).strip()
        offset += len(part) + 2
        if not body:
            continue
        page = page_resolver(raw_start) if page_resolver is not None else None
        paragraphs.append(PreparedParagraph(body=body, raw_start=raw_start, page=page))
    return paragraphs


def _looks_like_heading(line: str) -> Optional[int]:
    """Return heading level (1..3) if the line looks like a chapter/section
    title, else None."""
    stripped = line.strip()
    if not stripped or len(stripped) > 90:
        return None
    if _HEADING_NUMBER.match(stripped) is None:
        return None
    if _NO_TRAILING_SENTENCE.search(stripped) is None:
        return None
    stripped_no_hash = re.sub(r"^#+\s*", "", stripped)
    lower = stripped_no_hash.lower()
    for idx, marker in enumerate(_EN_CHAPTER_MARKERS):
        if re.search(rf"\b{marker}\b", lower):
            return min(idx + 1, 3)
    for marker in _CHAPTER_MARKERS:
        if marker in stripped_no_hash:
            return 1
    if re.match(r"^\s*#+", stripped):
        return min(len(stripped) - len(stripped.lstrip("# ")), 3)
    if re.match(r"^\s*\d", stripped_no_hash):
        return 2
    return None


def infer_title(text: str, file_name: Optional[str]) -> str:
    for line in text.split("\n")[:15]:
        if _looks_like_heading(line) is not None:
            return line.strip().lstrip("#").strip()[:120]
    for line in text.split("\n")[:15]:
        candidate = line.strip()
        if 4 <= len(candidate) <= 140:
            return candidate
    if file_name:
        return re.sub(r"\.[A-Za-z0-9]+$", "", file_name)[:120]
    return "كتاب بدون عنوان"


def analyze_structure(normalized_text: str) -> Dict[str, Any]:
    """Detect headings + language from the (already normalized) text."""
    lines: List[Dict[str, Any]] = []
    offset = 0
    for block in normalized_text.split("\n\n"):
        for line in block.split("\n"):
            start = offset
            offset += len(line) + 1
            lines.append({"text": line, "start": start, "end": start + len(line)})
        offset += 1

    sections: List[Dict[str, Any]] = []
    last_levels: Dict[int, int] = {}
    for line in lines:
        level = _looks_like_heading(line["text"])
        if level is None:
            continue
        title = line["text"].strip().lstrip("#").strip()[:160]
        parent = None
        for level_above in range(level - 1, 0, -1):
            if level_above in last_levels:
                parent = last_levels[level_above]
                break
        section_ref = len(sections)
        last_levels[level] = section_ref
        sections.append({
            "level": level,
            "title": title,
            "start_char": line["start"],
            "end_char": line["end"],
            "parent_section_id": parent,
            "order_index": len(sections),
        })
    for index, section in enumerate(sections):
        end = None
        for next_index in range(index + 1, len(sections)):
            if sections[next_index]["level"] <= section["level"]:
                end = sections[next_index]["start_char"]
                break
        section["end_char"] = end
    if not sections:
        sections.append({
            "level": 0,
            "title": "المستند",
            "start_char": 0,
            "end_char": len(normalized_text) if normalized_text else 0,
            "parent_section_id": None,
            "order_index": 0,
        })
    return {"sections": sections, "language": detect_language(normalized_text)}


class SemanticChunker:
    """Overlapping paragraph chunking with section + page attribution."""

    def __init__(self, chunk_size: int = 900, overlap: int = 100,
                 cleaner: Optional[TextCleaner] = None):
        self.chunk_size = max(64, min(int(chunk_size), 2000))
        self.overlap = max(0, min(int(overlap), self.chunk_size // 2))
        self.cleaner = cleaner or TextCleaner()

    def split(self, paragraphs: List[PreparedParagraph],
              normalized_text: str) -> Dict[str, Any]:
        """Return {chunks: [...], structure: [...], language: str}."""
        structure = analyze_structure(normalized_text)
        sections = self._index_sections(structure["sections"])
        chunks: List[Dict[str, Any]] = []

        buffer = ""
        buffer_start = 0
        buffer_page: Optional[str] = None
        buffer_paras = 0

        def flush() -> None:
            nonlocal buffer
            content = buffer.strip()
            if not content:
                buffer = ""
                return
            section = self._section_at(sections, buffer_start)
            chunks.append({
                "content": content,
                "chunk_key": hashlib.sha1(
                    self.cleaner.transform(content).encode("utf-8")
                ).hexdigest(),
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "char_count": len(content),
                "token_count": len(self.cleaner.tokens(content)),
                "page": buffer_page,
                "start_char": buffer_start,
                "section_id": section.ref,
                "section_title": section.title,
                "paragraph_count": buffer_paras,
            })
            buffer = ""

        def push(paragraph: PreparedParagraph, start: int, body: str) -> None:
            nonlocal buffer, buffer_start, buffer_page, buffer_paras
            if not body.strip():
                return
            if buffer and len(buffer) + 1 + len(body) > self.chunk_size:
                flush()
                tail = self._tail(buffer)
                if tail:
                    buffer = tail
                    buffer_paras = 0
                    buffer_start = start
                    if buffer_page is None:
                        buffer_page = paragraph.page
                else:
                    buffer = ""
            if not buffer:
                buffer_start = start
                buffer_page = paragraph.page
                buffer_paras = 0
            if buffer:
                buffer += "\n\n"
            buffer += body
            buffer_paras += 1
            if len(buffer) >= self.chunk_size:
                flush()

        norm_offset = 0
        for paragraph in paragraphs:
            body = paragraph.body
            start = norm_offset
            norm_offset += len(body) + 2
            if not body.strip():
                continue
            if len(body) > self.chunk_size and not buffer:
                for piece in self._split_long(body):
                    push(paragraph, start, piece)
            else:
                push(paragraph, start, body)

        if buffer.strip():
            flush()

        for index, chunk in enumerate(chunks):
            chunk["order_index"] = index
        return {
            "chunks": chunks,
            "structure": structure["sections"],
            "language": structure["language"],
        }

    def _tail(self, content: str) -> str:
        if self.overlap <= 0 or len(content) <= self.overlap:
            return ""
        tail = content[-self.overlap:]
        space = re.search(r"\s", tail)
        if space:
            return tail[space.start():].lstrip()
        return tail.lstrip()

    def _split_long(self, body: str) -> List[str]:
        pieces = [p.strip() for p in _SENTENCE_SPLIT.split(body) if p.strip()]
        if len(pieces) <= 1:
            pieces = [body[i:i + self.chunk_size]
                      for i in range(0, len(body), self.chunk_size)]
        chunks: List[str] = []
        current = ""
        for piece in pieces:
            if current and len(current) + 1 + len(piece) > self.chunk_size:
                chunks.append(current.strip())
                tail = self._tail(current)
                current = (tail + "\n\n" if tail else "") + piece
            else:
                current = (current + "\n\n" + piece) if current else piece
        if current.strip():
            chunks.append(current.strip())
        return [c for c in chunks if c]

    def _index_sections(self, sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        refs: List[Dict[str, Any]] = []
        ref_by_index: Dict[int, Any] = {}
        for index, section in enumerate(sections):
            parent_idx = section.get("parent_section_id")
            parent_ref = ref_by_index.get(parent_idx) if parent_idx is not None else None
            refs.append({
                "ref": len(refs),
                "title": section["title"],
                "start": section["start_char"],
                "end": section["end_char"],
                "parent_section_ref": parent_ref,
            })
            ref_by_index[index] = refs[-1]["ref"]
        return refs

    @staticmethod
    def _section_at(sections: List[Dict[str, Any]], char_index: int) -> Any:
        class _SectionInfo:
            def __init__(self, ref, title):
                self.ref = ref
                self.title = title

        best = None
        for section in sections:
            end = section["end"]
            if section["start"] <= char_index < (end if end is not None else 10 ** 12):
                best = section
                break
        if best is None and sections:
            best = sections[0]
        if best is None:
            return _SectionInfo(None, "")
        return _SectionInfo(best["parent_section_ref"], best["title"])


__all__ = [
    "PreparedParagraph",
    "SemanticChunker",
    "analyze_structure",
    "detect_language",
    "infer_title",
    "split_paragraphs",
]