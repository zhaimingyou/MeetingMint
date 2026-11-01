import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import { listJobs, resubmitJob } from "../../api/admin";
import type { AdminJobRow, JobStatus } from "../../api/types";

const STATUSES: (JobStatus | "")[] = [
  "",
  "live",
  "queued",
  "preprocessing",
  "transcribing",
  "generating",
  "completed",
  "failed",
];

export function AdminJobsView() {
  const [jobs, setJobs] = useState<AdminJobRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const pageSize = 20;

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await listJobs({
        status: status || undefined,
        page,
        pageSize,
      });
      setJobs(res.items);
      setTotal(res.total);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "加载任务失败");
    }
  }, [status, page]);

  useEffect(() => {
    void load();
  }, [load]);

  const onResubmit = async (meetingId: string) => {
    setError(null);
    setNotice(null);
    try {
      await resubmitJob(meetingId);
      setNotice("已重新提交");
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail || err.message : "重试失败");
    }
  };

  const pages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="card" data-testid="admin-jobs">
      <h2>任务监控</h2>
      {error && (
        <div className="alert alert--error" role="alert">
          {error}
        </div>
      )}
      {notice && <div className="alert alert--info">{notice}</div>}

      <div className="admin-jobs__filter">
        <label>
          状态：
          <select
            className="input"
            data-testid="status-filter"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setPage(1);
            }}
          >
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s === "" ? "全部" : s}
              </option>
            ))}
          </select>
        </label>
      </div>

      <table className="table" data-testid="jobs-table">
        <thead>
          <tr>
            <th>会议</th>
            <th>所有者</th>
            <th>状态</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.meeting_id} data-testid={`job-row-${j.meeting_id}`}>
              <td>{j.title}</td>
              <td>{j.owner_username}</td>
              <td>
                {j.status}
                {j.status === "failed" && j.error_description ? (
                  <span className="muted"> — {j.error_description}</span>
                ) : null}
              </td>
              <td>{new Date(j.created_at).toLocaleString()}</td>
              <td>
                {j.status === "failed" && (
                  <button
                    type="button"
                    className="btn btn--sm"
                    data-testid={`resubmit-${j.meeting_id}`}
                    onClick={() => onResubmit(j.meeting_id)}
                  >
                    重新提交
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="admin-jobs__pager">
        <button
          type="button"
          className="btn btn--sm"
          disabled={page <= 1}
          onClick={() => setPage((p) => Math.max(1, p - 1))}
        >
          上一页
        </button>
        <span className="muted" data-testid="jobs-page">
          第 {page} / {pages} 页（共 {total}）
        </span>
        <button
          type="button"
          className="btn btn--sm"
          disabled={page >= pages}
          onClick={() => setPage((p) => p + 1)}
        >
          下一页
        </button>
      </div>
    </div>
  );
}
