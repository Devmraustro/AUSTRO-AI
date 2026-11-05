import pytest
from unittest.mock import AsyncMock, MagicMock

from app.telegram.main import TELEGRAM_COMMANDS, _register_telegram_commands


def test_telegram_commands_include_required_entries():
    commands = {command.command for command in TELEGRAM_COMMANDS}
    assert "start" in commands
    assert "help" in commands


def test_telegram_commands_have_no_duplicates():
    names = [command.command for command in TELEGRAM_COMMANDS]
    assert len(names) == len(set(names))


def test_telegram_commands_are_valid():
    for command in TELEGRAM_COMMANDS:
        assert command.command == command.command.lower()
        assert command.command.isalnum() or "_" in command.command
        assert command.description
        assert len(command.description) <= 256


@pytest.mark.asyncio
async def test_register_telegram_commands_success():
    application = MagicMock()
    application.bot = MagicMock()
    application.bot.set_my_commands = AsyncMock()

    await _register_telegram_commands(application)

    application.bot.set_my_commands.assert_awaited_once()
    registered = application.bot.set_my_commands.await_args.args[0]
    registered_names = {command.command for command in registered}
    assert "start" in registered_names
    assert "help" in registered_names


@pytest.mark.asyncio
async def test_register_telegram_commands_failure_is_safe():
    application = MagicMock()
    application.bot = MagicMock()
    application.bot.set_my_commands = AsyncMock(side_effect=RuntimeError("boom"))

    await _register_telegram_commands(application)  # must not raise