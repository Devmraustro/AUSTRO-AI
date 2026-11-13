"""User-facing privacy commands: /export, /delete and knowledge source deletion.

WHY THIS MODULE EXISTS
=====================
`PRIVACY_RETENTION.md` §3.1 recorded the gap in plain words: the `/export`,
`/forget` and `/delete` commands were "not implemented and not registered", so a
user could neither obtain nor erase the data the bot stores about them, and
uploaded knowledge sources could not be deleted from the bot at all.

Design decisions that matter:

1. **Deletion needs a second tap.** `/delete` only *offers* erasure; the
   destruction happens on a callback carrying a one-time token that the user
   themselves triggered. A slash command can be typed by accident or replayed by
   a chat client; an explicit confirmation cannot.
2. **The confirmation expires.** The token is stored in ``context.user_data``
   with a deadline, so a stale button from yesterday cannot delete an account
   that has been used since.
3. **Words are never stronger than the facts.** If the database transaction
   fails, nothing was deleted and the reply says so. If it succeeded, the reply
   reports only what ``DeletionResult.verified`` proved.
4. **Export is sent as a file.** The snapshot is JSON and can be large; it is
   attached as a document instead of being truncated into a chat message.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.telegram import rendering as render
from app.telegram.handlers import get_services

logger = logging.getLogger(__name__)

#: How long a pending confirmation stays valid.
CONFIRM_TTL_MINUTES = 10
#: key in ``context.user_data`` holding the pending deletion request.
_PENDING_KEY = "privacy_pending_delete"


def _pending(context: ContextTypes.DEFAULT_TYPE) -> Optional[Dict[str, Any]]:
    return context.user_data.get(_PENDING_KEY)


def _clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_PENDING_KEY, None)


def _format_summary(counts: Dict[str, int]) -> str:
    """Human-readable list of what the account currently holds."""
    interesting = {name: count for name, count in counts.items() if count}
    if not interesting:
        return "لا توجد بيانات محفوظة عنك حاليًا."
    lines: List[str] = []
    total = 0
    for name in sorted(interesting):
        count = interesting[name]
        total += count
        lines.append(f"• {name}: {count}")
    return f"إجمالي السجلات: {total}\n" + "\n".join(lines)


async def send_export(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      services) -> None:
    """Send the caller a JSON copy of everything stored about them.

    The rate-limit guard lives in the `export_command` wrapper in
    ``app/telegram/main.py``, which owns the slash commands (see
    ``tests/test_rate_limit_commands.py``).
    """
    user_id = update.effective_user.id

    result = services.privacy.export(user_id)
    if result is None:
        await render.reply(update.message, "❌ تعذر تجهيز نسخة من بياناتك. حاول لاحقًا.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"austro-data-{user_id}-{stamp}.json"
    caption = (
        f"📦 نسخة من بياناتك ({result.total_rows} سجل)\n"
        "الملف بصيغة JSON ويحتوي على كل ما نحتفظ به عن حسابك."
    )
    try:
        await update.message.reply_document(
            document=result.to_bytes(),
            filename=filename,
            caption=caption,
        )
    except Exception:  # noqa: BLE001 - network/format failure must be reported, not raised
        logger.exception("Could not send the export file for %s", user_id)
        await render.reply(update.message, "❌ تعذر إرسال ملف البيانات. حاول لاحقًا.")


async def offer_deletion(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        services) -> None:
    """Explain the consequences of erasure and ask for an explicit confirmation.

    Destroys nothing: only :func:`delete_confirm_callback` does, and only for the
    user who triggered this request within ``CONFIRM_TTL_MINUTES``.
    """
    user_id = update.effective_user.id

    counts = services.privacy.summary(user_id)
    if counts is None:
        # Do not show "you have no data" when the read failed, and do not hand
        # out a token for an operation whose scope we cannot state.
        await render.reply(
            update.message,
            "❌ تعذر قراءة بيانات الحساب الآن. حاول لاحقًا.",
        )
        return
    token = secrets.token_hex(8)
    _clear_pending(context)
    context.user_data[_PENDING_KEY] = {
        "token": token,
        "user_id": user_id,
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(minutes=CONFIRM_TTL_MINUTES)),
    }

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🗑 نعم، احذف كل بياناتي نهائيًا",
            callback_data=f"privacy_delete_confirm:{token}",
        ),
        InlineKeyboardButton("↩️ تراجع", callback_data="privacy_delete_cancel"),
    ]])
    await render.reply(
        update.message,
        "⚠️ **حذف الحساب نهائيًا**\n\n"
        "سيُحذف كل ما يلي ولا يمكن استرجاعه:\n\n"
        f"{_format_summary(counts)}\n\n"
        f"للتأكيد اضغط الزر خلال {CONFIRM_TTL_MINUTES} دقائق. "
        "إن أردت نسخة أولًا فأرسل /export قبل الحذف.",
        reply_markup=keyboard,
    )


async def delete_confirm_callback(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the confirmation button (and refuse stale or foreign taps)."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id

    pending = _pending(context)
    data = query.data or ""
    token = data.split(":", 1)[1] if ":" in data else ""

    if (pending is None or pending.get("token") != token
            or pending.get("user_id") != user_id):
        await render.edit(query,
            "⌛ انتهت صلاحية طلب الحذف أو أنه لا يخصّك. أرسل /delete من جديد.")
        return

    if datetime.now(timezone.utc) > pending["expires_at"]:
        _clear_pending(context)
        await render.edit(query,
            "⌛ انتهت صلاحية طلب الحذف. أرسل /delete من جديد.")
        return

    _clear_pending(context)
    result = services.privacy.delete_account(user_id)
    if result is None:
        # Nothing was deleted: the transaction rolled back. Never say otherwise.
        await render.edit(query,
            "❌ تعذر حذف البيانات، ولم يُحذف أي شيء. حاول لاحقًا.")
        return

    if not result.verified:
        if result.residual is None:
            logger.error("Deletion for %s could not be verified: read failed", user_id)
            await render.edit(query,
                "⚠️ حُذفت سجلات حسابك من قاعدة البيانات، لكن تعذّر التحقق من "
                "اكتمال الحذف. تواصل مع الدعم للتأكيد.")
            return
        logger.error("Deletion for %s was not verified clean: %s",
                     user_id, result.residual)
        await render.edit(query,
            "⚠️ حُذف معظم البيانات لكن بقي أثر لم نستطع التأكد من إزالته. "
            "تواصل مع الدعم لإكمال الحذف.")
        return

    if result.files_failed:
        # The rows are gone, but stored files are not. Saying "all your data
        # was deleted" here would be false, and the files are now unreachable
        # from the database, so the operator must finish the job by hand.
        logger.error("Deletion for %s left %s stored file(s)",
                     user_id, result.files_failed)
        await render.edit(query,
            "⚠️ حُذفت سجلات حسابك، لكن تعذّر حذف "
            f"{result.files_failed} من الملفات المخزنة. "
            "تواصل مع الدعم لإكمال حذف الملفات.")
        return

    await render.edit(query,
        "✅ حُذفت جميع بياناتك نهائيًا.\n"
        f"السجلات المحذوفة: {result.total_rows}\n"
        f"الملفات المحذوفة: {result.files_removed}\n\n"
        "أرسل /start لبدء حساب جديد متى شئت.")


async def delete_cancel_callback(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a pending deletion."""
    query = update.callback_query
    await query.answer()
    _clear_pending(context)
    await render.edit(query, "✅ أُلغيت العملية، لم يُحذف أي شيء.")


async def delete_source_callback(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete one knowledge source the user uploaded (callback `kdel:<id>`).

    `KnowledgeService.delete_source` already removes the stored file first and
    keeps the row when that fails, so this handler only has to report the
    outcome honestly.
    """
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id

    data = query.data or ""
    try:
        source_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("طلب غير صالح", show_alert=True)
        return

    try:
        removed = services.knowledge.delete_source(user_id, source_id)
    except Exception:  # noqa: BLE001 - not found / storage failure
        logger.warning("Knowledge source deletion failed for %s/%s",
                       user_id, source_id, exc_info=True)
        removed = False

    if removed:
        await render.edit(query, "🗑 حُذف المصدر وملفه نهائيًا.")
    else:
        await render.edit(query,
            "❌ تعذر حذف المصدر (قد يكون محذوفًا بالفعل أو تعذر إزالة الملف).")


def get_privacy_handlers() -> List[Any]:
    """Inline-button handlers to register in the application.

    The `/export` and `/delete` commands themselves are registered by
    ``app/telegram/main.py`` (which owns slash commands and their rate-limit
    guards). Cancel must be matched before the generic confirm prefix.
    """
    from telegram.ext import CallbackQueryHandler

    return [
        CallbackQueryHandler(delete_cancel_callback,
                             pattern=r"^privacy_delete_cancel$"),
        CallbackQueryHandler(delete_confirm_callback,
                             pattern=r"^privacy_delete_confirm:"),
        CallbackQueryHandler(delete_source_callback, pattern=r"^kdel:\d+$"),
    ]
