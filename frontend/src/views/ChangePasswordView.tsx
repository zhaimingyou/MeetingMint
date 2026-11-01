import { useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { changePassword } from "../api/auth";

/** Self-service password change, available to any authenticated user (Req 8). */
export function ChangePasswordView() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setNotice(null);
    if (next !== confirm) {
      setError("两次输入的新密码不一致");
      return;
    }
    setBusy(true);
    try {
      await changePassword(current, next);
      setNotice("密码已修改，其他设备已退出登录");
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 401) setError("当前密码不正确");
        else if (err.status === 400) setError("新密码不符合要求");
        else setError(err.detail || err.message);
      } else {
        setError("修改失败，请稍后重试");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card" data-testid="change-password">
      <h2>修改密码</h2>
      {error && (
        <div className="alert alert--error" role="alert">
          {error}
        </div>
      )}
      {notice && <div className="alert alert--info">{notice}</div>}

      <form onSubmit={onSubmit}>
        <div className="field">
          <label className="field__label" htmlFor="cur-pwd">
            当前密码
          </label>
          <input
            id="cur-pwd"
            className="input"
            type="password"
            data-testid="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="field__label" htmlFor="new-pwd">
            新密码
          </label>
          <input
            id="new-pwd"
            className="input"
            type="password"
            data-testid="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="field__label" htmlFor="confirm-pwd">
            确认新密码
          </label>
          <input
            id="confirm-pwd"
            className="input"
            type="password"
            data-testid="confirm-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </div>
        <button
          type="submit"
          className="btn btn--primary"
          data-testid="submit-change-password"
          disabled={busy}
        >
          {busy ? "提交中…" : "修改密码"}
        </button>
      </form>
    </div>
  );
}
