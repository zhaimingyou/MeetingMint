"""Shared helpers for the Admin_Console routers (admin-console).

Small utilities reused across the admin endpoints: bounded pagination so list
responses stay bounded regardless of total row count (Req 9.3 / 10.3).
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "clamp_page",
    "clamp_page_size",
    "page_offset",
]

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def clamp_page(page: int) -> int:
    """Return a 1-based page number, never below 1."""
    return page if page >= 1 else 1


def clamp_page_size(page_size: int) -> int:
    """Clamp a requested page size into ``[1, MAX_PAGE_SIZE]`` (lenient, no 422)."""
    if page_size < 1:
        return 1
    return min(page_size, MAX_PAGE_SIZE)


def page_offset(page: int, page_size: int) -> int:
    """Compute the SQL OFFSET for a clamped ``page`` / ``page_size``."""
    return (clamp_page(page) - 1) * clamp_page_size(page_size)
