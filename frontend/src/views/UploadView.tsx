import { useCallback, useRef, useState, type ChangeEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { createMeeting } from "../api/meetings";
import { SUPPORTED_AUDIO_ACCEPT } from "../api/types";

export function UploadView() {
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement | null>(null);

  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

  const handlePick = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    setError(null);
    setFile(e.target.files?.[0] ?? null);
  }, []);

  const handleUpload = useCallback(async () => {
    if (!file) {
      setError("请选择一个音频文件");
      return;
    }
    if (file.size === 0) {
      setError("文件为空，请选择有效的音频文件");
      return;
    }
    setUploading(true);
    setError(null);
    try {
      const res = await createMeeting(file, file.name);
      navigate(`/meetings/${res.id}`);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 415) {
          setError(
            err.detail ?? "不支持的音频格式，请上传 mp3、wav、m4a 或 webm 文件",
          );
        } else if (err.status === 413) {
          setError(err.detail ?? "文件过大，超出允许的最大上传大小");
        } else if (err.status === 400) {
          setError(err.detail ?? "文件无效或为空");
        } else if (err.status === 500) {
          setError(err.detail ?? "服务器处理上传失败，请稍后重试");
        } else {
          setError(err.message || "上传失败，请稍后重试");
        }
      } else {
        setError("无法连接服务器，请稍后重试");
      }
    } finally {
      setUploading(false);
    }
  }, [file, navigate]);

  return (
    <div>
      <h1 className="page-title">上传音频</h1>
      <div className="card">
        {error && (
          <div className="alert alert--error" role="alert">
            {error}
          </div>
        )}

        <div className="field">
          <label className="field__label" htmlFor="audio-file">
            选择音频文件（支持 mp3、wav、m4a、webm）
          </label>
          <input
            id="audio-file"
            ref={inputRef}
            className="input"
            type="file"
            accept={SUPPORTED_AUDIO_ACCEPT}
            onChange={handlePick}
          />
        </div>

        {file && (
          <p className="muted" data-testid="selected-file">
            已选择：{file.name}
          </p>
        )}

        <div className="btn-row" style={{ marginTop: "var(--space-4)" }}>
          <button
            type="button"
            className="btn btn--primary"
            onClick={handleUpload}
            disabled={uploading || !file}
          >
            {uploading ? "上传中…" : "上传"}
          </button>
        </div>
      </div>
    </div>
  );
}
