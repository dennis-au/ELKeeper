"""Compatibility exports for the maintenance API.

The implementation is owned by :mod:`app.modules.maintenance.api`; this
module remains importable for existing application assembly and external
callers during the migration.
"""

from app.modules.platform import control_db as _platform_control_db  # public contract marker for compatibility tooling
from app.modules.maintenance.api import *  # noqa: F401,F403
from app.modules.maintenance.api import router

__all__ = ["router"]
