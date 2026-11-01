import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "./AuthContext";
import { PREVIEW_ENABLED } from "../preview";

/**
 * Guards the /admin/* routes: requires authentication AND the administrator
 * role. An unauthenticated user is sent to /login; an authenticated
 * non-administrator gets an access-denied panel (the backend authorizes
 * regardless of this gate).
 */
export function AdminGuard() {
  const { isAuthenticated, isAdmin } = useAuth();
  const location = useLocation();

  // TEMPORARY: preview mode (npm run dev only) skips the gate.
  if (PREVIEW_ENABLED) {
    return <Outlet />;
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  if (!isAdmin) {
    return (
      <div className="card" data-testid="admin-access-denied">
        <h2>无权访问</h2>
        <p className="muted">该页面需要管理员权限。</p>
      </div>
    );
  }
  return <Outlet />;
}
