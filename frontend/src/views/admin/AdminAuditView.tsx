import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import { listAudit } from "../../api/admin";
import type { AuditEntry } from "../../api/types";

export function AdminAuditView() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const pageSize = 20;

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await listAudit({ page, pageSize });
      setEntries(res.items);
      setTotal(res.total);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "加载审计日志失败");
    }
  }, [page]);

  useEffect(() => {
    void load();
  }, [load]);

  const pages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="card" data-testid="admin-audit">
      <h2>审计日志</h2>
      {error && (
        <div className="alert alert--error" role="alert">
          {error}
        </div>
      )}

      <table className="table" data-testid="audit-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>操作者</th>
            <th>动作</th>
            <th>对象</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr key={e.id} data-testid={`audit-row-${e.id}`}>
              <td>{new Date(e.created_at).toLocaleString()}</td>
              <td>{e.actor_username}</td>
              <td>{e.action}</td>
              <td>
                {e.target_type ?? ""}
                {e.target_id ? ` (${e.target_id})` : ""}
              </td>
              <td>
                <code>{e.detail ? JSON.stringify(e.detail) : ""}</code>
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
        <span className="muted" data-testid="audit-page">
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
