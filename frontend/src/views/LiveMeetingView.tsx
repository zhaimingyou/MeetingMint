import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  LiveSocket,
  applyResult,
  emptyPreview,
  previewSegments,
  startLiveMeeting,
} from "../api/live";
import type { PreviewState } from "../api/live";
import { formatDuration } from "../utils/duration";

const MSG_NO_DEVICE = "未找到音频输入设备";
const MSG_PERMISSION = "需要麦克风权限才能开始会议";
const MSG_UNSUPPORTED = "当前浏览器不支持录音功能";
const MSG_START_FAILED = "无法开始会议，请稍后重试";
const MSG_RECORDER_FAILED = "无法启动录音，建议使用 Chrome 浏览器重试";
const MSG_GIVE_UP = "与服务器的连接中断，请重新开始会议";
const MSG_RECORDER_ERROR = "录音过程出现错误，请重新开始会议";

type LiveState = "idle" | "starting" | "live" | "ending";

const PREFERRED_MIME_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4;codecs=mp4a.40.2",
  "audio/mp4",
];

/** Send one keepalive every N seconds so a silent mic still proves liveness. */
const KEEPALIVE_EVERY_SECONDS = 15;

function pickRecorderMimeType(): string | undefined {
  if (
    typeof MediaRecorder === "undefined" ||
    typeof MediaRecorder.isTypeSupported !== "function"
  ) {
    return undefined; // let the browser pick its default container
  }
  for (const t of PREFERRED_MIME_TYPES) {
    if (MediaRecorder.isTypeSupported(t)) return t;
  }
  // Nothing declared supported (e.g. Safari 17 does not offer audio/webm):
  // omit the option entirely so the constructor uses the browser's default
  // container instead of throwing NotSupportedError.
  return undefined;
}

export function LiveMeetingView() {
  const navigate = useNavigate();

  const [state, setState] = useState<LiveState>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<PreviewState>(emptyPreview());
  const [asrUnavailable, setAsrUnavailable] = useState(false);
  const [authExpired, setAuthExpired] = useState(false);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const socketRef = useRef<LiveSocket | null>(null);
  const timerRef = useRef<number | null>(null);
  const meetingIdRef = useRef<string | null>(null);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const stopStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  }, []);

  // Clean up everything on unmount.
  useEffect(() => {
    return () => {
      clearTimer();
      stopStream();
      socketRef.current?.close();
    };
  }, [clearTimer, stopStream]);

  const handleStart = useCallback(async () => {
    setError(null);
    setAuthExpired(false);

    const media = navigator.mediaDevices;
    if (!media || typeof media.getUserMedia !== "function") {
      setError(MSG_UNSUPPORTED);
      return;
    }
    // Req 2.5: refuse to start when there is no audio input device.
    if (typeof media.enumerateDevices === "function") {
      try {
        const devices = await media.enumerateDevices();
        if (!devices.some((d) => d.kind === "audioinput")) {
          setError(MSG_NO_DEVICE);
          return;
        }
      } catch {
        /* fall through to getUserMedia for a clearer error */
      }
    }
    // Req 2.4 / 2.5: surface permission/device errors without starting.
    let stream: MediaStream;
    try {
      stream = await media.getUserMedia({ audio: true });
    } catch (err) {
      const name = (err as { name?: string } | null)?.name;
      if (name === "NotAllowedError" || name === "PermissionDeniedError") {
        setError(MSG_PERMISSION);
      } else if (name === "NotFoundError" || name === "DevicesNotFoundError") {
        setError(MSG_NO_DEVICE);
      } else {
        setError("无法访问麦克风，请检查设备后重试");
      }
      return;
    }

    setState("starting");
    streamRef.current = stream;

    // Create the live meeting, then open the Streaming_Connection.
    let started;
    try {
      started = await startLiveMeeting();
    } catch (err) {
      stopStream();
      setState("idle");
      const reason = err instanceof Error ? err.message : "";
      setError(reason ? `${MSG_START_FAILED}（${reason}）` : MSG_START_FAILED);
      return;
    }

    meetingIdRef.current = started.id;
    setPreview(emptyPreview());
    setElapsed(0);

    // FIX 6: start the MediaRecorder ONLY after the socket opens so sequence 0
    // (the container header) is the first increment captured and sent.
    const startRecorder = () => {
      if (recorderRef.current || !streamRef.current) return;
      let recorder: MediaRecorder;
      try {
        const mimeType = pickRecorderMimeType();
        recorder =
          mimeType === undefined
            ? new MediaRecorder(streamRef.current)
            : new MediaRecorder(streamRef.current, { mimeType });
      } catch (err) {
        // e.g. NotSupportedError: surface it instead of leaving the user with
        // a silent, never-recording session that dies 5 minutes later.
        const reason = err instanceof Error ? err.message : String(err);
        socketRef.current?.close();
        stopStream();
        recorderRef.current = null;
        setState("idle");
        setError(`${MSG_RECORDER_FAILED}（${reason}）`);
        return;
      }
      recorder.ondataavailable = (event: BlobEvent) => {
        if (event.data && event.data.size > 0) {
          // Convert to bytes and hand to the socket (assigns seq + buffers).
          event.data
            .arrayBuffer()
            .then((buf) => socketRef.current?.pushChunk(buf))
            .catch(() => {
              /* ignore a single failed chunk read */
            });
        }
      };
      recorder.onstop = () => {
        // Defer end past the final ondataavailable's async read so the last
        // increment is buffered/sent before the socket closes.
        window.setTimeout(() => {
          socketRef.current?.end();
          const id = meetingIdRef.current;
          if (id) navigate(`/meetings/${id}`);
        }, 0);
      };
      recorder.onerror = () => {
        setError(MSG_RECORDER_ERROR);
      };
      recorderRef.current = recorder;
      recorder.start(Math.round(started.send_interval * 1000));
      setState("live");
      // Req 2.7: elapsed timer updated at least once per second. Every
      // KEEPALIVE_EVERY_SECONDS ticks also carry a keepalive frame so the
      // server sees activity even when nobody is speaking (otherwise a
      // silent meeting is swept as "no audio captured").
      let ticks = 0;
      timerRef.current = window.setInterval(() => {
        ticks += 1;
        setElapsed((s) => s + 1);
        if (ticks % KEEPALIVE_EVERY_SECONDS === 0) {
          socketRef.current?.keepalive();
        }
      }, 1000);
    };

    const socket = new LiveSocket(started.id, Math.round(started.reconnect_interval * 1000), {
      onOpen: startRecorder,
      onTranscript: (result) => setPreview((p) => applyResult(p, result)),
      onAsrUnavailable: () => setAsrUnavailable(true),
      onAsrAvailable: () => setAsrUnavailable(false),
      onAuthExpired: () => setAuthExpired(true),
      onGiveUp: () => {
        recorderRef.current = null;
        clearTimer();
        stopStream();
        setState("idle");
        setError(MSG_GIVE_UP);
      },
    });
    socketRef.current = socket;
    socket.connect();
  }, [navigate, stopStream, clearTimer]);

  const handleEnd = useCallback(() => {
    setState("ending");
    clearTimer();
    stopStream();
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop(); // onstop flushes the last chunk, sends end, navigates
    } else {
      socketRef.current?.end();
      const id = meetingIdRef.current;
      if (id) navigate(`/meetings/${id}`);
    }
  }, [clearTimer, stopStream, navigate]);

  const segments = previewSegments(preview);

  return (
    <div>
      <h1 className="page-title">实时会议</h1>
      <div className="card">
        {error && (
          <div className="alert alert--error" role="alert">
            {error}
          </div>
        )}
        {authExpired && (
          <div className="alert alert--error" role="alert">
            会话已过期。已保留本地音频，请{" "}
            <Link to="/login">重新登录</Link> 后继续。
          </div>
        )}

        <div className="recorder-clock" data-testid="live-clock">
          {formatDuration(elapsed)}
        </div>

        {state === "idle" && (
          <div className="center-actions">
            <button
              type="button"
              className="btn btn--primary btn--lg"
              onClick={handleStart}
            >
              开始会议
            </button>
          </div>
        )}

        {(state === "live" || state === "ending" || state === "starting") && (
          <>
            <div className="live-indicators">
              <span className="pill pill--processing" data-testid="preview-notice">
                预览，非最终记录
              </span>
              {asrUnavailable && (
                <span
                  className="pill pill--failed"
                  data-testid="asr-unavailable"
                >
                  实时转写暂不可用
                </span>
              )}
            </div>

            <div className="live-transcript" data-testid="live-preview">
              {segments.length === 0 && (
                <p className="muted">等待语音…</p>
              )}
              {segments.map((seg, i) => (
                <span
                  key={`${seg.is_final ? "f" : "p"}-${i}-${seg.start}`}
                  className={
                    seg.is_final ? "live-seg live-seg--final" : "live-seg live-seg--partial"
                  }
                  data-final={seg.is_final ? "true" : "false"}
                >
                  {seg.speaker ? `${seg.speaker}：` : ""}
                  {seg.text}{" "}
                </span>
              ))}
            </div>

            <div className="center-actions">
              <button
                type="button"
                className="btn btn--danger btn--lg"
                onClick={handleEnd}
                disabled={state === "ending"}
              >
                {state === "ending" ? "正在结束…" : "结束会议"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
