import sys
from unittest.mock import MagicMock

sys.modules['mmrelay'] = MagicMock()
sys.modules['mmrelay.log_utils'] = MagicMock()
sys.modules['mmrelay.plugin_loader'] = MagicMock()
sys.modules['mmrelay.plugins.base_plugin'] = MagicMock()
sys.modules['mmrelay.matrix_utils'] = MagicMock()
sys.modules['mmrelay.meshtastic_utils'] = MagicMock()
sys.modules['mmrelay.db_utils'] = MagicMock()
sys.modules['plugins.base_plugin'] = MagicMock()
