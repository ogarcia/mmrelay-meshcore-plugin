import sys
from unittest.mock import MagicMock
import types
sys.path.insert(0, ".")
sys.path.insert(0,".tests")
class DummyBasePlugin: pass
sys.modules['mmrelay.plugins.base_plugin'] = types.SimpleNamespace(BasePlugin=DummyBasePlugin)
sys.modules['plugins.base_plugin'] = types.SimpleNamespace(BasePlugin=DummyBasePlugin)
sys.modules['mmrelay.db_utils'] = types.SimpleNamespace(get_db_path=lambda: ':memory:')
from fake_nio import RoomMessageText
import plugin as plugin_mod
plugin = plugin_mod.Plugin()
plugin.logger = MagicMock()
plugin._mc = MagicMock()
plugin._mc.is_connected = True
plugin._mc.commands.send_msg = MagicMock(return_value='ok')
plugin.config = {'channel_mappings':[{'matrix_room':'!ROOM:test','meshcore_channel_name':'SLOTX','meshcore_channel_key':'11'*16,'meshcore_channel_index':1}]}
plugin._channels_by_idx[1] = {'channel_name':'SLOTX','channel_key':'11111111111111111111111111111111','channel_id':'fakeid','channel_index':1}
class DummyRoom:
    room_id = '!ROOM:test'
    def user_name(self, user): return 'DisplayName'
class DummyEvent(RoomMessageText):
    sender = '@ogarcia:matrix.org'
    body = 'Danger\x00!'
    server_timestamp = 12345678900
    source = {'content':{}}
plugin_mod.bot_start_time = 0
plugin_mod.bot_user_id = 'notme'
import asyncio
asyncio.run(plugin._on_matrix_room_message(DummyRoom(), DummyEvent()))
print('INFO LOGS:', plugin.logger.info.call_args_list)
