import { NavLink, Outlet } from "react-router-dom";

/** Sub-navigation shell for the Admin_Console screens. */
export function AdminLayout() {
  return (
    <div className="admin">
      <h1 className="admin__title">管理后台</h1>
      <nav className="admin__nav" data-testid="admin-subnav">
        <NavLink to="/admin/config">模型配置</NavLink>
        <NavLink to="/admin/users">用户管理</NavLink>
        <NavLink to="/admin/jobs">任务监控</NavLink>
        <NavLink to="/admin/audit">审计日志</NavLink>
      </nav>
      <div className="admin__body">
        <Outlet />
      </div>
    </div>
  );
}
