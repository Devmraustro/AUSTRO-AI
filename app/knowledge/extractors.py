"""AUSTRO AI - Book text extraction.

Pure-infrastructure adapters that turn raw file bytes into normalized text
plus page offsets. Only `pypdf` is a runtime dependency; DOCX/EPUB are read
with the standard library (zipfile + xml.etree). Extraction never executes
content and never parses instructions - text is DATA.

Supported: pdf, txt, md, docx, epub.
"""

from __future__ import annotations

import logging
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Set

from app.core.errors import ValidationError
from app.knowledge.models import ExtractedBook

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS: Set[str] = {"pdf", "txt", "md", "docx", "epub"}

_MIME_BY_FORMAT = {
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/markdown",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "epub": "application/epub+zip",
}

EXTENSION_ALIASES = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/x-markdown": "md",
    "application/xml": "txt",
    "markdown": "md",
    "doc": "docx",
}

_W: str = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def detect_format(file_name: Optional[str], mime_type: Optional[str]) -> str:
    """Resolve a canonical format from file extension and/or MIME type."""
    if file_name:
        lower = file_name.lower()
        for ext in SUPPORTED_FORMATS:
            if lower.endswith(f".{ext}"):
                if ext == "doc" and mime_type and mime_type.startswith("application/octet"):
                    return "docx"
                return ext
    if mime_type:
        normalized = mime_type.split(";")[0].strip().lower()
        alias = EXTENSION_ALIASES.get(normalized)
        if alias is not None:
            return alias
        if normalized in _MIME_BY_FORMAT.values():
            for fmt, mime in _MIME_BY_FORMAT.items():
                if mime == normalized:
                    return fmt
    raise ValidationError(
        "❌ صيغة الملف غير مدعومة. الصيغ المقبولة: PDF, TXT, MD, DOCX, EPUB."
    )


def mime_for(format_name: str) -> str:
    return _MIME_BY_FORMAT.get(format_name, "application/octet-stream")


class _PdfExtractor:
    def extract(self, data: bytes) -> ExtractedBook:
        try:
            import pypdf
        except ImportError as e:  # pragma: no cover - guard for minimal envs
            raise ValidationError("مستخرج PDF غير متاح") from e

        try:
            reader = pypdf.PdfReader(__import__("io").BytesIO(data))
        except Exception as e:  # noqa: BLE001 - bad PDFs throw arbitrary exceptions
            logger.warning(f"PDF could not be opened: {e}")
            raise ValidationError("ملف PDF تالف أو غير قابل للقراءة") from e

        pages = []
        offset = 0
        text_parts = []
        for index, page in enumerate(reader.pages, start=1):
            page_text = ""
            try:
                page_text = page.extract_text() or ""
            except Exception as e:  # noqa: BLE001 - degrade gracefully per page
                logger.warning(f"PDF page {index} extraction failed: {e}")
            start = offset
            if page_text.strip():
                text_parts.append(page_text.strip())
                offset += len(page_text.strip()) + 1
            pages.append({
                "number": index,
                "start": start,
                "end": offset - 1 if text_parts else start,
                "has_text": bool(page_text.strip()),
            })

        text = "\n".join(text_parts) if text_parts else ""
        if not text.strip():
            raise ValidationError("لم نتمكن من استخراج نص من هذا الـ PDF")
        return ExtractedBook(
            text=text,
            pages=pages,
            metadata={"page_count": len(pages)},
        )


class _DocxExtractor:
    def extract(self, data: bytes) -> ExtractedBook:
        try:
            with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
                xml_bytes = archive.read("word/document.xml")
        except (zipfile.BadZipFile, KeyError) as e:
            raise ValidationError("ملف DOCX تالف") from e

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            raise ValidationError("ملف DOCX تالف") from e

        paragraphs: List[str] = []
        for para in root.iter(f"{_W}p"):
            text = "".join(
                node.text or ""
                for node in para.iter(f"{_W}t")
            ).strip()
            if text:
                paragraphs.append(text)

        text = "\n\n".join(paragraphs)
        if not text.strip():
            raise ValidationError("لم نتمكن من استخراج نص من هذا الملف")
        return ExtractedBook(
            text=text,
            pages=[],
            metadata={"page_count": 0},
        )


class _TxtExtractor:
    def __init__(self, markdown: bool = False):
        self._markdown = markdown

    def extract(self, data: bytes) -> ExtractedBook:
        for encoding in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
            try:
                text = data.decode(encoding)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        else:
            text = data.decode("utf-8", errors="replace")

        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            raise ValidationError("الملف النصي فارغ")
        return ExtractedBook(
            text=text,
            pages=[],
            metadata={"markdown": self._markdown, "page_count": 0},
        )


class _EpubExtractor:
    def extract(self, data: bytes) -> ExtractedBook:
        try:
            archive = zipfile.ZipFile(__import__("io").BytesIO(data))
        except zipfile.BadZipFile as e:
            raise ValidationError("ملف EPUB تالف") from e

        try:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
        except (KeyError, ET.ParseError) as e:
            raise ValidationError("ملف EPUB تالف") from e

        rootfile = None
        for element in container.iter():
            if element.tag.endswith("rootfile"):
                rootfile = element.get("full-path")
                break
        if not rootfile:
            raise ValidationError("ملف EPUB تالف (لا يوجد rootfile)")

        try:
            with archive.open(rootfile) as handle:
                spine_root = ET.fromstring(handle.read())
        except (KeyError, ET.ParseError) as e:
            raise ValidationError("ملف EPUB تالف") from e

        manifest: Dict[str, str] = {}
        for element in spine_root.iter():
            if element.tag.endswith("item"):
                manifest[element.get("id")] = element.get("href") or ""
        spine_refs = []
        for element in spine_root.iter():
            if element.tag.endswith("itemref"):
                idref = element.get("idref")
                href = manifest.get(idref or "")
                if href:
                    spine_refs.append(href)

        parts: List[str] = []
        for href in spine_refs:
            href = href.split("#")[0]
            member = href if href in archive.namelist() else self._resolve(archive, rootfile, href)
            if member not in archive.namelist():
                continue
            try:
                with archive.open(member) as handle:
                    dom = ET.fromstring(handle.read())
            except (KeyError, ET.ParseError):
                continue
            parts.append(self._render(dom))

        text = "\n\n".join(p.strip() for p in parts if p.strip()).strip()
        if not text:
            raise ValidationError("لم نتمكن من استخراج نص من هذا الـ EPUB")
        return ExtractedBook(text=text, pages=[], metadata={"page_count": 0})

    def _resolve(self, archive: zipfile.ZipFile, rootfile: str, href: str) -> str:
        base = rootfile.rsplit("/", 1)[0] if "/" in rootfile else ""
        return f"{base}/{href}" if base else href

    def _render(self, dom: ET.Element) -> str:
        for element in dom.iter():
            tag = element.tag.rsplit("}", 1)[-1].lower()
            if tag in ("script", "style"):
                if element.text:
                    element.text = ""
        paragraphs: List[str] = []
        for element in dom.iter():
            tag = element.tag.rsplit("}", 1)[-1].lower()
            if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"):
                line = "".join(element.itertext()).strip()
                if line:
                    paragraphs.append(line)
        return "\n".join(paragraphs)


class TextExtractor:
    """Registry dispatching by canonical format."""

    _EXTRACTORS: Dict[str, object] = {
        "pdf": _PdfExtractor(),
        "docx": _DocxExtractor(),
        "txt": _TxtExtractor(markdown=False),
        "md": _TxtExtractor(markdown=True),
        "epub": _EpubExtractor(),
    }

    def extract(self, data: bytes, format_name: str) -> ExtractedBook:
        extractor = self._EXTRACTORS.get(format_name)
        if extractor is None:
            raise ValidationError("❌ صيغة الملف غير مدعومة")
        return extractor.extract(data)  # type: ignore[attr-defined]


__all__ = [
    "SUPPORTED_FORMATS",
    "TextExtractor",
    "detect_format",
    "mime_for",
]