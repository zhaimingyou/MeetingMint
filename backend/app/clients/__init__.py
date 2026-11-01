"""External service clients (ASR, LLM): interfaces, types, and exceptions.

This package defines the pluggable boundary to the external ASR_Service and
LLM_Service. Task 4.1 establishes the contracts here:

* Typed shapes (:mod:`app.clients.types`): ``TranscriptSegment``,
  ``ActionItemData``, ``MeetingMinutesData``, ``TranscriptInput``.
* Interfaces and exceptions (:mod:`app.clients.base`): the ``AsrClient`` /
  ``LlmClient`` Protocols and the ``AsrError`` / ``LlmError`` /
  ``LlmSchemaError`` hierarchy.

Concrete mock implementations (task 4.2, :mod:`app.clients.mock`) are
re-exported here, along with the HTTP implementation skeletons
(:mod:`app.clients.http`, fleshed out in tasks 5/6) and the selecting factories
(task 4.3, :mod:`app.clients.factory`).
"""

from app.clients.base import (
    AsrClient,
    AsrError,
    LlmClient,
    LlmError,
    LlmSchemaError,
)
from app.clients.factory import make_asr_client, make_llm_client
from app.clients.http import HttpAsrClient, HttpLlmClient
from app.clients.mock import MockAsrClient, MockLlmClient
from app.clients.types import (
    ActionItemData,
    MeetingMinutesData,
    TranscriptInput,
    TranscriptSegment,
)

__all__ = [
    # Interfaces
    "AsrClient",
    "LlmClient",
    # Exceptions
    "AsrError",
    "LlmError",
    "LlmSchemaError",
    # Mock implementations
    "MockAsrClient",
    "MockLlmClient",
    # HTTP implementations (skeletons; completed in tasks 5/6)
    "HttpAsrClient",
    "HttpLlmClient",
    # Factories
    "make_asr_client",
    "make_llm_client",
    # Typed shapes
    "TranscriptSegment",
    "ActionItemData",
    "MeetingMinutesData",
    "TranscriptInput",
]
