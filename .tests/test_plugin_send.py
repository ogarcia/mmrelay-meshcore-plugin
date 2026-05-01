import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest
from unittest.mock import AsyncMock, MagicMock
import re
import time

# --- MOCK DE BasePlugin PARA TEST AUTÓNOMO ---
import types
class DummyBasePlugin:
    pass
# Mockear en sys.modules ambos posibles paths antes de importar plugin
sys.modules['mmrelay.plugins.base_plugin'] = types.SimpleNamespace(BasePlugin=DummyBasePlugin)
sys.modules['plugins.base_plugin'] = types.SimpleNamespace(BasePlugin=DummyBasePlugin)

# --- MOCK DE DB Y LOGGER PARA TEST AUTÓNOMO ---
def dummy_get_db_path():
    return ':memory:'
import types
sys.modules['mmrelay.db_utils'] = types.SimpleNamespace(get_db_path=dummy_get_db_path)

from plugin import Plugin

@pytest.mark.asyncio
async def test_send_channel_message_with_timestamp(monkeypatch):
    plugin = Plugin()
    # Mock logger para evitar errores del init
    plugin.logger = MagicMock()
    # Mock mc object structure
    mc = MagicMock()
    mc.commands.send_msg = AsyncMock(return_value=None)

    channel_info = {
        "channel_id": "CHAN1234",
        "channel_name": "TestChannel",
    }
    message = "Hola mundo"
    display_name = "Tester"

    # Patch time.time to a fixed value for determinism
    fixed_time = 1700000000.123
    monkeypatch.setattr(time, "time", lambda: fixed_time)

    result = await plugin._send_channel_message_with_overrides(mc, channel_info, message, display_name)
    # Should call send_msg once with correct channel and timestamped message
    assert mc.commands.send_msg.call_count == 1
    sent_args = mc.commands.send_msg.call_args[0]
    assert sent_args[0] == channel_info["channel_id"]
    # Message starts with [timestamp_hex]
    m = re.match(r"\[(\w+)\] (.+)", sent_args[1])
    assert m, f"Message {sent_args[1]!r} does not start with [timestamp] prefix"
    ts_hex = m.group(1)
    sent_body = m.group(2)
    # Should be the expected (base 16 ms precision); base16(fixed_time*1000)
    assert ts_hex == format(int(fixed_time * 1000), "x")
    assert sent_body == message

@pytest.mark.asyncio
async def test_send_channel_message_handles_errors(caplog):
    plugin = Plugin()
    plugin.logger = MagicMock()
    mc = MagicMock()
    # Simulate error in send_msg
    mc.commands.send_msg = AsyncMock(side_effect=Exception("fail send"))
    channel_info = {"channel_id": "X1", "channel_name": "Err"}
    msg = "Error msg"
    # Should not throw, should log the error
    await plugin._send_channel_message_with_overrides(mc, channel_info, msg, "User")
    assert any("Failed to send to MeshCore channel" in r.message for r in caplog.records)
