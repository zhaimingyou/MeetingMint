import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { getMeetingStatus, listMeetings } from "../api/meetings";
import type { MeetingListItem } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { formatDateTime } from "../utils/format";
import { isInProgress } from "../utils/status";

const POLL_INTERVAL_MS = 4000;

/**
 * New_Meeting_Entry (Req 1.1-1.4): a prominent "新建会议" control rendered even
 * when the list is empty. Activating it offers exactly two options -- start a
 * live meeting (-> /live) or upload audio (-> the existing /upload flow). The
 * existing record/upload entry points in the top bar remain unchanged (Req 1.5).
 */
function NewMeetingEntry() {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  return (
    <div className="new-meeting">
      <button
        type="button"
        className="btn btn--primary"
        aria-expanded={open}
        data-testid="new-meeting-entry"
        onClick={() => setOpen((o) => !o)}
      >
        新建会议
      </button>
      {open && (
        <div className="new-meeting__menu" role="menu">
          <button
            type="button"
            role="menuitem"
            className="btn btn--ghost"
            data-testid="new-meeting-live"
            onClick={() => navigate("/live")}
          >
            开始实时会议
          </button>
          <button
            type="button"
            role="menuitem"
            className="btn btn--ghost"
            data-testid="new-meeting-upload"
            onClick={() => navigate("/upload")}
          >
            上传音频
          </button>
        </div>
      )}
    </div>
  );
}

export function MeetingListView() {
  const [meetings, setMeetings] = useState<MeetingListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const meetingsRef = useRef<MeetingListItem[]>([]);

  useEffect(() => {
    meetingsRef.current = meetings ?? [];
  }, [meetings]);

  // Initial load. The server already orders newest-first; do NOT re-sort.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await listMeetings();
        if (!cancelled) setMeetings(data);
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof ApiError
              ? err.message
              : "无法加载会议列表，请稍后重试",
          );
          setMeetings([]);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Poll status for in-progress meetings; no requests are issued once every
  // meeting has reached a terminal (completed/failed) state.
  useEffect(() => {
    const id = window.setInterval(async () => {
      const inProgress = meetingsRef.current.filter((m) =>
        isInProgress(m.status),
      );
      if (inProgress.length === 0) return;

      const updates = await Promise.all(
        inProgress.map(async (m) => {
          try {
            const s = await getMeetingStatus(m.id);
            return { id: m.id, status: s.status };
          } catch {
            return null;
          }
        }),
      );

      setMeetings((prev) =>
        prev
          ? prev.map((m) => {
              const u = updates.find((x) => x && x.id === m.id);
              return u ? { ...m, status: u.status } : m;
            })
          : prev,
      );
    }, POLL_INTERVAL_MS);

    return () => window.clearInterval(id);
  }, []);

  return (
    <div>
      <div className="page-header">
        <h1 className="page-title">会议列表</h1>
        <NewMeetingEntry />
      </div>

      {error && (
        <div className="alert alert--error" role="alert">
          {error}
        </div>
      )}

      {meetings === null && <p className="muted">加载中…</p>}

      {meetings !== null && meetings.length === 0 && (
        <div className="card empty-state">
          <p>还没有会议记录</p>
          <p className="muted">
            前往 <Link to="/record">录音</Link> 或{" "}
            <Link to="/upload">上传</Link> 创建第一个会议
          </p>
        </div>
      )}

      {meetings !== null && meetings.length > 0 && (
        <div className="list-stack">
          {meetings.map((m) => (
            <Link
              key={m.id}
              to={`/meetings/${m.id}`}
              className="card card--link"
            >
              <div className="meeting-row">
                <div className="meeting-row__main">
                  <div className="meeting-row__title">{m.title}</div>
                  <div className="tertiary">{formatDateTime(m.created_at)}</div>
                </div>
                <StatusBadge status={m.status} />
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
