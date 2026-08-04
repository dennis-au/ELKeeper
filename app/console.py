"""Compatibility module alias for the legacy console runtime.

Using a module alias, rather than copying symbols, preserves the historical
``app.console`` patch surface while allowing the implementation to live in a
separate runtime module.
"""

import sys

from . import console_runtime as _runtime

sys.modules[__name__] = _runtime
