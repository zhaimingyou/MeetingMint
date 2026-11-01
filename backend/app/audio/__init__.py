"""Audio inspection / processing helpers for the Backend_API and worker.

Currently hosts :mod:`app.audio.format_detect` (task 8.2): content-based
Supported_Audio_Format detection via ``ffprobe``, used by the upload endpoint to
reject unsupported uploads with 415 *before* a Meeting_Record is created.
"""

from __future__ import annotations
