import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest
from unittest.mock import AsyncMock, MagicMock
import types
import time

# Dummy base_class and db/path mocks for isolation
class DummyBasePlugin: pass
sys.modules['mmrelay.plugins.base_plugin'] = types.SimpleNamespace(BasePlugin=DummyBasePlugin)
sys.modules['plugins.base_plugin'] = types.SimpleNamespace(BasePlugin=DummyBasePlugin)
sys.modules['mmrelay.db_utils'] = types.SimpleNamespace(get_db_path=lambda: ':memory:')

from plugin import Plugin
class DummyMC:
    class Commands:
        pass
    def __init__(self):
        self.commands = self.Commands()
        self.is_connected = True

@pytest.mark.asyncio
async def test_matrix_to_meshcore_relay_sends_slot_and_sanitizes(monkeypatch):
    plugin = Plugin()
    plugin.logger = MagicMock()
    # Fake config mapping with slot index
    plugin.config = {'channel_mappings': [
        {"matrix_room": "!test:mx", "meshcore_channel_name": "TestSlot", "meshcore_channel_key": "11"*16, "meshcore_channel_index": 21 }
    ]}
    # dummy MeshCore connection,
    mc = DummyMC()
    mc.commands.send_msg = AsyncMock(return_value="ok")
    channel_info = {
        'channel_name': 'TestSlot',
        'channel_key': "11"*16,
        'channel_id': "cid-test",
        'channel_index': 21
    }
    dirty_message = 'h\x00e\x1fl!'
    # Run
    await plugin._send_channel_message_with_overrides(mc, channel_info, dirty_message, "SenderX")
    mc.commands.send_msg.assert_called_once()
    slot, text = mc.commands.send_msg.call_args[0]
    assert slot == 21
    assert '\x00' not in text and '\x1f' not in text
    assert 'hel!' in text  # Result must be sanitized, timestamped, lossless
    assert text.startswith("[")  # Has timestamp

@pytest.mark.asyncio
async def test_meshcore_to_matrix_successfully_relays(monkeypatch):
    plugin = Plugin()
    plugin.logger = MagicMock()
    plugin.send_matrix_message = AsyncMock()
    plugin.config = {'channel_mappings': [
        {"matrix_room": "!test:mx2", "meshcore_channel_name": "InSlot", "meshcore_channel_key": "22"*16}
    ]}
    # Register mapping
    idx = 5
    channel_info = {'channel_name': 'InSlot', 'channel_key': "22"*16, 'channel_id': "cid-in", 'channel_idx': idx }
    plugin._channels_by_idx[idx] = channel_info
    # Trigger incoming message
    class Event:
        def __init__(self, payload): self.payload = payload
    payload = { 'channel_idx': idx, 'text': 'Hola desde MeshCore' }
    await plugin._on_channel_msg(Event(payload))
    plugin.send_matrix_message.assert_called_once()
    args = plugin.send_matrix_message.call_args[0]
    assert args[0] == "!test:mx2"
    assert 'Hola desde MeshCore' in args[1]


@pytest.mark.asyncio
async def test_matrix_to_meshcore_with_public_channel(monkeypatch):
    plugin = Plugin()
    plugin.logger = MagicMock()
    plugin.config = {'channel_mappings': [
        {"matrix_room": "!test:mxpub", "meshcore_channel_name": "#PublicC", "meshcore_channel_index": 8}
    ]}
    mc = DummyMC()
    mc.commands.send_msg = AsyncMock(return_value="ok")
    channel_info = {
        'channel_name': '#PublicC',
        'channel_key': None,
        'channel_id': 'fakeid',
        'channel_index': 8
    }
    await plugin._send_channel_message_with_overrides(mc, channel_info, 'open to all', "OpenSender")
    mc.commands.send_msg.assert_called_once()
    slot, text = mc.commands.send_msg.call_args[0]
    assert slot == 8
    assert "open to all" in text
