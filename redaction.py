"""
AUSTRO AI - Redaction shim.

Phase B: redaction lives in `app.security.redaction`. This module re-exports
`redact` and `RedactingFormatter` so old imports keep working unchanged.
"""

from app.security.redaction import RedactingFormatter, redact  # noqa: F401

__all__ = ["redact", "RedactingFormatter"]