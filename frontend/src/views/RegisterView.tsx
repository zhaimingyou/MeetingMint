import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { register } from "../api/auth";
import { ThemeToggle } from "../components/ThemeToggle";

export function RegisterView() {
  const navigate = useNavigate();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);

    if (password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }

    setSubmitting(true);
    try {
      await register(username, password);
      // Registration succeeds without a session; route to login.
      navigate("/login", {
        replace: true,
        state: { registered: username },
      });
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 403) {
          setError("注册未开放，请联系管理员开通");
        } else if (err.status === 409) {
          setError("该用户名已被注册");
        } else if (err.status === 400) {
          setError(
            err.detail ||
              "请输入有效的用户名和密码（不能为空且不超过 256 个字符）",
          );
        } else {
          setError(err.message || "注册失败，请稍后重试");
        }
      } else {
        setError("无法连接服务器，请稍后重试");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-wrap">
      <ThemeToggle floating />
      <form className="card login-card" onSubmit={handleSubmit}>
        <h1 className="login-card__title">会议纪要</h1>
        <p className="login-card__subtitle muted">注册新账号</p>

        {error && (
          <div className="alert alert--error" role="alert">
            {error}
          </div>
        )}

        <div className="field">
          <label className="field__label" htmlFor="username">
            用户名
          </label>
          <input
            id="username"
            className="input"
            type="text"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>

        <div className="field">
          <label className="field__label" htmlFor="password">
            密码
          </label>
          <input
            id="password"
            className="input"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        <div className="field">
          <label className="field__label" htmlFor="confirm-password">
            确认密码
          </label>
          <input
            id="confirm-password"
            className="input"
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </div>

        <button
          type="submit"
          className="btn btn--primary btn--lg"
          style={{ width: "100%" }}
          disabled={submitting}
        >
          {submitting ? "注册中…" : "注册"}
        </button>

        <p className="login-card__hint muted">
          已有账号？<Link to="/login">直接登录</Link>
        </p>
      </form>
    </div>
  );
}
