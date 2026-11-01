import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { createMeeting } from "../api/meetings";
import { formatDuration } from "../utils/duration";

const MSG_NO_DEVICE = "未找到音频输入设备";
const MSG_PERMISSION = "需要麦克风权限才能录音";
const MSG_UNSUPPORTED = "当前浏览器不支持录音功能";
const MSG_SUBMIT_FAILED = "提交失败，录音已保留，请重新提交";

type RecorderState = "idle" | "recording" | "recorded";

/** Preferred capture types, most specific first. All normalize to audio/webm. */
const PREFERRED_MIME_TYPES = ["audio/webm;codecs=opus", "audio/webm"];

function pickRecorderMimeType(): string {
  const supports =
    typeof MediaRecorder !== "undefined" &&
    typeof MediaRecorder.isTypeSupported === "function"
      ? (t: string) => MediaRecorder.isTypeSupported(t)
      : () => true;
  for (const t of PREFERRED_MIME_TYPES) {
    if (supports(t)) return t;
  }
  return "audio/webm";
}

/** Reduce a full MIME string (with codecs) to its container type. */
function containerType(mimeType: string): string {
  const base = mimeType.split(";")[0]?.trim();
  return base || "audio/webm";
}

export function RecorderView() {
  const navigate = useNavigate();

  const [state, setState] = useState<RecorderState>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [recordedBlob, setRecordedBlob] = useState<Blob | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [attempted, setAttempted] = useState(false);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<number | null>(null);

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

  // Clean up timer + stream on unmount.
  useEffect(() => {
    return () => {
      clearTimer();
      stopStream();
    };
  }, [clearTimer, stopStream]);

  const handleStart = useCallback(async () => {
    setError(null);
    setSubmitError(null);

    const media = navigator.mediaDevices;
    if (!media || typeof media.getUserMedia !== "function") {
      setError(MSG_UNSUPPORTED);
      return;
    }

    // Req 2.6: refuse to start when there is no audio input device.
    if (typeof media.enumerateDevices === "function") {
      try {
        const devices = await media.enumerateDevices();
        const hasAudioInput = devices.some((d) => d.kind === "audioinput");
        if (!hasAudioInput) {
          setError(MSG_NO_DEVICE);
          return;
        }
      } catch {
        // If enumeration fails, fall through to getUserMedia for a clearer error.
      }
    }

    // Req 2.5 / 2.6: surface permission and device errors without starting.
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

    // Req 2.1: capture audio from the device via MediaRecorder.
    streamRef.current = stream;
    chunksRef.current = [];
    const mimeType = pickRecorderMimeType();
    const recorder = new MediaRecorder(stream, { mimeType });

    recorder.ondataavailable = (event: BlobEvent) => {
      if (event.data && event.data.size > 0) {
        chunksRef.current.push(event.data);
      }
    };
    recorder.onstop = () => {
      // Req 2.3: produce a Supported_Audio_Format (audio/webm) blob.
      const type = containerType(recorder.mimeType || mimeType);
      const blob = new Blob(chunksRef.current, { type });
      setRecordedBlob(blob);
      setState("recorded");
      stopStream();
    };

    recorderRef.current = recorder;
    setRecordedBlob(null);
    setAttempted(false);
    setElapsed(0);
    setState("recording");
    recorder.start();

    // Req 2.2: update elapsed time at least once per second.
    timerRef.current = window.setInterval(() => {
      setElapsed((s) => s + 1);
    }, 1000);
  }, [stopStream]);

  const handleStop = useCallback(() => {
    clearTimer();
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    }
  }, [clearTimer]);

  const handleReset = useCallback(() => {
    setRecordedBlob(null);
    setState("idle");
    setElapsed(0);
    setError(null);
    setSubmitError(null);
    setAttempted(false);
  }, []);

  const handleSubmit = useCallback(async () => {
    if (!recordedBlob) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const filename = `recording-${Date.now()}.webm`;
      const res = await createMeeting(recordedBlob, filename);
      // Req 2.7 success path: leave the recorder; navigate to the new meeting.
      navigate(`/meetings/${res.id}`);
    } catch (err) {
      // Req 2.7: retain the recorded blob and report the failure.
      const reason = err instanceof Error ? err.message : "";
      setSubmitError(reason ? `${MSG_SUBMIT_FAILED}（${reason}）` : MSG_SUBMIT_FAILED);
      setAttempted(true);
    } finally {
      setSubmitting(false);
    }
  }, [recordedBlob, navigate]);

  return (
    <div>
      <h1 className="page-title">录音</h1>
      <div className="card">
        {error && (
          <div className="alert alert--error" role="alert">
            {error}
          </div>
        )}
        {submitError && (
          <div className="alert alert--error" role="alert">
            {submitError}
          </div>
        )}

        <div className="recorder-clock" data-testid="recorder-clock">
          {formatDuration(elapsed)}
        </div>

        {state === "recording" && (
          <p className="recorder-status">录音中…</p>
        )}
        {state === "recorded" && recordedBlob && (
          <p className="recorder-status" data-testid="recording-ready">
            录音完成 · 格式 {recordedBlob.type} · 时长 {formatDuration(elapsed)}
          </p>
        )}

        <div className="center-actions">
          {state === "idle" && (
            <button
              type="button"
              className="btn btn--primary btn--lg"
              onClick={handleStart}
            >
              开始录音
            </button>
          )}

          {state === "recording" && (
            <button
              type="button"
              className="btn btn--danger btn--lg"
              onClick={handleStop}
            >
              停止录音
            </button>
          )}

          {state === "recorded" && (
            <>
              <button
                type="button"
                className="btn btn--secondary"
                onClick={handleReset}
                disabled={submitting}
              >
                重新录制
              </button>
              <button
                type="button"
                className="btn btn--primary"
                onClick={handleSubmit}
                disabled={submitting}
              >
                {submitting ? "提交中…" : attempted ? "重新提交" : "提交录音"}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
