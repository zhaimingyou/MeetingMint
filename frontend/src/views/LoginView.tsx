import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { ThemeToggle } from "../components/ThemeToggle";

interface LocationState {
  from?: { pathname?: string };
  registered?: string;
}

export function LoginView() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const registeredUser = (location.state as LocationState | null)?.registered;

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(username, password);
      const target =
        (location.state as LocationState | null)?.from?.pathname ?? "/";
      navigate(target, { replace: true });
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 401) {
          setError("用户名或密码错误");
        } else if (err.status === 400) {
          setError("请输入有效的用户名和密码（不能为空且不超过 256 个字符）");
        } else {
          setError(err.message || "登录失败，请稍后重试");
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
        <p className="login-card__subtitle muted">登录以继续</p>

        {registeredUser && (
          <div className="alert alert--info" role="status">
            注册成功，请登录。
          </div>
        )}

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
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        <button
          type="submit"
          className="btn btn--primary btn--lg"
          style={{ width: "100%" }}
          disabled={submitting}
        >
          {submitting ? "登录中…" : "登录"}
        </button>

        <p className="login-card__hint muted">
          没有账号？<Link to="/register">注册</Link>
        </p>
      </form>
    </div>
  );
}
