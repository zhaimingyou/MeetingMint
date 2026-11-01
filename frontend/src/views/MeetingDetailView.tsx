import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../api/client";
import {
  correctBatch,
  correctSelection,
  deleteMeeting,
  exportMeeting,
  getMeeting,
  resubmitMeeting,
  saveMinutes,
  updateTranscript,
} from "../api/meetings";
import type { JobStatus, MeetingDetail, MeetingMinutes } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { downloadBlob } from "../utils/download";
import { formatDateTime } from "../utils/format";
import { formatDuration } from "../utils/duration";
import { isInProgress, statusLabel } from "../utils/status";
import { useGlow } from "../hooks/useGlow";

const POLL_INTERVAL_MS = 4000;

const EMPTY_MINUTES: MeetingMinutes = {
  summary: "",
  topics: [],
  decisions: [],
  action_items: [],
};

export function MeetingDetailView() {
  const { id = "" } = useParams();
  const navigate = useNavigate();

  const [meeting, setMeeting] = useState<MeetingDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState<MeetingMinutes>(EMPTY_MINUTES);

  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [correctBusy, setCorrectBusy] = useState(false);
  const [correctResult, setCorrectResult] = useState<string | null>(null);
  const [correctError, setCorrectError] = useState<string | null>(null);
  const [correctSegmentIdx, setCorrectSegmentIdx] = useState<number | null>(null);
  const [replaceBusy, setReplaceBusy] = useState(false);
  const [replaceMsg, setReplaceMsg] = useState<string | null>(null);
  const [guidance, setGuidance] = useState("");

  // Floating correction popup on text selection (WPS-style).
  const [showFloating, setShowFloating] = useState(false);
  const [floatingPos, setFloatingPos] = useState({ x: 0, y: 0 });
  const [floatingGuidance, setFloatingGuidance] = useState("");
  const [selectionData, setSelectionData] = useState<{
    text: string;
    segmentIdx: number | null;
  } | null>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const onGlow = useGlow();

  const statusRef = useRef<JobStatus | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await getMeeting(id);
      setMeeting(data);
      statusRef.current = data.status;
      if (data.minutes) {
        setDraft({
          summary: data.minutes.summary ?? "",
          topics: [...(data.minutes.topics ?? [])],
          decisions: [...(data.minutes.decisions ?? [])],
          action_items: (data.minutes.action_items ?? []).map((a) => ({
            ...a,
          })),
        });
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setLoadError("未找到该会议");
      } else {
        setLoadError("加载会议失败，请稍后重试");
      }
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  // While the job is in progress, refetch when it reaches a terminal state.
  useEffect(() => {
    const handle = window.setInterval(async () => {
      if (!statusRef.current || !isInProgress(statusRef.current)) {
        return;
      }
      try {
        const data = await getMeeting(id);
        statusRef.current = data.status;
        setMeeting(data);
        if (data.minutes) {
          setDraft({
            summary: data.minutes.summary ?? "",
            topics: [...(data.minutes.topics ?? [])],
            decisions: [...(data.minutes.decisions ?? [])],
            action_items: (data.minutes.action_items ?? []).map((a) => ({
              ...a,
            })),
          });
        }
      } catch {
        // keep current view on transient errors
      }
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(handle);
  }, [id]);

  // Track text selection in transcript area for floating correction button.
  useEffect(() => {
    const onMouseUp = () => {
      const sel = window.getSelection();
      const text = sel?.toString().trim();
      if (text && text.length > 0) {
        const anchor = sel?.anchorNode;
        const card = transcriptRef.current;
        if (anchor && card && card.contains(anchor)) {
          // Save selection before interacting with popup loses it.
          let segIdx: number | null = null;
          const el = anchor.nodeType === 3 ? (anchor as Text).parentElement : (anchor as Element);
          const segEl = el?.closest?.("[data-segment-index]");
          if (segEl) {
            const idx = segEl.getAttribute("data-segment-index");
            if (idx !== null) segIdx = Number(idx);
          }
          const range = sel!.getRangeAt(0);
          setSelectionData({ text, segmentIdx: segIdx });

          const rect = range.getBoundingClientRect();
          setFloatingPos({
            x: rect.left + rect.width / 2,
            y: rect.top + window.scrollY - 8,
          });
          setShowFloating(true);
          return;
        }
      }
      setShowFloating(false);
      setSelectionData(null);
      setFloatingGuidance("");
    };
    document.addEventListener("mouseup", onMouseUp);
    return () => document.removeEventListener("mouseup", onMouseUp);
  }, []);

  const handleResubmit = useCallback(async () => {
    setActionErr(null);
    setBusy(true);
    try {
      await resubmitMeeting(id);
      await load();
    } catch {
      setActionErr("重新提交失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }, [id, load]);

  const handleSave = useCallback(async () => {
    setSaveMsg(null);
    setSaveErr(null);
    setBusy(true);
    try {
      await saveMinutes(id, draft);
      setSaveMsg("纪要已保存");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setSaveErr("当前会议没有可保存的纪要");
      } else {
        setSaveErr("保存失败，已保留原有纪要");
      }
    } finally {
      setBusy(false);
    }
  }, [id, draft]);

  const handleExport = useCallback(async (format: "md" | "docx" = "md") => {
    setActionErr(null);
    setBusy(true);
    try {
      const { blob, filename } = await exportMeeting(id, format);
      const ext = format === "docx" ? ".docx" : ".md";
      downloadBlob(blob, filename ?? `meeting-${id}${ext}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setActionErr("没有可导出的纪要");
      } else {
        setActionErr("导出失败，请稍后重试");
      }
    } finally {
      setBusy(false);
    }
  }, [id]);

  const handleDelete = useCallback(async () => {
    if (!window.confirm("确定要删除该会议吗？此操作不可恢复。")) return;
    setActionErr(null);
    setBusy(true);
    try {
      await deleteMeeting(id);
      navigate("/", { replace: true });
    } catch {
      setActionErr("删除失败，请稍后重试");
      setBusy(false);
    }
  }, [id, navigate]);

  const handleCorrect = useCallback(async () => {
    setCorrectError(null);
    setCorrectResult(null);
    setCorrectSegmentIdx(null);
    setReplaceMsg(null);
    // Use saved selection data — the live selection may be lost.
    if (!selectionData?.text) {
      setCorrectError("请先在转写文本中选中需要纠错的内容");
      return;
    }
    const { text, segmentIdx: foundIdx } = selectionData;
    setCorrectSegmentIdx(foundIdx);

    // Build context: meeting title + surrounding segments.
    let context = "";
    const segs = meeting?.transcript?.segments;
    if (segs && foundIdx !== null) {
      const parts: string[] = [];
      if (meeting?.title) parts.push(`会议主题：${meeting.title}`);
      if (foundIdx > 0 && segs[foundIdx - 1]) {
        const prev = segs[foundIdx - 1];
        parts.push(`上文：${prev.speaker ? prev.speaker + "：" : ""}${prev.text}`);
      }
      if (foundIdx < segs.length - 1 && segs[foundIdx + 1]) {
        const next = segs[foundIdx + 1];
        parts.push(`下文：${next.speaker ? next.speaker + "：" : ""}${next.text}`);
      }
      context = parts.join("\n");
    }
    // Append user guidance (merged from both inputs).
    const merged = [guidance.trim(), floatingGuidance.trim()]
      .filter(Boolean)
      .join("；");
    if (merged) {
      context = context
        ? `${context}\n纠错提示：${merged}`
        : `纠错提示：${merged}`;
    }

    setCorrectBusy(true);
    try {
      const res = await correctSelection(id, text, context || undefined);
      setCorrectResult(res.corrected);
    } catch (err) {
      setCorrectError(
        err instanceof ApiError ? err.message : "纠错失败，请稍后重试",
      );
    } finally {
      setCorrectBusy(false);
    }
  }, [id, guidance, floatingGuidance, selectionData]);

  const handleReplace = useCallback(async () => {
    if (!meeting?.transcript || correctSegmentIdx === null || !correctResult) return;
    setReplaceMsg(null);
    setReplaceBusy(true);
    try {
      const segments = meeting.transcript.segments.map((seg, i) => {
        if (i === correctSegmentIdx) {
          return { ...seg, text: correctResult };
        }
        return { start: seg.start, end: seg.end, text: seg.text, speaker: seg.speaker };
      });
      await updateTranscript(id, segments);
      // Update local state so the display reflects the change immediately.
      setMeeting({
        ...meeting,
        transcript: { segments },
      });
      setReplaceMsg("转写文本已更新");
      setCorrectResult(null);
      setCorrectSegmentIdx(null);
    } catch (err) {
      setCorrectError(
        err instanceof ApiError ? err.message : "保存失败，请稍后重试",
      );
    } finally {
      setReplaceBusy(false);
    }
  }, [id, meeting, correctSegmentIdx, correctResult]);

  const handleFullCorrect = useCallback(async () => {
    if (!meeting?.transcript?.segments?.length) return;
    setCorrectError(null);
    setCorrectResult(null);
    setReplaceMsg(null);
    setCorrectBusy(true);
    try {
      const segs = meeting.transcript.segments;
      const batchInput = segs.map((seg, i) => ({
        index: i,
        text: seg.text,
        speaker: seg.speaker,
      }));

      const res = await correctBatch(id, batchInput);

      // Map results by index — robust, no line-count heuristic.
      const correctedMap = new Map(res.segments.map((s) => [s.index, s.text]));
      const corrected = segs.map((seg, i) => ({
        start: seg.start,
        end: seg.end,
        text: correctedMap.get(i) ?? seg.text,
        speaker: seg.speaker,
      }));

      await updateTranscript(id, corrected);
      setMeeting({ ...meeting, transcript: { segments: corrected } });

      // Surface missing indices — LLM returned fewer results than input.
      const missing = segs.length - correctedMap.size;
      if (missing > 0) {
        setReplaceMsg(
          `全文纠错完成：${correctedMap.size} 条已更新，${missing} 条未能纠错已保留原文`,
        );
      } else {
        setReplaceMsg("全文纠错完成，转写文本已更新");
      }
    } catch (err) {
      setCorrectError(
        err instanceof ApiError ? err.message : "全文纠错失败，请稍后重试",
      );
    } finally {
      setCorrectBusy(false);
    }
  }, [id, meeting]);

  // ---- draft editing helpers ----
  const setSummary = (summary: string) =>
    setDraft((d) => ({ ...d, summary }));

  const updateListItem = (
    key: "topics" | "decisions",
    index: number,
    value: string,
  ) =>
    setDraft((d) => {
      const next = [...d[key]];
      next[index] = value;
      return { ...d, [key]: next };
    });

  const addListItem = (key: "topics" | "decisions") =>
    setDraft((d) => ({ ...d, [key]: [...d[key], ""] }));

  const removeListItem = (key: "topics" | "decisions", index: number) =>
    setDraft((d) => ({ ...d, [key]: d[key].filter((_, i) => i !== index) }));

  const updateActionItem = (
    index: number,
    field: "description" | "owner",
    value: string,
  ) =>
    setDraft((d) => {
      const next = d.action_items.map((a) => ({ ...a }));
      next[index] = { ...next[index], [field]: value };
      return { ...d, action_items: next };
    });

  const addActionItem = () =>
    setDraft((d) => ({
      ...d,
      action_items: [...d.action_items, { description: "", owner: "未指派" }],
    }));

  const removeActionItem = (index: number) =>
    setDraft((d) => ({
      ...d,
      action_items: d.action_items.filter((_, i) => i !== index),
    }));

  if (loadError) {
    return (
      <div>
        <button className="btn btn--ghost" onClick={() => navigate("/")}>
          ← 返回列表
        </button>
        <div className="alert alert--error" role="alert">
          {loadError}
        </div>
      </div>
    );
  }

  if (!meeting) {
    return <p className="muted">加载中…</p>;
  }

  const completed = meeting.status === "completed";

  return (
    <div>
      <button className="btn btn--ghost" onClick={() => navigate("/")}>
        ← 返回列表
      </button>

      <div
        className="meeting-row"
        style={{ margin: "var(--space-3) 0 var(--space-5)" }}
      >
        <div className="meeting-row__main">
          <h1 className="page-title" style={{ marginBottom: 4 }}>
            {meeting.title}
          </h1>
          <div className="tertiary">{formatDateTime(meeting.created_at)}</div>
        </div>
        <StatusBadge status={meeting.status} />
      </div>

      {actionErr && (
        <div className="alert alert--error" role="alert">
          {actionErr}
        </div>
      )}

      {/* Req 9.5: when not completed, show status and state-appropriate actions. */}
      {!completed && (
        <div className="card" data-testid="status-section" onMouseMove={onGlow}>
          <h2 className="section-title">处理状态</h2>
          <p>
            当前状态：<strong>{statusLabel(meeting.status)}</strong>
          </p>
          {meeting.status === "failed" && (
            <>
              <div className="alert alert--error" style={{ marginTop: 12 }}>
                处理失败
              </div>
              <button
                className="btn btn--primary"
                onClick={handleResubmit}
                disabled={busy}
              >
                {busy ? "提交中…" : "重新提交"}
              </button>
            </>
          )}
          <div className="btn-row" style={{ marginTop: "var(--space-4)" }}>
            <button
              className="btn btn--danger"
              onClick={handleDelete}
              disabled={busy}
            >
              删除
            </button>
          </div>
        </div>
      )}

      {/* Req 9.1: when completed, show transcript + editable minutes. */}
      {completed && (
        <>
          <div className="card" data-testid="transcript-section" ref={transcriptRef} onMouseMove={onGlow}>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginBottom: "var(--space-3)",
                gap: "var(--space-2)",
              }}
            >
              <h2 className="section-title" style={{ margin: 0 }}>
                转写文本
              </h2>
              <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
                <input
                  className="input"
                  type="text"
                  placeholder="引导词：项目名/人名/术语…"
                  value={guidance}
                  onChange={(e) => setGuidance(e.target.value)}
                  style={{ fontSize: "0.85rem", width: 180 }}
                />
                <button
                  className="btn btn--secondary"
                  onClick={handleFullCorrect}
                  disabled={correctBusy}
                >
                  {correctBusy ? "纠错中…" : "一键全文纠错"}
                </button>
              </div>
            </div>

            {/* Floating correction popup — WPS-style, appears on text selection */}
            {showFloating && (
              <div
                style={{
                  position: "fixed",
                  left: floatingPos.x,
                  top: floatingPos.y,
                  transform: "translate(-50%, -100%)",
                  zIndex: 999,
                }}
              >
                <div
                  onMouseDown={(e) => e.stopPropagation()}
                  style={{
                    background: "var(--color-surface-1)",
                    border: "1px solid var(--color-accent)",
                    borderRadius: 8,
                    padding: "var(--space-2) var(--space-3)",
                    boxShadow: "0 4px 16px rgba(0,0,0,0.15)",
                    display: "flex",
                    flexDirection: "column",
                    gap: "var(--space-2)",
                    minWidth: 280,
                  }}
                >
                  <input
                    className="input"
                    type="text"
                    placeholder="引导词（可选）…"
                    value={floatingGuidance}
                    onChange={(e) => setFloatingGuidance(e.target.value)}
                    style={{ fontSize: "0.85rem" }}
                    autoFocus
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        setShowFloating(false);
                        handleCorrect();
                      }
                    }}
                  />
                  <div style={{ display: "flex", gap: "var(--space-2)", justifyContent: "flex-end" }}>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => {
                        setShowFloating(false);
                        setSelectionData(null);
                        setFloatingGuidance("");
                      }}
                    >
                      取消
                    </button>
                    <button
                      className="btn btn--primary btn--sm"
                      onClick={() => {
                        setShowFloating(false);
                        handleCorrect();
                      }}
                    >
                      智能纠错
                    </button>
                  </div>
                </div>
              </div>
            )}

            {correctError && (
              <div className="alert alert--error" role="alert">
                {correctError}
              </div>
            )}

            {correctResult && (
              <div
                className="card"
                onMouseMove={onGlow}
                style={{
                  marginBottom: "var(--space-3)",
                  background: "var(--color-surface-2)",
                  border: "1px solid var(--color-accent)",
                }}
              >
                <div style={{ fontWeight: 600, marginBottom: 8 }}>纠错结果：</div>
                <div style={{ whiteSpace: "pre-wrap", marginBottom: 12 }}>
                  {correctResult}
                </div>
                <div style={{ display: "flex", gap: "var(--space-2)" }}>
                  <button
                    className="btn btn--primary"
                    onClick={handleReplace}
                    disabled={replaceBusy}
                  >
                    {replaceBusy ? "保存中…" : "替换原文"}
                  </button>
                  <button
                    className="btn btn--ghost"
                    onClick={() => {
                      setCorrectResult(null);
                      setCorrectError(null);
                      setCorrectSegmentIdx(null);
                    }}
                  >
                    关闭
                  </button>
                </div>
              </div>
            )}

            {replaceMsg && (
              <div className="alert alert--success" role="status">
                {replaceMsg}
              </div>
            )}

            {meeting.transcript && meeting.transcript.segments.length > 0 ? (
              meeting.transcript.segments.map((seg, i) => (
                <div
                  className="segment"
                  key={i}
                  data-segment-index={i}
                  style={
                    showFloating && selectionData?.segmentIdx === i
                      ? {
                          background: "rgba(66,133,244,0.12)",
                          borderRadius: 6,
                          padding: "var(--space-1) var(--space-2)",
                          outline: "2px solid rgba(66,133,244,0.35)",
                        }
                      : undefined
                  }
                >
                  <div className="segment__meta">
                    {seg.speaker && (
                      <span className="segment__speaker">{seg.speaker}</span>
                    )}
                    <span>
                      {formatDuration(seg.start)} - {formatDuration(seg.end)}
                    </span>
                  </div>
                  <div style={{ userSelect: "text", cursor: "text" }}>
                    {seg.text}
                  </div>
                </div>
              ))
            ) : (
              <p className="muted">暂无转写文本</p>
            )}
          </div>

          <div className="card" data-testid="minutes-section" onMouseMove={onGlow}>
            <h2 className="section-title">会议纪要</h2>

            {saveMsg && (
              <div className="alert alert--success" role="status">
                {saveMsg}
              </div>
            )}
            {saveErr && (
              <div className="alert alert--error" role="alert">
                {saveErr}
              </div>
            )}

            <div className="field">
              <label className="field__label" htmlFor="summary">
                摘要
              </label>
              <textarea
                id="summary"
                className="textarea"
                value={draft.summary}
                onChange={(e) => setSummary(e.target.value)}
              />
            </div>

            <div className="field">
              <label className="field__label">议题</label>
              {draft.topics.map((t, i) => (
                <div className="editable-list-item" key={i}>
                  <input
                    className="input"
                    aria-label={`议题 ${i + 1}`}
                    value={t}
                    onChange={(e) => updateListItem("topics", i, e.target.value)}
                  />
                  <button
                    className="btn btn--ghost"
                    onClick={() => removeListItem("topics", i)}
                    aria-label={`删除议题 ${i + 1}`}
                  >
                    删除
                  </button>
                </div>
              ))}
              <button
                className="btn btn--secondary"
                onClick={() => addListItem("topics")}
              >
                添加议题
              </button>
            </div>

            <div className="field">
              <label className="field__label">决议</label>
              {draft.decisions.map((t, i) => (
                <div className="editable-list-item" key={i}>
                  <input
                    className="input"
                    aria-label={`决议 ${i + 1}`}
                    value={t}
                    onChange={(e) =>
                      updateListItem("decisions", i, e.target.value)
                    }
                  />
                  <button
                    className="btn btn--ghost"
                    onClick={() => removeListItem("decisions", i)}
                    aria-label={`删除决议 ${i + 1}`}
                  >
                    删除
                  </button>
                </div>
              ))}
              <button
                className="btn btn--secondary"
                onClick={() => addListItem("decisions")}
              >
                添加决议
              </button>
            </div>

            <div className="field">
              <label className="field__label">行动项</label>
              {draft.action_items.map((a, i) => (
                <div className="action-item-row" key={i}>
                  <input
                    className="input input--desc"
                    aria-label={`行动项 ${i + 1} 描述`}
                    placeholder="描述"
                    value={a.description}
                    onChange={(e) =>
                      updateActionItem(i, "description", e.target.value)
                    }
                  />
                  <input
                    className="input input--owner"
                    aria-label={`行动项 ${i + 1} 负责人`}
                    placeholder="负责人"
                    value={a.owner}
                    onChange={(e) =>
                      updateActionItem(i, "owner", e.target.value)
                    }
                  />
                  <button
                    className="btn btn--ghost"
                    onClick={() => removeActionItem(i)}
                    aria-label={`删除行动项 ${i + 1}`}
                  >
                    删除
                  </button>
                </div>
              ))}
              <button className="btn btn--secondary" onClick={addActionItem}>
                添加行动项
              </button>
            </div>

            <div className="btn-row" style={{ marginTop: "var(--space-4)" }}>
              <button
                className="btn btn--primary"
                onClick={handleSave}
                disabled={busy}
              >
                保存
              </button>
              <button
                className="btn btn--secondary"
                onClick={() => handleExport("md")}
                disabled={busy}
              >
                导出 MD
              </button>
              <button
                className="btn btn--secondary"
                onClick={() => handleExport("docx")}
                disabled={busy}
              >
                导出 Word
              </button>
              <button
                className="btn btn--danger"
                onClick={handleDelete}
                disabled={busy}
              >
                删除
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
