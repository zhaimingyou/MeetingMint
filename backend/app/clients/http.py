"""HTTP ASR/LLM client **skeletons** (created in task 4.3, completed in 5/6).

These classes exist now so that the selecting factories
(:mod:`app.clients.factory`, task 4.3) can return an ``"http"`` implementation
and the factory-selection property test (Property 28, task 4.4) can run *before*
the real transport logic is written. They satisfy the
:class:`~app.clients.base.AsrClient` / :class:`~app.clients.base.LlmClient`
Protocols structurally -- each defines the required method -- but the method
bodies intentionally raise :class:`NotImplementedError`.

Construction is real and side-effect-free: ``__init__`` reads and stores the
relevant configuration from :class:`~app.config.Settings` and opens **no**
sockets or connections. This matches the design's "do not open connections at
construction" guidance and the mock-friendly goal: a process can construct an
``HttpAsrClient`` / ``HttpLlmClient`` without any model deployed, and only a
``transcribe`` / ``generate_minutes`` call would reach the network.

What tasks 5 and 6 must implement here
--------------------------------------
* **Task 5 -- :class:`HttpAsrClient.transcribe`**: probe duration (ffprobe),
  chunk Standardized_Audio into sequential, non-overlapping windows of
  ``<= chunk_length_seconds`` covering ``[0, duration]`` (Req 5.2); per-chunk
  ``httpx`` POST with ``request_timeout_seconds`` and an in-client retry loop
  bounded by ``max_retries`` (Req 5.1, 5.5); offset each chunk's timestamps and
  assemble segments ordered by ascending ``start`` (Req 5.3); map speaker labels
  only when present (Req 5.4); on terminal failure raise
  :class:`~app.clients.base.AsrError` identifying the failed chunk (Req 5.6) and
  persist no partial transcript.
* **Task 6 -- :class:`HttpLlmClient.generate_minutes`**: OpenAI-compatible chat
  call to ``endpoint_url`` with ``model_name`` and a Chinese system prompt
  within ``request_timeout_seconds`` (Req 6.1); parse/validate the structured
  schema, defaulting missing lists to empty lists (Req 6.2) and absent owners to
  ``app.enums.UNASSIGNED_OWNER`` (Req 6.3); Chinese transcript => Chinese minutes
  (Req 6.4); raise :class:`~app.clients.base.LlmError` on transport/timeout
  failure (Req 6.5) and :class:`~app.clients.base.LlmSchemaError` on
  non-conforming content (Req 6.6); persist nothing partial.

Until then, ``transcribe`` / ``generate_minutes`` raise ``NotImplementedError``
with a message pointing at the owning task. ``httpx`` is intentionally NOT
imported here yet -- no network machinery is wired up until tasks 5/6.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import httpx

from app.clients.base import AsrError, LlmError, LlmSchemaError
from app.clients.chunking import compute_chunks, probe_duration
from app.clients.types import (
    ActionItemData,
    MeetingMinutesData,
    TranscriptInput,
    TranscriptSegment,
)
from app.enums import UNASSIGNED_OWNER

if TYPE_CHECKING:  # avoid an import cycle / heavy import at runtime
    from app.config import Settings

__all__ = ["HttpAsrClient", "HttpLlmClient", "LLM_SYSTEM_PROMPT"]


# --------------------------------------------------------------------------- #
# LLM prompt (task 6.1)
# --------------------------------------------------------------------------- #
# Chinese system prompt instructing the LLM_Service to return *strict* JSON for
# the structured minutes schema (Req 6.1, 6.4). Targets are Ollama / vLLM, which
# may not support an OpenAI ``response_format`` JSON-mode, so the contract is
# carried entirely by this prompt: produce a 会议纪要 as a single JSON object with
# exactly the keys ``summary`` / ``topics`` / ``decisions`` / ``action_items``,
# where ``action_items`` is a list of ``{description, owner}`` objects, and the
# minutes content is written in Chinese. Robust validation, empty-list defaults,
# and owner defaulting are hardened in task 6.2; the schema is described here so
# the model emits a parseable shape.
LLM_SYSTEM_PROMPT = (
    "你是一名专业的会议纪要助理。请阅读用户提供的会议转写文本，"
    "总结并生成结构化的中文会议纪要。\n"
    "你必须只输出一个严格合法的 JSON 对象，不要包含任何额外说明、注释或 Markdown 代码块标记。\n"
    "JSON 对象必须且只能包含以下键：\n"
    '  - "summary"：字符串，会议的整体摘要。\n'
    '  - "topics"：字符串数组，会议讨论的议题列表；若无则为空数组。\n'
    '  - "decisions"：字符串数组，会议形成的决议列表；若无则为空数组。\n'
    '  - "action_items"：对象数组，每个对象包含 "description"（字符串，待办事项描述）'
    '和 "owner"（字符串，负责人）；若某项待办没有明确负责人，'
    f'请将 "owner" 设为 "{UNASSIGNED_OWNER}"；若无待办则为空数组。\n'
    "所有文本内容请使用中文。请确保输出是可被直接解析的 JSON。"
)

# System prompt for post-ASR phonetic correction. Sent as a dedicated,
# focused prompt so the model corrects only the text without reformatting
# or summarizing it.
LLM_CORRECTION_PROMPT = (
    "你是一名专业的中文语音转写校对助手。请对用户提供的语音转写文本进行发音纠错。\n"
    "语音识别可能产生以下错误，请你修正：\n"
    "  - 同音字/近音字错误（如'在'误识别为'再'，'会议'误识别为'回忆'）\n"
    "  - 根据上下文修复不通顺或不合理的词序\n"
    "  - 专有名词、术语的拼写纠正\n"
    "规则：\n"
    "  - 只修正明显的语音识别错误，不要改变原意或删减内容\n"
    "  - 保持原文的语气、风格和结构\n"
    "  - 如果原文没有明显错误，原样返回\n"
    "  - 只输出纠错后的文本，不要加任何说明、注释或标记\n"
    "如果用户提供了「上下文」信息，请利用它来推断正确的用词和术语，"
    "尤其是专有名词、项目名称、人名的纠错应优先参考上下文。"
)

# System prompt for batch (windowed) post-ASR correction. The model receives
# a numbered list of segments and must return a JSON array of {index, text}
# objects — one per input segment, index-aligned.
LLM_BATCH_CORRECTION_PROMPT = (
    "你是一名专业的中文语音转写校对助手。请对以下语音转写文本进行发音纠错。\n"
    "语音识别可能产生以下错误，请你修正：\n"
    "  - 同音字/近音字错误（如'在'误识别为'再'，'会议'误识别为'回忆'）\n"
    "  - 根据上下文修复不通顺或不合理的词序\n"
    "  - 专有名词、术语的拼写纠正\n"
    "规则：\n"
    "  - 只修正明显的语音识别错误，不要改变原意或删减内容\n"
    "  - 保持原文的语气、风格和结构\n"
    "  - 如果某条没有明显错误，原样返回\n"
    "  - 必须为每条输入返回一条对应的纠错结果，不能跳过或遗漏\n"
    "  - 只能输出一个合法的 JSON 数组，不要包含任何额外说明或 Markdown 标记\n"
    "输出格式：\n"
    "  [{\"index\": 0, \"text\": \"纠错后文本\"}, {\"index\": 1, \"text\": \"纠错后文本\"}, ...]\n"
    "每个元素必须包含输入中对应的 index 号和纠错后的 text。"
)

BATCH_WINDOW_SIZE = 25
BATCH_WINDOW_OVERLAP = 2

# A "raw" segment is one chunk-relative segment as returned by the ASR_Service
# *before* the client offsets its timestamps by the chunk start. The contract is
# documented on ``HttpAsrClient._transcribe_chunk``.
RawSegment = Mapping[str, Any]

# The injectable "send one chunk" seam: given the audio path and the chunk's
# ``[start, end]`` window (seconds, absolute in the source audio), return the
# list of chunk-relative raw segment dicts for that window. The default
# implementation (:meth:`HttpAsrClient._transcribe_chunk`) performs the httpx
# POST; tests inject a fake so no real ASR_Service or ffmpeg is needed.
ChunkTranscriber = Callable[[str, float, float], Sequence[RawSegment]]

# The injectable "slice one chunk" seam (task: real-ASR adaptation): given the
# source audio path and a ``[start, end]`` window (absolute seconds), return a
# filesystem path to an audio file containing just that window, suitable for
# upload to the OpenAI-compatible transcriptions endpoint. The default
# implementation (:meth:`HttpAsrClient._slice_chunk`) uses ffmpeg; tests inject a
# fake so the transport runs without ffmpeg or a real audio file.
AudioSlicer = Callable[[str, float, float], str]


# --------------------------------------------------------------------------- #
# OpenAI-compatible URL resolution
# --------------------------------------------------------------------------- #
def _chat_completions_url(base: str) -> str:
    """Resolve the chat-completions URL from a configured endpoint/base URL.

    Accepts either a full chat-completions URL (used as-is) or a base URL and
    appends the standard ``/chat/completions`` path, so all of these work
    against an OpenAI-compatible provider (e.g. DeepSeek, vLLM, Ollama):

    * ``https://api.deepseek.com``             -> ``https://api.deepseek.com/chat/completions``
    * ``https://api.deepseek.com/v1``          -> ``https://api.deepseek.com/v1/chat/completions``
    * ``http://host:8000/v1/chat/completions`` -> unchanged
    """
    u = base.rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    return f"{u}/chat/completions"


def _transcriptions_url(base: str) -> str:
    """Resolve the audio-transcriptions URL from a configured endpoint/base URL.

    Accepts a full transcriptions URL (used as-is) or a base URL and appends the
    standard OpenAI-compatible path, matching the DGX Spark FunASR service
    (see deploy/asr-service/ASR-API.md):

    * ``http://192.0.2.20:19100``           -> ``.../v1/audio/transcriptions``
    * ``http://host:19100/v1``                  -> ``.../v1/audio/transcriptions``
    * ``.../v1/audio/transcriptions``           -> unchanged
    """
    u = base.rstrip("/")
    if u.endswith("/audio/transcriptions"):
        return u
    if u.endswith("/v1"):
        return f"{u}/audio/transcriptions"
    return f"{u}/v1/audio/transcriptions"


def _parse_chunk_response(payload: Any) -> list[RawSegment]:
    """Coerce an ASR_Service chunk response into a list of raw segment dicts.

    Accepts either a top-level JSON list of segment objects or an object with a
    ``segments`` list (both forms are part of the documented contract on
    :meth:`HttpAsrClient._transcribe_chunk`). Returns the segment list as-is for
    the caller to offset/normalize.

    :raises AsrError: if the payload is neither a list nor a ``{"segments": [...]}``
        object, or a contained segment is not an object -- a malformed body is a
        terminal ASR failure (no partial transcript).
    """
    if isinstance(payload, Mapping):
        segments = payload.get("segments")
    else:
        segments = payload
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise AsrError(
            "ASR_Service returned a malformed chunk response: "
            "expected a list of segments or {'segments': [...]}"
        )
    raw_segments: list[RawSegment] = []
    for seg in segments:
        if not isinstance(seg, Mapping):
            raise AsrError(
                "ASR_Service returned a malformed segment "
                f"(expected an object, got {type(seg).__name__})"
            )
        raw_segments.append(seg)
    return raw_segments


class HttpAsrClient:
    """HTTP implementation of :class:`~app.clients.base.AsrClient` (skeleton).

    Stores the ASR transport configuration from :class:`~app.config.Settings`.
    Opens no connections at construction. The real chunking/HTTP/assembly logic
    is implemented in **task 5** (:meth:`transcribe` currently raises).

    Configuration captured (so task 5 can use it directly):
      * ``endpoint_url`` -- ASR_Service URL (``settings.asr_endpoint_url``).
      * ``request_timeout_seconds`` -- per-request httpx timeout (Req 5.1).
      * ``chunk_length_seconds`` -- chunking threshold/bound (Req 5.2).
      * ``max_retries`` -- in-client retry bound (Req 5.5).

    Chunking seam (task 5.1):
      * ``duration_prober`` -- a callable ``(audio_path) -> seconds`` used by
        :meth:`plan_chunks` to discover the audio duration. Defaults to
        :func:`app.clients.chunking.probe_duration` (ffprobe). Tests and the
        worker may inject a fake so chunk planning runs without a real file or
        the ``ffprobe`` binary.
    """

    def __init__(
        self,
        settings: "Settings",
        *,
        duration_prober: Callable[[str], float] | None = None,
        chunk_transcriber: ChunkTranscriber | None = None,
        audio_slicer: AudioSlicer | None = None,
    ) -> None:
        # AnyHttpUrl | None -> str | None; task 5 requires a present URL in
        # http mode (enforced by startup validation, task 1.3).
        self.endpoint_url: str | None = (
            str(settings.asr_endpoint_url)
            if settings.asr_endpoint_url is not None
            else None
        )
        # Model id sent as the OpenAI-compatible ``model`` form field
        # (e.g. "sensevoice"); see deploy/asr-service/ASR-API.md.
        self.model_name: str = settings.asr_model_name
        self.request_timeout_seconds: int = settings.asr_request_timeout_seconds
        self.chunk_length_seconds: int = settings.asr_chunk_length_seconds
        self.max_retries: int = settings.asr_max_retries
        # Injectable duration source; default shells out to ffprobe.
        self._duration_prober: Callable[[str], float] = (
            duration_prober if duration_prober is not None else probe_duration
        )
        # Injectable per-chunk audio slicer; default cuts the window with ffmpeg.
        # The OpenAI-compatible ASR service transcribes whole files, so each
        # chunk window is sliced to its own file before upload. Tests inject a
        # fake (e.g. returning the source path) so the transport runs without
        # ffmpeg or a real audio file.
        self._audio_slicer: AudioSlicer = (
            audio_slicer if audio_slicer is not None else self._slice_chunk
        )
        # Injectable "send one chunk" seam (task 5.2). Defaults to the real
        # httpx POST in :meth:`_transcribe_chunk`; tests and future callers may
        # inject a fake so the orchestration (chunk loop, retry, assembly) runs
        # without a real ASR_Service or ffmpeg-based audio slicing.
        self._chunk_transcriber: ChunkTranscriber = (
            chunk_transcriber
            if chunk_transcriber is not None
            else self._transcribe_chunk
        )

    def plan_chunks(self, audio_path: str) -> list[tuple[float, float]]:
        """Compute the per-request ``(start, end)`` windows for ``audio_path``.

        Probes the audio duration (via the injected ``duration_prober``) and
        splits ``[0, duration]`` into sequential, non-overlapping windows of at
        most ``self.chunk_length_seconds`` (Req 5.2 / Property 10), delegating
        the math to :func:`app.clients.chunking.compute_chunks`.

        Task 5.2 consumes these windows to issue per-chunk ASR requests and to
        offset each chunk's segment timestamps by its start.

        :param audio_path: filesystem path to the Standardized_Audio.
        :returns: ordered list of ``(start_seconds, end_seconds)`` windows;
            empty when the audio has non-positive duration.
        """
        duration = self._duration_prober(audio_path)
        return compute_chunks(duration, self.chunk_length_seconds)

    def transcribe(self, audio_path: str) -> list[TranscriptSegment]:
        """Transcribe Standardized_Audio into globally-ordered segments.

        Orchestration (task 5.2):

        1. **Plan chunks** via :meth:`plan_chunks` (task 5.1). A non-positive
           duration yields no chunks, so an empty transcript ``[]`` is returned
           without any ASR call (documented in :mod:`app.clients.chunking`).
        2. **Per-chunk request with bounded retries.** Each chunk's
           ``[start, end]`` window is submitted through the injectable
           ``chunk_transcriber`` seam inside a retry loop bounded by
           ``self.max_retries`` (total attempts = ``max_retries + 1``). Transport
           errors, timeouts, and error HTTP statuses (all ``httpx.HTTPError``
           subclasses) are retried; on exhaustion an :class:`AsrError` carrying
           the failing ``chunk_index`` is raised (Req 5.5/5.6) and no partial
           transcript is returned.
        3. **Assemble.** Each returned segment's ``start``/``end`` (which are
           *chunk-relative*) are offset by the chunk's absolute start position;
           segments are collected across all chunks and the final list is sorted
           by ascending ``start`` (Req 5.3).
        4. **Speaker mapping.** A ``speaker`` label is carried only when the ASR
           response supplies one for that segment; otherwise the segment's
           ``speaker`` is ``None`` (Req 5.4 / Property 12).

        :param audio_path: filesystem path to the mono 16 kHz WAV.
        :returns: assembled segments ordered by ascending ``start``; ``[]`` for
            zero-/non-positive-duration audio.
        :raises AsrError: on terminal per-chunk failure after retries (Req 5.6).
        """
        chunks = self.plan_chunks(audio_path)
        if not chunks:
            # Zero-/non-positive-duration audio: nothing to transcribe.
            return []

        assembled: list[TranscriptSegment] = []
        for index, (chunk_start, chunk_end) in enumerate(chunks):
            raw_segments = self._transcribe_chunk_with_retries(
                audio_path, chunk_start, chunk_end, index
            )
            for raw in raw_segments:
                assembled.append(self._offset_segment(raw, chunk_start))

        # Global ordering by ascending start across all chunks (Req 5.3).
        assembled.sort(key=lambda seg: seg["start"])
        return assembled

    def _transcribe_chunk_with_retries(
        self,
        audio_path: str,
        start: float,
        end: float,
        index: int,
    ) -> Sequence[RawSegment]:
        """Call the chunk-transcriber seam, retrying up to ``max_retries`` times.

        Two failure classes are handled differently and deliberately (Req 5.5/5.6):

        * **Retryable transport failures** -- ``httpx.HTTPError`` and *all* its
          subclasses cover the three failure modes named in Req 5.5: *unreachable*
          (``httpx.ConnectError``), *timeout* (``httpx.ConnectTimeout`` /
          ``httpx.ReadTimeout`` / ``httpx.PoolTimeout`` / ``httpx.TimeoutException``),
          and *error* (``httpx.HTTPStatusError`` from ``raise_for_status`` plus
          other transport errors). These are retried up to ``self.max_retries``
          times, giving ``self.max_retries + 1`` total attempts. When every
          attempt is exhausted, a terminal :class:`AsrError` identifying ``index``
          (Req 5.6) is raised with a descriptive message (chunk index, window, and
          the underlying cause).

        * **Malformed-response / configuration failures** -- an :class:`AsrError`
          raised by the chunk transcriber itself (e.g. the malformed-body error
          from :func:`_parse_chunk_response`, or a missing endpoint URL). These are
          **terminal and NOT retried**: a malformed response body is deterministic,
          so re-issuing the same request cannot turn it into a well-formed one, and
          a misconfiguration will not fix itself. We fail fast, but still stamp the
          failing ``chunk_index`` onto the error (when not already set) so the
          failed chunk is identified per Req 5.6.

        In both cases ``transcribe`` propagates the :class:`AsrError` rather than
        returning, so no partial transcript leaks (Req 5.5). Task 5.3 verifies this
        terminal behavior thoroughly (Property 13).

        :param audio_path: path passed through to the chunk transcriber.
        :param start: chunk start (seconds, absolute in the source audio).
        :param end: chunk end (seconds, absolute in the source audio).
        :param index: 0-based chunk index, recorded on a terminal failure.
        :returns: the chunk's raw (chunk-relative) segments.
        :raises AsrError: immediately on a malformed/terminal content error, or
            once all retryable attempts are exhausted.
        """
        attempts = self.max_retries + 1
        last_exc: Exception | None = None
        for _ in range(attempts):
            try:
                return self._chunk_transcriber(audio_path, start, end)
            except AsrError as exc:
                # Malformed body or misconfiguration: deterministic, so do not
                # retry. Identify the failed chunk (Req 5.6) and fail fast.
                if exc.chunk_index is None:
                    exc.chunk_index = index
                raise
            except httpx.HTTPError as exc:
                last_exc = exc
        raise AsrError(
            f"ASR transcription failed for chunk {index} "
            f"[{start:.3f}, {end:.3f}] after {attempts} attempt(s): {last_exc}",
            chunk_index=index,
        )

    @staticmethod
    def _offset_segment(raw: RawSegment, chunk_start: float) -> TranscriptSegment:
        """Offset a chunk-relative raw segment into absolute time and normalize.

        The ASR_Service returns timestamps relative to the start of the chunk it
        received; adding ``chunk_start`` maps them back onto the original audio
        timeline (Req 5.3). The ``speaker`` field is preserved exactly when the
        response provides one and is ``None`` otherwise (Req 5.4 / Property 12).
        """
        speaker = raw.get("speaker")
        return {
            "start": float(raw["start"]) + chunk_start,
            "end": float(raw["end"]) + chunk_start,
            "text": str(raw.get("text", "")),
            "speaker": speaker if speaker is not None else None,
        }

    def _slice_chunk(self, audio_path: str, start: float, end: float) -> str:
        """Extract the ``[start, end]`` window of ``audio_path`` to a temp WAV.

        The OpenAI-compatible ASR service transcribes whole files, so each chunk
        window is cut to its own self-contained mono/16 kHz WAV via ffmpeg before
        upload. Returns the temp file path; the caller (:meth:`_transcribe_chunk`)
        removes it after the request. The source audio is only read, never
        modified.

        :raises AsrError: if ffmpeg is missing, times out, or exits non-zero --
            a deterministic (non-retryable) terminal failure for the chunk.
        """
        duration = max(0.0, float(end) - float(start))
        fd, out_path = tempfile.mkstemp(prefix="asr-chunk-", suffix=".wav")
        os.close(fd)
        cmd = [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-ss", repr(float(start)),
            "-i", audio_path,
            "-t", repr(duration),
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav",
            out_path,
        ]
        try:
            result = subprocess.run(  # noqa: S603 - argv from literals + path
                cmd,
                capture_output=True,
                text=True,
                timeout=float(self.request_timeout_seconds),
                check=False,
            )
        except FileNotFoundError as exc:
            self._remove_quietly(out_path)
            raise AsrError("ffmpeg executable not found; cannot slice audio chunk") from exc
        except subprocess.TimeoutExpired as exc:
            self._remove_quietly(out_path)
            raise AsrError(
                f"ffmpeg timed out slicing chunk [{start:.3f}, {end:.3f}]"
            ) from exc
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            self._remove_quietly(out_path)
            raise AsrError(
                f"ffmpeg failed slicing chunk [{start:.3f}, {end:.3f}]: {stderr}"
            )
        return out_path

    @staticmethod
    def _remove_quietly(path: str) -> None:
        """Best-effort delete of a temp slice; never raises."""
        try:
            os.remove(path)
        except OSError:
            pass

    def _transcribe_chunk(
        self, audio_path: str, start: float, end: float
    ) -> Sequence[RawSegment]:
        """Send one chunk window to the OpenAI-compatible ASR endpoint.

        **Request/response contract** (DGX Spark FunASR / SenseVoice; see
        ``deploy/asr-service/ASR-API.md``):

        * **Slice first.** The ``[start, end]`` window is cut to its own temp WAV
          via the injectable ``self._audio_slicer`` (default ffmpeg). The service
          transcribes whole files, so chunking is realized by sending each window
          as a separate file; the chunk-relative timestamps it returns are offset
          by ``start`` during assembly (Req 5.2 / 5.3).
        * **Request:** ``POST`` ``multipart/form-data`` to
          ``{endpoint}/v1/audio/transcriptions`` (resolved by
          :func:`_transcriptions_url`, so a base URL like
          ``http://192.0.2.20:19100`` works) with:

          - file part ``file`` -- the sliced WAV;
          - form field ``model`` -- ``self.model_name`` (e.g. ``sensevoice``);
          - form field ``response_format`` -- ``verbose_json`` (so the response
            carries per-segment timestamps).

          Per-request timeout is ``self.request_timeout_seconds`` via
          ``httpx.Timeout`` (Req 5.1); a fresh ``httpx.Client`` per call, no
          connection held at construction.
        * **Response:** JSON ``{..., "segments": [{start, end, text, ...}]}`` (or
          a bare list), parsed by :func:`_parse_chunk_response`. SenseVoice does
          not emit ``speaker``, so segments map to ``speaker=None`` (Req 5.4);
          a future diarizing model that includes ``speaker`` is carried through.

        :raises httpx.HTTPError: transport failure, timeout, or error status
            (drives the retry loop in :meth:`_transcribe_chunk_with_retries`).
        :raises AsrError: if no endpoint URL is configured, or the slice fails.
        """
        if self.endpoint_url is None:
            # http mode without a URL is prevented by startup validation
            # (task 1.3); guard defensively so a misconfiguration is a clear,
            # terminal error rather than an httpx crash.
            raise AsrError(
                "ASR endpoint URL is not configured; cannot transcribe over HTTP"
            )

        url = _transcriptions_url(self.endpoint_url)
        timeout = httpx.Timeout(float(self.request_timeout_seconds))
        chunk_path = self._audio_slicer(audio_path, start, end)
        try:
            with open(chunk_path, "rb") as fh:
                files = {"file": (os.path.basename(chunk_path), fh, "audio/wav")}
                data = {"model": self.model_name, "response_format": "verbose_json"}
                with httpx.Client(timeout=timeout) as client:
                    response = client.post(url, files=files, data=data)
            response.raise_for_status()
            payload = response.json()
            # Adaptation for OpenAI-compatible ASR services that return the full
            # transcription in the top-level "text" field with an EMPTY (or
            # absent) "segments" array -- e.g. the DGX FunASR/SenseVoice build,
            # which is non-autoregressive and emits no per-segment timestamps.
            # Without this fallback a perfectly good transcript would be dropped
            # (segments=[] -> empty Transcript). Synthesize one chunk-relative
            # segment spanning the whole window; assembly then offsets it onto
            # the absolute timeline (Req 5.3). A response that DOES carry
            # segments, or a bare list, falls through to the normal parser.
            if isinstance(payload, Mapping) and not payload.get("segments"):
                text = payload.get("text")
                if isinstance(text, str) and text.strip():
                    return [
                        {
                            "start": 0.0,
                            "end": max(0.0, float(end) - float(start)),
                            "text": text,
                        }
                    ]
            return _parse_chunk_response(payload)
        finally:
            # Remove the per-chunk temp slice, but never the caller's source
            # (an injected slicer may return the source path unchanged).
            if chunk_path != audio_path:
                self._remove_quietly(chunk_path)


# --------------------------------------------------------------------------- #
# LLM minutes schema validation / normalization (task 6.2)
# --------------------------------------------------------------------------- #
# Scalar JSON types that the parser is willing to coerce to ``str`` for list
# entries and action-item descriptions. ``bool`` is a subclass of ``int`` and is
# included implicitly; ``None`` and containers (list / dict) are deliberately
# excluded so a clearly wrong shape becomes a schema error rather than a silent
# ``"None"`` / ``"[]"`` string.
_SCALAR_TYPES = (str, int, float)


def _coerce_scalar_to_str(value: Any) -> str | None:
    """Coerce a JSON scalar to ``str``; return ``None`` for non-scalar shapes.

    A non-``None`` return is the normalized string. ``None`` signals that the
    value is not a coercible scalar (it was ``None`` itself or a container), so
    the caller can reject it as a schema violation. ``str`` passes through
    unchanged so Chinese text round-trips byte-for-byte (Req 6.4).
    """
    if isinstance(value, str):
        return value
    # bool is a subclass of int -> handled by the int/float branch via str().
    if isinstance(value, _SCALAR_TYPES):
        return str(value)
    return None


def _strip_code_fences(content: str) -> str:
    """Remove a Markdown code-fence wrapper from model output, if present.

    Despite the system prompt forbidding it, some models still wrap their JSON
    in a ```` ```json ... ``` ```` (or bare ```` ``` ... ``` ````) fence. This
    strips a single leading fence line (optionally tagged, e.g. ``json``) and a
    matching trailing fence so the inner JSON parses. Content with no fence is
    returned trimmed and otherwise unchanged. Never raises.
    """
    text = content.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    lines = lines[1:]  # drop the opening ``` / ```json line
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]  # drop the closing fence
    return "\n".join(lines).strip()


def _validate_summary(data: Mapping[str, Any]) -> str:
    """Validate/normalize the ``summary`` field (Req 6.2).

    Rules:
      * absent or explicit ``null`` -> ``""`` (always a ``str``).
      * present and a ``str`` -> used verbatim (Chinese preserved, Req 6.4).
      * present and any other shape (number, bool, list, object) ->
        :class:`LlmSchemaError`.
    """
    if "summary" not in data:
        return ""
    value = data["summary"]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LlmSchemaError(
            "LLM minutes field 'summary' must be a string "
            f"(got {type(value).__name__})"
        )
    return value


def _validate_str_list(data: Mapping[str, Any], key: str) -> list[str]:
    """Validate/normalize a string-list field (``topics`` / ``decisions``).

    Rules (Req 6.2):
      * absent or explicit ``null`` -> ``[]`` (never ``None``/missing).
      * present but not a JSON list -> :class:`LlmSchemaError`.
      * each entry is coerced to ``str`` when it is a scalar; a non-scalar entry
        (``null``, list, object) is a :class:`LlmSchemaError` (a clearly wrong
        shape, not something to silently stringify).
    """
    if key not in data:
        return []
    value = data[key]
    if value is None:
        return []
    if not isinstance(value, list):
        raise LlmSchemaError(
            f"LLM minutes field '{key}' must be a list "
            f"(got {type(value).__name__})"
        )
    result: list[str] = []
    for entry in value:
        coerced = _coerce_scalar_to_str(entry)
        if coerced is None:
            raise LlmSchemaError(
                f"LLM minutes field '{key}' contained a non-scalar entry "
                f"(got {type(entry).__name__})"
            )
        result.append(coerced)
    return result


def _normalize_owner(value: Any) -> str:
    """Normalize an action item's owner, defaulting to the unassigned indicator.

    The owner is the *lenient* field (Req 6.3): rather than reject odd shapes it
    falls back to :data:`app.enums.UNASSIGNED_OWNER` so every action item always
    carries a non-empty owner string. Specifically:
      * absent / ``null`` / non-scalar (list, object) -> ``UNASSIGNED_OWNER``.
      * a scalar that is empty or whitespace-only -> ``UNASSIGNED_OWNER``.
      * otherwise the (whitespace-trimmed) scalar string.
    """
    coerced = _coerce_scalar_to_str(value)
    if coerced is None:
        return UNASSIGNED_OWNER
    coerced = coerced.strip()
    return coerced or UNASSIGNED_OWNER


def _validate_action_items(data: Mapping[str, Any]) -> list[ActionItemData]:
    """Validate/normalize the ``action_items`` field (Req 6.2 / 6.3).

    Rules:
      * absent or explicit ``null`` -> ``[]``.
      * present but not a JSON list -> :class:`LlmSchemaError`.
      * each entry MUST be a JSON object; a non-object entry is a
        :class:`LlmSchemaError` (the first-cut parse silently skipped these --
        hardened here to a schema error per Req 6.6 intent).
      * each entry MUST carry a ``description`` that is a coercible scalar; an
        entry missing ``description`` (or with a non-scalar description) is a
        :class:`LlmSchemaError`.
      * ``owner`` is normalized via :func:`_normalize_owner` so it is always a
        non-empty string, defaulting to ``UNASSIGNED_OWNER`` when absent/empty.
    """
    if "action_items" not in data:
        return []
    value = data["action_items"]
    if value is None:
        return []
    if not isinstance(value, list):
        raise LlmSchemaError(
            "LLM minutes field 'action_items' must be a list "
            f"(got {type(value).__name__})"
        )
    items: list[ActionItemData] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise LlmSchemaError(
                "LLM minutes 'action_items' entry must be an object "
                f"(got {type(entry).__name__})"
            )
        if "description" not in entry:
            raise LlmSchemaError(
                "LLM minutes 'action_items' entry is missing 'description'"
            )
        description = _coerce_scalar_to_str(entry["description"])
        if description is None:
            raise LlmSchemaError(
                "LLM minutes 'action_items' entry 'description' must be a string "
                f"(got {type(entry['description']).__name__})"
            )
        owner = _normalize_owner(entry.get("owner"))
        items.append({"description": description, "owner": owner})
    return items


class HttpLlmClient:
    """HTTP implementation of :class:`~app.clients.base.LlmClient`.

    Stores the LLM transport configuration from :class:`~app.config.Settings`.
    Opens no connections at construction. **Task 6.1** implements the
    OpenAI-compatible chat call and the request/response transport in
    :meth:`generate_minutes`; schema parsing/validation hardening is **task 6.2**
    and the full failure taxonomy is **task 6.3**.

    Configuration captured:
      * ``endpoint_url`` -- OpenAI-compatible chat/completions URL
        (``settings.llm_endpoint_url``).
      * ``model_name`` -- model identifier (``settings.llm_model_name``).
      * ``request_timeout_seconds`` -- request httpx timeout (Req 6.1).
    """

    def __init__(self, settings: "Settings") -> None:
        self.endpoint_url: str | None = (
            str(settings.llm_endpoint_url)
            if settings.llm_endpoint_url is not None
            else None
        )
        self.model_name: str | None = settings.llm_model_name
        # Optional bearer token. Sent as ``Authorization: Bearer <key>`` only
        # when configured -- required by hosted providers (e.g. DeepSeek), unset
        # for a local unauthenticated LLM (Ollama / vLLM).
        self.api_key: str | None = settings.llm_api_key
        self.request_timeout_seconds: int = settings.llm_request_timeout_seconds
        # Qwen3 thinking control (see Settings.llm_thinking). Decides whether to
        # inject ``chat_template_kwargs.enable_thinking=false`` to suppress
        # chain-of-thought for speed on correction calls.
        self.llm_thinking: str = settings.llm_thinking

    def generate_minutes(self, transcript: TranscriptInput) -> MeetingMinutesData:
        """Generate structured minutes via an OpenAI-compatible chat call.

        Scope (task 6.1 -- the transport/request half):

        1. **Render** the transcript segments into a single prompt string via
           :meth:`_format_transcript` (speaker labels included when present).
        2. **Request** the OpenAI-compatible Chat Completions endpoint at
           ``self.endpoint_url`` with ``self.model_name``, a Chinese system
           prompt (:data:`LLM_SYSTEM_PROMPT`) plus the rendered transcript as the
           user message, within ``self.request_timeout_seconds`` applied via
           ``httpx.Timeout`` (Req 6.1). A fresh ``httpx.Client`` is created per
           call and closed immediately; no connection is held at construction
           (tests monkeypatch ``httpx.Client`` with ``httpx.MockTransport``).
        3. **Extract** the assistant message content from the OpenAI-compatible
           response (``choices[0].message.content``) via :meth:`_extract_content`.
        4. **Parse** that content as JSON and build a :class:`MeetingMinutesData`
           via :meth:`_parse_minutes` -- a *first-cut* parse only.

        .. note:: **Failure taxonomy (finalized in task 6.3).**
           * **Transport / timeout / error status / malformed envelope** ->
             :class:`LlmError` (Req 6.5), raised by :meth:`_call_llm`.
           * **Non-conforming content** (missing ``choices[0].message.content``,
             content not a string, content not valid JSON, content not a JSON
             object, or schema-invalid fields) -> :class:`LlmSchemaError`
             (Req 6.6), raised by :meth:`_extract_content` / :meth:`_parse_minutes`.
           Because ``LlmSchemaError`` subclasses ``LlmError``, the worker can
           catch ``LlmError`` broadly. **No partial result** is ever returned:
           every failure path raises before a :class:`MeetingMinutesData` is
           built, so the worker persists nothing partial.

        :param transcript: assembled :class:`TranscriptInput` (ordered segments
            plus optional ``language`` hint).
        :returns: validated :class:`MeetingMinutesData` parsed from the model.
        :raises LlmError: on transport failure, timeout, error status, or a
            non-JSON response envelope (Req 6.5).
        :raises LlmSchemaError: when the returned content does not conform to the
            expected minutes schema (Req 6.6).
        """
        content = self._call_llm(self._format_transcript(transcript))
        return self._parse_minutes(content)

    def correct_text(self, text: str, *, context: str | None = None) -> str:
        """Post-ASR phonetic correction for a selected text span.

        Sends the text to the LLM with a dedicated correction prompt
        (:data:`LLM_CORRECTION_PROMPT`) and returns the corrected text.
        The correction is focused: only homophone/phonetic errors and
        word-order issues are fixed; meaning and style are preserved.

        :param text: the raw ASR transcript text to correct.
        :param context: optional surrounding context (title, adjacent
            segments) to help the LLM disambiguate homophones and infer
            proper nouns.
        :returns: the corrected text as a plain string.
        :raises LlmError: on transport failure or timeout.
        """
        user_content = (
            f"上下文：\n{context}\n\n待纠错文本：\n{text}"
            if context
            else text
        )
        correction_body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": LLM_CORRECTION_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
            "max_tokens": 65536,
            "stream": False,
        }
        correction_body.update(self._thinking_extra(is_correction=True))
        url = _chat_completions_url(self.endpoint_url)
        headers = (
            {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        )
        timeout = httpx.Timeout(float(self.request_timeout_seconds))
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=correction_body, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LlmError(f"LLM correction request failed: {exc}") from exc
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LlmError(
                f"LLM correction response was not valid JSON: {exc}"
            ) from exc
        return self._extract_content(payload)

    def correct_batch(
        self, segments: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Post-ASR phonetic correction for a batch of segments.

        Groups ``segments`` into windows of ``BATCH_WINDOW_SIZE`` with an
        overlap of ``BATCH_WINDOW_OVERLAP`` segments for cross-window
        context continuity. Each window is sent to the LLM as one request;
        results are collected by index and returned in order.

        :param segments: ordered list of ``{index, text, speaker?}`` dicts.
        :returns: list of ``{index, text}`` dicts, one per input segment.
        :raises LlmError: on transport failure, timeout, or malformed response.
        """
        if not segments:
            return []

        # Partition into overlapping windows.
        windows = self._partition_windows(segments)
        results: dict[int, str] = {}

        for win_idx, window in enumerate(windows):
            prompt = self._format_batch_prompt(window)
            response_text = self._call_batch_correction(prompt)
            parsed = self._parse_batch_response(response_text, window, win_idx)
            for item in parsed:
                idx = item["index"]
                # First window's result wins for overlapped segments (they appear
                # with full context in the earlier window).
                if idx not in results:
                    results[idx] = item["text"]

        # Return results in original index order.
        return [
            {"index": i, "text": results.get(i, str(seg.get("text", "")))}
            for i, seg in enumerate(segments)
        ]

    # ----------------------------------------------------------------- #
    # Batch-correction helpers
    # ----------------------------------------------------------------- #
    @staticmethod
    def _partition_windows(
        segments: list[dict[str, object]],
    ) -> list[list[dict[str, object]]]:
        """Partition segments into overlapping windows.

        Windows are ``BATCH_WINDOW_SIZE`` segments each, with
        ``BATCH_WINDOW_OVERLAP`` segments carried over to tie context
        across window boundaries.
        """
        if len(segments) <= BATCH_WINDOW_SIZE:
            return [segments]

        windows: list[list[dict[str, object]]] = []
        i = 0
        while i < len(segments):
            end = min(i + BATCH_WINDOW_SIZE, len(segments))
            windows.append(segments[i:end])
            if end >= len(segments):
                break
            i = end - BATCH_WINDOW_OVERLAP
        return windows

    @staticmethod
    def _format_batch_prompt(segments: list[dict[str, object]]) -> str:
        """Render a window of segments into a numbered prompt for the LLM.

        Each line: ``[索引] 说话人：文本`` or ``[索引] 文本`` when no speaker.
        """
        lines: list[str] = []
        for seg in segments:
            idx = seg.get("index", 0)
            text = str(seg.get("text", ""))
            speaker = seg.get("speaker")
            if speaker and str(speaker):
                lines.append(f"[{idx}] {speaker}：{text}")
            else:
                lines.append(f"[{idx}] {text}")
        return "\n".join(lines)

    def _call_batch_correction(self, prompt: str) -> str:
        """Send a windowed batch-correction prompt to the LLM.

        Uses the same transport infra as ``correct_text`` but with the
        batch system prompt and a slightly higher temperature for variety
        across windows.
        """
        if self.endpoint_url is None or not self.model_name:
            raise LlmError(
                "LLM endpoint URL and model name must be configured for "
                "batch correction over HTTP"
            )
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": LLM_BATCH_CORRECTION_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 65536,
            "stream": False,
        }
        body.update(self._thinking_extra(is_correction=True))
        url = _chat_completions_url(self.endpoint_url)
        headers = (
            {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        )
        timeout = httpx.Timeout(float(self.request_timeout_seconds))
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=body, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LlmError(f"LLM batch correction request failed: {exc}") from exc
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LlmError(
                f"LLM batch correction response was not valid JSON: {exc}"
            ) from exc
        return self._extract_content(payload)

    @staticmethod
    def _parse_batch_response(
        raw: str,
        window: list[dict[str, object]],
        win_idx: int,
    ) -> list[dict[str, object]]:
        """Parse and validate the LLM's batch-correction JSON response.

        :param raw: the raw assistant message content (should be a JSON
            array of ``{index, text}`` objects).
        :param window: the input window segments (for fallback / validation).
        :param win_idx: window index (for error messages).
        :returns: validated list of ``{index, text}`` dicts.
        :raises LlmError: if the response is not parseable or is misaligned.
        """
        try:
            data = json.loads(_strip_code_fences(raw))
        except (json.JSONDecodeError, TypeError) as exc:
            raise LlmError(
                f"LLM batch correction window {win_idx} returned non-JSON: {exc}"
            ) from exc

        if not isinstance(data, list):
            raise LlmError(
                f"LLM batch correction window {win_idx} returned "
                f"{type(data).__name__} instead of a JSON array"
            )

        result: list[dict[str, object]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("index")
            text = entry.get("text")
            if idx is not None and isinstance(text, str):
                result.append({"index": int(idx), "text": text})

        if not result:
            raise LlmError(
                f"LLM batch correction window {win_idx} returned no "
                "valid {index, text} entries"
            )

        return result

    # ----------------------------------------------------------------- #
    # Request construction / transport (task 6.1)
    # ----------------------------------------------------------------- #
    @staticmethod
    def _format_transcript(transcript: TranscriptInput) -> str:
        """Render transcript segments into a single prompt string.

        Each segment is rendered on its own line. When a segment carries a
        ``speaker`` label it is prefixed (``发言人A: 文本``) so the model can use
        speaker attribution; segments without a speaker render their text alone.
        Timestamps are intentionally omitted from the prompt to keep it focused
        on content (they are preserved on the persisted transcript regardless).

        An empty transcript renders to an empty string; the caller still issues
        the request so the model can return empty minutes sections.
        """
        lines: list[str] = []
        for seg in transcript.get("segments", []):
            text = str(seg.get("text", "")).strip()
            if not text:
                continue
            speaker = seg.get("speaker")
            if speaker:
                lines.append(f"{speaker}: {text}")
            else:
                lines.append(text)
        return "\n".join(lines)

    def _thinking_extra(self, *, is_correction: bool) -> dict[str, Any]:
        """Extra request fields to disable chain-of-thought, when configured.

        Qwen3-style chat templates (the local llama.cpp llama-server) honor
        ``chat_template_kwargs.enable_thinking``. Thinking helps minutes-
        extraction quality but is pure overhead for post-ASR phonetic
        correction (measured ~5x faster with identical output). Honors
        :attr:`llm_thinking`:

        * ``"off"``: disable thinking on every call.
        * ``"auto"``: disable thinking on correction calls only (extraction
          keeps it for quality).
        * ``"on"``: never disable (model default = thinking on).

        A field is emitted ONLY when disabling thinking; the model default
        (thinking on) needs no parameter, which keeps the body clean for
        providers that don't recognize it (e.g. DeepSeek ignores the field).
        """
        if self.llm_thinking == "off" or (
            self.llm_thinking == "auto" and is_correction
        ):
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return {}

    def _build_request_body(self, transcript_text: str) -> dict[str, Any]:
        """Build the OpenAI-compatible Chat Completions request payload.

        Sends the model name, a system message carrying the Chinese
        structured-JSON instructions (:data:`LLM_SYSTEM_PROMPT`), and the
        rendered transcript as the user message. ``temperature`` is pinned low
        for more deterministic, extraction-style output. ``stream`` is false so
        the whole completion arrives in one response body. Thinking is left on
        for extraction unless ``llm_thinking == "off"`` (see
        :meth:`_thinking_extra`).
        """
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user", "content": transcript_text},
            ],
            "temperature": 0.2,
            "max_tokens": 65536,
            "stream": False,
        }
        body.update(self._thinking_extra(is_correction=False))
        return body

    def _call_llm(self, transcript_text: str) -> str:
        """POST the chat request and return the assistant message content.

        Creates a fresh ``httpx.Client`` with a ``httpx.Timeout`` of
        ``self.request_timeout_seconds`` (Req 6.1), POSTs the request body to
        ``self.endpoint_url`` (treated as the full chat/completions URL), and
        returns the extracted assistant text.

        **Failure taxonomy (task 6.3).** This method owns the *transport*
        boundary, so every transport-level failure becomes an
        :class:`~app.clients.base.LlmError` (Req 6.5):

        * **Unreachable** -- ``httpx.ConnectError`` and other connection errors.
        * **Timeout** -- ``httpx.ConnectTimeout`` / ``httpx.ReadTimeout`` /
          ``httpx.WriteTimeout`` / ``httpx.PoolTimeout`` (all
          ``httpx.TimeoutException`` subclasses), covering the per-request
          ``llm_request_timeout_seconds`` budget (Req 6.1/6.5).
        * **Error status** -- ``httpx.HTTPStatusError`` raised by
          ``raise_for_status`` for 4xx/5xx responses.
        * **Other transport errors** -- any remaining ``httpx.HTTPError`` subclass.
        * **Malformed envelope** -- the *transport* response body is not valid
          JSON, so ``response.json()`` raises ``json.JSONDecodeError``. This is a
          protocol/transport-level defect (the server did not return a JSON
          envelope at all), so it is deliberately mapped to :class:`LlmError`,
          **not** :class:`LlmSchemaError`. ``LlmSchemaError`` is reserved for the
          case where a well-formed OpenAI envelope is returned but the assistant
          *message content* does not conform to the minutes schema
          (handled in :meth:`_extract_content` / :meth:`_parse_minutes`).

        Because ``LlmSchemaError`` subclasses ``LlmError``, the worker (task 9)
        can catch ``LlmError`` broadly to fail the job, and on no failure path
        does this method return a partial result -- it either returns the fully
        extracted assistant content or raises.

        :raises LlmError: when no endpoint URL / model name is configured, on any
            ``httpx`` transport error / timeout / non-success HTTP status, or when
            the response envelope is not decodable JSON (Req 6.5).
        :raises LlmSchemaError: when the (well-formed) envelope lacks a string
            ``choices[0].message.content`` (Req 6.6).
        """
        if self.endpoint_url is None or not self.model_name:
            # http mode without URL/model is prevented by startup validation
            # (task 1.3); guard defensively for a clear terminal error.
            raise LlmError(
                "LLM endpoint URL and model name must be configured to call "
                "the LLM_Service over HTTP"
            )

        timeout = httpx.Timeout(float(self.request_timeout_seconds))
        body = self._build_request_body(transcript_text)
        # Resolve to the chat-completions URL (accepts a base like
        # ``https://api.deepseek.com`` or ``.../v1``) and attach the bearer
        # token when one is configured (required by hosted providers such as
        # DeepSeek; omitted for a local unauthenticated LLM).
        url = _chat_completions_url(self.endpoint_url)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=body, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Transport failure / timeout / error status (Req 6.5). All httpx
            # errors -- ConnectError, the TimeoutException family, and
            # HTTPStatusError from raise_for_status -- funnel here.
            raise LlmError(f"LLM request failed: {exc}") from exc

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            # The transport succeeded (2xx) but the *envelope* body is not JSON.
            # Treated as a protocol/transport-level malformation -> LlmError
            # (distinct from a schema-invalid assistant message, which is an
            # LlmSchemaError). See the method docstring for the rationale.
            raise LlmError(
                f"LLM response envelope was not valid JSON: {exc}"
            ) from exc

        return self._extract_content(payload)

    @staticmethod
    def _extract_content(payload: Any) -> str:
        """Pull ``choices[0].message.content`` from an OpenAI-compatible body.

        :raises LlmSchemaError: if the envelope does not contain a string
            assistant message at ``choices[0].message.content`` (Req 6.6,
            scaffolded; task 6.3 hardens this).
        """
        try:
            choices = payload["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmSchemaError(
                "LLM response did not contain choices[0].message.content"
            ) from exc
        if not isinstance(content, str):
            raise LlmSchemaError(
                "LLM response message content was not a string"
            )
        return content

    @staticmethod
    def _parse_minutes(content: str) -> MeetingMinutesData:
        """Parse and strictly validate the model's JSON content into minutes.

        Hardened in **task 6.2** from the first-cut defensive parse into strict
        schema validation plus the full Req 6.2 / 6.3 normalization semantics.

        Decoding / top-level shape (a non-conforming variant -> ``LlmSchemaError``,
        Req 6.6):
          * ``content`` MUST decode as JSON, else :class:`LlmSchemaError`.
          * the decoded value MUST be a JSON object, else :class:`LlmSchemaError`.

        Field normalization (delegated to the module-level validators):
          * **summary** (:func:`_validate_summary`) -- always a ``str``; absent /
            ``null`` -> ``""``; a present non-string is a schema error.
          * **topics** / **decisions** (:func:`_validate_str_list`) -- always a
            ``list[str]``; absent / ``null`` -> ``[]``; present-but-not-a-list is
            a schema error; scalar entries are coerced to ``str`` and a
            non-scalar entry is a schema error.
          * **action_items** (:func:`_validate_action_items`) -- always a list of
            ``{description, owner}`` objects; absent / ``null`` -> ``[]``;
            present-but-not-a-list, a non-object entry, or an entry missing
            ``description`` is a schema error; each ``owner`` defaults to
            :data:`app.enums.UNASSIGNED_OWNER` when absent/empty so every action
            item carries a non-empty owner (Req 6.3).

        Language is never transformed here: a conforming Chinese response yields
        Chinese minutes unchanged (Req 6.4). The output language is driven by the
        Chinese system prompt (:data:`LLM_SYSTEM_PROMPT`, task 6.1).

        :raises LlmSchemaError: when ``content`` is not valid JSON, is not a JSON
            object, or does not conform to the structured schema (Req 6.6). Task
            6.3 finalizes how the worker maps this to ``Job_Status = failed``.
        """
        try:
            data = json.loads(_strip_code_fences(content))
        except (json.JSONDecodeError, TypeError) as exc:
            raise LlmSchemaError(
                "LLM content was not valid JSON"
            ) from exc
        if not isinstance(data, Mapping):
            raise LlmSchemaError("LLM content was not a JSON object")

        return {
            "summary": _validate_summary(data),
            "topics": _validate_str_list(data, "topics"),
            "decisions": _validate_str_list(data, "decisions"),
            "action_items": _validate_action_items(data),
        }
