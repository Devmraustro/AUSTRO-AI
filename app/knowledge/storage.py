"""AUSTRO AI - On-disk storage service for uploaded knowledge files.

Files live under the configured storage root; the database only keeps a
relative `storage_key`, the checksum and size. The service re-checksums on
read so tampered files are detected before extraction.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path
from typing import Optional

from app.core.errors import ValidationError
from app.knowledge.repositories import KnowledgeStore

logger = logging.getLogger(__name__)


class StorageService:
    """Store, locate and verify raw uploaded files on disk."""

    def __init__(self, storage_root: str, store: KnowledgeStore):
        self.root = Path(storage_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._store = store

    def _resolve(self, storage_key: str) -> Path:
        # Guard against path traversal: keys are always relative and single-level.
        base = Path(storage_key)
        if base.is_absolute() or ".." in base.parts:
            raise ValidationError("مسار ملف غير صالح")
        return self.root / base

    def save(self, *, owner_user_id: int, file_name: str, data: bytes,
             file_format: str, checksum: Optional[str] = None) -> "tuple[str, str]":
        """Write raw bytes to disk. Returns (storage_key, sha256 checksum)."""
        checksum = checksum or hashlib.sha256(data).hexdigest()
        storage_key = f"{owner_user_id}_{uuid.uuid4().hex}.{file_format}"
        target = self._resolve(storage_key)
        try:
            target.write_bytes(data)
        except OSError as e:
            logger.error(f"Storage write failed for {storage_key}: {e}")
            raise ValidationError("تعذر حفظ الملف على القرص") from e
        return storage_key, checksum

    def read(self, storage_key: str, expected_checksum: Optional[str] = None) -> bytes:
        """Read raw bytes, verifying the checksum when one is expected."""
        path = self._resolve(storage_key)
        if not path.exists():
            raise ValidationError("الملف غير موجود في التخزين")
        data = path.read_bytes()
        if expected_checksum:
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected_checksum:
                logger.error(
                    f"Storage checksum mismatch for {storage_key}: "
                    f"expected {expected_checksum}, got {actual}"
                )
                raise ValidationError("الملف يبدو معطوباً أو معدلاً")
        return data

    def delete(self, storage_key: Optional[str]) -> None:
        if not storage_key:
            return
        try:
            path = self._resolve(storage_key)
            if path.exists():
                path.unlink()
        except (OSError, ValidationError) as e:
            logger.error(f"Storage delete failed for {storage_key}: {e}")

    def exists(self, storage_key: str) -> bool:
        try:
            return self._resolve(storage_key).exists()
        except ValidationError:
            return False

    def register_file(self, *, owner_user_id: int, source_id: int, storage_key: str,
                      file_name: str, file_format: str, size_bytes: int,
                      checksum: str) -> bool:
        return self._store.storage.register(
            owner_user_id=owner_user_id,
            source_id=source_id,
            storage_key=storage_key,
            file_name=file_name,
            file_format=file_format,
            size_bytes=size_bytes,
            checksum=checksum,
        )


__all__ = ["StorageService"]