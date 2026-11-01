"""ASR/LLM client factories selecting the implementation by configuration.

Implements the design's ``make_asr_client(settings)`` / ``make_llm_client(settings)``
factories (task 4.3): each returns the implementation named by the corresponding
``*_impl`` selector in :class:`~app.config.Settings` -- the mock implementation
(:mod:`app.clients.mock`) when the selector is ``"mock"``, or the HTTP
implementation (:mod:`app.clients.http`) when it is ``"http"`` -- with no code
change required to switch (Req 11.1, 11.2; Property 28).

The selector type (``Literal["http", "mock"]``) is constrained by ``Settings``,
so the two arms are exhaustive; an unexpected value still raises a clear
``ValueError`` rather than silently returning ``None``.

Both factories accept an explicit :class:`~app.config.Settings` and default to
the process-wide cached :func:`app.config.get_settings` when called with
``None`` (or no argument), so callers in the worker/API can simply call
``make_asr_client()`` while tests can pass a purpose-built ``Settings``.
"""

from __future__ import annotations

from app.clients.base import AsrClient, LlmClient
from app.clients.http import HttpAsrClient, HttpLlmClient
from app.clients.mock import MockAsrClient, MockLlmClient
from app.config import Settings, get_settings

__all__ = ["make_asr_client", "make_llm_client"]


def make_asr_client(settings: Settings | None = None) -> AsrClient:
    """Return the ASR_Client implementation designated by ``asr_impl``.

    :param settings: configuration to read ``asr_impl`` (and, for HTTP, the
        endpoint/timeout/chunk/retry fields) from; defaults to
        :func:`app.config.get_settings` when ``None``.
    :returns: a :class:`MockAsrClient` when ``asr_impl == "mock"``, otherwise an
        :class:`HttpAsrClient` configured from ``settings``.
    :raises ValueError: if ``asr_impl`` is an unrecognized value.
    """
    settings = settings if settings is not None else get_settings()
    if settings.asr_impl == "mock":
        return MockAsrClient()
    if settings.asr_impl == "http":
        return HttpAsrClient(settings)
    raise ValueError(f"unknown asr_impl: {settings.asr_impl!r}")


def make_llm_client(settings: Settings | None = None) -> LlmClient:
    """Return the LLM_Client implementation designated by ``llm_impl``.

    :param settings: configuration to read ``llm_impl`` (and, for HTTP, the
        endpoint/model/timeout fields) from; defaults to
        :func:`app.config.get_settings` when ``None``.
    :returns: a :class:`MockLlmClient` when ``llm_impl == "mock"``, otherwise an
        :class:`HttpLlmClient` configured from ``settings``.
    :raises ValueError: if ``llm_impl`` is an unrecognized value.
    """
    settings = settings if settings is not None else get_settings()
    if settings.llm_impl == "mock":
        return MockLlmClient()
    if settings.llm_impl == "http":
        return HttpLlmClient(settings)
    raise ValueError(f"unknown llm_impl: {settings.llm_impl!r}")
