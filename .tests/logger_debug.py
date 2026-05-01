import sys
sys.path.insert(0, ".")
sys.path.insert(0,".tests")
from unittest.mock import MagicMock
import types
class DummyBasePlugin: pass
sys.modules['mmrelay.plugins.base_plugin'] = types.SimpleNamespace(BasePlugin=DummyBasePlugin)
sys.modules['plugins.base_plugin'] = types.SimpleNamespace(BasePlugin=DummyBasePlugin)
sys.modules['mmrelay.db_utils'] = types.SimpleNamespace(get_db_path=lambda: ':memory:')
from fake_nio import RoomMessageText
import plugin as plugin_mod
plugin = plugin_mod.Plugin()
plugin.logger = MagicMock()
plugin._mc = None
plugin.config = {'channel_mappings':[{'matrix_room':'!test:mx','meshcore_channel_name':'NCS','meshcore_channel_key':'aa'*16,'meshcore_channel_index':99}]}
class DummyRoom:
    room_id = '!test:mx'
    def user_name(self,u): return 'User'
class DummyEvent(RoomMessageText):
    sender = 'user'
    body = 'fail connect'
    server_timestamp = 1
    source = {'content':{}}
plugin_mod.bot_start_time = 0
plugin_mod.bot_user_id = 'other'

plugin._get_channel_info_for_room = lambda room_id: {'channel_name':'NCS','channel_key':'aa'*16,'channel_index':99}

import asyncio
asyncio.run(plugin._on_matrix_room_message(DummyRoom(), DummyEvent()))
print('LOGGER.WARNING CALLS:', plugin.logger.warning.call_args_list)
