"""AUSTRO AI - safe message rendering for the Telegram presentation layer.

Why this module exists
======================
Every AI answer, document title, goal name and plan was rendered straight into
``reply_text(..., parse_mode="Markdown")``. Telegram's legacy *Markdown* parser
is strict: a single unbalanced ``*``, ``_``, ``[`` or a backtick inside the
generated text makes the whole API call fail with ``BadRequest: Can't parse
entities``. The handler then raised, the global error handler answered
"❌ عذراً، حدث خطأ", and the user lost the *entire* answer because of one
character in it. That is one of the two ways the bot looked broken to users
(the other was the ``curricula`` shadowing bug in the learning engine).

What this module guarantees
===========================
* **Text is never lost.** If Telegram rejects the formatted message, the same
  text is sent again as plain text (formatting removed), and only if *that*
  fails does the caller see an error.
* **Bad markup is pre-cleaned.** ``normalize_markup`` closes/reduces unbalanced
  emphasis markers and turns Telegram-invalid Markdown into something the
  parser accepts.
* **Long answers are chunked.** Telegram rejects anything above 4096
  characters; long lessons/AI answers used to be an error. ``chunk_text``
  splits on paragraph/line/word boundaries so continuation messages are only
  used when they are really needed.
* **HTML is available for new copy.** ``html.escape`` based helpers let new
  handlers use ``parse_mode="HTML"``, which is far more forgiving than
  Markdown and does not collide with ``_``/``*`` inside Arabic names.

The module is deliberately free of Telegram objects so it can be unit tested
without a bot; ``reply``/``edit`` accept any object exposing the matching
python-telegram-bot coroutine.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Telegram's hard limit for a single message body.
MAX_MESSAGE_LENGTH = 4096
#: Keep a little headroom for the continuation marker we may append.
_SAFE_CHUNK = MAX_MESSAGE_LENGTH - 64

#: Marker appended to every chunk but the last.
CONTINUATION_MARKER = "\n…"


def chunk_text(text: str, limit: int = _SAFE_CHUNK) -> List[str]:
    """Split ``text`` into Telegram-sized chunks on natural boundaries.

    Empty input yields no chunks (callers should not send an empty message).
    A hard split is used only when a single line is longer than ``limit``.
    """
    body = text or ""
    if not body.strip():
        return []
    if len(body) <= limit:
        return [body]

    chunks: List[str] = []
    remaining = body
    while len(remaining) > limit:
        window = remaining[:limit]
        # Prefer a paragraph break, then a line break, then a sentence end,
        # then any space, and only then a hard cut.
        cut = -1
        for separator in ("\n\n", "\n", ". ", "، ", " "):
            candidate = window.rfind(separator)
            if candidate > limit // 2:
                cut = candidate + len(separator)
                break
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk]


def strip_markdown(text: str) -> str:
    """Remove the Markdown characters that make Telegram's parser fail.

    Used as the plain-text fallback, so it must never raise and must keep the
    readable content (including Arabic text) intact.
    """
    cleaned = text or ""
    # Escapes become the literal character they escaped - this has to happen
    # BEFORE the marker stripping below, or "\_" would lose the "_" and keep
    # the orphaned backslash.
    cleaned = re.sub(r"\\([\\_*`\[\]()~>#+\-.!])", r"\1", cleaned)
    # Code fences and inline code keep their content, lose the markers.
    cleaned = re.sub(r"```[a-zA-Z0-9_+-]*\n?", "", cleaned)
    cleaned = cleaned.replace("`", "")
    # Emphasis / strike-through markers.
    cleaned = re.sub(r"(\*\*|__|~~|\*|_)", "", cleaned)
    # Links: [label](url) -> label (url)
    cleaned = re.sub(r"\[([^\]]*)\]\(([^)\s]*)\)", r"\1 (\2)", cleaned)
    # Telegram Markdown v1 has no block-level syntax; leading markers would
    # otherwise show up as literal characters.
    cleaned = re.sub(r"(?m)^#{1,6}\s*", "", cleaned)
    cleaned = re.sub(r"(?m)^>\s?", "", cleaned)
    return cleaned


def _unescaped_count(text: str, marker: str) -> int:
    """Count ``marker`` occurrences that are neither escaped nor inside code.

    Telegram's legacy Markdown supports ``\\*``/``\\_``/``\\```` escapes, so an
    escaped marker is *valid* markup and must never be stripped - doing that
    turned ``@Mr\\_Austro`` into ``@Mr\\Austro`` and broke the rendered name.
    """
    count = 0
    index = 0
    in_code = False
    while index < len(text):
        char = text[index]
        if text.startswith("```", index):
            in_code = not in_code
            index += 3
            continue
        if char == "`":
            in_code = not in_code
            index += 1
            continue
        if char == "\\" and index + 1 < len(text):
            index += 2  # the escaped character is literal content
            continue
        if not in_code and text.startswith(marker, index):
            count += 1
            index += len(marker)
            continue
        index += 1
    return count


def _balanced(text: str, marker: str) -> bool:
    """True when ``marker`` occurs an even number of unescaped times."""
    return _unescaped_count(text, marker) % 2 == 0


def normalize_markup(text: str) -> str:
    """Return ``text`` with Telegram-Markdown-breaking input neutralised.

    The legacy parser has no escaping for plain text, so the only safe repair
    is to drop stray emphasis/code markers. This keeps the message sendable
    *with* formatting for the parts that are valid, instead of falling back to
    plain text for the entire answer.
    """
    cleaned = text or ""
    for marker in ("*", "_", "`", "~~"):
        while not _balanced(cleaned, marker):
            # Drop the last unmatched occurrence, which is the one the parser
            # would choke on; keep everything before it intact.
            index = cleaned.rfind(marker)
            if index < 0:
                break
            cleaned = cleaned[:index] + cleaned[index + len(marker):]
    return cleaned


def _is_parse_error(error: BaseException) -> bool:
    """True when Telegram rejected the *formatting* of a message.

    ``telegram.error.BadRequest`` is imported lazily so this module keeps
    working in contexts where python-telegram-bot is not installed (tests,
    scripts) and so a future PTB rename cannot break imports.
    """
    message = str(error).lower()
    if "parse" in message and ("entit" in message or "markdown" in message):
        return True
    try:  # pragma: no cover - trivial import guard
        from telegram.error import BadRequest  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False
    return isinstance(error, BadRequest) and "message" in message


async def _send(
    target: Any,
    text: str,
    *,
    method: str,
    reply_markup: Any = None,
    parse_mode: Optional[str] = None,
    attempts: Sequence[Optional[str]] = (),
) -> Any:
    """Call ``target.<method>`` with progressive formatting fallback."""
    call = getattr(target, method, None)
    if call is None:  # pragma: no cover - defensive
        raise AttributeError(f"{type(target).__name__} has no {method!r}")

    chunks = chunk_text(text)
    if not chunks:
        return None

    # A callback query can edit exactly one existing Telegram message.  For
    # oversized edits, replace that message with the first chunk and send the
    # remaining chunks as new messages in the same chat.  Keeping the keyboard
    # on the edited (primary) message preserves the controls users are acting
    # on; continuation messages are content-only.
    if method == "edit_message_text" and len(chunks) > 1:
        first_body = chunks[0] + CONTINUATION_MARKER
        try:
            sent = await _send_one(
                call, first_body,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                attempts=attempts,
            )
        except Exception as error:  # noqa: BLE001
            # The first chunk may be identical while later chunks changed.
            # Telegram's "Message is not modified" applies only to that edit;
            # it must not suppress delivery of the remaining content.
            if "not modified" not in str(error).lower():
                raise
            logger.debug("Skipped identical primary chunk edit")
            sent = None
        message = getattr(target, "message", None)
        continuation_call = getattr(message, "reply_text", None)
        if continuation_call is None:
            raise RuntimeError(
                "Cannot deliver a long edited message: callback has no "
                "message.reply_text continuation target"
            )
        for index, chunk in enumerate(chunks[1:], start=1):
            is_last = index == len(chunks) - 1
            body = chunk if is_last else chunk + CONTINUATION_MARKER
            sent = await _send_one(
                continuation_call, body,
                reply_markup=None,
                parse_mode=parse_mode,
                attempts=attempts,
            )
        return sent

    sent = None
    for index, chunk in enumerate(chunks):
        is_last = index == len(chunks) - 1
        body = chunk if is_last else chunk + CONTINUATION_MARKER
        keyboard = reply_markup if is_last else None
        sent = await _send_one(
            call, body,
            reply_markup=keyboard,
            parse_mode=parse_mode,
            attempts=attempts,
        )
    return sent


async def _send_one(
    call: Any,
    body: str,
    *,
    reply_markup: Any,
    parse_mode: Optional[str],
    attempts: Sequence[Optional[str]],
) -> Any:
    """Send ONE chunk, degrading the markup until Telegram accepts it."""
    plan: List[Optional[str]] = []
    for mode in (parse_mode, *attempts):
        if mode not in plan:
            plan.append(mode)
    if None not in plan:
        plan.append(None)  # plain text is always the last resort

    last_error: Optional[BaseException] = None
    for mode in plan:
        payload = body
        kwargs = {}
        if mode is not None:
            kwargs["parse_mode"] = mode
            payload = normalize_markup(body) if mode.lower().startswith("markdown") else body
        else:
            payload = strip_markdown(body)
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        try:
            return await call(payload, **kwargs)
        except Exception as error:  # noqa: BLE001 - retried without markup
            if not _is_parse_error(error):
                raise
            logger.warning(
                "Telegram rejected %s markup, retrying without it: %s",
                mode, type(error).__name__,
            )
            last_error = error
    if last_error is not None:  # pragma: no cover - defensive
        raise last_error
    return None


async def reply(
    message: Any,
    text: str,
    *,
    reply_markup: Any = None,
    parse_mode: Optional[str] = None,
) -> Any:
    """Send ``text`` as a new message, never losing it to a parsing error.

    ``parse_mode`` defaults to ``None``: call sites opt in to formatting
    explicitly, so plain-text notices keep working exactly as before.
    """
    return await _send(
        message, text, method="reply_text",
        reply_markup=reply_markup, parse_mode=parse_mode,
    )


async def edit(
    query: Any,
    text: str,
    *,
    reply_markup: Any = None,
    parse_mode: Optional[str] = None,
) -> Any:
    """Edit the message a callback query points at, with the same guarantees.

    When the *only* change would be the markup (identical text), Telegram
    answers ``BadRequest: Message is not modified``. That is a harmless no-op
    for the user, so it is swallowed here instead of surfacing as an error.
    """
    try:
        return await _send(
            query, text, method="edit_message_text",
            reply_markup=reply_markup, parse_mode=parse_mode,
        )
    except Exception as error:  # noqa: BLE001
        if "not modified" in str(error).lower():
            logger.debug("Skipped identical message edit")
            return None
        # The message may be too old to edit (or already replaced); answer in
        # a fresh message so the user still gets the content.
        message = getattr(query, "message", None)
        if message is not None and hasattr(message, "reply_text"):
            logger.warning(
                "edit_message_text failed (%s); sending a new message",
                type(error).__name__,
            )
            return await reply(
                message, text,
                reply_markup=reply_markup, parse_mode=parse_mode,
            )
        raise


def html_escape(text: str) -> str:
    """Escape text for ``parse_mode="HTML"``.

    New copy should prefer HTML: unlike Markdown it has a real escape hatch, so
    user/AI text can be embedded safely instead of being stripped.
    """
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def bullet_lines(items: Iterable[str], marker: str = "•") -> str:
    """Render an iterable of strings as a bullet list (Markdown-safe)."""
    return "\n".join(f"{marker} {item}" for item in items)


__all__ = [
    "MAX_MESSAGE_LENGTH",
    "CONTINUATION_MARKER",
    "chunk_text",
    "strip_markdown",
    "normalize_markup",
    "reply",
    "edit",
    "html_escape",
    "bullet_lines",
]
