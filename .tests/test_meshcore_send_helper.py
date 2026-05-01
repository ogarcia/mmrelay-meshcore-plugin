import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest
from unittest.mock import AsyncMock, MagicMock
import time
from meshcore_helpers import send_channel_message_with_timestamp, has_timestamp_prefix
import re

@pytest.mark.asyncio
async def test_send_channel_message_with_timestamp(monkeypatch):
    mc = MagicMock()
    mc.commands.send_msg = AsyncMock(return_value="ok")
    channel_index = 3
    message = "Hello MeshCore"
    fixed_time = 1700000000.123
    monkeypatch.setattr(time, "time", lambda: fixed_time)
    result = await send_channel_message_with_timestamp(mc, channel_index, message)
    mc.commands.send_msg.assert_called_once()
    args, kwargs = mc.commands.send_msg.call_args
    sent_msg = args[1]
    assert has_timestamp_prefix(sent_msg), f"No hay timestamp en: {sent_msg}"
    ts_pref = f"[{format(int(fixed_time*1000), 'x')}] "
    assert sent_msg.startswith(ts_pref)
    assert sent_msg.endswith(message)
    assert args[0] == channel_index
    assert result == "ok"

@pytest.mark.asyncio
@pytest.mark.parametrize("problem_text", [
    "",  # Empty
    "\x00\t\x1fOK\x7f",  # Control characters
    "Test😀𝄞汉字",  # Unicode, emojis
    "A" * 220,    # Long string, over MeshCore limit
])
async def test_send_channel_message_with_problematic_inputs(problem_text, monkeypatch):
    mc = MagicMock()
    mc.commands.send_msg = AsyncMock(return_value="ok")
    channel_index = 4
    fixed_time = 1701234567.89
    monkeypatch.setattr(time, "time", lambda: fixed_time)
    result = await send_channel_message_with_timestamp(mc, channel_index, problem_text)
    mc.commands.send_msg.assert_called_once()
    args, kwargs = mc.commands.send_msg.call_args
    sent_msg = args[1]
    assert has_timestamp_prefix(sent_msg)
    assert isinstance(sent_msg, str)
    # El mensaje nunca debe contener controles ASCII (salvo saltos de línea, si los hubiera)
    assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", sent_msg)
    assert args[0] == channel_index
    assert result == "ok"

@pytest.mark.asyncio
async def test_send_channel_message_with_send_msg_error(monkeypatch):
    mc = MagicMock()
    mc.commands.send_msg = AsyncMock(side_effect=Exception("boom!"))
    channel_index = 8
    with pytest.raises(Exception):
        await send_channel_message_with_timestamp(mc, channel_index, "Testing error")
