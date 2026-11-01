import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import * as authApi from "../api/auth";
import {
  clearStoredIsAdmin,
  clearStoredUser,
  clearToken,
  getStoredIsAdmin,
  getStoredUser,
  getToken,
  setStoredIsAdmin,
  setStoredUser,
  setToken,
  setUnauthorizedHandler,
} from "../api/client";

interface AuthContextValue {
  token: string | null;
  user: string | null;
  isAdmin: boolean;
  isAuthenticated: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() => getToken());
  const [user, setUserState] = useState<string | null>(() => getStoredUser());
  const [isAdmin, setIsAdminState] = useState<boolean>(() => getStoredIsAdmin());

  const clearSession = useCallback(() => {
    clearToken();
    clearStoredUser();
    clearStoredIsAdmin();
    setTokenState(null);
    setUserState(null);
    setIsAdminState(false);
  }, []);

  // When any request hits 401, the client clears storage and calls this.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setTokenState(null);
      setUserState(null);
      setIsAdminState(false);
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  // On mount with an existing token, refresh identity/role from the server so
  // the admin gate reflects the current account (e.g. a session predating the
  // admin-console feature, or a role change). Best-effort; a 401 self-clears.
  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    authApi
      .getMe()
      .then((me) => {
        if (cancelled) return;
        setUserState(me.username);
        setStoredUser(me.username);
        setIsAdminState(me.is_admin);
        setStoredIsAdmin(me.is_admin);
      })
      .catch(() => {
        /* 401 is handled by the unauthorized handler; ignore others */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const res = await authApi.login(username, password);
    const admin = Boolean(res.is_admin);
    setToken(res.token);
    setStoredUser(username);
    setStoredIsAdmin(admin);
    setTokenState(res.token);
    setUserState(username);
    setIsAdminState(admin);
  }, []);

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      // Best-effort: clear the local session regardless of server response.
    } finally {
      clearSession();
    }
  }, [clearSession]);

  const value = useMemo<AuthContextValue>(
    () => ({
      token,
      user,
      isAdmin,
      isAuthenticated: Boolean(token),
      login,
      logout,
    }),
    [token, user, isAdmin, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
