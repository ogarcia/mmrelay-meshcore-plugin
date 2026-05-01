import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest
import time
from unittest.mock import AsyncMock, MagicMock
import types

# Mock base class for plugin
class DummyBasePlugin:
    pass

sys.modules['mmrelay.plugins.base_plugin'] = types.SimpleNamespace(BasePlugin=DummyBasePlugin)
sys.modules['plugins.base_plugin'] = types.SimpleNamespace(BasePlugin=DummyBasePlugin)
sys.modules['mmrelay.db_utils'] = types.SimpleNamespace(get_db_path=lambda: ':memory:')

from plugin import Plugin

class DummyEvent:
    def __init__(self, payload):
        self.payload = payload

@pytest.mark.asyncio
async def test_buffer_and_replay_pending_slot_message():
    plugin = Plugin()
    plugin.logger = MagicMock()
    plugin.send_matrix_message = AsyncMock()
    # Provide minimum config for mapping to work
    plugin.config = {
        'channel_mappings': [
            {"matrix_room": "!room:test:42", "meshcore_channel_name": "Slot42", "meshcore_channel_key": "61616161616161616161616161616161"}
        ]
    }
    test_idx = 42
    msg_dict = {'channel_idx': test_idx, 'text': 'Pending test'}
    # -- Llega mensaje cuando no hay mapping para el slot --
    await plugin._on_channel_msg(DummyEvent(msg_dict))
    assert test_idx in plugin._pending_slot_messages
    assert len(plugin._pending_slot_messages[test_idx]) == 1
    # -- Al llegar CHANNEL_INFO para ese slot, el buffer se vacía y se envía a Matrix --
    payload = {'channel_name': 'Slot42', 'channel_secret': b'a'*16, 'channel_idx': test_idx}
    await plugin._on_channel_info(DummyEvent(payload))
    # -- Ahora el buffer ya no tiene ese slot --
    assert test_idx not in plugin._pending_slot_messages
    # -- Se llamó a send_matrix_message (vía reproceso) --
    assert plugin.send_matrix_message.call_count == 1
    called_args = plugin.send_matrix_message.call_args[0]
    assert 'Pending test' in called_args[1]

@pytest.mark.asyncio
async def test_buffer_cleanup_timeout(monkeypatch):
    plugin = Plugin()
    plugin.logger = MagicMock()
    # Buffer con mensaje antiguo (> 10s atrás)
    now = time.time()
    idx = 77
    plugin._pending_slot_messages[idx] = [ (now-10, {'channel_idx': idx, 'text': 'Should disappear'}) ]
    await plugin._cleanup_pending_slot_messages()
    assert idx not in plugin._pending_slot_messages  # buffer limpiado
    # Se ha registrado un warning
    logs = [str(c[0]) for c in plugin.logger.warning.call_args_list]
    assert any('message dropped after' in m for m in logs)

@pytest.mark.asyncio
async def test_message_for_unmapped_slot_is_buffered_and_cleaned(monkeypatch):
    plugin = Plugin()
    plugin.logger = MagicMock()
    # Llega mensaje para slot nunca mapeado; pasa el timeout y luego desaparece
    idx = 111
    msg = {'channel_idx': idx, 'text': 'Orphan'}
    old_time = time.time()
    monkeypatch.setattr(time, "time", lambda: old_time)
    await plugin._on_channel_msg(DummyEvent(msg))
    assert idx in plugin._pending_slot_messages
    # Avanza tiempo y ejecuta limpieza
    monkeypatch.setattr(time, "time", lambda: old_time + 7)
    await plugin._cleanup_pending_slot_messages()
    assert idx not in plugin._pending_slot_messages

@pytest.mark.asyncio
async def test_sanitization_on_buffered_and_replayed(monkeypatch):
    from meshcore_helpers import sanitize_text
    plugin = Plugin()
    plugin.logger = MagicMock()
    plugin.send_matrix_message = AsyncMock()
    # Provide minimum config for mapping to work
    plugin.config = {
        'channel_mappings': [
            {"matrix_room": "!room:test:5", "meshcore_channel_name": "SafeSlot", "meshcore_channel_key": "62626262626262626262626262626262"}
        ]
    }
    idx = 5
    unsanitized = 'Unsafe\\x00\\x1fdone'
    msg_dict = {'channel_idx': idx, 'text': unsanitized}
    await plugin._on_channel_msg(DummyEvent(msg_dict))
    # Llega mapping; se procesa mensaje...
    payload = {'channel_name': 'SafeSlot', 'channel_secret': b'b'*16, 'channel_idx': idx}
    await plugin._on_channel_info(DummyEvent(payload))
    # Se ha llamado a send_matrix_message con el texto ya sanitizado
    assert plugin.send_matrix_message.call_count == 1
    content = plugin.send_matrix_message.call_args[0][1]
    assert sanitize_text(unsanitized) in content
    assert '\x00' not in content and '\x1f' not in content
