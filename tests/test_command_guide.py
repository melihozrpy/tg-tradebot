from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.telegram import handlers_v3


@pytest.mark.asyncio
async def test_command_guide_explains_structural_entry_commands(monkeypatch):
    monkeypatch.setattr(handlers_v3, "_reject_unauthorized", AsyncMock(return_value=False))
    reply_text = AsyncMock()
    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text))

    await handlers_v3.cmd_komutlar(update, SimpleNamespace())

    messages = "\n".join(call.args[0] for call in reply_text.await_args_list)
    assert "🎯 ENTRY • OB/FVG • MSS" in messages
    assert "/islemplani THYAO" in messages
    assert "net entry, gerekçe" in messages
    assert "Son kapanışı entry yapmaz" in messages
    assert "/kirilsanaryo THYAO" in messages


@pytest.mark.asyncio
async def test_command_guide_messages_fit_telegram_limit(monkeypatch):
    monkeypatch.setattr(handlers_v3, "_reject_unauthorized", AsyncMock(return_value=False))
    reply_text = AsyncMock()
    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text))

    await handlers_v3.cmd_komutlar(update, SimpleNamespace())

    assert reply_text.await_count == 2
    assert all(len(call.args[0]) <= 4096 for call in reply_text.await_args_list)
