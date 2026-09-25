"""
AUSTRO AI - Domain error taxonomy.

Every layer raises one of these typed errors instead of leaking raw
exceptions to the Telegram UI. The presentation layer maps them to safe,
user-facing Arabic messages via `public_message()`.
"""

from __future__ import annotations


class AUSTROError(Exception):
    """Base class for all AUSTRO AI domain errors."""


class ValidationError(AUSTROError):
    """Invalid user input that must be re-asked."""


class AuthorizationError(AUSTROError):
    """The user is not allowed to perform this action."""


class NotFoundError(AUSTROError):
    """The requested resource does not exist."""


class AIProviderError(AUSTROError):
    """The AI provider failed or could not be reached."""


class DatabaseError(AUSTROError):
    """A persistence-layer failure occurred."""


class ExternalServiceError(AUSTROError):
    """An external service outside our control failed."""


class RateLimitError(AUSTROError):
    """A usage/rate limit was exceeded (quota, 429, ...)."""


class ConfigurationError(AUSTROError):
    """Bad or missing configuration at startup."""


# Safe, static user-facing messages. No exception internals are ever shown.
_PUBLIC_MESSAGES = {
    ValidationError: "❌ البيانات المدخلة غير صحيحة.",
    AuthorizationError: "⛔ هذه العملية غير مسموح بها.",
    NotFoundError: "❌ لم يتم العثور على البيانات المطلوبة.",
    AIProviderError: "🤖 تعذر الوصول إلى الذكاء الاصطناعي حالياً. حاول لاحقاً.",
    DatabaseError: "❌ حدث خطأ في قاعدة البيانات. حاول مرة أخرى لاحقاً.",
    ExternalServiceError: "❌ حدث خطأ في خدمة خارجية. حاول لاحقاً.",
    RateLimitError: "⚠️ وصلت إلى الحد المسموح من الاستخدام اليومي. حاول لاحقاً.",
    ConfigurationError: "❌ خطأ في الإعدادات. تأكد من ملف .env ثم أعد التشغيل.",
}

_GENERIC_MESSAGE = "❌ عذراً، حدث خطأ. الرجاء المحاولة مرة أخرى."


def public_message(error: BaseException) -> str:
    """Map a caught exception to a safe, user-facing Arabic message.

    Known AUSTRO errors get their fixed message; ValidationError and
    AIProviderError may carry a specific safe message; anything else gets the
    generic message. Raw exception details are never exposed to the user.
    """
    if isinstance(error, ValidationError) and str(error).strip():
        return str(error)
    if isinstance(error, AIProviderError) and str(error).strip():
        return str(error)
    for error_type, message in _PUBLIC_MESSAGES.items():
        if isinstance(error, error_type):
            return message
    return _GENERIC_MESSAGE