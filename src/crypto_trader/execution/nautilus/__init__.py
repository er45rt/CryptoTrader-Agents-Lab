"""Nautilus implementation of the execution boundary (Phase 4, infrastructure layer).

Importing this package is importing infrastructure: the engine is a hard dependency
(``nautilus_trader==1.231.0`` is pinned in the production dependency set).
"""

from .guard import (
    SUPPORTED_ENGINE_VERSION,
    TESTED_BOOK_TYPE_NAME,
    QueueAwareSettings,
    installed_engine_version,
    require_queue_aware_settings,
    require_supported_engine_version,
    tested_queue_aware_settings,
)
from .runtime import NautilusExecutionRuntime

__all__ = [
    "SUPPORTED_ENGINE_VERSION",
    "TESTED_BOOK_TYPE_NAME",
    "NautilusExecutionRuntime",
    "QueueAwareSettings",
    "installed_engine_version",
    "require_queue_aware_settings",
    "require_supported_engine_version",
    "tested_queue_aware_settings",
]
