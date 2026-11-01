import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { ApiError } from "../../api/client";
import {
  createUser,
  deleteUser,
  listUsers,
  resetUserPassword,
  updateUser,
} from "../../api/admin";
import type { AdminUserRow } from "../../api/types";
import { formatDateTime } from "../../utils/format";

export function AdminUsersView() {
  const [users, setUsers] = useState<AdminUserRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newIsAdmin, setNewIsAdmin] = useState(false);
  const [resetId, setResetId] = useState<string | null>(null);
  const [resetPwd, setResetPwd] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const load = useCallback(async () => {
    setError(null);
    try {
      setUsers(await listUsers());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "加载用户失败");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return users;
    return users.filter((u) => u.username.toLowerCase().includes(q));
  }, [users, search]);

  const guard = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail || err.message : "操作失败");
    }
  };

  const onCreate = async (e: FormEvent) => {
    e.preventDefault();
    await guard(async () => {
      await createUser(newUsername, newPassword, newIsAdmin);
      setNewUsername("");
      setNewPassword("");
      setNewIsAdmin(false);
    });
  };

  const onReset = async (id: string) => {
    await guard(async () => {
      await resetUserPassword(id, resetPwd);
      setResetId(null);
      setResetPwd("");
    });
  };

  return (
    <div className="card" data-testid="admin-users">
      <div className="admin-users__header">
        <h2>用户管理（共 {users.length} 个）</h2>
        <input
          className="input admin-users__search"
          type="search"
          placeholder="搜索用户名"
          data-testid="user-search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      {error && (
        <div className="alert alert--error" role="alert">
          {error}
        </div>
      )}

      <form className="admin-users__create" onSubmit={onCreate}>
        <input
          className="input"
          placeholder="用户名"
          data-testid="new-username"
          value={newUsername}
          onChange={(e) => setNewUsername(e.target.value)}
        />
        <input
          className="input"
          type="password"
          placeholder="密码"
          data-testid="new-password"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
        />
        <label className="checkbox">
          <input
            type="checkbox"
            data-testid="new-is-admin"
            checked={newIsAdmin}
            onChange={(e) => setNewIsAdmin(e.target.checked)}
          />
          管理员
        </label>
        <button type="submit" className="btn btn--primary" data-testid="create-user">
          创建
        </button>
      </form>

      <table className="table" data-testid="users-table">
        <thead>
          <tr>
            <th>用户名</th>
            <th>管理员</th>
            <th>状态</th>
            <th>注册时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {filtered.map((u) => (
            <tr key={u.id} data-testid={`user-row-${u.username}`}>
              <td>{u.username}</td>
              <td>{u.is_admin ? "是" : "否"}</td>
              <td>{u.disabled ? "已禁用" : "正常"}</td>
              <td className="muted">{formatDateTime(u.created_at)}</td>
              <td className="admin-users__actions">
                <button
                  type="button"
                  className="btn btn--sm"
                  data-testid={`toggle-admin-${u.username}`}
                  onClick={() =>
                    guard(() => updateUser(u.id, { is_admin: !u.is_admin }))
                  }
                >
                  {u.is_admin ? "取消管理员" : "设为管理员"}
                </button>
                <button
                  type="button"
                  className="btn btn--sm"
                  data-testid={`toggle-disabled-${u.username}`}
                  onClick={() =>
                    guard(() => updateUser(u.id, { disabled: !u.disabled }))
                  }
                >
                  {u.disabled ? "启用" : "禁用"}
                </button>
                <button
                  type="button"
                  className="btn btn--sm"
                  data-testid={`reset-${u.username}`}
                  onClick={() => {
                    setResetId(u.id);
                    setResetPwd("");
                  }}
                >
                  重置密码
                </button>
                {confirmDelete === u.id ? (
                  <button
                    type="button"
                    className="btn btn--sm btn--danger"
                    data-testid={`confirm-delete-${u.username}`}
                    onClick={() =>
                      guard(async () => {
                        await deleteUser(u.id);
                        setConfirmDelete(null);
                      })
                    }
                  >
                    确认删除
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn btn--sm btn--danger"
                    data-testid={`delete-${u.username}`}
                    onClick={() => setConfirmDelete(u.id)}
                  >
                    删除
                  </button>
                )}
                {resetId === u.id && (
                  <span className="admin-users__reset">
                    <input
                      className="input"
                      type="password"
                      placeholder="新密码"
                      data-testid={`reset-input-${u.username}`}
                      value={resetPwd}
                      onChange={(e) => setResetPwd(e.target.value)}
                    />
                    <button
                      type="button"
                      className="btn btn--sm"
                      data-testid={`reset-confirm-${u.username}`}
                      onClick={() => onReset(u.id)}
                    >
                      确认
                    </button>
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
