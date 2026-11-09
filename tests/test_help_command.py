import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config.settings import settings
from app.telegram.main import help_command


@pytest.mark.asyncio
async def test_help_command_replies_without_error():
    update = MagicMock()
    user = MagicMock()
    user.id = 1
    update.effective_user = user
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await help_command(update, context)

    assert update.message.reply_text.await_count == 1
    text = update.message.reply_text.await_args.args[0]
    assert "المساعدة" in text
    assert "@Mr\\_Austro" in text
    assert "@your_support" not in text


def test_support_username_default_is_configured():
    assert settings.support_username == "Mr_Austro"