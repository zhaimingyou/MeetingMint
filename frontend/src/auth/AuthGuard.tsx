import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "./AuthContext";
import { PREVIEW_ENABLED } from "../preview";

/** Redirects unauthenticated users to /login, preserving the target path. */
export function AuthGuard() {
  const { isAuthenticated } = useAuth();
  const location = useLocation();

  // TEMPORARY: preview mode (npm run dev only) skips the login gate.
  if (PREVIEW_ENABLED) {
    return <Outlet />;
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <Outlet />;
}
