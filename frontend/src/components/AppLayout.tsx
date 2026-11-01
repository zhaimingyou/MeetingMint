import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { ThemeToggle } from "./ThemeToggle";

export function AppLayout() {
  const { user, isAdmin, logout } = useAuth();
  const navigate = useNavigate();

  const handleLogout = async () => {
    await logout();
    navigate("/login", { replace: true });
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar__brand">
          <span className="topbar__brand-dot" />
          会议纪要
        </div>
        <nav className="topbar__nav">
          <NavLink to="/" end>
            会议列表
          </NavLink>
          <NavLink to="/record">录音</NavLink>
          <NavLink to="/upload">上传</NavLink>
          {isAdmin && (
            <NavLink to="/admin" data-testid="nav-admin">
              管理后台
            </NavLink>
          )}
        </nav>
        <div className="topbar__spacer" />
        {user && <span className="topbar__user">{user}</span>}
        <NavLink to="/settings" className="btn btn--ghost" data-testid="nav-settings">
          设置
        </NavLink>
        <ThemeToggle />
        <button
          type="button"
          className="btn btn--ghost"
          onClick={handleLogout}
        >
          退出登录
        </button>
      </header>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
