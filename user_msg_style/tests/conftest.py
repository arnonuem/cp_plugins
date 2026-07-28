"""Make the plugin importable as a namespace package during tests.

The plugin ships without an ``__init__.py`` because Code Puppy's user-tier
loader imports ``register_callbacks.py`` directly by file location
(``code_puppy/plugins/__init__.py:151-161``) after putting the plugins
directory on ``sys.path``. Tests reproduce that layout by putting the
plugin's PARENT directory on ``sys.path``, which makes ``user_msg_style``
resolve as a PEP 420 namespace package.
"""

import sys
from pathlib import Path

_PLUGIN_PARENT = str(Path(__file__).resolve().parents[2])

if _PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, _PLUGIN_PARENT)
