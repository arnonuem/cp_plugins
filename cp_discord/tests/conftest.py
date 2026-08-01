"""Test harness for the cp_discord plugin.

Makes the plugin importable the same way Code Puppy's user-tier loader does
it: the loader puts the PLUGINS directory on ``sys.path`` and imports
``register_callbacks.py`` by file location (``code_puppy/plugins/__init__.py``
``:124-127``, ``:288-292``).  Tests reproduce that layout by putting the
plugin's PARENT directory on ``sys.path``, so ``import cp_discord.authz``
resolves against the working tree.

Why this file exists at all: the suites were written when the plugin still
lived inside the code_puppy repo and imported itself as
``code_puppy.plugins.cp_discord``.  That path is gone -- ``cp_plugins`` is now
the single source of truth -- so the old import raises ``ModuleNotFoundError``
before a single assertion runs.  The sibling plugins (``wmux``,
``user_msg_style``) already solve it exactly this way.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_PARENT = str(Path(__file__).resolve().parents[2])

if _PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, _PLUGIN_PARENT)
