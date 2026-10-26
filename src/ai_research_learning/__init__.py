"""AI Research & Learning Explorer package."""
from __future__ import annotations

from .api import (
    DEFAULT_API_HOST,
    DEFAULT_API_PORT,
    APIError,
    APIResponse,
    LocalResearchLibraryAPI,
    create_http_server,
)

__all__ = [
    "APIError",
    "APIResponse",
    "DEFAULT_API_HOST",
    "DEFAULT_API_PORT",
    "LocalResearchLibraryAPI",
    "__version__",
    "create_http_server",
]
__version__ = "0.1.0"

