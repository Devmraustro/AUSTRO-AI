"""AUSTRO AI - Book text extraction.

Pure-infrastructure adapters that turn raw file bytes into normalized text
plus page offsets. Only `pypdf` is a runtime dependency; DOCX/EPUB are read
with the standard library (zipfile + xml.etree). Extraction never executes
content and never parses instructions - text is DATA.

Supported: pdf, txt, md, docx, epub.
"""

from __future__ import annotations

import io
import logging
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Set

# Untrusted EPUB container XML and book content are parsed with defusedxml so
# entity-expansion / external-reference attacks cannot read files or exhaust
# memory. Manual element construction (jar_path, _render) stays on stdlib.
from defusedxml import ElementTree as _SafeElementTree

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

# --- ZIP bomb / decompression-bomb guards ---------------------------------
#
# `knowledge_max_file_size_mb` bounds the *compressed upload*, so it does not
# bound what DOCX/EPUB extraction inflates. deflate can compress a run of
# identical bytes ~1000:1, so the audit reproduced a 0.195 MiB upload that
# expanded `word/document.xml` to 200 MiB and cost 656 MiB of peak heap before
# `ingestion.py` finally compared the resulting text against
# `knowledge_max_chars` and rejected it.
#
# These guards read only the ZIP central directory (no member is decompressed)
# so they are cheap, and they reject the upload rather than paying for it.
_MAX_UNCOMPRESSED_BYTES: int = 200 * 1024 * 1024
_MAX_ZIP_MEMBERS: int = 2000
_MAX_ZIP_RATIO: int = 200


def _check_archive_bomb(archive: zipfile.ZipFile) -> None:
    """Reject a ZIP that decompresses to an unreasonable size.

    Raises ``ValidationError`` based on the central directory alone, so no
    member is decompressed before the decision is made.
    """
    max_bytes, max_members, max_ratio = _resolve_limits()

    try:
        infos = archive.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValidationError("ملف مضغوط تالف") from exc

    if len(infos) > max_members:
        logger.warning(f"Rejecting archive with {len(infos)} members (max {max_members})")
        raise ValidationError("الملف يحتوي على عدد عناصر كبير جدًا")

    total_uncompressed = 0
    for info in infos:
        total_uncompressed += info.file_size
        if total_uncompressed > max_bytes:
            logger.warning(
                f"Rejecting archive: declared uncompressed size "
                f"{total_uncompressed / 1024 / 1024:.1f} MiB "
                f"exceeds {max_bytes / 1024 / 1024:.0f} MiB"
            )
            raise ValidationError("الملف كبير جدًا بعد فك الضغط")
        # A huge compression ratio on a single member is the classic zip-bomb
        # signature. Guard it too, so a many-member bomb cannot slip past the
        # aggregate check by spreading bytes across small members.
        if info.file_size > max_bytes:
            raise ValidationError("الملف كبير جدًا بعد فك الضغط")
        if info.compress_size > 0 and info.file_size // info.compress_size > max_ratio:
            logger.warning(
                f"Rejecting archive member {info.filename!r}: ratio "
                f"{info.file_size // info.compress_size} exceeds {max_ratio}"
            )
            raise ValidationError("نسبة ضغط الملف غير طبيعية")


def _read_zip_member(archive: zipfile.ZipFile, name: str, *, limit: int) -> bytes:
    """Read one ZIP member, aborting as soon as `limit` bytes are exceeded.

    ``limit`` is enforced *during* decompression, so a member that lies about
    its central-directory size is still cut off.
    """
    chunks: List[bytes] = []
    total = 0
    try:
        with archive.open(name) as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    logger.warning(
                        f"Rejecting ZIP member {name!r}: exceeded {limit} bytes"
                    )
                    raise ValidationError("الملف كبير جدًا بعد فك الضغط")
                chunks.append(chunk)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValidationError("ملف مضغوط تالف") from exc
    return b"".join(chunks)


def _pdf_limits() -> tuple:
    """Return the configured (max_pages, max_chars) for PDF extraction."""
    try:
        from app.config.settings import settings
        return settings.knowledge_max_pages, settings.knowledge_max_chars
    except Exception:  # noqa: BLE001 - never fail extraction over config lookup
        return 1000, 2_000_000


def _open_checked_zip(data: bytes) -> zipfile.ZipFile:
    """Open a ZIP archive after validating it against the bomb limits."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValidationError("ملف مضغوط تالف") from exc
    _check_archive_bomb(archive)
    return archive


def _resolve_limits() -> tuple:
    """Read the configurable ZIP-bomb limits, falling back to module defaults."""
    try:
        from app.config.settings import settings
        return (
            settings.knowledge_max_uncompressed_mb * 1024 * 1024,
            settings.knowledge_max_zip_members,
            settings.knowledge_max_zip_ratio,
        )
    except Exception:  # noqa: BLE001 - never fail extraction over config lookup
        return (
            _MAX_UNCOMPRESSED_BYTES,
            _MAX_ZIP_MEMBERS,
            _MAX_ZIP_RATIO,
        )


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
            reader = pypdf.PdfReader(io.BytesIO(data))
        except Exception as e:  # noqa: BLE001 - bad PDFs throw arbitrary exceptions
            logger.warning(f"PDF could not be opened: {e}")
            raise ValidationError("ملف PDF تالف أو غير قابل للقراءة") from e

        # `knowledge_max_pages` used to be enforced *after* every page had been
        # parsed, so a 100k-page PDF paid full extraction cost (and full memory)
        # before being rejected. Read `len(reader.pages)` from the page tree,
        # which is already parsed, and stop before extracting anything.
        max_pages, max_chars = _pdf_limits()
        declared_pages = len(reader.pages)
        if declared_pages > max_pages:
            logger.warning(
                f"Rejecting PDF with {declared_pages} pages (max {max_pages})"
            )
            raise ValidationError("عدد صفحات الملف يتجاوز الحد المسموح")
        if declared_pages == 0:
            raise ValidationError("لم نتمكن من استخراج نص من هذا الـ PDF")

        pages = []
        offset = 0
        extracted_chars = 0
        text_parts = []
        for index, page in enumerate(reader.pages, start=1):
            page_text = ""
            try:
                page_text = page.extract_text() or ""
            except Exception as e:  # noqa: BLE001 - degrade gracefully per page
                logger.warning(f"PDF page {index} extraction failed: {e}")
            start = offset
            if page_text.strip():
                stripped = page_text.strip()
                text_parts.append(stripped)
                extracted_chars += len(stripped) + 1
                offset += len(stripped) + 1
                if extracted_chars > max_chars:
                    logger.warning(
                        f"Rejecting PDF: text exceeded {max_chars} chars at page {index}"
                    )
                    raise ValidationError("محتوى الملف كبير جدًا")
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
        max_bytes, _members, _ratio = _resolve_limits()
        with _open_checked_zip(data) as archive:
            try:
                xml_bytes = _read_zip_member(
                    archive, "word/document.xml", limit=max_bytes
                )
            except KeyError as e:
                raise ValidationError("ملف DOCX تالف") from e

        try:
            root = _SafeElementTree.fromstring(xml_bytes)
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
        max_bytes, _members, _ratio = _resolve_limits()
        # `with` so the ZipFile (and its file object) is released on every path,
        # including the ValidationError rejections below.
        with _open_checked_zip(data) as archive:
            return self._extract_from(archive, max_bytes)

    def _extract_from(self, archive: zipfile.ZipFile, max_bytes: int) -> ExtractedBook:
        try:
            try:
                container_xml = _read_zip_member(
                    archive, "META-INF/container.xml", limit=max_bytes
                )
            except KeyError as e:
                raise ValidationError("ملف EPUB تالف") from e
            container = _SafeElementTree.fromstring(container_xml)
        except ET.ParseError as e:
            raise ValidationError("ملف EPUB تالف") from e

        rootfile = None
        for element in container.iter():
            if element.tag.endswith("rootfile"):
                rootfile = element.get("full-path")
                break
        if not rootfile:
            raise ValidationError("ملف EPUB تالف (لا يوجد rootfile)")

        try:
            spine_root = _SafeElementTree.fromstring(_read_zip_member(archive, rootfile, limit=max_bytes))
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

        # Each chapter may legitimately decompress to a few MB; the aggregate
        # per-member budget stops a single oversized chapter and the cumulative
        # total keeps a many-chapter EPUB bounded.
        per_member_limit = max(1, max_bytes // 8)
        total_limit = max_bytes
        spent = 0

        parts: List[str] = []
        names = set(archive.namelist())
        for href in spine_refs:
            href = href.split("#")[0]
            member = href if href in names else self._resolve(archive, rootfile, href)
            if member not in names:
                continue
            try:
                raw = _read_zip_member(
                    archive,
                    member,
                    limit=min(per_member_limit, total_limit - spent),
                )
            except ValidationError as e:
                logger.warning(f"EPUB member {member!r} rejected: {e}")
                break
            spent += len(raw)
            try:
                dom = _SafeElementTree.fromstring(raw)
            except ET.ParseError:
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